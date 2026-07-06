"""Scraper for the LA Superior Court public case-document system.

1. GET ``GuestInformation`` -> sets the guest session cookie.
2. POST ``DocumentImages/SearchCaseNumber`` with a case number -> HTML results
   listing every imaged document (date, description, per-doc securityKey).
   Done via in-page ``fetch()`` from the parked search form (no navigation),
   so probing an empty case number costs one round-trip, not two page loads.
3. GET ``DocumentImages/PreviewWait?id=..&securityKey=..`` -> a reCAPTCHA page
   (Browserbase solves it) -> 302 -> a one-time PDF URL that Chrome downloads.
4. Browserbase captures the download; we pull the zip via get_downloads() and
   match each PDF back to its document by the docId embedded in the filename.

Everything runs in the Browserbase browser — its residential proxy and real
Chrome fingerprint are what get past the WAF; the in-page fetch inherits both.
"""

import asyncio
import hashlib
import io
import os
import re
import zipfile
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from html import unescape
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

# Global ceiling on captcha-gated downloads in flight at once, across every
# worker and case. Without it, a big case (up to 50 docs) times LA_CONCURRENCY
# sessions opens hundreds of simultaneous previews and melts down.
_MAX_CONCURRENT_DOWNLOADS = 24

# Recycle a worker's session after this many downloaded cases: get_downloads()
# is cumulative per session and re-fetched each confirm, so an unbounded session
# grows quadratic. Reusing across a few cases still avoids a fresh session (and
# a re-search) per case, which is what the plan's concurrency budget bottlenecks.
_RECYCLE_SESSION_EVERY = 8


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
        # Worker sessions (each probes + downloads, so one Browserbase session
        # per worker). Raise toward BROWSERBASE_MAX_CONCURRENCY for big sweeps;
        # gains taper off once the captcha solver saturates.
        self.concurrency = int(os.environ.get("LA_CONCURRENCY", "8"))
        self._attempted = 0
        self._claimed = 0  # cases committed to downloading (quota gate)
        self._scraped = 0
        # Caps concurrent downloads across all workers (see constant above).
        self._dl_sem = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)

    async def scrape(self, insert_case: InsertCase) -> None:
        # No point opening more sessions than explicit case numbers to search.
        workers = min(self.concurrency, len(self.case_numbers) or self.concurrency)
        print(
            f"[los_angeles] starting — up to {self.max_cases} case(s), "
            f"{self.max_docs} doc(s) each; opening {workers} browser session(s)…"
        )
        case_iter = iter(self._target_case_numbers())
        results = await asyncio.gather(
            *(self._worker(case_iter, insert_case) for _ in range(workers)),
            return_exceptions=True,  # one dead session must not kill the rest
        )
        for exc in results:
            if isinstance(exc, BaseException):
                print(f"[los_angeles] a browser session failed: {exc!r}")
        print(
            f"[los_angeles] done: scraped {self._scraped} of "
            f"{self._attempted} case(s) tried"
        )

    async def _worker(self, case_iter: Iterator[str], insert_case: InsertCase) -> None:
        """Pull case numbers off the shared iterator until the quota is filled
        or the numbers run out, probing and downloading in the SAME session so
        there's no fresh session (or re-search) per case. The session is
        recycled every _RECYCLE_SESSION_EVERY downloaded cases to keep its
        cumulative get_downloads() zip small. Sharing a plain iterator between
        workers is safe: next() has no await point."""
        while self._claimed < self.max_cases:
            bb = self.browser.new_browser_base()
            async with bb as (_session, page):
                await self._continue_as_guest(page)
                downloaded = 0
                for case_number in case_iter:
                    if self._claimed >= self.max_cases:
                        return  # quota already claimed (by any worker)
                    self._attempted += 1
                    try:
                        print(f"[{case_number}] checking for documents…")
                        docs, html, pages = await self._search(page, case_number)
                    except Exception as exc:  # one bad case must not kill the sweep
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
                            page, bb, case_number, docs, html, pages
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
                    downloaded += 1
                    if downloaded >= _RECYCLE_SESSION_EVERY:
                        break  # recycle the session to bound its download zip
                else:
                    return  # case_iter exhausted

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
            await page.wait_for_selector("#CaseNumber", timeout=15000)
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
        bb: BrowserBase,
        case_number: str,
        docs: list[dict[str, str]],
        html: str,
        pages: list[int],
    ) -> ScrapedTrialCase | None:
        # docs/html/pages come from the probe search in this same session — no
        # re-search. html is the page-1 results HTML; it carries case metadata.
        docs = await _collect_all_documents(page, docs, self.max_docs, pages)
        selected = docs[: self.max_docs]
        print(
            f"[{case_number}] {len(docs)} document(s) found; "
            f"downloading {len(selected)}"
        )

        pdf_by_id = await self._download_documents(page, bb, case_number, selected)

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
        self,
        page: Page,
        bb: BrowserBase,
        case_number: str,
        selected: list[dict[str, str]],
    ) -> dict[str, bytes]:
        """Download every selected doc and return {docId: pdf_bytes}.

        Each doc previews in its own tab (bounded by _dl_sem), but the whole
        batch is confirmed from a single get_downloads() per round rather than
        one fetch per document — the cumulative session zip is fetched a handful
        of times, not once per doc. A truncated capture is rejected by
        _pdfs_by_doc_id and retried once with a fresh preview."""
        got: dict[str, bytes] = {}

        async def start(i: int, d: dict[str, str], tabs: dict[str, Page]) -> bool:
            # Slot released once the download has started; the tab stays open so
            # the PDF finishes transferring — closing early would truncate it.
            async with self._dl_sem:  # bound concurrent captcha-gated previews
                did = d["docId"]
                print(
                    f"  [{case_number}] downloading {i}/{len(selected)}: "
                    f"{d['description']}"
                )
                tab = await page.context.new_page()
                tabs[did] = tab
                try:
                    return await self._trigger_download(tab, d)
                except Exception as exc:  # one doc must not sink its siblings
                    print(f"  [{case_number}] doc {did} download failed: {exc!r}")
                    return False

        for attempt in range(2):
            todo = [(i, d) for i, d in enumerate(selected, 1) if d["docId"] not in got]
            if not todo:
                break
            tabs: dict[str, Page] = {}  # docId -> open tab, closed once its PDF lands
            flags = await asyncio.gather(*(start(i, d, tabs) for i, d in todo))
            started = {d["docId"] for (_, d), ok in zip(todo, flags) if ok}
            # One zip fetch per poll; close each tab only once its PDF has fully
            # landed, so nothing is cut off mid-transfer. Wait only on downloads
            # that actually began — one that never started can't arrive, and
            # polling for it would burn zip fetches and sleeps for nothing.
            for _ in range(6 if started else 0):
                got = _pdfs_by_doc_id(await bb.get_downloads())
                for did in [k for k in tabs if k in got]:
                    await tabs.pop(did).close()
                if started <= got.keys():
                    break
                await asyncio.sleep(2)
            for tab in tabs.values():  # never arrived — retried next round
                await tab.close()
            if attempt == 0 and len(got) < len(selected):
                print(
                    f"  [{case_number}] {len(selected) - len(got)} download(s) "
                    "did not arrive — trying those again"
                )
        return got

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


def _pdfs_by_doc_id(zip_bytes: bytes) -> dict[str, bytes]:
    """Map docId -> PDF bytes from a Browserbase downloads zip. Filenames look
    like ``e78869237(1)-1783196950852.pdf`` — the docId is embedded."""
    result: dict[str, bytes] = {}
    if not zip_bytes:
        return result
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            data = zf.read(name)
            # Accept only complete PDFs: a capture truncated when its tab closed
            # mid-download still starts with %PDF but lacks the %%EOF trailer.
            if not data.startswith(b"%PDF") or b"%%EOF" not in data[-2048:]:
                continue
            doc_id = _doc_id_in(name)
            # Keep the largest capture if a doc downloaded more than once.
            if doc_id and (doc_id not in result or len(data) > len(result[doc_id])):
                result[doc_id] = data
    return result


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
