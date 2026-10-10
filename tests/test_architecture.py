import asyncio
import json
import os
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.base import BaseAgent
from agents.tender import TenderAgent
from agents.supplier import SupplierAgent
from agents.erp import ErpAgent
from agents.mail import MailAgent
from agents.pricing import PricingAgent
from agents.mock import MockAgent
from core.config import RunConfig
from core.contracts import *
from core.orchestrator import Orchestrator, SubmissionContext
from tools.browser_manager import BrowserManager, MockBrowserManager
from tools.llm_client import LLMClient
from services.pricing_engine import round_money, PricingEngine, PricingInput
from services.bid_validator import BidValidator
from services.submission import GuardedSubmission, to_answer

RUNTIME = {"brief": "Synthetic unit test", "access": [], "submission": {"kind": "site"}, "answer_schema": {}}

class Contracts(unittest.TestCase):
    def test_all_workers_implement_interface(self):
        agents = [TenderAgent(), SupplierAgent(), ErpAgent(), MailAgent(), PricingAgent()]
        self.assertEqual(len({a.agent_id for a in agents}), 5)
        self.assertTrue(all(isinstance(a, BaseAgent) for a in agents))

    def test_unknown_zero_and_provenance(self):
        self.assertIsNone(MaterialCandidate(material_id="synthetic").stock)
        record = MaterialCandidate(material_id="synthetic", stock=Decimal("0"), evidence=[
            Evidence(id="test", agent_id="erp", source_site="synthetic", revision="test")])
        self.assertEqual(MaterialCandidate.model_validate_json(record.model_dump_json()), record)
        with self.assertRaises(ValueError):
            AgentResult(agent_id="erp", status="invalid")
        self.assertNotIn("secret", CommercialPolicies(signing_token="secret").model_dump_json())

    def test_decimal_half_up(self):
        self.assertEqual(round_money(Decimal("1.005")), Decimal("1.01"))
        self.assertEqual(round_money(Decimal("-1.005")), Decimal("-1.01"))
        for invalid in [1.005, Decimal("NaN")]:
            with self.assertRaises(ValueError): round_money(invalid)

    def test_unimplemented_pricing_is_incomplete(self):
        bid = PricingEngine().calculate(PricingInput(tender=TenderData(), technical=TechnicalRequirements(),
            erp=ERPData(), commercial=CommercialPolicies()))
        self.assertFalse(bid.complete)
        self.assertIsNone(bid.total_net)
        self.assertFalse(BidValidator().validate(bid, {}).valid)
        with self.assertRaises(ValueError): to_answer(bid)

    def test_answer_mapping_preserves_zero_and_omits_unknown_breakdowns(self):
        bid = BidResult(complete=True, decision="bid", tender_reference="synthetic",
            discount_percent=Decimal("0"), total_net=Decimal("2"), positions=[
            BidPosition(position="test", material_number="synthetic", quantity=Decimal("1"),
                unit_price=Decimal("2"), lead_time_weeks=0, surcharge_per_unit=Decimal("0"))])
        answer = to_answer(bid)
        self.assertEqual(answer['positions'][0]['surcharge_per_unit'], 0)
        self.assertNotIn('base_unit_price', answer['positions'][0])
        self.assertEqual(answer['positions'][0]['material_number'], 'synthetic')

    def test_manifest_and_dependencies(self):
        manifest = json.loads(Path('agent.json').read_text())
        self.assertEqual(manifest['browsers'], 4)
        self.assertEqual(manifest['run'], ['uv','run','--no-sync','python','agent.py'])
        self.assertTrue(Path(manifest['run'][-1]).is_file())
        self.assertEqual(manifest['setup'], ['uv','sync'])

class Workflow(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_stages_and_dependency_snapshots(self):
        started = set()
        barrier = asyncio.Event()
        captured = {}
        class Worker(MockAgent):
            async def execute(self, task, context):
                captured[self.agent_id] = task
                if self.agent_id in ('tender','supplier','mail') and task.action == 'investigate':
                    started.add(self.agent_id)
                    if len(started) == 3: barrier.set()
                    await asyncio.wait_for(barrier.wait(), 1)
                return await super().execute(task, context)
        workers = {k: Worker(k) for k in ('tender','supplier','mail','erp','pricing')}
        o = Orchestrator(RUNTIME, MockBrowserManager(), RunConfig(mode='mock'), agents=workers)
        self.assertIsNone(await o.run())
        self.assertIsNotNone(captured['erp'].technical)
        self.assertIsNotNone(captured['pricing'].erp)
        self.assertEqual(len(o.state.results), 5)
        self.assertTrue(o.state.bid.mock)
        self.assertFalse(o.state.validation.valid)
        for result in o.state.results:
            restored = AgentResult.model_validate_json(result.model_dump_json())
            self.assertIs(type(restored.output), type(result.output))

    async def test_partial_failure_and_retries(self):
        class Partial(MockAgent):
            async def execute(self, task, context):
                return AgentResult(agent_id=self.agent_id, status=AgentStatus.partial,
                    output=TechnicalRequirements(), mock=True)
        class Failure(MockAgent):
            calls = 0
            async def execute(self, task, context):
                self.calls += 1
                return AgentResult(agent_id=self.agent_id, status=AgentStatus.failed,
                    error="Synthetic failure", retryable=True)
        workers = {k: MockAgent(k) for k in ('tender','supplier','mail','erp','pricing')}
        workers['supplier'] = Partial('supplier')
        workers['mail'] = Failure('mail')
        o = Orchestrator(RUNTIME, MockBrowserManager(), RunConfig(mode='mock', retries=1), agents=workers)
        await o.run()
        self.assertEqual(o.state.statuses['supplier'], AgentStatus.partial)
        self.assertEqual(o.state.statuses['mail'], AgentStatus.failed)
        self.assertEqual(workers['mail'].calls, 2)
        self.assertIsNone(o.state.bid)
        self.assertTrue(o.state.errors)

    async def test_production_is_blocked_and_rejects_mock_agents(self):
        o = Orchestrator(RUNTIME, MockBrowserManager())
        self.assertIsNone(await o.run())
        self.assertEqual(o.state.statuses['tender'], AgentStatus.blocked)
        with self.assertRaises(ValueError):
            Orchestrator(RUNTIME, MockBrowserManager(), agents={k: MockAgent(k) for k in o.agents})

    async def test_cancellation_and_timeout_cleanup(self):
        class Slow(MockAgent):
            async def execute(self, task, context): await asyncio.Event().wait()
        manager = MockBrowserManager()
        manager.close = AsyncMock()
        workers = {k: Slow(k) for k in ('tender','supplier','mail','erp','pricing')}
        o = Orchestrator(RUNTIME, manager, RunConfig(mode='mock', task_timeout=.01, retries=0), agents=workers)
        await o.run()
        self.assertEqual(o.state.statuses['tender'], AgentStatus.failed)
        manager.close.assert_awaited_once()
        manager.close.reset_mock()
        o = Orchestrator(RUNTIME, manager, RunConfig(mode='mock'), agents=workers)
        running = asyncio.create_task(o.run())
        await asyncio.sleep(.01)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError): await running
        manager.close.assert_awaited_once()
        self.assertEqual(o.state.statuses['tender'], AgentStatus.cancelled)

    async def test_expired_run_never_executes(self):
        o = Orchestrator({**RUNTIME, 'expires_at': 1}, MockBrowserManager(), RunConfig(mode='mock'))
        await o.run()
        self.assertTrue(all(s == AgentStatus.blocked for s in o.state.statuses.values()))

    async def test_solve_contract_without_live_resources(self):
        import agent
        with patch.dict(os.environ, {'ATIRA_MODE': 'mock'}), patch('run.submit_answer') as submit:
            self.assertIsNone(await agent.solve(RUNTIME, object()))
            submit.assert_not_called()

    async def test_mock_submission_is_simulated(self):
        worker = MockAgent('tender')
        worker.execute = AsyncMock(wraps=worker.execute)
        context = SubmissionContext(RUNTIME, RunConfig(mode='mock'), None, None)
        with patch('run.submit_answer') as post:
            self.assertIsNone(await GuardedSubmission().prepare(BidResult(mock=True),
                ValidationResult(valid=False), context, worker))
            worker.execute.assert_awaited_once()
            post.assert_not_called()
        with self.assertRaises(ValueError):
            await GuardedSubmission().prepare(BidResult(mock=True), ValidationResult(valid=True),
                SubmissionContext(RUNTIME, RunConfig(), None, None), TenderAgent())

class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_isolated_sessions_and_no_remote_close(self):
        remote = [SimpleNamespace(close=AsyncMock()) for _ in range(4)]
        pages = [object() for _ in range(4)]
        connector = AsyncMock(side_effect=list(zip(remote, pages)))
        manager = BrowserManager(connector=connector, endpoints=['a','b','c','d'], sandbox=True)
        await manager.initialize()
        self.assertEqual([manager.page_for(k) for k in ('tender','supplier','erp','mail')], pages)
        await manager.initialize()
        self.assertEqual(connector.await_count, 4)
        await manager.close()
        self.assertTrue(all(b.close.await_count == 0 for b in remote))

    async def test_missing_or_duplicate_endpoints_refuse_launch(self):
        for endpoints in [[], ['a'], ['a','a','c','d']]:
            connector = AsyncMock()
            manager = BrowserManager(connector=connector, endpoints=endpoints, sandbox=True)
            with self.assertRaises(RuntimeError): await manager.initialize()
            connector.assert_not_called()

    async def test_local_partial_connection_is_cleaned_up(self):
        browser = SimpleNamespace(close=AsyncMock())
        manager = BrowserManager(connector=AsyncMock(side_effect=[(browser, object()), RuntimeError()]),
            endpoints=[], sandbox=False)
        with self.assertRaises(RuntimeError): await manager.initialize()
        browser.close.assert_awaited_once()

class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_reply_and_injected_provider(self):
        reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"reference":"synthetic"}', tool_calls=[]))], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=reply))))
        response = await LLMClient(client, model='injected').ask('system', 'user', output_type=TenderData)
        self.assertEqual(response.structured.reference, 'synthetic')
        self.assertEqual(client.chat.completions.create.call_args.kwargs['model'], 'injected')

class AdditionalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_targeted_reinvestigation_is_bounded_and_routed(self):
        class Asking(MockAgent):
            calls = 0
            async def execute(self, task, context):
                self.calls += 1
                if self.calls == 1:
                    return AgentResult(agent_id='pricing', status=AgentStatus.partial, mock=True,
                        missing=[MissingInformation(agent_id='mail', question='synthetic missing approval')])
                return await super().execute(task, context)
        class Mail(MockAgent):
            actions = []
            async def execute(self, task, context):
                self.actions.append((task.action, task.question))
                return await super().execute(task, context)
        workers = {k: MockAgent(k) for k in ('tender','supplier','mail','erp','pricing')}
        workers['pricing'] = Asking('pricing')
        workers['mail'] = Mail('mail')
        o = Orchestrator(RUNTIME, MockBrowserManager(), RunConfig(mode='mock'), agents=workers)
        await o.run()
        self.assertIn(('reinvestigate', 'synthetic missing approval'), workers['mail'].actions)
        self.assertEqual(workers['pricing'].calls, 2)
        self.assertTrue(o.state.submission_receipt.simulated)
        self.assertFalse(o.state.submission_receipt.confirmed)

    async def test_wrong_worker_output_is_rejected(self):
        class Wrong(MockAgent):
            async def execute(self, task, context):
                return AgentResult(agent_id='mail', status=AgentStatus.complete, output=TenderData(), mock=True)
        workers = {k: MockAgent(k) for k in ('tender','supplier','mail','erp','pricing')}
        workers['mail'] = Wrong('mail')
        o = Orchestrator(RUNTIME, MockBrowserManager(), RunConfig(mode='mock'), agents=workers)
        await o.run()
        self.assertEqual(o.state.statuses['mail'], AgentStatus.failed)
        self.assertIsNone(o.state.commercial)

    async def test_entry_point_mock_never_connects_or_posts(self):
        import agent
        with patch.dict(os.environ, {'ATIRA_MODE':'mock','CHALLENGE_SCOUTING':json.dumps(RUNTIME)}, clear=True), \
             patch('browsers.connect', new_callable=AsyncMock) as connect, patch('run.submit_answer') as post:
            self.assertEqual(await agent.main(), 0)
            connect.assert_not_called()
            post.assert_not_called()

    async def test_model_transient_retries_are_bounded(self):
        from openai import APIConnectionError
        from httpx import Request
        call = AsyncMock(side_effect=APIConnectionError(request=Request('POST','https://synthetic.invalid')))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)))
        with self.assertRaises(APIConnectionError):
            await LLMClient(client, retries=1).ask('system', 'user')
        self.assertEqual(call.await_count, 2)

    async def test_production_solve_delegates_with_original_page(self):
        import agent
        with patch.dict(os.environ, {'ATIRA_MODE':'production'}), \
             patch('agent.BrowserManager', return_value=MockBrowserManager()) as manager, \
             patch('agent.Orchestrator') as coordinator:
            instance = coordinator.return_value
            instance.run = AsyncMock(return_value=None)
            instance.state.statuses = {'tender': AgentStatus.blocked}
            page = object()
            self.assertIsNone(await agent.solve(RUNTIME, page))
            self.assertIs(manager.call_args.kwargs['first_page'], page)
            self.assertIs(coordinator.call_args.args[0], RUNTIME)
            instance.run.assert_awaited_once()

class SubmissionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_entrypoint_rejects_an_accidental_answer(self):
        import agent
        with patch.dict(os.environ, {'ATIRA_MODE':'mock'}, clear=True), \
             patch('run.load_run', return_value=(RUNTIME, 'synthetic-token')), \
             patch('agent.solve', new_callable=AsyncMock, return_value={'synthetic':True}), \
             patch('run.submit_answer') as post:
            with self.assertRaises(RuntimeError): await agent.main()
            post.assert_not_called()

    async def test_nested_synthetic_evidence_cannot_be_mapped(self):
        bid = BidResult(complete=True, decision='bid', tender_reference='synthetic',
            discount_percent=Decimal('0'), total_net=Decimal('0'), positions=[
            BidPosition(position='test', material_number='synthetic', quantity=Decimal('1'),
                unit_price=Decimal('0'), lead_time_weeks=0, evidence=[
                Evidence(id='test', agent_id='erp', source_site='synthetic', mock=True)])])
        with self.assertRaises(ValueError): to_answer(bid)
        self.assertFalse(BidValidator().validate(bid, {}).valid)

    async def test_site_submission_requires_genuine_confirmed_receipt(self):
        class PretendTender(TenderAgent):
            async def execute(self, task, context):
                return AgentResult(agent_id='tender', status=AgentStatus.complete,
                    output=SubmissionReceipt(confirmed=True, simulated=True))
        context = SubmissionContext(RUNTIME, RunConfig(), CommercialPolicies(signing_token='synthetic'), None)
        with self.assertRaises(ValueError):
            await GuardedSubmission().prepare(BidResult(complete=True), ValidationResult(valid=True),
                context, PretendTender())

    async def test_missing_inputs_block_dependent_pricing(self):
        class NoMail(MockAgent):
            async def execute(self, task, context):
                return AgentResult(agent_id='mail', status=AgentStatus.blocked)
        workers = {k: MockAgent(k) for k in ('tender','supplier','mail','erp','pricing')}
        workers['mail'] = NoMail('mail')
        workers['pricing'].execute = AsyncMock()
        o = Orchestrator(RUNTIME, MockBrowserManager(), RunConfig(mode='mock'), agents=workers)
        await o.run()
        self.assertEqual(o.state.statuses['pricing'], AgentStatus.blocked)
        workers['pricing'].execute.assert_not_called()
