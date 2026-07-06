import asyncio
import time
from typing import Any

import httpx
from browserbase import AsyncBrowserbase, RateLimitError
from browserbase.types.session import Session
from browserbase.types.session_create_params import BrowserSettings
from browserbase.types.session_create_response import SessionCreateResponse
from playwright.async_api import Browser, Page, Playwright, async_playwright

# The per-file downloads API (list files / fetch one) — not wrapped by the
# Python SDK (<=1.14), so called over plain REST.
_API = "https://api.browserbase.com/v1"


class BrowserBase:
    """Async context manager that owns a Browserbase session + Playwright browser.

    Returns (bb_session, page) on entry; caller handles all navigation and logic.
    """

    def __init__(self, bb: AsyncBrowserbase, project_id: str) -> None:
        self._bb = bb
        self._project_id = project_id
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._bb_session: Session | None = None
        self._http: httpx.AsyncClient | None = None
        self._began: dict[str, str] = {}  # download guid -> suggested filename
        # Setup phase timings (seconds), filled by __aenter__ so the worker can
        # log where its startup time went (429 backoff shows up in create).
        self.create_secs = 0.0
        self.connect_secs = 0.0
        # Filenames the browser reported fully transferred (CDP download
        # events) — once a name lands here its tab can close without
        # truncating the PDF, and the file is (about to be) in storage.
        self.completed_downloads: set[str] = set()

    async def __aenter__(self) -> tuple[Session, Page]:
        try:
            t0 = time.monotonic()
            self._bb_session = await self._create_session()
            self.create_secs = time.monotonic() - t0
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(
                self._bb_session.connect_url
            )
            self.connect_secs = time.monotonic() - t0 - self.create_secs
            # Route browser downloads into Browserbase storage so they can be
            # pulled back with the downloads API. downloadPath must be
            # "downloads"; eventsEnabled feeds the completion tracking below.
            cdp = await self._browser.new_browser_cdp_session()
            await cdp.send(  # pyright: ignore[reportUnknownMemberType]
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": "downloads",
                    "eventsEnabled": True,
                },
            )

            def on_begin(e: dict[str, Any]) -> None:
                self._began[e["guid"]] = e["suggestedFilename"]

            def on_progress(e: dict[str, Any]) -> None:
                if e.get("state") == "completed" and e["guid"] in self._began:
                    self.completed_downloads.add(self._began[e["guid"]])

            cdp.on("Browser.downloadWillBegin", on_begin)
            cdp.on("Browser.downloadProgress", on_progress)
            self._http = httpx.AsyncClient(
                headers={"X-BB-API-Key": self._bb.api_key}, timeout=60.0
            )
            context = self._browser.contexts[0]
            page = context.pages[0]
            return self._bb_session, page
        except BaseException:
            await self._take_down()
            raise

    async def _create_session(self) -> SessionCreateResponse:
        """Create a Browserbase session, backing off on rate limits so a worker
        rides out a burst of 429s instead of dying. This is the outer net for
        when the SDK's own per-call retries are exhausted under sustained load."""
        for attempt in range(4):
            try:
                return await self._bb.sessions.create(
                    project_id=self._project_id,
                    browser_settings=BrowserSettings(solve_captchas=True),
                    proxies=True,
                    # Max session lifetime (6h), so Browserbase can't reap a
                    # session mid-run during a long full-sweep scrape.
                    api_timeout=21600,
                )
            except RateLimitError:
                if attempt == 3:
                    raise
                await asyncio.sleep(2 * 2**attempt)  # 2s, 4s, 8s
        raise AssertionError("unreachable: loop returns or raises")

    async def list_download_files(self) -> list[dict[str, Any]]:
        """Metadata for every file downloaded this session: id, filename, size."""
        assert self._bb_session is not None and self._http is not None
        resp = await self._http.get(
            f"{_API}/downloads", params={"sessionId": self._bb_session.id}
        )
        resp.raise_for_status()
        return resp.json()["downloads"]

    async def get_download_file(self, file_id: str) -> bytes:
        """One downloaded file's bytes, by its listing id."""
        assert self._http is not None
        resp = await self._http.get(f"{_API}/downloads/{file_id}")
        resp.raise_for_status()
        return resp.content

    async def __aexit__(self, *_: object) -> None:
        await self._take_down()

    async def _take_down(self):
        try:
            if self._browser:
                await asyncio.wait_for(self._browser.close(), timeout=5.0)
        except BaseException:
            pass
        try:
            if self._pw:
                await asyncio.wait_for(self._pw.stop(), timeout=5.0)
        except BaseException:
            pass
        try:
            if self._http:
                await self._http.aclose()
        except BaseException:
            pass
        self._browser = None
        self._pw = None
        self._bb_session = None
        self._http = None


class BrowserBaseFactory:
    def __init__(self, api_key: str, project_id: str, max_sessions: int = 16) -> None:
        # max_sessions = how many parallel browser sessions the scraper runs
        # (one per worker). Keep it at or below your plan's concurrent-browser
        # limit (Developer 25, Startup 100); the default 16 is the measured
        # throughput sweet spot. Set via BROWSERBASE_CONCURRENCY.
        self.max_sessions = max_sessions
        self._api_key = api_key
        self._project_id = project_id
        # max_retries: the SDK retries 429s itself with backoff + retry-after;
        # a bigger budget rides out short bursts under high concurrency.
        self._bb = AsyncBrowserbase(api_key=self._api_key, max_retries=5)

    def new_browser_base(self) -> BrowserBase:
        return BrowserBase(self._bb, self._project_id)
