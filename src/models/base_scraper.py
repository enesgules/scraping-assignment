from collections.abc import Awaitable, Callable
from datetime import date

from .case import ScrapedTrialCase

type InsertCase = Callable[[ScrapedTrialCase], Awaitable[None]]


class TrialScraper:
    scraper_id: str
    court_id: str

    def __init__(self, to_date: date, from_date: date) -> None:
        self.to_date = to_date
        self.from_date = from_date

    async def scrape(self, insert_case: InsertCase): ...
