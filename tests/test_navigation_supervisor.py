"""Navigation fault simulations only; no production business defaults."""
import asyncio
import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from agents.base import AgentContext
from agents.mail import MailAgent
from core.config import RunConfig
from core.contracts import AgentTask, AgentResult, AgentStatus, Evidence, CommercialPolicies, MissingInformation, TenderData
from tools.navigation import BrowserAction
from tools.navigation_supervisor import (NavigationSupervisor, SupervisedNavigator, NavigationBlocked,
    NavigationDeadline, AuthenticationFailure, ActionRejected, MissingRecord, normalized_state, session_expired)
from tools.logging import RunLogger
from test_architecture import manager, RUNTIME


def page(text='Search screen', label='Search', **changes):
    result = {'url': 'https://site-3.invalid/search', 'http_status': 200, 'downloads': [], 'tabs': [],
        'frames': [{'text': text, 'controls': [{'target': 'f0:e0', 'tag': 'button', 'type': 'button',
            'label': label, 'visible': True, 'disabled': False, 'row_key': 'row-1'}]}]}
    result.update(changes)
    return result


def login(error=''):
    return page('Login ' + error, frames=[{'text': 'Login ' + error, 'controls': [
        {'target': 'f0:e0', 'tag': 'input', 'type': 'text', 'name': 'username', 'label': 'Username', 'filled': False},
        {'target': 'f0:e1', 'tag': 'input', 'type': 'password', 'label': 'Password', 'filled': False},
        {'target': 'f0:e2', 'tag': 'button', 'type': 'submit', 'label': 'Sign in'}]}])


class Browser:
    def __init__(self, initial, outcome=None):
        self.state = copy.deepcopy(initial)
        self.outcome = outcome
        self.calls = []
        self.page = SimpleNamespace(url=initial['url'], set_default_timeout=lambda _: None)
        self.targets = {}
    async def initialize(self):
        pass
    async def observe(self, offset=0):
        self.page.url = self.state['url']
        return copy.deepcopy(self.state)
    async def act(self, action, **kwargs):
        self.calls.append(action)
        if self.outcome:
            return await self.outcome(self, action)


class Supervision(unittest.IsolatedAsyncioTestCase):
    def make(self, initial=None, outcome=None, **kwargs):
        self.logs = []
        config = RunConfig(navigation_no_progress_limit=6, **kwargs)
        supervisor = NavigationSupervisor('mail', config, RunLogger(self.logs.append))
        supervisor.start({}, 'Read selected records')
        supervisor.checkpoint.stage = 'investigation'
        raw = Browser(initial or page(), outcome)
        return supervisor, SupervisedNavigator(raw, supervisor), raw

    async def test_click_on_unchanged_page_is_not_success_and_repeated_control_is_vetoed(self):
        supervisor, nav, raw = self.make()
        await nav.observe()
        click = BrowserAction(operation='click', target='f0:e0')
        for _ in range(2):
            await nav.act(click)
            self.assertEqual(supervisor.last_outcome, 'no_effect')
        with self.assertRaises(ActionRejected):
            await nav.act(click)
        self.assertEqual(len(raw.calls), 2)
        self.assertIn('alternative_route', ''.join(self.logs))
        self.assertTrue(supervisor.context()['avoid_actions'])

    async def test_same_url_business_changes_and_checked_state_are_progress(self):
        async def change(raw, action):
            raw.state['frames'][0]['text'] = 'Selected record detail'
            raw.state['frames'][0]['controls'][0]['checked'] = True
        supervisor, nav, raw = self.make(outcome=change)
        await nav.observe()
        await nav.act(BrowserAction(operation='click', target='f0:e0'))
        self.assertEqual(supervisor.last_outcome, 'progress')
        self.assertEqual(supervisor.stagnant, 0)
        self.assertEqual(supervisor.checkpoint.state, normalized_state(raw.state))

    async def test_known_page_cycle_does_not_reset_progress(self):
        async def cycle(raw, action):
            raw.state['frames'][0]['text'] = 'B' if raw.state['frames'][0]['text'] == 'A' else 'A'
        supervisor, nav, raw = self.make(page('A'), cycle)
        await nav.observe()
        for i in range(5):
            action = BrowserAction(operation='goto', url=f'https://site-3.invalid/view/{i}')
            await nav.act(action)
        self.assertGreaterEqual(supervisor.stagnant, 4)
        self.assertTrue(supervisor.recovery_required)
        self.assertEqual(supervisor.recoveries, 1)

    async def test_checkpoint_return_is_verified_and_retains_evidence(self):
        supervisor, nav, raw = self.make()
        original = await nav.observe()
        e = Evidence(id='mail:' + 'a' * 32, agent_id='mail', source_site='test', excerpt='Observed policy')
        supervisor.evidence(AgentResult(agent_id='mail', status=AgentStatus.partial,
            output=CommercialPolicies(), evidence=[e], missing=[MissingInformation(agent_id='mail', question='Read another page')]))
        raw.state = page('Unexpected screen', url='https://site-3.invalid/unexpected')
        await nav.observe()
        supervisor.stagnant, supervisor.recovery_required = 4, True
        async def restore(raw, action):
            raw.state = original
        raw.outcome = restore
        await nav.act(BrowserAction(operation='goto', url=supervisor.checkpoint.url))
        self.assertEqual(supervisor.last_outcome, 'checkpoint_recovered')
        self.assertEqual(supervisor.stagnant, 0)
        self.assertEqual(supervisor.checkpoint.evidence, [e.id])
        self.assertEqual(supervisor.context()['remaining_information'], ['Read another page'])

    async def test_expired_session_does_not_replace_business_checkpoint(self):
        async def expire(raw, action):
            raw.state = login()
        supervisor, nav, raw = self.make(outcome=expire)
        original = await nav.observe()
        await nav.act(BrowserAction(operation='click', target='f0:e0'))
        self.assertEqual(supervisor.last_outcome, 'session_expired')
        self.assertEqual(supervisor.checkpoint.state, normalized_state(original))
        for _ in range(2):
            supervisor.session_recovery()
        with self.assertRaises(NavigationBlocked):
            supervisor.session_recovery()

    async def test_rejected_credentials_are_authentication_failure(self):
        async def reject(raw, action):
            raw.state = login('Incorrect password')
        supervisor, nav, raw = self.make(login(), reject)
        await nav.observe()
        with self.assertRaises(AuthenticationFailure):
            await nav.act(BrowserAction(operation='click', target='f0:e2'), authenticating=True)
        self.assertEqual(len(raw.calls), 1)

    async def test_disabled_misleading_control_is_rejected_before_act(self):
        initial = page()
        initial['frames'][0]['controls'][0]['disabled'] = True
        supervisor, nav, raw = self.make(initial)
        await nav.observe()
        with self.assertRaises(ActionRejected):
            await nav.act(BrowserAction(operation='click', target='f0:e0'))
        self.assertFalse(raw.calls)
        self.assertEqual(supervisor.last_outcome, 'blocked_navigation')

    async def test_misleading_fill_cannot_succeed_by_changing_unrelated_text(self):
        initial = page()
        initial['frames'][0]['controls'][0].update(tag='input', type='text', value='', filled=False)
        async def misleading(raw, action):
            raw.state['frames'][0]['text'] = 'Unrelated notice appeared'
            raw.state['frames'][0]['controls'][0].update(value='wrong value', filled=True)
        supervisor, nav, raw = self.make(initial, misleading)
        await nav.observe()
        await nav.act(BrowserAction(operation='fill', target='f0:e0', value='requested value'))
        self.assertEqual(supervisor.last_outcome, 'blocked_navigation')
        self.assertEqual(supervisor.stagnant, 1)

    async def test_checkpoint_resume_is_bounded_even_when_login_succeeded(self):
        supervisor, nav, raw = self.make(checkpoint_recovery_steps=1)
        await nav.observe()
        supervisor.session_recovery()
        raw.state = page('Mailbox home after login', url='https://site-3.invalid/home')
        await nav.observe()
        with self.assertRaises(NavigationBlocked):
            await nav.act(BrowserAction(operation='goto', url='https://site-3.invalid/home'))
        self.assertTrue(supervisor.resume_required)

    async def test_cached_document_does_not_manufacture_progress(self):
        async def document(raw, action):
            return {'name': 'selected.txt', 'text': 'Observed selected document'}
        supervisor, nav, raw = self.make(outcome=document)
        await nav.observe()
        action = BrowserAction(operation='read_document', url='https://site-3.invalid/document')
        await nav.act(action)
        self.assertEqual(supervisor.last_outcome, 'document_observed')
        await nav.act(action)
        self.assertEqual(supervisor.last_outcome, 'no_effect')

    async def test_timeout_observes_again_and_is_classified(self):
        async def hang(raw, action):
            await asyncio.Event().wait()
        supervisor, nav, raw = self.make(outcome=hang, action_timeout_ms=100)
        await nav.observe()
        with self.assertRaises(TimeoutError):
            await nav.act(BrowserAction(operation='click', target='f0:e0'))
        self.assertIsNotNone(supervisor.last_observation)
        self.assertEqual(supervisor.last_outcome, 'timeout')

    async def test_deadline_and_global_action_budget_are_enforced(self):
        supervisor, nav, raw = self.make(max_supervised_actions=10)
        await nav.observe()
        supervisor.actions = 10
        with self.assertRaises(NavigationBlocked):
            await nav.act(BrowserAction(operation='click', target='f0:e0'))
        supervisor.actions = 0
        supervisor.deadline = time.monotonic() - 1
        with self.assertRaises(NavigationDeadline):
            await nav.observe()
        self.assertFalse(raw.calls)

    async def test_new_evidence_resets_stagnation_but_renewed_citation_does_not(self):
        supervisor, nav, raw = self.make()
        e = Evidence(id='mail:' + 'a' * 32, agent_id='mail', source_site='test', excerpt='Observed fact')
        result = AgentResult(agent_id='mail', status=AgentStatus.partial, evidence=[e])
        supervisor.stagnant = 3
        self.assertTrue(supervisor.evidence(result))
        self.assertEqual(supervisor.stagnant, 0)
        supervisor.stagnant = 3
        self.assertFalse(supervisor.evidence(result.model_copy(update={'evidence': [e.model_copy(update={'id': 'mail:' + 'b' * 32})]})))
        self.assertEqual(supervisor.stagnant, 3)

    async def test_logs_exclude_page_text_query_values_urls_and_secrets(self):
        supervisor, nav, raw = self.make(page('Sensitive token secret-user', label='secret-password'))
        raw.state['frames'][0]['controls'][0].update(tag='input', type='text')
        await nav.observe()
        await nav.act(BrowserAction(operation='fill', target='f0:e0', value='private-query'))
        logs = ''.join(self.logs)
        for value in ('Sensitive', 'secret-user', 'secret-password', 'private-query', 'https://'):
            self.assertNotIn(value, logs)

    def test_sorting_clocks_and_session_url_parameters_are_normalized(self):
        a = page('Row A\nRow B\nClock 12:00:01')
        a['frames'][0]['controls'][0]['href'] = 'https://site-3.invalid/record?material=M1&sid=one'
        b = copy.deepcopy(a)
        b['frames'][0]['text'] = 'Row B\nClock 12:00:02\nRow A'
        b['frames'][0]['controls'][0]['target'] = 'f0:e45'
        b['frames'][0]['controls'][0]['href'] = 'https://site-3.invalid/record?material=M1&sid=two'
        self.assertEqual(normalized_state(a), normalized_state(b))
        b['frames'][0]['text'] += '\nNew record'
        self.assertNotEqual(normalized_state(a), normalized_state(b))

    def test_login_detection_requires_login_evidence(self):
        self.assertTrue(session_expired(login()))
        change_password = login()
        change_password['frames'][0]['controls'][-1].update(label='Save', type='button')
        self.assertFalse(session_expired(change_password))
        self.assertEqual(NavigationSupervisor.classify(MissingRecord()), 'missing_records')
        self.assertEqual(NavigationSupervisor.classify(ValueError(), extracting=True), 'extraction_error')


class SharedLoop(unittest.IsolatedAsyncioTestCase):
    async def test_session_reauth_resumes_checkpoint_and_model_receives_context(self):
        business = page('Mailbox overview')
        raw = Browser(business)
        login_attempts = 0
        requests = []
        async def behavior(browser, action):
            if action.secret:
                target = next(c for c in browser.state['frames'][0]['controls'] if c['target'] == action.target)
                target['filled'] = True
            elif action.operation == 'click' and action.target == 'f0:e2':
                nonlocal login_attempts
                login_attempts += 1
                browser.state = business
            elif action.operation == 'click':
                browser.state = login()
        raw.outcome = behavior
        class Model:
            async def ask(self, system, payload, output_type=None):
                data = json.loads(payload)
                from agents.website import AuthenticationStep
                if output_type is AuthenticationStep:
                    return SimpleNamespace(structured=AuthenticationStep(action=BrowserAction(operation='finish'), authenticated=True))
                requests.append(data)
                if len(requests) == 1:
                    action = BrowserAction(operation='click', target='f0:e0')
                else:
                    action = BrowserAction(operation='inspect')
                return SimpleNamespace(structured=action)
        worker = MailAgent()
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=3), page=object(), llm=Model(), access_manager=manager())
        with patch('agents.website.BrowserNavigator', return_value=raw):
            result = await worker.execute(AgentTask(tender=TenderData(reference='test-reference')), context)
        self.assertEqual(login_attempts, 1, (result.error, [(a.operation, a.target) for a in raw.calls]))
        self.assertEqual(worker.supervisor.sessions, 1)
        self.assertIn('navigation_supervisor', requests[-1])
        self.assertEqual(requests[-1]['navigation_supervisor']['last_verified_url'], business['url'])
        self.assertIn(result.status, (AgentStatus.blocked, AgentStatus.partial))


class LiveTargets(unittest.IsolatedAsyncioTestCase):
    async def test_reordered_rows_disabled_and_readonly_controls_are_not_acted_on(self):
        from playwright.async_api import async_playwright
        from tools.navigation import BrowserNavigator
        html = '''<table><tr id="one"><td>ROW-ONE</td><td><button onclick="window.clicked='one'">Details</button></td></tr>
                  <tr id="two"><td>ROW-TWO</td><td><button onclick="window.clicked='two'">Details</button></td></tr></table>
                  <label>Query<input id="query"></label>'''
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page_object = await browser.new_page()
                await page_object.route('https://portal.test/**', lambda route: route.fulfill(content_type='text/html', body=html))
                await page_object.goto('https://portal.test/')
                raw = BrowserNavigator(page_object, SimpleNamespace(url='https://portal.test/'))
                supervisor = NavigationSupervisor('erp', RunConfig(), RunLogger(lambda _: None))
                supervisor.start({}, 'Read selected rows')
                supervisor.checkpoint.stage = 'investigation'
                nav = SupervisedNavigator(raw, supervisor)
                obs = await nav.observe()
                first = next(c for c in obs['frames'][0]['controls'] if c.get('row_key') == 'ROW-ONE')
                await page_object.evaluate("document.querySelector('tbody').prepend(document.getElementById('two'))")
                with self.assertRaises(ValueError):
                    await nav.act(BrowserAction(operation='click', target=first['target']))
                self.assertIsNone(await page_object.evaluate('window.clicked || null'))
                obs = await nav.observe()
                second = next(c for c in obs['frames'][0]['controls'] if c.get('row_key') == 'ROW-TWO')
                await page_object.locator('#two button').evaluate('e => e.disabled = true')
                with self.assertRaises(ValueError):
                    await nav.act(BrowserAction(operation='click', target=second['target']))
                obs = await nav.observe()
                query = next(c for c in obs['frames'][0]['controls'] if c.get('label') == 'Query')
                await page_object.locator('#query').evaluate('e => e.readOnly = true')
                with self.assertRaises(ValueError):
                    await nav.act(BrowserAction(operation='fill', target=query['target'], value='test query'))
                self.assertEqual(await page_object.locator('#query').input_value(), '')
                self.assertEqual(supervisor.last_outcome, 'blocked_navigation')
            finally:
                await browser.close()
