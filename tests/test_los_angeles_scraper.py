"""Checks for the pure parsing/mapping helpers (no network)."""

# pyright: reportPrivateUsage=false

import asyncio
from datetime import date, datetime
from typing import cast

from playwright.async_api import Page

from src.browser_base_factory import BrowserBaseFactory
from src.ports.los_angeles_scraper import (
    DocRow,
    LosAngelesScraper,
    _case_meta,
    _collect_all_documents,
    _doc_id_in,
    _is_complete_pdf,
    _is_opinion,
    _parse_date,
    _pick_download_files,
)


def _row(doc_id: str, description: str = "a doc") -> DocRow:
    return DocRow(
        docId=doc_id,
        securityKey="key",
        caseType="CIV",
        source="0",
        caseNumber="X",
        date="4/9/2019",
        description=description,
    )


def test_doc_id_in():
    assert _doc_id_in("e78869237(1)-1783196950852.pdf") == "78869237"
    assert _doc_id_in("e123-99.pdf") == "123"
    assert _doc_id_in("random.pdf") is None


def test_pick_download_files_maps_and_skips_unrecognized():
    files = [
        {"id": "a", "filename": "e78869237(1).pdf", "size": 100},
        {"id": "b", "filename": "notes.txt", "size": 5},
    ]
    out = _pick_download_files(files)
    assert set(out) == {"78869237"}
    assert out["78869237"]["id"] == "a"


def test_pick_download_files_keeps_largest_duplicate():
    # A doc captured twice: the truncated (smaller) file loses.
    files = [
        {"id": "small", "filename": "e5(1).pdf", "size": 10},
        {"id": "big", "filename": "e5(2).pdf", "size": 999},
    ]
    assert _pick_download_files(files)["5"]["id"] == "big"


def test_pick_download_files_empty():
    assert _pick_download_files([]) == {}


def test_is_complete_pdf():
    assert _is_complete_pdf(b"%PDF-1.7 real\n%%EOF")
    assert not _is_complete_pdf(b"%PDF-1.7 cut off mid-stream")  # no trailer
    assert not _is_complete_pdf(b"<html>error page</html>")
    assert not _is_complete_pdf(b"")


def test_download_pool_resubmits_failed_doc():
    """A doc whose job resolves None (preview never started, bad capture, …)
    is resubmitted once and can succeed on the second pass."""

    async def run():
        scraper = LosAngelesScraper(
            date(2019, 12, 31), date(2019, 1, 1), cast(BrowserBaseFactory, None)
        )
        pdf = b"%PDF ok %%EOF"
        failures = {"2": 1}  # docId -> jobs that resolve None before succeeding

        async def consumer():
            while True:
                doc, fut = await scraper._dl_queue.get()
                if failures.get(doc["docId"], 0) > 0:
                    failures[doc["docId"]] -= 1
                    fut.set_result(None)
                else:
                    fut.set_result(pdf)

        task = asyncio.create_task(consumer())
        selected = [_row("1"), _row("2")]
        try:
            got = await scraper._download_documents("X", selected)
        finally:
            task.cancel()
        assert got == {"1": pdf, "2": pdf}  # doc 2 recovered on resubmission
        assert failures["2"] == 0

    asyncio.run(run())


def test_parse_date():
    assert _parse_date("4/9/2019") == datetime(2019, 4, 9)
    assert _parse_date(" 9/21/2020 ") == datetime(2020, 9, 21)
    assert _parse_date("") is None  # missing cell must not crash the case
    assert _parse_date("not a date") is None


def test_is_opinion():
    assert _is_opinion("Minute Order (Hearing on Motion)")
    assert _is_opinion("Notice of Ruling")
    assert not _is_opinion("Request for Dismissal")


def test_case_meta():
    html = (
        "<b>Case Number: </b> 19STCV12345 <br>"
        "<b>Case Title: </b> SHERMAN VS MICHELMAN &amp; ROBINSON <br>"
        "<b>Case Type: </b> Motor Vehicle - Personal Injury <br>"
        "<b>Filing Date: </b> 4/9/2019 <br>"
    )
    meta = _case_meta(html)
    title, case_type = meta["case_title"], meta["case_type"]
    assert title == "SHERMAN VS MICHELMAN & ROBINSON"  # &amp; decoded
    assert meta["filing_date"] == "4/9/2019"
    assert case_type is not None and "Motor Vehicle" in case_type


class _FakePage:
    """Minimal stand-in for a Playwright Page: models a paged result set of
    50 docs/page so we can test the paging loop without a browser."""

    def __init__(self, total_docs: int) -> None:
        self.pages: list[list[DocRow]] = [
            [_row(str(i)) for i in range(p, min(p + 50, total_docs))]
            for p in range(0, total_docs, 50)
        ] or [[]]
        self.current = 1

    async def evaluate(self, script: str):
        if "pagnation" in script:  # _PAGE_LINKS: all pages except the current
            return [n for n in range(1, len(self.pages) + 1) if n != self.current]
        return self.pages[self.current - 1]  # _EXTRACT_DOCS for current page

    async def goto(self, url: str, **_: object) -> None:
        self.current = int(url.split("page=")[1])


def _collect(page: _FakePage, max_docs: int) -> list[DocRow]:
    pager = list(range(2, len(page.pages) + 1))  # page 1's pager, as _search sees it
    return asyncio.run(
        _collect_all_documents(cast(Page, page), page.pages[0], max_docs, pager)
    )


def test_paging_walks_all_pages_when_wanted():
    page = _FakePage(total_docs=116)  # 3 pages: 50 + 50 + 16
    docs = _collect(page, max_docs=1000)
    assert len(docs) == 116
    assert len({d["docId"] for d in docs}) == 116  # no duplicates across pages


def test_paging_stops_early_at_max_docs():
    page = _FakePage(total_docs=200)
    docs = _collect(page, max_docs=5)
    assert len(docs) == 50  # page 1 already satisfies max_docs; no extra pages
    assert page.current == 1


def test_paging_does_not_mutate_page_one_list():
    page = _FakePage(total_docs=116)
    _collect(page, max_docs=1000)
    assert len(page.pages[0]) == 50  # caller's list untouched


def test_paging_single_page_case():
    page = _FakePage(total_docs=20)
    docs = _collect(page, max_docs=1000)
    assert len(docs) == 20  # no next page; terminates cleanly


def test_paging_raises_on_empty_further_page():
    # Every real further page has doc rows; an empty read means the session
    # lost its case state and must fail loudly, not silently truncate.
    page = _FakePage(total_docs=116)
    page.pages[1] = []
    try:
        _collect(page, max_docs=1000)
    except RuntimeError as exc:
        assert "no document rows" in str(exc)
    else:
        raise AssertionError("expected RuntimeError on empty results page")
