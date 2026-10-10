"""A link seen on an earlier page (e.g. a tab only shown on the overview) stays reachable after leaving that page."""
import unittest
import unittest.mock
from types import SimpleNamespace

from playwright.async_api import async_playwright

from tools.navigation import BrowserAction, BrowserNavigator

PAGES = {
    "/tender": '<a href="/tender?tab=documents">Unterlagen</a> <a href="/tender/submission">Angebotsabgabe</a>',
    "/tender?tab=documents": '<a href="/tender/doc/1">Leistungsbeschreibung</a>',
    "/tender/submission": "Angebotskonfigurator",
    "/tender/doc/1": "Text",
}


class SeenLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_from_earlier_page_is_listed_and_navigable(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()

                async def serve(route):
                    url = route.request.url.removeprefix("https://portal.test")
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                        body=f"<!doctype html><body>{PAGES.get(url, 'missing')}</body>")
                await context.route("https://portal.test/**", serve)
                page = await context.new_page()
                await page.goto("https://portal.test/tender")
                navigator = BrowserNavigator(page, SimpleNamespace(url="https://portal.test/"))
                await navigator.observe()
                await navigator.act(BrowserAction(operation="goto", url="https://portal.test/tender?tab=documents"))
                observation = await navigator.observe()
                self.assertNotIn("https://portal.test/tender/submission", navigator.links)
                self.assertIn({"label": "Angebotsabgabe", "url": "https://portal.test/tender/submission"},
                              navigator.unopened_links("/tender"))
                await navigator.act(BrowserAction(operation="goto", url="https://portal.test/tender/submission"))
                self.assertIn("Angebotskonfigurator", await page.inner_text("body"))
                with self.assertRaises(ValueError):  # never observed: still refused
                    await navigator.act(BrowserAction(operation="goto", url="https://portal.test/tender/secret"))
                self.assertTrue(observation["frames"])
            finally:
                await browser.close()


class TransientErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_without_page_is_retried_after_a_click(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                failures = {"left": 2}

                async def serve(route):
                    url = route.request.url
                    if url.endswith("/submission") and failures["left"]:
                        failures["left"] -= 1
                        await route.fulfill(status=503, body="")  # Chrome: ERR_HTTP_RESPONSE_CODE_FAILURE
                        return
                    body = "Angebotskonfigurator" if url.endswith("/submission") else \
                        '<a href="/tender/submission">Angebotsabgabe</a>'
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                        body=f"<!doctype html><body>{body}</body>")
                await context.route("https://portal.test/**", serve)
                page = await context.new_page()
                await page.goto("https://portal.test/tender")
                navigator = BrowserNavigator(page, SimpleNamespace(url="https://portal.test/"))
                await navigator.observe()
                target = next(t for t, (_, _, c) in navigator.targets.items() if c.get("label") == "Angebotsabgabe")
                with unittest.mock.patch("tools.navigation.RETRY_DELAYS", (0, 0, 0, None)):
                    await navigator.act(BrowserAction(operation="click", target=target))
                self.assertEqual(page.url, "https://portal.test/tender/submission")
                self.assertIn("Angebotskonfigurator", await page.inner_text("body"))
                self.assertEqual(failures["left"], 0)
            finally:
                await browser.close()


class RevealRowTests(unittest.IsolatedAsyncioTestCase):
    async def test_revealed_text_is_labelled_by_row_key_not_a_cut_row(self):
        long = ("Originalwortlaut Bohrung 80 mm, Hub 200 mm, Betriebsdruck 210 bar, Befestigung Schwenkauge, "
                "Zertifizierung EN 1493. Kataloggruppe GR-8923: in der ERP-Suche eingeben")
        html = (f"<table><tr><td>A-1</td><td>{long}</td><td><button onclick=\"this.nextElementSibling.hidden=false\">"
                "Prüfvermerk</button><span hidden>EN 1493</span></td></tr></table>")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)
                navigator = BrowserNavigator(page, SimpleNamespace(url="https://portal.test/"))
                observation = await navigator.observe()
                control = observation["frames"][0]["controls"][0]
                self.assertTrue(control["row"].endswith("…"))
                result = await navigator.reveal(control["target"])
                self.assertTrue(result["text"].startswith("A-1 => "))
                self.assertIn("EN 1493", result["text"])
                self.assertNotIn("GR-8 ", result["text"] + " ")  # never a value cut mid-way
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
