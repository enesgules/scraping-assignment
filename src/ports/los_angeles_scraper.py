from ..browser_base_factory import BrowserBaseFactory

from ..models import TrialScraper, InsertCase, ScrapedTrialCase


class LosAngelesScraper(TrialScraper):
    scraper_id = ""
    court_id = ""

    async def scrape(self, insert_case: InsertCase) -> None:
        factory=BrowserBaseFactory("", "")

        async with factory.new_browser_base() as (bb_session, page):
            input = await page.get_attribute("aria-label="Case Number Input Field"")

            await page.type(input, "dfn[odsaf]")
            await page.wait_for_load_state()

            await get_document()

            ScrapedTrialCase("case_num", "los", "los", page.print(), )