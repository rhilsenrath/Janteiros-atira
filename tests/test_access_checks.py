"""Unlock actions use observed controls and emit value-free diagnostics."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from playwright.async_api import async_playwright
from agents.website import WebsiteAgent, quick_access_action
from tools.logging import RunLogger
from tools.navigation import BrowserAction, BrowserNavigator


class AccessDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_failure_are_logged_without_control_data(self):
        worker = object.__new__(WebsiteAgent)
        worker.agent_id = 'mail'
        worker.navigator = SimpleNamespace(act=AsyncMock())
        logs = []
        context = SimpleNamespace(logger=RunLogger(sink=logs.append))
        observation = {'frames': [{'controls': [{'target': 'f0:e0', 'type': 'checkbox',
                       'checked': False, 'label': 'Ich bin kein Roboter'}]}]}
        action = quick_access_action(observation)
        await worker.perform_access_action(context, action, observation, authenticating=True)
        worker.navigator.act.side_effect = ValueError('private detail')
        with self.assertRaises(ValueError):
            await worker.perform_access_action(context, action, observation)
        self.assertEqual([json.loads(line)['event'] for line in logs],
                         ['access_check_started', 'access_check_no_progress',
                          'access_check_started', 'access_check_failed'])
        self.assertNotIn('Roboter', ''.join(logs))
        self.assertNotIn('private detail', ''.join(logs))


class AccessBrowser(unittest.IsolatedAsyncioTestCase):
    async def test_slider_end_and_checkbox_click(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route('https://portal.test/**', lambda route: route.fulfill(
                    content_type='text/html', body='''<label>Zum Entsperren
                    <input type="range" min="0" max="75" value="0"></label>
                    <label>Ich bin kein Roboter<input type="checkbox"></label>'''))
                await page.goto('https://portal.test/')
                nav = BrowserNavigator(page, SimpleNamespace(url='https://portal.test/'))
                observation = await nav.observe()
                action = quick_access_action(observation)
                with self.assertRaises(ValueError):
                    await nav.act(action.model_copy(update={'value': '74'}))
                await nav.act(action)
                self.assertEqual(await page.locator('input[type=range]').input_value(), '75')
                observation = await nav.observe()
                await nav.act(quick_access_action(observation))
                self.assertTrue(await page.locator('input[type=checkbox]').is_checked())
                self.assertIsNone(quick_access_action(await nav.observe()))
            finally:
                await browser.close()
