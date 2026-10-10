import unittest
from types import SimpleNamespace
from playwright.async_api import async_playwright
from tools.mail_navigation import MailNavigator
from tools.navigation import BrowserAction


class MailNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_unlock_and_header_details_use_observed_controls(self):
        async with async_playwright() as playwright:
            browser=await playwright.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('''<body>
                    <label>Zum Entsperren ziehen<input type="range" min="0" max="7" value="0"></label>
                    <label>Ich bin kein Roboter<input type="checkbox"></label>
                    <details><summary>Umschlag / vollständige Kopfzeilen</summary><p>Original headers</p></details>
                    <div hidden><iframe srcdoc="<p>Hidden old message</p>"></iframe></div>
                    </body>''')
                navigator=MailNavigator(page,SimpleNamespace(url='https://mail.test'))
                observation=await navigator.observe()
                controls=observation['frames'][0]['controls']
                slider=next(c for c in controls if c['type']=='range')
                self.assertEqual(slider['max'],'7')
                await navigator.act(BrowserAction(operation='fill',target=slider['target'],value='7'))
                self.assertEqual(await page.locator('input[type=range]').input_value(),'7')
                observation=await navigator.observe()
                checkbox=next(c for c in observation['frames'][0]['controls'] if c['type']=='checkbox')
                await navigator.act(BrowserAction(operation='click',target=checkbox['target']))
                observation=await navigator.observe()
                checkbox=next(c for c in observation['frames'][0]['controls'] if c['type']=='checkbox')
                self.assertTrue(checkbox['checked'])
                summary=next(c for c in observation['frames'][0]['controls'] if c['tag']=='summary')
                await navigator.act(BrowserAction(operation='click',target=summary['target']))
                self.assertTrue(await page.locator('details').evaluate('e=>e.open'))
                self.assertEqual(len(observation['frames']),1)
            finally:
                await browser.close()

    async def test_original_can_return_to_observed_search_and_readonly_is_refused(self):
        async with async_playwright() as playwright:
            browser=await playwright.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                async def serve(route):
                    if '/messages/' in route.request.url:
                        html='<label>Adresse<input readonly value="display"></label><p>Original message</p><a href="/tools">Zurück</a>'
                    else:
                        html='<label>Suchbegriff<input></label><a href="/messages/one">Original öffnen</a>'
                    await route.fulfill(content_type='text/html',body=html)
                await page.route('**/*',serve)
                search='https://mail.test/?folder=archive&q=RFQ-1+Technikfreigabe&page=2'
                await page.goto(search)
                navigator=MailNavigator(page,SimpleNamespace(url='https://mail.test'),action_timeout=1000)
                observation=await navigator.observe()
                self.assertEqual(observation['mailbox_return_url'],search)
                await navigator.act(BrowserAction(operation='goto',url='https://mail.test/messages/one'))
                observation=await navigator.observe()
                self.assertNotIn(search,navigator.links)
                self.assertEqual(observation['mailbox_return_url'],search)
                address=next(c for c in observation['frames'][0]['controls'] if c['label']=='Adresse')
                with self.assertRaisesRegex(ValueError,'read-only'):
                    await navigator.act(BrowserAction(operation='fill',target=address['target'],value=search))
                await navigator.act(BrowserAction(operation='goto',url=observation['mailbox_return_url']))
                self.assertEqual(page.url,search)
                with self.assertRaises(ValueError):
                    await navigator.act(BrowserAction(operation='goto',url='https://other.test/'))
                with self.assertRaises(ValueError):
                    await navigator.act(BrowserAction(operation='goto',url='https://mail.test/unobserved'))
            finally:
                await browser.close()
