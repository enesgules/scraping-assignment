"""Checks for the pure parsing/mapping helpers (no network)."""

# pyright: reportPrivateUsage=false, reportArgumentType=false

import asyncio
import io
import zipfile
from datetime import date, datetime

from src.ports.los_angeles_scraper import (
    LosAngelesScraper,
    _case_meta,
    _doc_id_in,
    _is_opinion,
    _parse_date,
    _pdfs_by_doc_id,
)


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_doc_id_in():
    assert _doc_id_in("e78869237(1)-1783196950852.pdf") == "78869237"
    assert _doc_id_in("e123-99.pdf") == "123"
    assert _doc_id_in("random.pdf") is None


def test_pdfs_by_doc_id_maps_and_skips_non_pdf():
    z = _zip(
        {
            "e78869237(1)-1.pdf": b"%PDF-1.7 real",
            "notes.txt": b"not a pdf",
        }
    )
    out = _pdfs_by_doc_id(z)
    assert set(out) == {"78869237"}
    assert out["78869237"].startswith(b"%PDF")


def test_pdfs_by_doc_id_keeps_largest_duplicate():
    z = _zip(
        {
            "e5(1)-1.pdf": b"%PDF small",
            "e5(2)-2.pdf": b"%PDF the larger capture wins",
        }
    )
    assert _pdfs_by_doc_id(z)["5"] == b"%PDF the larger capture wins"


def test_pdfs_by_doc_id_empty():
    assert _pdfs_by_doc_id(b"") == {}


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
        "<b>Case Title: </b> MATTHEW SHERMAN, ET AL. VS GEORGE BARNETT <br>"
        "<b>Case Type: </b> Motor Vehicle - Personal Injury <br>"
        "<b>Filing Date: </b> 4/9/2019 <br>"
    )
    meta = _case_meta(html)
    title, case_type = meta["case_title"], meta["case_type"]
    assert title is not None and title.startswith("MATTHEW SHERMAN")
    assert meta["filing_date"] == "4/9/2019"
    assert case_type is not None and "Motor Vehicle" in case_type


class _FakePage:
    """Minimal stand-in for a Playwright Page: models a paged result set of
    50 docs/page so we can test the paging loop without a browser."""

    def __init__(self, total_docs: int) -> None:
        self.pages = [
            [{"docId": str(i)} for i in range(p, min(p + 50, total_docs))]
            for p in range(0, total_docs, 50)
        ] or [[]]
        self.current = 1

    async def evaluate(self, script: str):
        if "pagnation" in script:  # _PAGE_LINKS: all pages except the current
            return [n for n in range(1, len(self.pages) + 1) if n != self.current]
        return self.pages[self.current - 1]  # _EXTRACT_DOCS for current page

    async def goto(self, url: str, **_: object) -> None:
        self.current = int(url.split("page=")[1])

    async def wait_for_timeout(self, _ms: int) -> None:
        pass


def _scraper(max_docs: int) -> LosAngelesScraper:
    s = LosAngelesScraper(date(2019, 12, 31), date(2019, 1, 1), browser=None)
    s.max_docs = max_docs
    return s


def test_paging_walks_all_pages_when_wanted():
    page = _FakePage(total_docs=116)  # 3 pages: 50 + 50 + 16
    docs = asyncio.run(_scraper(1000)._collect_all_documents(page, page.pages[0][:]))
    assert len(docs) == 116
    assert len({d["docId"] for d in docs}) == 116  # no duplicates across pages


def test_paging_stops_early_at_max_docs():
    page = _FakePage(total_docs=200)
    docs = asyncio.run(_scraper(5)._collect_all_documents(page, page.pages[0][:]))
    assert len(docs) == 50  # page 1 already satisfies max_docs; no extra pages
    assert page.current == 1


def test_paging_single_page_case():
    page = _FakePage(total_docs=20)
    docs = asyncio.run(_scraper(1000)._collect_all_documents(page, page.pages[0][:]))
    assert len(docs) == 20  # no next page; terminates cleanly


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all passed")
