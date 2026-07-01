import asyncio

from browserbase import AsyncBrowserbase
from browserbase.types.session import Session
from browserbase.types.session_create_params import BrowserSettings
from playwright.async_api import Browser, Page, Playwright, async_playwright


class BrowserBase:
    """Async context manager that owns a Browserbase session + Playwright browser.

    Returns (bb_session, page) on entry; caller handles all navigation and logic.
    """

    def __init__(
        self, bb: AsyncBrowserbase, project_id: str, semaphore: asyncio.Semaphore
    ) -> None:
        self._bb = bb
        self._project_id = project_id
        self._semaphore = semaphore
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._bb_session: Session | None = None

    async def __aenter__(self) -> tuple[Session, Page]:
        await self._semaphore.acquire()
        try:
            self._bb_session = await self._bb.sessions.create(
                project_id=self._project_id,
                browser_settings=BrowserSettings(solve_captchas=True),
                proxies=True,
                api_timeout=21600,
            )
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(
                self._bb_session.connect_url
            )
            context = self._browser.contexts[0]
            page = context.pages[0]
            return self._bb_session, page
        except:
            await self._take_down()
            raise

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
        self._browser = None
        self._pw = None
        self._bb_session = None
        self._semaphore.release()


class BrowserBaseFactory:
    max_sessions: int = 20

    def __init__(self, api_key: str, project_id: str) -> None:
        self._api_key = api_key
        self._project_id = project_id
        self._bb = AsyncBrowserbase(api_key=self._api_key)
        self._semaphore = asyncio.Semaphore(self.max_sessions)

    def new_browser_base(self) -> BrowserBase:
        return BrowserBase(self._bb, self._project_id, self._semaphore)
