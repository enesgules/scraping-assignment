from ..models import TrialScraper, InsertCase


class LosAngelesScraper(TrialScraper):
    scraper_id = ""
    court_id = ""

    async def scrape(self, insert_case: InsertCase) -> None: ...
