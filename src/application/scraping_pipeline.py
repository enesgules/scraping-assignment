from dataclasses import dataclass
from datetime import date

from ..ports import BrowserBaseFactory
from ..models import TrialScraper, ScrapedTrialCase


@dataclass
class ScrapingPipelineDeps:
    browser_base: BrowserFactory
    scrapers: list[type[TrialScraper]]


def create_scraping_pipeline(deps: ScrapingPipelineDeps):
    async def scraping_pipeline(to_date: date, from_date: date):
        async def insert_case(case: ScrapedTrialCase):
            doc_names = ", ".join(d.document_name for d in case.document_list[:3])
            suffix = (
                f" +{len(case.document_list) - 3} more"
                if len(case.document_list) > 3
                else ""
            )
            print(
                f"[{case.case_number}] {case.court_name} — {len(case.document_list)} doc(s): {doc_names}{suffix}"
            )

        for Scraper in deps.scrapers:
            scraper = Scraper(to_date, from_date)
            await scraper.scrape(insert_case)

    return scraping_pipeline
