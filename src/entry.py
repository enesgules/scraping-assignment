import argparse
import asyncio
import io
import os
import sys
from datetime import date

from dotenv import load_dotenv

from .application.scraping_pipeline import (
    ScrapingPipelineDeps,
    create_scraping_pipeline,
)
from .browser_base_factory import BrowserBaseFactory
from .ports import LosAngelesScraper


def require_env(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        raise ValueError(f"missing environment var: {k}")
    return v


def main():
    parser = argparse.ArgumentParser(description="Run the court scraping pipeline")
    parser.add_argument(
        "--from-date", required=True, type=date.fromisoformat, metavar="YYYY-MM-DD"
    )
    parser.add_argument(
        "--to-date", required=True, type=date.fromisoformat, metavar="YYYY-MM-DD"
    )
    args = parser.parse_args()

    # Flush progress per line even when piped to a file (long runs otherwise
    # look frozen while stdout block-buffers).
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportUnknownMemberType]
    load_dotenv()
    pipeline = create_scraping_pipeline(
        deps=ScrapingPipelineDeps(
            browser_base=BrowserBaseFactory(
                project_id=require_env("BROWSERBASE_PROJECT_ID"),
                api_key=require_env("BROWSERBASE_API_KEY"),
                # Parallel browser sessions = workers. Default 16 (measured
                # throughput sweet spot); keep it at or below your plan's
                # concurrent-browser limit (Developer 25, Startup 100).
                max_sessions=int(os.environ.get("BROWSERBASE_CONCURRENCY", "16")),
            ),
            scrapers=[LosAngelesScraper],
        )
    )
    try:
        asyncio.run(pipeline(to_date=args.to_date, from_date=args.from_date))
    except KeyboardInterrupt:
        # asyncio.run already cancelled the workers and closed the Browserbase
        # sessions on the way out; the partial-run summary printed from the
        # scraper's finally block. Just don't dump the traceback.
        print("interrupted — browser sessions closed", file=sys.stderr)
        raise SystemExit(130)
