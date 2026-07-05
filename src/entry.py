import argparse
import asyncio
import os
from datetime import date

from dotenv import load_dotenv
from .application.scraping_pipeline import (
    ScrapingPipelineDeps,
    create_scraping_pipeline,
)
from .ports import BrowserBaseFactory, LosAngelesScraper

load_dotenv()


def require_env(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        raise ValueError(f"missing environment var: {k}")
    return v


pipeline = create_scraping_pipeline(
    deps=ScrapingPipelineDeps(
        browser_base=BrowserBaseFactory(
            project_id=require_env("BROWSERBASE_PROJECT_ID"),
            api_key=require_env("BROWSERBASE_API_KEY"),
        ),
        scrapers=[LosAngelesScraper],
    )
)


def main():
    parser = argparse.ArgumentParser(description="Run the court scraping pipeline")
    parser.add_argument(
        "--from-date", required=True, type=date.fromisoformat, metavar="YYYY-MM-DD"
    )
    parser.add_argument(
        "--to-date", required=True, type=date.fromisoformat, metavar="YYYY-MM-DD"
    )
    args = parser.parse_args()

    asyncio.run(pipeline(to_date=args.to_date, from_date=args.from_date))
