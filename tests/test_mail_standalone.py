import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.access import AccessManager
from core.contracts import AgentResult, AgentStatus, CommercialPolicies, CommercialRule
from tools.run_mail import SelectedMailAccess, investigate, load_task, main, parser


class StandaloneMailTests(unittest.TestCase):
    def test_restart_workflow_is_explicit_opt_in(self):
        self.assertFalse(parser().parse_args(['--reference','RFQ-1']).restart_between_topics)
        self.assertTrue(parser().parse_args(['--reference','RFQ-1','--restart-between-topics']).restart_between_topics)

    def test_reference_requires_no_other_upstream_records(self):
        task = load_task(parser().parse_args(['--reference', ' CURRENT-RFQ ']))
        self.assertEqual(task.tender.reference, 'CURRENT-RFQ')
        self.assertIsNone(task.erp)
        self.assertIsNone(task.technical)
        self.assertEqual(task.action, 'investigate')

    def test_optional_upstream_records_and_question(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            path = Path(folder, 'task.json')
            path.write_text(json.dumps({'tender': {'reference': 'CURRENT-RFQ',
                'positions': [{'position_id': '10', 'packaging_key': 'carton-A'}]}}), encoding='utf-8')
            task = load_task(parser().parse_args(['--task-file', str(path), '--question', 'Check carton-A']))
        self.assertEqual(task.tender.positions[0].packaging_key, 'carton-A')
        self.assertEqual(task.question, 'Check carton-A')

    def test_empty_reference_and_submission_are_rejected(self):
        with self.assertRaises(ValueError):
            load_task(parser().parse_args(['--reference', ' ']))
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            path = Path(folder, 'task.json')
            path.write_text('{"action":"submit","tender":{"reference":"CURRENT-RFQ"}}', encoding='utf-8')
            with self.assertRaises(ValueError):
                load_task(parser().parse_args(['--task-file', str(path)]))

    def test_explicit_mail_selection_does_not_assign_other_roles(self):
        access = AccessManager({'access': [{'siteId': str(i), 'url': f'https://site-{i}.invalid',
            'credentials': {'username': 'user', 'password': 'secret'}} for i in range(4)]}, env={})
        selected = SelectedMailAccess(access, '2')
        self.assertEqual(selected.for_role('mail').site_id, '2')
        self.assertIsNone(access.assignments)
        with self.assertRaises(ValueError):
            selected.for_role('tender')
        with self.assertRaises(ValueError):
            SelectedMailAccess(access, 'missing')

    def test_executes_only_supplied_mail_worker(self):
        task = load_task(parser().parse_args(['--reference', 'CURRENT-RFQ']))
        result = AgentResult(agent_id='mail', status=AgentStatus.partial)
        worker = SimpleNamespace(execute=AsyncMock(return_value=result), latest=None)
        context = object()
        actual = asyncio.run(investigate(task, context, worker=worker))
        self.assertEqual(actual, result)
        worker.execute.assert_awaited_once_with(task, context)

    def test_timeout_preserves_collected_rules(self):
        policies = CommercialPolicies(rules=[CommercialRule(key='discount', kind='discount', text='Observed rule')])
        latest = AgentResult(agent_id='mail', status=AgentStatus.partial, output=policies)

        async def expire(*args):
            raise TimeoutError()

        worker = SimpleNamespace(execute=expire, latest=latest)
        task = load_task(parser().parse_args(['--reference', 'CURRENT-RFQ']))
        result = asyncio.run(investigate(task, object(), worker=worker))
        self.assertEqual(result.status, AgentStatus.partial)
        self.assertEqual(result.output.rules, policies.rules)
        self.assertTrue(result.missing)

    def test_main_opens_one_browser_and_saves_redacted_isolated_result(self):
        runtime = {'brief': 'Current scenario', 'access': [
            {'siteId': str(i), 'url': f'https://site-{i}.invalid',
             'credentials': {'username': 'private-user', 'password': 'private-password'}}
            for i in range(4)]}
        browser = SimpleNamespace(close=AsyncMock())
        page = object()
        result = AgentResult(agent_id='mail', status=AgentStatus.partial,
            output=CommercialPolicies(rules=[CommercialRule(key='test', kind='test', text='private-password')]))
        worker = SimpleNamespace(execute=AsyncMock(return_value=result), latest=None)
        playwright_scope = SimpleNamespace(__aenter__=None)

        class Scope:
            async def __aenter__(self):
                return playwright_scope

            async def __aexit__(self, *args):
                pass

        args = parser().parse_args(['--reference', 'CURRENT-RFQ', '--site-id', '3'])
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            destination = Path(folder, 'mail-run')
            real_path = Path
            def paths(*parts):
                if parts[:2] == ('artifacts', 'mail-runs'):
                    return destination
                return real_path(*parts)
            with patch('tools.run_mail.contract.load_run', return_value=(runtime, None)), \
                 patch('tools.run_mail.browsers.cdp_urls', return_value=[]), \
                 patch('tools.run_mail.browsers.connect', new=AsyncMock(return_value=(browser, page))) as connect, \
                 patch('tools.run_mail.async_playwright', return_value=Scope()), \
                 patch('tools.run_mail.MailAgent', return_value=worker), \
                 patch('tools.run_mail.LLMClient') as model, \
                 patch('tools.run_mail.Path', side_effect=paths), patch('builtins.print'):
                model.return_value.close = AsyncMock()
                code = asyncio.run(main(args))
            self.assertEqual(code, 1)
            connect.assert_awaited_once_with(playwright_scope, 0)
            browser.close.assert_awaited_once()
            model.return_value.close.assert_awaited_once()
            context = worker.execute.await_args.args[1]
            self.assertIs(context.page, page)
            self.assertFalse(context.config.enable_submission)
            self.assertIsNone(context.get_task)
            self.assertEqual(context.access_manager.for_role('mail').site_id, '3')
            saved = destination.joinpath('result.json').read_text(encoding='utf-8')
            self.assertNotIn('private-password', saved)
            self.assertIn('[REDACTED]', saved)


if __name__ == '__main__':
    unittest.main()
