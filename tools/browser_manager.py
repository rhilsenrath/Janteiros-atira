"""Own all four sessions. Never close sandbox-managed Chromium."""
import asyncio
import os
import json
from urllib.parse import urlsplit
import browsers

WEBSITE_AGENTS = ("tender", "supplier", "erp", "mail")

class BrowserManager:
    @staticmethod
    def validate_environment(env=None):
        env = os.environ if env is None else env
        if env.get("CDP_URLS"):
            try:
                urls = json.loads(env["CDP_URLS"])
                if not isinstance(urls, list) or not all(isinstance(url, str) and url for url in urls):
                    raise ValueError()
            except (TypeError, ValueError):
                raise RuntimeError("CDP_URLS must be a JSON array of endpoint strings") from None
        else:
            urls = browsers.cdp_urls(env)
        if env.get("CHALLENGE_RUN_TOKEN") and len(urls) != 4:
            raise RuntimeError("Exactly four managed CDP endpoints required for a run")
        if urls and (len(urls) != 4 or len(set(urls)) != 4 or any(
                urlsplit(url).scheme not in ("http", "https", "ws", "wss") or not urlsplit(url).hostname for url in urls)):
            raise RuntimeError("Four distinct valid managed CDP endpoints required")
        return urls

    def __init__(self, playwright=None, first_page=None, connector=None, endpoints=None, sandbox=None):
        self.playwright = playwright
        self.first_page = first_page
        self.connector = connector or browsers.connect
        self.endpoints = self.validate_environment() if endpoints is None else endpoints
        self.sandbox = bool(os.environ.get("CHALLENGE_RUN_TOKEN")) if sandbox is None else sandbox
        self.pages = {}
        self.owned = []
        self.lock = asyncio.Lock()

    async def initialize(self):
        async with self.lock:
            if self.pages:
                return
            if self.sandbox and len(self.endpoints) < 4:
                raise RuntimeError("Four managed CDP endpoints required; refusing standalone sandbox browsers")
            if self.endpoints and (len(self.endpoints) < 4 or len(set(self.endpoints[:4])) != 4):
                raise RuntimeError("Four distinct CDP endpoints required")
            try:
                for index, owner in enumerate(WEBSITE_AGENTS):
                    if index == 0 and self.first_page is not None:
                        self.pages[owner] = self.first_page
                    else:
                        browser, page = await self.connector(self.playwright, index)
                        self.owned.append(browser)
                        self.pages[owner] = page
                if len({id(p) for p in self.pages.values()}) != 4:
                    raise RuntimeError("Connector reused a page between agents")
            except BaseException:
                await self.close()
                raise

    def page_for(self, owner):
        return self.pages[owner]

    async def close(self):
        # Playwright scope disconnects CDP; sandbox owns remote browser shutdown.
        if not self.endpoints:
            await asyncio.gather(*(browser.close() for browser in self.owned), return_exceptions=True)
        self.owned.clear()
        self.pages.clear()
