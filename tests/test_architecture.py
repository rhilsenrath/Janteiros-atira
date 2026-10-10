import asyncio
import json
import os
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.base import BaseAgent, AgentContext, SubmissionContext
from agents.tender import TenderAgent
from agents.supplier import SupplierAgent
from agents.erp import ErpAgent
from agents.mail import MailAgent
from agents.pricing import PricingAgent
from core.access import AccessManager, RoleAssignments
from core.config import RunConfig
from core.contracts import (AgentTask, AgentResult, AgentStatus, Evidence, TenderData, TenderPosition,
    TechnicalRequirements, TechnicalRequirement, ERPData, MaterialCandidate, CommercialPolicies,
    CommercialRule, BidResult, BidPosition, ValidationResult, is_synthetic)
from core.orchestrator import Orchestrator
from core.state import RunState
from services.pricing_engine import round_money, PricingEngine, PricingInput
from services.bid_validator import BidValidator
from services.submission import GuardedSubmission, to_answer
from tools.browser_manager import BrowserManager
from tools.navigation import BrowserAction
from tools.logging import RunLogger, redact
from fakes import FakeAgent, FakeBrowserManager, ScriptedModel

RUNTIME={'brief':'Unit test only','access':[], 'submission':{'kind':'site'},'answer_schema':{}}

def access_entries():
    return [{'siteId':str(i),'url':f'https://site-{i}.invalid','description':str(i),
             'credentials':{'username':f'user-{i}','password':f'password-{i}'}} for i in range(4)]

def manager():
    m=AccessManager({'access':access_entries()},env={})
    m.assignments=RoleAssignments(tender='0',supplier='1',erp='2',mail='3')
    return m

class Contracts(unittest.TestCase):
    def test_workers_share_interface_and_no_production_mock_module(self):
        workers=[TenderAgent(), SupplierAgent(),ErpAgent(),MailAgent(),PricingAgent()]
        self.assertTrue(all(isinstance(w,BaseAgent) and not w.is_mock for w in workers))
        self.assertEqual(len({w.agent_id for w in workers}),5)
        self.assertFalse(Path('agents/mock.py').exists())

    def test_unknown_zero_and_discriminated_roundtrip(self):
        for output in [TenderData(),TechnicalRequirements(),ERPData(),CommercialPolicies(),BidResult()]:
            result=AgentResult(agent_id='test',status=AgentStatus.partial,output=output)
            restored=AgentResult.model_validate_json(result.model_dump_json())
            self.assertIs(type(restored.output),type(output))
        self.assertIsNone(MaterialCandidate(material_id='test').stock)
        self.assertEqual(MaterialCandidate(material_id='test',stock='0').stock,Decimal('0'))
        self.assertNotIn('sensitive-token',CommercialPolicies(signing_token='sensitive-token').model_dump_json())

    def test_decimal_half_up_and_incomplete_pricing(self):
        self.assertEqual(round_money(Decimal('1.005')),Decimal('1.01'))
        self.assertEqual(round_money(Decimal('-1.005')),Decimal('-1.01'))
        with self.assertRaises(ValueError): round_money(1.005)
        bid=PricingEngine().calculate(PricingInput(tender=TenderData(),technical=TechnicalRequirements(),
            erp=ERPData(),commercial=CommercialPolicies()))
        self.assertFalse(bid.complete); self.assertIsNone(bid.total_net)
        self.assertFalse(BidValidator().validate(bid,{}).valid)

    def test_manifest_preserves_lifecycle(self):
        manifest=json.loads(Path('agent.json').read_text())
        self.assertEqual(manifest['run'],['uv','run','--no-sync','python','agent.py'])
        self.assertEqual(manifest['setup'],['uv','sync']); self.assertEqual(manifest['browsers'],4)

    def test_mock_mode_not_available_from_environment(self):
        for mode in ('mock','test','invalid'):
            with patch.dict(os.environ,{'ATIRA_MODE':mode}):
                with self.assertRaises(ValueError): RunConfig.from_env()

    def test_action_schema_rejects_unsafe_operations(self):
        for payload in [{'operation':'evaluate','value':'alert(1)'}, {'operation':'click'},
                        {'operation':'press','target':'f0:e0','value':'Control+A'},
                        {'operation':'fill','target':'f0:e0','secret':'password','value':'unsafe'}]:
            with self.assertRaises(ValueError): BrowserAction.model_validate(payload)
        self.assertEqual(BrowserAction(operation='fill',target='f0:e0',secret='password').secret,'password')

    def test_log_redaction_and_allowlisted_metadata(self):
        lines=[]; logger=RunLogger(lines.append)
        logger.emit('browser_error',agent='erp',error_type='TimeoutError')
        self.assertEqual(json.loads(lines[0])['agent'],'erp')
        self.assertEqual(redact('user-x password-x',['user-x','password-x']),'[REDACTED] [REDACTED]')
        with self.assertRaises(TypeError): logger.emit('unsafe',password='secret')

class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_role_assignment_and_hidden_credentials(self):
        m=AccessManager({'access':access_entries()},env={})
        model=ScriptedModel([{'tender':'3','supplier':'2','erp':'1','mail':'0'}])
        await m.resolve(model,'Roles described at runtime')
        self.assertEqual(m.for_role('tender').site_id,'3')
        self.assertNotIn('password-',model.requests[0][1]); self.assertNotIn('user-',model.requests[0][1])
        self.assertNotIn('password-',repr(m.for_role('mail')))

    def test_runtime_env_fallback_and_invalid_formats(self):
        self.assertEqual(len(AccessManager({},env={'CHALLENGE_SITE_ACCESS':json.dumps(access_entries())}).sites),4)
        for raw in ('not-json','{}','[]'):
            with self.assertRaises(ValueError): AccessManager({},env={'CHALLENGE_SITE_ACCESS':raw})
        entries=access_entries(); entries[0]['url']='https://u:p@site.invalid'
        with self.assertRaisesRegex(ValueError,'redacted'): AccessManager({'access':entries},env={})

    async def test_role_assignments_must_be_unique_and_runtime_known(self):
        m=manager(); model=ScriptedModel([{'tender':'0','supplier':'0','erp':'2','mail':'3'}])
        with self.assertRaises(ValueError): await m.resolve(model,'test')

class Workflow(unittest.IsolatedAsyncioTestCase):
    async def test_injected_test_workers_concurrent_and_dependent(self):
        started=set(); barrier=asyncio.Event(); inputs={}
        class Worker(FakeAgent):
            async def execute(self,task,context):
                inputs[self.agent_id]=task
                if self.agent_id in ('tender','supplier','mail'):
                    started.add(self.agent_id)
                    if len(started)==3: barrier.set()
                    await asyncio.wait_for(barrier.wait(),1)
                return await super().execute(task,context)
        workers={k:Worker(k) for k in ('tender','supplier','erp','mail','pricing')}
        o=Orchestrator(RUNTIME,FakeBrowserManager(),RunConfig(mode='test'),agents=workers,logger=RunLogger(lambda _:None))
        self.assertIsNone(await o.run()); self.assertIsNotNone(inputs['erp'].technical)
        self.assertIsNotNone(inputs['pricing'].commercial)
        self.assertFalse(o.state.validation.valid)

    async def test_production_rejects_synthetic_workers(self):
        with self.assertRaises(ValueError):
            Orchestrator(RUNTIME,FakeBrowserManager(),agents={k:FakeAgent(k) for k in ('tender','supplier','erp','mail','pricing')})

    async def test_partial_runtime_evidence_unblocks_dependency_before_worker_finishes(self):
        observed=asyncio.Event(); supplier_finish=asyncio.Event()
        class Worker(BaseAgent):
            responsibility='test';
            def __init__(self,role):self.agent_id=role
            async def execute(self,task,context):
                e=Evidence(id=self.agent_id,agent_id=self.agent_id,source_site='local-test')
                if self.agent_id=='tender': output=TenderData(reference='test',evidence=[e])
                elif self.agent_id=='supplier':
                    output=TechnicalRequirements(requirements=[TechnicalRequirement(requirement_id='test')],evidence=[e])
                    await context.report(AgentResult(agent_id='supplier',status=AgentStatus.partial,output=output))
                    await asyncio.wait_for(supplier_finish.wait(),1)
                elif self.agent_id=='erp':
                    task=await context.get_task(task)
                    self.assertion=bool(task.technical.requirements)
                    observed.set(); supplier_finish.set(); output=ERPData(evidence=[e])
                else:
                    task=await context.get_task(task); output=CommercialPolicies(evidence=[e])
                return AgentResult(agent_id=self.agent_id,status=AgentStatus.partial,output=output)
        workers={k:Worker(k) for k in ('tender','supplier','erp','mail')}; workers['pricing']=PricingAgent()
        o=Orchestrator(RUNTIME,FakeBrowserManager(),RunConfig(max_reinvestigations=0),agents=workers,
            access_manager=manager(),logger=RunLogger(lambda _:None))
        await o.run(); self.assertTrue(observed.is_set()); self.assertTrue(workers['erp'].assertion)
        self.assertFalse(any(is_synthetic(r) for r in o.state.results))

    async def test_deadline_cancellation_and_retry_policy(self):
        class Slow(FakeAgent):
            async def execute(self,task,context): await asyncio.Event().wait()
        workers={k:Slow(k) for k in ('tender','supplier','erp','mail','pricing')}
        browser=FakeBrowserManager(); browser.close=AsyncMock()
        o=Orchestrator(RUNTIME,browser,RunConfig(mode='test',task_timeout=.01,retries=0),agents=workers,
            logger=RunLogger(lambda _:None))
        await o.run(); self.assertEqual(o.state.statuses['tender'],AgentStatus.failed)
        browser.close.assert_awaited_once()
        o=Orchestrator({**RUNTIME,'expires_at':1},browser,RunConfig(mode='test'),agents=workers)
        await o.run(); self.assertTrue(all(s==AgentStatus.blocked for s in o.state.statuses.values()))

    async def test_real_pricing_requests_missing_and_conflicting_evidence(self):
        result=await PricingAgent().execute(AgentTask(tender=TenderData(conflicts=['test ambiguity'])),AgentContext(RUNTIME,RunConfig()))
        self.assertEqual(result.status,AgentStatus.blocked); self.assertTrue(result.missing)
        result=await PricingAgent().execute(AgentTask(tender=TenderData(evidence=[
            Evidence(id='test',agent_id='tender',source_site='fixture',mock=True)])),AgentContext(RUNTIME,RunConfig()))
        self.assertIn('Synthetic',result.error)

class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_sessions_and_remote_cleanup_ownership(self):
        browsers=[SimpleNamespace(close=AsyncMock()) for _ in range(4)]; pages=[object() for _ in range(4)]
        connector=AsyncMock(side_effect=list(zip(browsers,pages)))
        m=BrowserManager(connector=connector,endpoints=['a','b','c','d'],sandbox=True)
        await m.initialize();await m.initialize();self.assertEqual(connector.await_count,4)
        self.assertEqual([m.page_for(k) for k in ('tender','supplier','erp','mail')],pages)
        await m.close();self.assertTrue(all(b.close.await_count==0 for b in browsers))

    async def test_missing_duplicate_endpoints_never_launch_standalone(self):
        for endpoints in ([],['a'],['a','a','c','d']):
            connector=AsyncMock();m=BrowserManager(connector=connector,endpoints=endpoints,sandbox=True)
            with self.assertRaises(RuntimeError):await m.initialize()
            connector.assert_not_called()

    async def test_connection_failure_closes_owned_local_browser(self):
        browser=SimpleNamespace(close=AsyncMock())
        m=BrowserManager(connector=AsyncMock(side_effect=[(browser,object()),RuntimeError()]),endpoints=[],sandbox=False)
        with self.assertRaises(RuntimeError):await m.initialize()
        browser.close.assert_awaited_once()

class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_requires_explicit_enable_and_synthetic_always_refused(self):
        service=GuardedSubmission(); context=SubmissionContext(RUNTIME,RunConfig(),None,None)
        with self.assertRaisesRegex(ValueError,'enabled'):
            await service.prepare(BidResult(complete=True),ValidationResult(valid=True),context,TenderAgent())
        context=SubmissionContext(RUNTIME,RunConfig(enable_submission=True),None,None)
        with self.assertRaises(ValueError):await service.prepare(BidResult(mock=True),ValidationResult(valid=True),context,TenderAgent())

    async def test_original_solve_signature_delegates_real_coordinator(self):
        import agent
        with patch.dict(os.environ,{'ATIRA_MODE':'production'}),patch('agent.BrowserManager',return_value=FakeBrowserManager()) as browser,patch('agent.Orchestrator') as coordinator:
            instance=coordinator.return_value;instance.run=AsyncMock(return_value=None);instance.state.statuses={}
            page=object();self.assertIsNone(await agent.solve(RUNTIME,page))
            self.assertIs(browser.call_args.kwargs['first_page'],page)
            instance.run.assert_awaited_once()

class ProgressAndEnvironmentTests(unittest.TestCase):
    def test_partial_state_merges_fields_and_deduplicates_evidence(self):
        evidence=Evidence(id='unit-source',agent_id='tender',source_site='fixture')
        state=RunState({},RunConfig())
        state.collect(AgentResult(agent_id='tender',status=AgentStatus.partial,
            output=TenderData(reference='test',positions=[TenderPosition(position_id='one',quantity='2')],evidence=[evidence]),evidence=[evidence]))
        state.collect(AgentResult(agent_id='tender',status=AgentStatus.partial,
            output=TenderData(positions=[TenderPosition(position_id='one',unit='Stück')],evidence=[evidence]),evidence=[evidence]))
        self.assertEqual(state.tender.reference,'test')
        self.assertEqual(state.tender.positions[0].quantity,Decimal('2'))
        self.assertEqual(state.tender.positions[0].unit,'Stück');self.assertEqual(len(state.evidence),1)

    def test_managed_endpoint_schema_rejects_objects_and_missing_sessions(self):
        for env in ({'CDP_URLS':'{"a":"x"}'}, {'CHALLENGE_RUN_TOKEN':'test'},
                    {'CDP_URLS':'["invalid"]'}, {'CDP_URLS':'[]','CHALLENGE_RUN_TOKEN':'test'}):
            with self.assertRaises(RuntimeError):BrowserManager.validate_environment(env)
        self.assertEqual(BrowserManager.validate_environment({}),[])
