"""Synthetic evidence here is confined to test workers, never production defaults."""
import asyncio
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock
from pydantic import SecretStr
from agents.base import BaseAgent, DependencyUnavailable, SubmissionContext
from core.config import RunConfig
from core.contracts import (AgentTask, AgentResult, AgentStatus, Evidence, TenderData, TenderPosition,
    TechnicalRequirements, TechnicalRequirement, SpecificationRevision, PositionConversion, ERPData,
    MaterialCandidate, PriceCondition, CommercialPolicies, CommercialRule, MissingInformation, BidResult,
    ValidationResult, SubmissionReceipt)
from core.dependencies import Dependencies, fingerprint
from core.orchestrator import Orchestrator
from core.state import RunState
from tools.logging import RunLogger
from services.submission import GuardedSubmission
from fakes import FakeBrowserManager

ROLES = ('tender', 'supplier', 'erp', 'mail', 'pricing')
RUNTIME = {'brief': 'Test only', 'submission': {'kind': 'site'}, 'answer_schema': {}}


def evidence(role, reference=None):
    return Evidence(id='test:' + role, agent_id=role, source_site='fixture', reference=reference)


def tender():
    e = evidence('tender')
    return TenderData(reference='RFQ-test', qa_complete=True, evidence=[e], positions=[
        TenderPosition(position_id='1', requirement_id='A-1', specification_reference='SPEC-test',
                       quantity=2, unit='Stück', evidence=[e]),
        TenderPosition(position_id='2', requirement_id='A-2', specification_reference='SPEC-test',
                       quantity=3, unit='Stück', evidence=[e])])


def requirement(identifier='A-1', **updates):
    r = TechnicalRequirement(requirement_id=identifier, specification_id='SPEC-test', revision='R1',
        catalogue_group='GROUP-test', bore=10, stroke=20, pressure=30, mounting='mount-test',
        certification='cert-test', evidence=[evidence('supplier', identifier)])
    return r.model_copy(update=updates)


def technical():
    e = evidence('supplier')
    return TechnicalRequirements(governing_revision='R1', governing_specification='SPEC-test', evidence=[e],
        requirements=[requirement(), requirement('A-2', stroke=None)],
        revisions=[SpecificationRevision(specification_id='SPEC-test', revision='R1', current=True, evidence=[e])],
        conversions=[PositionConversion(position_id=str(i), requirement_id=f'A-{i}', quantity=q,
                                       unit='Stück', pieces=q) for i, q in ((1, 2), (2, 3))])


def output(role):
    return {'tender': tender, 'supplier': technical, 'erp': lambda: ERPData(evidence=[evidence('erp')],
            searched_catalogue_groups=['GROUP-test']),
            'mail': lambda: CommercialPolicies(evidence=[evidence('mail')]),
            'pricing': lambda: BidResult(complete=False)}[role]()


class Worker(BaseAgent):
    responsibility = 'test'
    def __init__(self, role, callback=None):
        self.agent_id = role
        self.callback = callback
        self.calls = []
    async def execute(self, task, context):
        self.calls.append(task)
        if self.callback:
            return await self.callback(task, context)
        if self.agent_id != 'pricing':
            task = await context.get_task(task)
        return AgentResult(agent_id=self.agent_id, status=AgentStatus.partial, output=output(self.agent_id))


def orchestrator(**kwargs):
    agents = kwargs.pop('agents', {r: Worker(r) for r in ROLES})
    return Orchestrator(RUNTIME, FakeBrowserManager(), kwargs.pop('config', RunConfig(mode='test', retries=0)),
                        agents=agents, logger=RunLogger(lambda _: None), **kwargs)


class Readiness(unittest.TestCase):
    def setUp(self):
        self.state = RunState({}, RunConfig(), tender=tender(), technical=technical())
        self.d = Dependencies(self.state)

    def test_per_position_gate_and_final_quantities(self):
        self.assertEqual(self.d.erp_ready_positions(), ['1'])
        self.state.tender = self.state.tender.model_copy(update={'qa_complete': False})
        self.assertFalse(self.d.ready('erp'))
        self.state.tender = tender()
        self.state.technical = self.state.technical.model_copy(update={'conversions': []})
        self.assertFalse(self.d.ready('erp'))

    def test_missing_provenance_conflicts_and_reserve_block(self):
        self.state.technical = technical().model_copy(update={'conflicts': ['conflicting revision']})
        self.assertFalse(self.d.ready('erp'))
        self.state.technical = technical().model_copy(update={'evidence': []})
        self.assertFalse(self.d.ready('erp'))
        self.state.technical = technical().model_copy(update={'requirements': [
            requirement(usable_travel=18, stroke_reserve_per_end=None)]})
        self.assertFalse(self.d.ready('erp'))

    def test_supplier_and_mail_start_on_different_inputs(self):
        self.state.tender = TenderData(reference='RFQ-test', evidence=[evidence('tender')])
        self.assertTrue(self.d.ready('mail'))
        self.assertFalse(self.d.ready('supplier'))
        self.state.tender = tender()
        self.assertTrue(self.d.ready('supplier'))

    def test_erp_snapshot_is_scoped_and_isolated(self):
        o = orchestrator(config=RunConfig())
        o.state.tender, o.state.technical = tender(), technical()
        task = o.task(agent_id='erp')
        self.assertEqual([p.position_id for p in task.tender.positions], ['1'])
        self.assertEqual([r.requirement_id for r in task.technical.requirements], ['A-1'])
        task.tender.positions.clear()
        self.assertEqual(len(o.state.tender.positions), 2)

    def test_mail_new_keys_and_no_duplicate_observations(self):
        o = orchestrator()
        o.state.tender, o.state.technical = tender(), technical()
        first = o.dependencies.input_key('mail', o.task())
        o.state.erp = ERPData(materials=[MaterialCandidate(material_id='M-test', metal_index_code='INDEX-test',
            conditions=[PriceCondition(fx_key='FX-test')])])
        self.assertNotEqual(first, o.dependencies.input_key('mail', o.task()))
        e = evidence('tender')
        later = e.model_copy(update={'id': 'renewed-source', 'observed_at': e.observed_at + timedelta(seconds=1)})
        self.assertEqual(fingerprint(e), fingerprint(later))
        self.assertEqual(fingerprint(TenderData(evidence=[e])), fingerprint(TenderData(evidence=[e, later])))

    def test_complete_claim_with_missing_evidence_is_blocked(self):
        o = orchestrator(config=RunConfig())
        o.collect(AgentResult(agent_id='tender', status=AgentStatus.complete, output=TenderData()))
        self.assertEqual(o.state.statuses['tender'], AgentStatus.blocked)
        o.collect(AgentResult(agent_id='supplier', status=AgentStatus.complete, output=technical(),
            missing=[MissingInformation(agent_id='mail', question='Need release')]))
        self.assertEqual(o.state.statuses['supplier'], AgentStatus.blocked)

    def test_complete_erp_batches_preserve_previous_materials(self):
        o = orchestrator()
        for code in ('M1-test', 'M2-test'):
            o.collect(AgentResult(agent_id='erp', status=AgentStatus.complete,
                output=ERPData(materials=[MaterialCandidate(material_id=code)])))
        self.assertEqual(len(o.state.erp.materials), 2)

    def test_pricing_waits_for_erp_coverage_and_conflicts_block_completion(self):
        self.state.commercial = CommercialPolicies(evidence=[evidence('mail')])
        self.state.erp = ERPData(evidence=[evidence('erp')])
        self.assertFalse(self.d.ready('pricing'))
        self.state.erp = self.state.erp.model_copy(update={'searched_catalogue_groups': ['GROUP-test']})
        self.assertTrue(self.d.ready('pricing'))  # observed group exhausted without a match
        self.state.erp = self.state.erp.model_copy(update={'materials': [MaterialCandidate(
            material_id='M-test', active=None, specification=requirement())]})
        self.assertFalse(self.d.ready('pricing'))
        o = orchestrator(config=RunConfig())
        o.collect(AgentResult(agent_id='tender', status=AgentStatus.complete,
            output=tender().model_copy(update={'conflicts': ['unresolved answer']})))
        self.assertEqual(o.state.statuses['tender'], AgentStatus.blocked)
        self.assertTrue(any(m.agent_id == 'tender' for m in o.state.missing))


class Scheduling(unittest.IsolatedAsyncioTestCase):
    async def test_all_sessions_start_and_partial_result_unblocks_before_finish(self):
        started = set()
        barrier, erp_started = asyncio.Event(), asyncio.Event()
        async def run(role, task, context):
            if task.action == 'investigate':
                started.add(role)
                if len(started) == 4:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), 1)
            task = await context.get_task(task)
            if role == 'supplier' and task.action == 'investigate':
                await context.report(AgentResult(agent_id=role, status=AgentStatus.partial, output=technical()))
                await asyncio.wait_for(erp_started.wait(), 1)
            if role == 'erp':
                self.assertEqual([p.position_id for p in task.tender.positions], ['1'])
                erp_started.set()
            return AgentResult(agent_id=role, status=AgentStatus.partial, output=output(role))
        agents = {r: Worker(r, lambda t, c, role=r: run(role, t, c)) for r in ROLES if r != 'pricing'}
        agents['pricing'] = Worker('pricing')
        o = orchestrator(agents=agents)
        await asyncio.wait_for(o.run(), 3)
        self.assertEqual(started, set(ROLES) - {'pricing'})
        self.assertTrue(erp_started.is_set())
        self.assertTrue(agents['pricing'].calls)

    async def test_pricing_runs_while_supplier_is_still_investigating(self):
        priced = asyncio.Event()
        supplier_finished = False
        async def supplier(task, context):
            nonlocal supplier_finished
            await context.get_task(task)
            await context.report(AgentResult(agent_id='supplier', status=AgentStatus.partial, output=technical()))
            await asyncio.wait_for(priced.wait(), 1)
            supplier_finished = True
            return AgentResult(agent_id='supplier', status=AgentStatus.partial, output=technical())
        async def pricing(task, context):
            self.assertFalse(supplier_finished)
            self.assertEqual(len(task.tender.positions), 2)  # preserve portfolio allocations
            priced.set()
            return AgentResult(agent_id='pricing', status=AgentStatus.partial, output=BidResult())
        agents = {r: Worker(r) for r in ROLES}
        agents['supplier'] = Worker('supplier', supplier)
        agents['pricing'] = Worker('pricing', pricing)
        o = orchestrator(agents=agents)
        await asyncio.wait_for(o.run(), 3)
        self.assertTrue(priced.is_set())

    async def test_new_evidence_during_pricing_triggers_fresh_portfolio(self):
        price_started, updated = asyncio.Event(), asyncio.Event()
        fresh = technical().model_copy(update={'requirements': [requirement(), requirement('A-2')]})
        async def supplier(task, context):
            await context.get_task(task)
            await context.report(AgentResult(agent_id='supplier', status=AgentStatus.partial, output=technical()))
            await asyncio.wait_for(price_started.wait(), 1)
            await context.report(AgentResult(agent_id='supplier', status=AgentStatus.partial, output=fresh))
            updated.set()
            return AgentResult(agent_id='supplier', status=AgentStatus.partial, output=fresh)
        async def pricing(task, context):
            price_started.set()
            await asyncio.wait_for(updated.wait(), 1)
            return AgentResult(agent_id='pricing', status=AgentStatus.partial, output=BidResult())
        agents = {r: Worker(r) for r in ROLES}
        agents['supplier'] = Worker('supplier', supplier)
        agents['pricing'] = Worker('pricing', pricing)
        o = orchestrator(agents=agents)
        await asyncio.wait_for(o.run(), 2)
        self.assertGreaterEqual(len(agents['pricing'].calls), 2)
        self.assertTrue(all(r.stroke == 20 for r in agents['pricing'].calls[-1].technical.requirements))

    async def test_release_resolves_cycle_without_supplier_browser_call(self):
        o = orchestrator(config=RunConfig())
        o.collect(AgentResult(agent_id='tender', status=AgentStatus.partial, output=tender()))
        data = technical().model_copy(update={'requirements': [requirement(stroke=18, stroke_kind='nutzbarer Weg')]})
        o.collect(AgentResult(agent_id='supplier', status=AgentStatus.partial, output=data))
        self.assertIsNone(o.state.technical.requirements[0].stroke)
        self.assertTrue(any(m.agent_id == 'mail' for m in o.state.missing))
        policy = CommercialPolicies(evidence=[evidence('mail')], rules=[CommercialRule(key='RELEASE-test',
            kind='Technikfreigabe', text='R1 Reserve pro Ende 1 mm', tender_reference='RFQ-test',
            evidence=[evidence('mail')])])
        o.collect(AgentResult(agent_id='mail', status=AgentStatus.partial, output=policy))
        self.assertEqual(o.state.technical.requirements[0].stroke, 20)
        self.assertEqual(o.dependencies.erp_ready_positions(), ['1'])
        self.assertFalse(any('stroke reserve' in m.question for m in o.state.missing))
        self.assertFalse(o.agents['supplier'].calls)

    async def test_followups_coalesce_and_are_not_repeated_for_unchanged_inputs(self):
        o = orchestrator()
        o.state.tender = tender()
        o.consumed['mail'] = o.dependencies.input_key('mail', o.task())
        o.invocations['mail'] = 1
        o.state.missing = [MissingInformation(agent_id='mail', position_id='1', question='Read discount'),
                           MissingInformation(agent_id='mail', position_id='2', question='Read FX')]
        # Other workers are considered busy so this test isolates Mail routing.
        blockers = {r: asyncio.create_task(asyncio.Event().wait()) for r in ('tender', 'supplier', 'erp', 'pricing')}
        o.active.update(blockers)
        o.schedule_updates()
        job = o.active['mail']
        await job
        del o.active['mail']
        self.assertIn('1: Read discount', o.agents['mail'].calls[0].question)
        o.state.missing = [MissingInformation(agent_id='mail', position_id='1', question='Read discount')]
        o.schedule_updates()
        self.assertNotIn('mail', o.active)
        await o.stop_workers()

    async def test_new_erp_keys_restart_finished_mail_once(self):
        o = orchestrator()
        o.state.tender, o.state.technical = tender(), technical()
        o.consumed['mail'] = o.dependencies.input_key('mail', o.task())
        o.finished.add('mail')
        o.state.erp = ERPData(materials=[MaterialCandidate(material_id='M-test', metal_index_code='INDEX-test',
            conditions=[PriceCondition(fx_key='FX-test')])])
        o.active.update({r: asyncio.create_task(asyncio.Event().wait())
                         for r in ('tender', 'supplier', 'erp', 'pricing')})
        o.schedule_updates()
        await o.active['mail']
        del o.active['mail']
        o.schedule_updates()
        self.assertEqual(len(o.agents['mail'].calls), 1)
        self.assertNotIn('mail', o.active)
        await o.stop_workers()

    async def test_resolved_release_does_not_restart_supplier(self):
        o = orchestrator(config=RunConfig())
        one = tender().model_copy(update={'positions': [tender().positions[0]]})
        o.collect(AgentResult(agent_id='tender', status=AgentStatus.partial, output=one))
        data = technical().model_copy(update={'requirements': [requirement(stroke=18, stroke_kind='nutzbarer Weg')]})
        o.collect(AgentResult(agent_id='supplier', status=AgentStatus.partial, output=data))
        o.collect(AgentResult(agent_id='mail', status=AgentStatus.partial, output=CommercialPolicies(
            evidence=[evidence('mail')], rules=[CommercialRule(key='release', kind='Technikfreigabe',
                text='R1 Reserve pro Ende 1 mm', evidence=[evidence('mail')])])))
        o.active.update({r: asyncio.create_task(asyncio.Event().wait()) for r in ('tender', 'erp', 'mail', 'pricing')})
        o.schedule_updates()
        self.assertNotIn('supplier', o.active)
        self.assertEqual(o.state.statuses['supplier'], AgentStatus.complete)
        await o.stop_workers()

    async def test_running_worker_receives_new_followup_in_snapshot(self):
        o = orchestrator()
        o.state.tender = tender()
        task = await o.refresh_task('mail', AgentTask())
        o.state.missing = [MissingInformation(agent_id='mail', position_id='1', question='Read approval')]
        task = await o.refresh_task('mail', task)
        self.assertEqual(task.question, '1: Read approval')
        again = await o.refresh_task('mail', task)
        self.assertEqual(again.question, task.question)

    async def test_failed_supplier_leaves_erp_blocked_and_does_not_loop(self):
        async def fail(task, context):
            await context.get_task(task)
            return AgentResult(agent_id='supplier', status=AgentStatus.failed, error='test failure')
        agents = {r: Worker(r) for r in ROLES}
        agents['supplier'] = Worker('supplier', fail)
        o = orchestrator(agents=agents, config=RunConfig(mode='test', retries=0, max_reinvestigations=0))
        await asyncio.wait_for(o.run(), 1)
        self.assertEqual(o.state.statuses['supplier'], AgentStatus.failed)
        self.assertEqual(o.state.statuses['erp'], AgentStatus.blocked)
        self.assertEqual(len(agents['supplier'].calls), 1)

    async def test_finished_upstream_leaves_dependency_blocked_without_loop(self):
        o = orchestrator()
        o.finished.add('tender')
        with self.assertRaises(DependencyUnavailable):
            await o.refresh_task('supplier', AgentTask())
        self.assertEqual(o.state.statuses['supplier'], AgentStatus.blocked)

    async def test_deadline_preserves_submission_reserve_and_closes_jobs(self):
        async def slow(task, context):
            await asyncio.Event().wait()
        agents = {r: Worker(r, slow) for r in ROLES}
        o = orchestrator(agents=agents, config=RunConfig(mode='test', task_timeout=10, deadline_reserve=.1))
        o.runtime = {**RUNTIME, 'expires_at': (time.time() + .2) * 1000}
        o.browsers.close = AsyncMock()
        await asyncio.wait_for(o.run(), 1)
        self.assertFalse(o.active)
        o.browsers.close.assert_awaited_once()
        self.assertTrue(all(s == AgentStatus.blocked for s in o.state.statuses.values()))

    async def test_signing_token_requested_late_and_retains_policies(self):
        actions = []
        async def mail(task, context):
            actions.append(task.action)
            return AgentResult(agent_id='mail', status=AgentStatus.complete,
                output=CommercialPolicies(signing_token=SecretStr('test-only-token')))
        agents = {r: Worker(r) for r in ROLES}
        agents['mail'] = Worker('mail', mail)
        o = orchestrator(agents=agents)
        o.state.commercial = CommercialPolicies(rules=[CommercialRule(key='test', kind='test', text='test')])
        before = await o.execute_signing_token()
        self.assertEqual(before.status, AgentStatus.blocked)
        self.assertFalse(actions)
        o.state.bid = BidResult(complete=True)
        o.state.validation = ValidationResult(valid=True)
        result = await o.execute_signing_token()
        self.assertEqual(actions, ['retrieve_signing_token'])
        self.assertEqual(result.status, AgentStatus.complete)
        self.assertEqual(len(o.state.commercial.rules), 1)

    async def test_submission_receives_tender_bid_and_token_from_coordinator(self):
        seen = []
        async def submit(task, context):
            seen.append(task)
            return AgentResult(agent_id='tender', status=AgentStatus.complete,
                               output=SubmissionReceipt(confirmed=True))
        agents = {r: Worker(r) for r in ROLES}
        agents['tender'] = Worker('tender', submit)
        o = orchestrator(agents=agents, config=RunConfig(enable_submission=True))
        o.state.tender = tender()
        o.state.bid = BidResult(complete=True, evidence=[evidence('pricing')])
        o.state.commercial = CommercialPolicies(signing_token=SecretStr('test-only-token'))
        context = SubmissionContext(o.runtime, o.config, o.state.commercial, o.context('tender'))
        service = GuardedSubmission()
        await service.prepare(o.state.bid, ValidationResult(valid=True), context, agents['tender'])
        self.assertEqual(seen[0].action, 'submit')
        self.assertEqual(seen[0].tender.reference, 'RFQ-test')
        self.assertEqual(seen[0].bid, o.state.bid)
        self.assertTrue(seen[0].commercial.signing_token)
        self.assertTrue(service.receipt.confirmed)
