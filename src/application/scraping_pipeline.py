import asyncio
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..browser_base_factory import BrowserBaseFactory
from ..models import ScrapedTrialCase, TrialScraper

# Where scraped cases are persisted.
DB_PATH = Path("cases.json")


@dataclass
class ScrapingPipelineDeps:
    browser_base: BrowserBaseFactory
    scrapers: list[type[TrialScraper]]


def _record(case: ScrapedTrialCase) -> dict[str, object]:
    """A JSON-serializable case record: metadata only — no PDF bytes and no
    page HTML (both large). Each document keeps its hash and size so the row is
    still verifiable/dedupable without carrying the file."""
    return {
        "case_number": case.case_number,
        "court_id": case.court_id,
        "court_name": case.court_name,
        "meta_data": case.meta_data,
        "documents": [
            {
                "docket_entry_date": d.docket_entry_date.date().isoformat(),
                "description": d.description,
                "document_name": d.document_name,
                "content_hash": d.content_hash,
                "is_opinion": d.is_opinion,
                "size_bytes": len(d.raw_content),
            }
            for d in case.document_list
        ],
    }


def create_scraping_pipeline(deps: ScrapingPipelineDeps):
    async def scraping_pipeline(to_date: date, from_date: date):
        cases: list[dict[str, object]] = []
        lock = asyncio.Lock()

        async def insert_case(case: ScrapedTrialCase) -> None:
            # Workers call this concurrently; serialize the read-modify-write and
            # rename the file atomically so the DB is never half-written.
            async with lock:
                cases.append(_record(case))
                tmp = DB_PATH.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(cases, indent=2))
                tmp.replace(DB_PATH)
            print(
                f"[{case.case_number}] saved — {len(case.document_list)} doc(s); "
                f"{len(cases)} case(s) now in {DB_PATH}"
            )

        for Scraper in deps.scrapers:
            scraper = Scraper(to_date, from_date, deps.browser_base)
            await scraper.scrape(insert_case)

    return scraping_pipeline
