"""Evaluation, maintenance and tip popups are closed by the page-side watcher the moment they appear."""
import asyncio
import unittest

from playwright.async_api import async_playwright

from types import SimpleNamespace

from tools.navigation import AUTO_DISMISS_INIT_SCRIPT, TAKE_AUTO_DISMISSED_SCRIPT, BrowserNavigator

PAGE = """<!doctype html><html><body><div style="position:fixed;top:0;z-index:5" aria-label="Downloads">
<span>Archivverzeichnis.html</span><a href="/files/archiv.html">Öffnen</a>
<button aria-label="Downloadleiste schließen" onclick="this.parentElement.remove()">×</button></div>
<main>Dokumente</main><script>
function popup(id, text, button, role) {
  const d = document.createElement('div'); d.id = id; d.setAttribute('role', role || 'dialog');
  d.innerHTML = '<p>' + text + '</p><button onclick="this.parentElement.remove()">' + button + '</button>';
  document.body.appendChild(d);
}
setTimeout(() => popup('evaluation', 'Wie zufrieden sind Sie mit dem Portal? Bitte bewerten Sie uns.', 'Später'), 50);
setTimeout(() => popup('maintenance', 'Wartungsarbeiten am Samstag 22:00 bis 02:00 Uhr.', 'Verstanden', 'alertdialog'), 100);
setTimeout(() => popup('tip', 'Tipp: Mit der Suche finden Sie Dokumente schneller.', '×'), 150);
setTimeout(() => popup('viewer', 'Dokumentenanzeige Druckansicht und Bildschirmansicht können unterschiedliche Spaltenbreiten verwenden. Die ausgewählte Revision bleibt erhalten. <button>Akzeptieren und fortfahren</button>', '×'), 120);
setTimeout(() => popup('cookie', 'Cookie-Einstellungen: nur notwendige Cookies?', 'Schließen'), 150);
setTimeout(() => popup('business', 'Revision R-6156 ersetzt R-4034.', 'Schließen'), 150);
</script></body></html>"""


class PopupWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_popups_closed_on_appearance_but_consent_and_business_dialogs_kept(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route("https://portal.test/**", lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=PAGE))
                await context.add_init_script(AUTO_DISMISS_INIT_SCRIPT)
                page = await context.new_page()
                await page.goto("https://portal.test/documents")
                await asyncio.sleep(.6)  # no observation or action in between
                present = {id_: await page.locator(f"#{id_}").count()
                           for id_ in ("evaluation", "maintenance", "tip", "viewer", "cookie", "business")}
                self.assertEqual(present, {"evaluation": 0, "maintenance": 0, "tip": 0, "viewer": 0, "cookie": 1, "business": 1})
                self.assertEqual(await page.get_by_label("Downloads").count(), 0)
                navigator = BrowserNavigator(page, SimpleNamespace(url="https://portal.test/"))
                observation = await navigator.observe()
                self.assertEqual(len(observation["auto_dismissed"]), 5)
                self.assertEqual(observation["download_bar_links"], ["https://portal.test/files/archiv.html"])
                self.assertIn("https://portal.test/files/archiv.html", navigator.links)
                self.assertIsNone((await navigator.observe())["auto_dismissed"])
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
