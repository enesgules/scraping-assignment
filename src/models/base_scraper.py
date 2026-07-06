from collections.abc import Awaitable, Callable
from datetime import date

from ..browser_base_factory import BrowserBaseFactory
from .case import ScrapedTrialCase

type InsertCase = Callable[[ScrapedTrialCase], Awaitable[None]]
# A document or case that was found but couldn't be captured, recorded so a
# later run can retry it (see scraping_pipeline). Free-form dict: always carries
# ``case_number`` and ``reason``, plus whatever identifies the thing to refetch.
type RecordFailure = Callable[[dict[str, object]], Awaitable[None]]


class TrialScraper:
    scraper_id: str
    court_id: str
    court_name: str

    def __init__(
        self, to_date: date, from_date: date, browser: BrowserBaseFactory
    ) -> None:
        self.to_date = to_date
        self.from_date = from_date
        self.browser = browser

    async def scrape(
        self, insert_case: InsertCase, record_failure: RecordFailure
    ) -> None:
        raise NotImplementedError
