"""Browsers: attach to the sandbox's browsers over CDP (up to 4); launch our own only when developing locally."""

from __future__ import annotations

import json
import os

from playwright.async_api import Browser, BrowserContext, Page, async_playwright


class BrowserPool:
    def __init__(self):
        self.pw = None
        self.browsers: list[Browser] = []
        self.contexts: list[BrowserContext] = []
        self.local = False
        self._fresh: list[bool] = []

    async def start(self, want: int = 4) -> "BrowserPool":
        self.pw = await async_playwright().start()
        urls = json.loads(os.environ.get("CDP_URLS") or "[]") or ([os.environ["CDP_URL"]] if os.environ.get("CDP_URL") else [])
        if urls:
            for u in urls[:4]:
                b = await self.pw.chromium.connect_over_cdp(u)
                self.browsers.append(b)
                ctx = b.contexts[0] if b.contexts else await b.new_context()
                self.contexts.append(ctx)
                self._fresh.append(True)
        else:
            self.local = True
            headless = os.environ.get("JANTEIROS_HEADFUL") != "1"
            for _ in range(want):
                b = await self.pw.chromium.launch(headless=headless)
                self.browsers.append(b)
                ctx = await b.new_context(viewport={"width": 1280, "height": 800}, locale="de-DE", timezone_id="Europe/Berlin")
                self.contexts.append(ctx)
                self._fresh.append(True)
        return self

    async def page(self, index: int) -> Page:
        """The browser's existing first tab the first time, a new tab afterwards."""
        i = index % len(self.contexts)
        ctx = self.contexts[i]
        if self._fresh[i]:
            self._fresh[i] = False
            if ctx.pages:
                return ctx.pages[0]
        return await ctx.new_page()

    async def close(self) -> None:
        try:
            if self.local:
                for b in self.browsers:
                    await b.close()
            if self.pw:
                await self.pw.stop()
        except Exception:  # noqa: BLE001 - shutting down must never fail the run
            pass
