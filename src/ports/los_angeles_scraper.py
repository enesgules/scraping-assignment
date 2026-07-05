"""Scraper for the LA Superior Court public case-document system.

1. GET ``GuestInformation`` -> sets the guest session cookie.
2. POST ``DocumentImages/SearchCaseNumber`` with a case number -> HTML results
   page listing every imaged document (date, description, per-doc securityKey).
3. GET ``DocumentImages/PreviewWait?id=..&securityKey=..`` -> a reCAPTCHA page
   (Browserbase solves it) -> 302 -> a one-time PDF URL that Chrome downloads.
4. Browserbase captures the download; we pull the zip via get_downloads() and
   match each PDF back to its document by the docId embedded in the filename.

Everything runs in the Browserbase browser: the search needs only a cookie, but
the download is captcha-gated, so driving one proxied browser is simpler than
juggling a saved session for HTTP calls.
"""

import asyncio
import hashlib
import io
import os
import re
import zipfile
from collections.abc import Iterable
from datetime import date, datetime
from urllib.parse import quote

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

# Pull every document row out of the results table: date, description, and the
# preview(id, securityKey, caseType, source, caseNumber) call args.
_EXTRACT_DOCS = r"""
() => {
  const rows = [...document.querySelectorAll('#paosForm tr')]
    .filter(r => r.querySelector('input[type=checkbox][id^="Doc"]'));
  return rows.map(r => {
    const tds = r.querySelectorAll(':scope > td');
    const prev = r.querySelector('input[onclick^="preview"]');
    const m = prev && prev.getAttribute('onclick')
      .match(/preview\('([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)'\)/);
    if (!m) return null;
    return {docId: m[1], securityKey: m[2], caseType: m[3], source: m[4],
            caseNumber: m[5], date: (tds[1]?.innerText || '').trim(),
            description: (tds[2]?.innerText || '').trim()};
  }).filter(Boolean);
}
"""

# Page numbers linked in the results pager (".pagnation" — their spelling).
# Results hold 50 documents per page; extra pages are plain links to
# SelectDocuments?page=N, served from the case held in the session.
_PAGE_LINKS = r"""
() => [...document.querySelectorAll('.pagnation a')]
  .map(a => parseInt(new URL(a.href).searchParams.get('page')))
  .filter(Number.isInteger)
"""

_OPINION_HINTS = ("opinion", "ruling", "order", "judgment", "minute order")


class LosAngelesScraper(TrialScraper):
    scraper_id = "los_angeles"
    court_id = "CA_LA_SUPERIOR"
    court_name = "Superior Court of California, County of Los Angeles"

    def __init__(
        self, to_date: date, from_date: date, browser: BrowserBaseFactory
    ) -> None:
        super().__init__(to_date, from_date, browser)
        # Runtime knobs (see README), read once here. Small, bounded run by
        # default: prove the pipeline on one or two real cases rather than
        # sweep thousands of empty sequence numbers.
        # TODO: env read here (not entry.py) because the pipeline's scraper
        # constructor is fixed; centralize if that interface gains a config slot.
        raw = os.environ.get("LA_CASE_NUMBERS", "")
        self.case_numbers = [c.strip() for c in raw.split(",") if c.strip()]
        self.max_cases = int(os.environ.get("LA_MAX_CASES", "1"))
        self.max_docs = int(os.environ.get("LA_MAX_DOCS", "5"))

    async def scrape(self, insert_case: InsertCase) -> None:
        print(
            f"[los_angeles] starting — up to {self.max_cases} case(s), "
            f"{self.max_docs} doc(s) each; opening browser…"
        )
        bb = self.browser.new_browser_base()
        attempted = 0
        scraped = 0
        async with bb as (_session, page):
            await self._continue_as_guest(page)
            for case_number in self._target_case_numbers():
                if scraped >= self.max_cases:
                    break
                attempted += 1
                try:
                    case = await self._scrape_case(page, bb, case_number)
                except Exception as exc:  # one bad case must not kill the sweep
                    print(f"[{case_number}] error, skipping: {exc!r}")
                    continue
                if case is None:
                    continue
                await insert_case(case)
                scraped += 1
        print(f"[los_angeles] done: scraped {scraped} of {attempted} case(s) tried")

    def _target_case_numbers(self) -> Iterable[str]:
        return self.case_numbers or generate_case_numbers(self.from_date, self.to_date)

    async def _continue_as_guest(self, page: Page) -> None:
        # Visiting GuestInformation establishes the guest cookie and lands on
        # the search form.
        await page.goto(
            f"{BASE}/GuestInformation", wait_until="domcontentloaded", timeout=90000
        )

    async def _scrape_case(
        self, page: Page, bb: BrowserBase, case_number: str
    ) -> ScrapedTrialCase | None:
        for attempt in range(2):
            await page.goto(
                f"{BASE}/DocumentImages/SearchCaseNumber",
                wait_until="domcontentloaded",
                timeout=90000,
            )
            try:
                await page.wait_for_selector("#CaseNumber", timeout=15000)
                break
            except PlaywrightTimeoutError:
                if attempt:
                    raise
                # Session likely expired back to login; re-establish and retry.
                await self._continue_as_guest(page)

        print(f"[{case_number}] searching…")
        await page.fill("#CaseNumber", case_number)
        # The results table is server-rendered, so waiting for the search POST
        # to finish navigating guarantees it's present — no blind sleep, no
        # reading a still-loading page and mistaking it for "no documents".
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=90000):
            await page.click("input[value='Search']")

        docs = await page.evaluate(_EXTRACT_DOCS)
        if not docs:
            print(f"[{case_number}] no documents — skipping")
            return None  # case not found or has no imaged documents
        html = await page.content()  # page 1 carries the case metadata

        docs = await self._collect_all_documents(page, docs)
        selected = docs[: self.max_docs]
        print(
            f"[{case_number}] {len(docs)} document(s) found; "
            f"downloading {len(selected)}"
        )

        for i, d in enumerate(selected, 1):
            print(
                f"  [{case_number}] downloading {i}/{len(selected)}: {d['description']}"
            )
            await self._trigger_download(page, d)

        pdf_by_id = await self._fetch_downloads(bb, {d["docId"] for d in selected})

        documents: list[ScrapedTrialDocument] = []
        for d in selected:
            raw = pdf_by_id.get(d["docId"])
            if not raw:
                print(f"  [{case_number}] doc {d['docId']} not captured, skipping")
                continue
            docket_date = _parse_date(d["date"])
            if docket_date is None:
                print(f"  [{case_number}] doc {d['docId']} bad date {d['date']!r}")
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

    async def _collect_all_documents(
        self, page: Page, docs: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """Walk the results pager, appending each page's documents until we
        have enough for max_docs or run out of pages. Results hold 50 docs per
        page; page 1 is already in ``docs``, further pages are at
        SelectDocuments?page=N (the case is held in the session)."""
        current = 1
        while len(docs) < self.max_docs:
            page_numbers = await page.evaluate(_PAGE_LINKS)
            if current + 1 not in page_numbers:
                break  # no next page
            current += 1
            await page.goto(
                f"{BASE}/DocumentImages/SelectDocuments?page={current}",
                wait_until="domcontentloaded",
                timeout=90000,
            )
            await page.wait_for_timeout(2500)
            docs += await page.evaluate(_EXTRACT_DOCS)
        return docs

    async def _fetch_downloads(
        self, bb: BrowserBase, wanted: set[str]
    ) -> dict[str, bytes]:
        """Pull the downloads zip, retrying while expected PDFs are still
        syncing to Browserbase storage (a fixed sleep could drop a slow one)."""
        pdf_by_id: dict[str, bytes] = {}
        for _ in range(4):
            pdf_by_id = _pdfs_by_doc_id(await bb.get_downloads())
            if wanted <= pdf_by_id.keys():
                break  # every expected doc has arrived
            await asyncio.sleep(2)
        return pdf_by_id

    async def _trigger_download(self, page: Page, doc: dict[str, str]) -> None:
        """Drive Preview (captcha auto-solved) until Chrome starts the PDF
        download. Retries once — captcha solving is best-effort."""
        preview_url = (
            f"{BASE}/DocumentImages/PreviewWait?id={quote(doc['docId'])}"
            f"&securityKey={quote(doc['securityKey'])}"
            f"&source={quote(doc['source'])}&caseType={quote(doc['caseType'])}"
            f"&caseNumber={quote(doc['caseNumber'])}"
        )
        for attempt in range(2):
            try:
                async with page.expect_download(timeout=120000):
                    try:
                        await page.goto(
                            preview_url, wait_until="domcontentloaded", timeout=120000
                        )
                    except Exception:
                        pass  # navigation aborts when the download begins
                await page.wait_for_timeout(3000)  # let Browserbase sync it
                return
            except PlaywrightTimeoutError:
                print(f"  doc {doc['docId']} no download (attempt {attempt + 1})")


def _pdfs_by_doc_id(zip_bytes: bytes) -> dict[str, bytes]:
    """Map docId -> PDF bytes from a Browserbase downloads zip. Filenames look
    like ``e78869237(1)-1783196950852.pdf`` — the docId is embedded."""
    result: dict[str, bytes] = {}
    if not zip_bytes:
        return result
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            data = zf.read(name)
            if not data.startswith(b"%PDF"):
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
        return m.group(1).strip() if m else None

    return {
        "case_title": field("Case Title"),
        "case_type": field("Case Type"),
        "filing_date": field("Filing Date"),
    }
