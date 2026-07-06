from dataclasses import dataclass
from datetime import date

from ..browser_base_factory import BrowserBaseFactory
from ..case_store import CaseStore
from ..models import TrialScraper


@dataclass
class ScrapingPipelineDeps:
    browser_base: BrowserBaseFactory
    scrapers: list[type[TrialScraper]]


def create_scraping_pipeline(deps: ScrapingPipelineDeps):
    async def scraping_pipeline(to_date: date, from_date: date):
        # One store per run — it accumulates this run's cases/failures. The
        # scrapers write to its insert_case / record_failure sinks.
        store = CaseStore()
        for Scraper in deps.scrapers:
            scraper = Scraper(to_date, from_date, deps.browser_base)
            await scraper.scrape(store.insert_case, store.record_failure)

    return scraping_pipeline
