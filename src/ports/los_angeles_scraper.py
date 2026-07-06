"""Scraper for the LA Superior Court public case-document system.

1. GET ``GuestInformation`` -> sets the guest session cookie.
2. POST ``DocumentImages/SearchCaseNumber`` with a case number -> HTML results
   listing every imaged document (date, description, per-doc securityKey).
   Done via in-page ``fetch()`` from the parked search form (no navigation),
   so probing an empty case number costs one round-trip, not two page loads.
3. GET ``DocumentImages/PreviewWait?id=..&securityKey=..`` -> a reCAPTCHA page
   (Browserbase solves it) -> 302 -> a one-time PDF URL that Chrome downloads.
4. Browserbase captures the download; CDP download events say when each PDF
   has fully transferred, then each file is pulled back individually via the
   downloads API and matched to its document by the docId in the filename.

Everything runs in the Browserbase browser — its residential proxy and real
Chrome fingerprint are what get past the WAF; the in-page fetch inherits both.
"""

import asyncio
import hashlib
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime
from html import unescape
from typing import Any
from urllib.parse import quote

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..browser_base_factory import BrowserBase, BrowserBaseFactory
from ..models import (
    InsertCase,
    ScrapedTrialCase,
    ScrapedTrialDocument,
    TrialScraper,
)
from .los_angeles_case_numbers import generate_case_numbers

BASE = "https://www.lacourt.ca.gov/paos/v2web3"

# Pull every document row out of a results table root: date, description, and
# the preview(id, securityKey, caseType, source, caseNumber) call args. Works
# on the live document (pagination) or a DOMParser document (fetch search).
_DOC_ROWS = r"""
(root) => {
  const rows = [...root.querySelectorAll('#paosForm tr')]
    .filter(r => r.querySelector('input[type=checkbox][id^="Doc"]'));
  return rows.map(r => {
    const tds = r.querySelectorAll(':scope > td');
    const prev = r.querySelector('input[onclick^="preview"]');
    const m = prev && prev.getAttribute('onclick')
      .match(/preview\('([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)'\)/);
    if (!m) return null;
    return {docId: m[1], securityKey: m[2], caseType: m[3], source: m[4],
            caseNumber: m[5], date: (tds[1]?.innerText || '').trim(),
            description: (tds[2]?.innerText || '').replace(/\s+/g, ' ').trim()};
  }).filter(Boolean);
}
"""

_EXTRACT_DOCS = f"() => ({_DOC_ROWS})(document)"

# Page numbers linked in the results pager (".pagnation" — their spelling).
# Results hold 50 documents per page; extra pages are plain links to
# SelectDocuments?page=N, served from the case held in the session.
_PAGE_NUMS = r"""
(root) => [...root.querySelectorAll('.pagnation a')]
  .map(a => parseInt(new URL(a.href, location.href).searchParams.get('page')))
  .filter(Number.isInteger)
"""

_PAGE_LINKS = f"() => ({_PAGE_NUMS})(document)"

# Search without navigating: POST the form via in-page fetch() (same cookies,
# TLS, and fingerprint as the real form — the WAF can't tell the difference)
# and parse the response off-DOM. One round-trip per probe instead of two page
# loads. The POST needs the form's antiforgery token, read off the loaded
# page; it stays valid for many fetches. A found case redirects to
# SelectDocuments AND becomes the session's current case, so pagination and
# downloads keep working afterwards (verified live). ``searchForm`` in the
# result distinguishes "no documents" (form redisplayed) from an expired
# session or token (login page / 400), which the caller retries.
_SEARCH_FETCH = f"""
async (caseNumber) => {{
  const token = document.querySelector(
    '#paosForm input[name="__RequestVerificationToken"]')?.value;
  const resp = await fetch('SearchCaseNumber', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
    body: new URLSearchParams({{
      CaseNumber: caseNumber, Remark: '',
      __RequestVerificationToken: token || '',
    }}).toString(),
  }});
  const html = await resp.text();
  const dom = new DOMParser().parseFromString(html, 'text/html');
  const docs = ({_DOC_ROWS})(dom);
  // The server renders results from per-session state, so searches running
  // concurrently in one session cross-contaminate: the response can carry
  // ANOTHER case's rows (verified live: 6/10 wrong at 10 concurrent). Rows
  // name their case, so a foreign response is detectable. Searches must stay
  // serial per session; this flag turns a violation into a retry instead of
  // silently filing another case's documents under our case number.
  const foreign = docs.some(d => d.caseNumber !== caseNumber);
  // Only a found case ships its HTML back over the CDP bridge — empty-case
  // probes dominate and nobody reads their redisplayed-form page.
  return {{ok: resp.ok, foreign, searchForm: !!dom.querySelector('#CaseNumber'),
          html: docs.length ? html : '', docs, pages: ({_PAGE_NUMS})(dom)}};
}}
"""

_OPINION_HINTS = ("opinion", "ruling", "order", "judgment", "minute order")

# Preview tabs per worker session serving the shared download pool. Sized to
# the captcha solver's per-session appetite (~2-4 concurrent solves, measured
# live); more tabs would just queue behind the solver. The global ceiling on
# captcha-gated previews is structural: _TABS_PER_SESSION x workers.
_TABS_PER_SESSION = 3


class LosAngelesScraper(TrialScraper):
    scraper_id = "los_angeles"
    court_id = "CA_LA_SUPERIOR"
    court_name = "Superior Court of California, County of Los Angeles"

    def __init__(
        self, to_date: date, from_date: date, browser: BrowserBaseFactory
    ) -> None:
        super().__init__(to_date, from_date, browser)
        # Runtime knobs (see README), read once here. Defaults give a small but
        # non-trivial run — a few real cases, downloaded concurrently — not a
        # sweep of thousands of empty sequence numbers.
        raw = os.environ.get("LA_CASE_NUMBERS", "")
        self.case_numbers = [c.strip() for c in raw.split(",") if c.strip()]
        self.max_cases = int(os.environ.get("LA_MAX_CASES", "3"))
        self.max_docs = int(os.environ.get("LA_MAX_DOCS", "10"))
        # Worker sessions (each probes + downloads + runs pool tabs). Measured
        # sweet spot ~16: throughput rose 8->16 (20.6->23.7 docs/min) then FELL
        # at 25 (16.0, 11% of docs failed) as ~75 concurrent captchas overload
        # Browserbase's solver. Past ~16 you saturate it and reliability drops.
        self.concurrency = int(os.environ.get("LA_CONCURRENCY", "16"))
        self._attempted = 0  # case numbers probed
        self._claimed = 0  # cases committed to downloading (quota gate)
        self._scraped = 0  # cases with >=1 document saved
        self._docs_saved = 0
        self._docs_failed = 0  # docs found but not captured
        # Shared download pool: any worker session's consumers execute any
        # case's preview jobs (securityKeys are session-independent), so one
        # case's captchas solve across ALL sessions at once.
        self._dl_queue: asyncio.Queue[
            tuple[dict[str, str], asyncio.Future[bytes | None]]
        ] = asyncio.Queue()
        # Workers still probing/downloading. A worker that runs out of work
        # keeps its pool tabs alive until this hits zero, so the run's last
        # case solves its captchas on the FULL pool, not a shrinking one.
        self._active_workers = 0
        self._all_workers_done = asyncio.Event()

    async def scrape(self, insert_case: InsertCase) -> None:
        # No point opening more sessions than explicit case numbers to search.
        workers = min(self.concurrency, len(self.case_numbers) or self.concurrency)
        print(
            f"[los_angeles] starting — up to {self.max_cases} case(s), "
            f"{self.max_docs} doc(s) each; opening {workers} browser session(s)…"
        )
        started = time.monotonic()
        self._active_workers = workers
        case_iter = iter(self._target_case_numbers())
        results = await asyncio.gather(
            *(self._worker(case_iter, insert_case) for _ in range(workers)),
            return_exceptions=True,  # one dead session must not kill the rest
        )
        for exc in results:
            if isinstance(exc, BaseException):
                print(f"[los_angeles] a browser session failed: {exc!r}")

        elapsed = time.monotonic() - started
        per_min = self._docs_saved / elapsed * 60 if elapsed else 0.0
        per_case = elapsed / self._scraped if self._scraped else 0.0
        total_docs = self._docs_saved + self._docs_failed
        success = 100 * self._docs_saved / total_docs if total_docs else 100.0
        print(
            f"[los_angeles] done in {elapsed:.0f}s — {self._scraped} case(s), "
            f"{self._docs_saved} doc(s) saved, {self._docs_failed} failed "
            f"({success:.0f}% of {total_docs} attempted)"
        )
        print(
            f"[los_angeles]   {per_min:.1f} docs/min · avg {per_case:.0f}s/case · "
            f"{self._attempted} case number(s) probed"
        )

    async def _worker(self, case_iter: Iterator[str], insert_case: InsertCase) -> None:
        """Pull case numbers off the shared iterator until the quota is filled
        or the numbers run out, probing and downloading in ONE long-lived
        session so there's no fresh session (or re-search) per case. Sharing a
        plain iterator between workers is safe: next() has no await point."""
        retired = False

        def retire() -> None:
            # Idempotent: runs in the probe loop's finally AND the outer one,
            # so a session that dies during setup still gets counted down —
            # otherwise every other worker would park forever below.
            nonlocal retired
            if retired:
                return
            retired = True
            self._active_workers -= 1
            if self._active_workers == 0:
                self._all_workers_done.set()

        try:
            await self._worker_session(case_iter, insert_case, retire)
        finally:
            retire()

    async def _worker_session(
        self,
        case_iter: Iterator[str],
        insert_case: InsertCase,
        retire: Callable[[], None],
    ) -> None:
        bb = self.browser.new_browser_base()
        async with bb as (_session, page):
            await self._continue_as_guest(page)
            # This session's share of the download pool (see _download_documents).
            consumers = [
                asyncio.create_task(self._consume(bb, page))
                for _ in range(_TABS_PER_SESSION)
            ]
            try:
                for case_number in case_iter:
                    if self._claimed >= self.max_cases:
                        return  # quota already claimed (by any worker)
                    self._attempted += 1
                    try:
                        print(f"[{case_number}] checking for documents…")
                        docs, html, pages = await self._search(page, case_number)
                    except Exception as exc:  # a bad case must not kill the sweep
                        print(f"[{case_number}] error, skipping: {exc!r}")
                        continue
                    if not docs:
                        print(f"[{case_number}] no documents")
                        continue
                    # Claim a slot *before* the expensive download-scrape so no
                    # worker downloads a case beyond the quota.
                    if self._claimed >= self.max_cases:
                        return
                    self._claimed += 1
                    print(f"[{case_number}] found documents — downloading")
                    try:
                        case = await self._scrape_case(
                            page, case_number, docs, html, pages
                        )
                    except Exception as exc:
                        print(f"[{case_number}] error, skipping: {exc!r}")
                        self._claimed -= 1  # slot didn't pan out; free it
                        continue
                    if case is None:
                        self._claimed -= 1
                        continue
                    self._scraped += 1
                    await insert_case(case)
            finally:
                retire()
                try:
                    # Out of cases, but other workers may still be downloading:
                    # park here so this session's pool tabs keep solving their
                    # captchas until everyone is done. A worker whose session
                    # just broke (exception in flight) tears down instead.
                    if sys.exc_info()[0] is None:
                        await self._all_workers_done.wait()
                finally:
                    # Cancelled consumers hand any in-flight job back to the
                    # queue, so a worker retiring early never strands another
                    # case's doc.
                    for c in consumers:
                        c.cancel()
                    await asyncio.gather(*consumers, return_exceptions=True)

    def _target_case_numbers(self) -> Iterable[str]:
        return self.case_numbers or generate_case_numbers(self.from_date, self.to_date)

    async def _continue_as_guest(self, page: Page) -> None:
        # Visiting GuestInformation establishes the guest cookie and lands on
        # the search form.
        await page.goto(
            f"{BASE}/GuestInformation", wait_until="domcontentloaded", timeout=90000
        )

    async def _search(
        self, page: Page, case_number: str
    ) -> tuple[list[dict[str, str]], str, list[int]]:
        """Submit the case number via in-page fetch (see _SEARCH_FETCH) and
        return (page 1's document rows, results HTML, pager page numbers) —
        ([], "", []) if the case has none. The page itself stays parked on the
        search form, so its antiforgery token serves every probe; it only
        reloads if something navigated away (pagination does) or the
        session/token went stale, in which case the guest session is
        re-established and the search retried once."""

        async def attempt() -> tuple[list[dict[str, str]], str, list[int]]:
            if "/DocumentImages/SearchCaseNumber" not in page.url:
                await page.goto(
                    f"{BASE}/DocumentImages/SearchCaseNumber",
                    wait_until="domcontentloaded",
                    timeout=90000,
                )
            # No wait_for_selector here: the form is server-rendered (present
            # at domcontentloaded), and its poller starves when sibling preview
            # tabs monopolize the renderer — it timed out on pages that were
            # fine, losing real cases. A genuinely missing form/token makes the
            # fetch below report searchForm=false, which retries anyway.
            result = await page.evaluate(_SEARCH_FETCH, case_number)
            if (
                result["ok"]
                and not result["foreign"]
                and (result["docs"] or result["searchForm"])
            ):
                return result["docs"], result["html"], result["pages"]
            # Expired token/session, WAF hiccup, or a contaminated response
            # carrying another case's rows — worth one fresh retry.
            raise RuntimeError(
                f"unexpected search response "
                f"(ok={result['ok']}, foreign={result['foreign']})"
            )

        try:
            return await attempt()
        except (PlaywrightError, RuntimeError) as exc:
            print(f"[{case_number}] search failed, trying again: {exc!r}")
            await self._continue_as_guest(page)  # session may have expired
            return await attempt()

    async def _scrape_case(
        self,
        page: Page,
        case_number: str,
        docs: list[dict[str, str]],
        html: str,
        pages: list[int],
    ) -> ScrapedTrialCase | None:
        # docs/html/pages come from the probe search in this same session — no
        # re-search. html is the page-1 results HTML; it carries case metadata.
        # Pagination MUST happen here, in the session that searched (the server
        # holds the current case per session); downloads then go to the pool.
        docs = await _collect_all_documents(page, docs, self.max_docs, pages)
        selected = docs[: self.max_docs]
        print(
            f"[{case_number}] {len(docs)} document(s) found; "
            f"downloading {len(selected)}"
        )

        pdf_by_id = await self._download_documents(case_number, selected)

        documents: list[ScrapedTrialDocument] = []
        for d in selected:
            raw = pdf_by_id.get(d["docId"])
            if raw is None:
                print(
                    f"  [{case_number}] doc {d['docId']} could not be "
                    "downloaded — skipping it"
                )
                continue
            docket_date = _parse_date(d["date"])
            if docket_date is None:
                print(
                    f"  [{case_number}] doc {d['docId']} has an unreadable "
                    f"date {d['date']!r} — skipping it"
                )
                continue
            documents.append(
                ScrapedTrialDocument(
                    docket_entry_date=docket_date,
                    content_hash=hashlib.sha256(raw).hexdigest(),
                    is_opinion=_is_opinion(d["description"]),
                    description=d["description"],
                    document_name=d["description"],
                    raw_content=raw,
                )
            )

        self._docs_saved += len(documents)
        self._docs_failed += len(selected) - len(documents)

        if not documents:
            return None

        return ScrapedTrialCase(
            case_number=case_number,
            court_id=self.court_id,
            court_name=self.court_name,
            meta_data=_case_meta(html),
            html=html,
            document_list=documents,
        )

    async def _download_documents(
        self, case_number: str, selected: list[dict[str, str]]
    ) -> dict[str, bytes]:
        """Download every selected doc via the shared pool and return
        {docId: pdf_bytes}. Each doc becomes a queue job executed by whichever
        worker session has a free preview tab — a case's ~25s-per-doc captchas
        solve across ALL sessions at once instead of queueing behind this
        worker's own solver. A doc that never starts or lands incomplete is
        resubmitted once (a fresh preview, likely on another session)."""
        got: dict[str, bytes] = {}
        loop = asyncio.get_running_loop()
        for attempt in range(2):
            todo = [d for d in selected if d["docId"] not in got]
            if not todo:
                break
            jobs = [(d, loop.create_future()) for d in todo]
            for job in jobs:
                self._dl_queue.put_nowait(job)
            for d, fut in jobs:
                data = await fut
                if data is not None:
                    got[d["docId"]] = data
            if attempt == 0 and len(got) < len(selected):
                print(
                    f"  [{case_number}] {len(selected) - len(got)} download(s) "
                    "did not arrive — trying those again"
                )
        return got

    async def _consume(self, bb: BrowserBase, page: Page) -> None:
        """One preview-tab slot of the download pool: pull jobs off the shared
        queue and run them in this worker's session until cancelled (when the
        worker retires). A job interrupted by that cancellation goes back on
        the queue for a still-live session; any other failure resolves the job
        with None so the submitting case can resubmit or move on."""
        while True:
            doc, fut = await self._dl_queue.get()
            try:
                fut.set_result(await self._execute_job(bb, page, doc))
            except asyncio.CancelledError:
                self._dl_queue.put_nowait((doc, fut))
                raise
            except Exception as exc:  # one doc must not kill this pool slot
                print(f"  [{doc['caseNumber']}] doc {doc['docId']}: {exc!r}")
                if not fut.done():
                    fut.set_result(None)

    async def _execute_job(
        self, bb: BrowserBase, page: Page, doc: dict[str, str]
    ) -> bytes | None:
        """Preview one doc in a fresh tab of this session and return its PDF
        bytes — None if the download never starts or lands incomplete."""
        did = doc["docId"]
        print(f"  [{doc['caseNumber']}] downloading: {doc['description']}")
        tab = await page.context.new_page()
        try:
            if not await self._trigger_download(tab, doc):
                return None
            # The transfer finishes well under a second after it begins; wait
            # on this session's CDP completion events (in-memory, no network)
            # so the tab never closes mid-transfer. 30s covers a straggler.
            for _ in range(60):
                if did in {_doc_id_in(n) for n in bb.completed_downloads}:
                    break
                await asyncio.sleep(0.5)
        finally:
            await tab.close()
        # Fetch the bytes once via the per-file downloads API; storage sync can
        # lag the completion event a moment, so re-list briefly for stragglers.
        for round_ in range(3):
            if round_:
                await asyncio.sleep(2)
            files = _pick_download_files(await bb.list_download_files())
            if did in files:
                data = await bb.get_download_file(files[did]["id"])
                # An incomplete capture won't heal by refetching; resubmission
                # re-previews it fresh (largest capture then wins the pick).
                return data if _is_complete_pdf(data) else None
        return None

    async def _trigger_download(self, page: Page, doc: dict[str, str]) -> bool:
        """Open the captcha-gated Preview once and return True if Chrome starts
        the PDF download (False if it never starts). Browserbase solves the
        captcha while we wait; retrying is handled by _download_documents."""
        preview_url = (
            f"{BASE}/DocumentImages/PreviewWait?id={quote(doc['docId'])}"
            f"&securityKey={quote(doc['securityKey'])}"
            f"&source={quote(doc['source'])}&caseType={quote(doc['caseType'])}"
            f"&caseNumber={quote(doc['caseNumber'])}"
        )
        try:
            async with page.expect_download(timeout=120000):
                try:
                    await page.goto(
                        preview_url, wait_until="domcontentloaded", timeout=120000
                    )
                except PlaywrightError:
                    pass  # navigation aborts when the download begins
            return True
        except PlaywrightTimeoutError:
            return False


async def _collect_all_documents(
    page: Page,
    docs: list[dict[str, str]],
    max_docs: int,
    page_numbers: list[int],
) -> list[dict[str, str]]:
    """Walk the results pager, returning page 1's ``docs`` plus each further
    page's documents until we have enough for max_docs or run out of pages.
    Results hold 50 docs per page; further pages are at
    SelectDocuments?page=N (the case is held in the session). ``page_numbers``
    is page 1's pager as seen by the fetch search — the live DOM is still the
    search form at that point, so it can't be read from there; after each
    navigation the pager is reread from the newly-loaded page."""
    docs = list(docs)
    current = 1
    while len(docs) < max_docs and current + 1 in page_numbers:
        current += 1
        await page.goto(
            f"{BASE}/DocumentImages/SelectDocuments?page={current}",
            wait_until="domcontentloaded",
            timeout=90000,
        )
        # Every further page has doc rows, so wait for one instead of a blind
        # sleep — a slow page must not silently truncate the document list.
        await page.wait_for_selector("input[type=checkbox][id^='Doc']", timeout=30000)
        docs += await page.evaluate(_EXTRACT_DOCS)
        page_numbers = await page.evaluate(_PAGE_LINKS)
    return docs


def _pick_download_files(
    files: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Map docId -> the downloads-API listing entry to fetch. Filenames look
    like ``e78869237(1).pdf`` — the docId is embedded. If a doc was captured
    more than once, the largest file wins (a truncated capture is smaller)."""
    best: dict[str, dict[str, Any]] = {}
    for f in files:
        doc_id = _doc_id_in(f["filename"])
        if doc_id and (doc_id not in best or f["size"] > best[doc_id]["size"]):
            best[doc_id] = f
    return best


def _is_complete_pdf(data: bytes) -> bool:
    # A capture truncated mid-transfer still starts with %PDF but lacks the
    # %%EOF trailer.
    return data.startswith(b"%PDF") and b"%%EOF" in data[-2048:]


def _doc_id_in(filename: str) -> str | None:
    # Filenames start with "e<docId>", e.g. "e78869237(1)-1783196950852.pdf".
    m = re.match(r"e(\d+)", filename)
    return m.group(1) if m else None


def _parse_date(text: str) -> datetime | None:
    # Rows normally carry an M/D/YYYY date, but guard the odd empty/malformed
    # cell — a bare strptime would crash the case after downloads were spent.
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y")
    except ValueError:
        return None


def _is_opinion(description: str) -> bool:
    # TODO: keyword heuristic; a real system would map document type codes.
    low = description.lower()
    return any(h in low for h in _OPINION_HINTS)


def _case_meta(html: str) -> dict[str, str | None]:
    def field(label: str) -> str | None:
        m = re.search(rf"{label}:\s*</b>\s*([^<]+?)\s*<br", html)
        # Read from raw HTML, so decode entities (e.g. &amp; -> &).
        return unescape(m.group(1).strip()) if m else None

    return {
        "case_title": field("Case Title"),
        "case_type": field("Case Type"),
        "filing_date": field("Filing Date"),
    }
