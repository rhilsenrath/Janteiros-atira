import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agents.tender import TenderAgent
from agents.website import ReferenceExtraction, SourceCitation, observed_quote, explicit_dates
from agents.base import AgentContext
from agents.base import BaseAgent
from core.config import RunConfig
from core.contracts import MissingInformation, TenderData, TenderPosition, Evidence, AgentResult, AgentStatus, AgentTask, TechnicalRequirements
from core.orchestrator import Orchestrator
from core.state import merge_progress
from tools.check_models import CheckReply
from tools.access_flow import login_actions, access_action, unresolved_access, business_access_ready
from tools.model_context import business_schema, compact_facts, model_context
from tools.llm_client import LLMClient
from tools.navigation import BrowserNavigator, BrowserAction
from tools.logging import RunLogger
from test_architecture import manager, RUNTIME


def observation(controls):
    return {'frames':[{'text':'Live business application', 'controls':controls}]}


class AccessRegressionTests(unittest.TestCase):
    def test_conflicts_clear_only_when_the_new_record_explicitly_resolves_them(self):
        previous=TechnicalRequirements(conflicts=['Revision not established'])
        self.assertEqual(merge_progress(previous,TechnicalRequirements()).conflicts,previous.conflicts)
        self.assertEqual(merge_progress(previous,TechnicalRequirements(conflicts=[])).conflicts,[])
        evidence=Evidence(id='observed',agent_id='supplier',source_site='portal',excerpt='Current revision')
        attached=TenderAgent().attach(TechnicalRequirements(),[evidence])
        self.assertNotIn('conflicts',attached.model_fields_set)

    def test_selected_document_read_gate_uses_observed_receipt_without_choosing_revision(self):
        controls=[{'target':'f0:e0','tag':'input','type':'radio','label':'Verbindliche Unterlagen zur Kenntnis genommen','form':0,'checked':False},
                  {'target':'f0:e1','tag':'input','label':'Belegnummer übertragen','form':0,'filled':False},
                  {'target':'f0:e2','tag':'input','type':'checkbox','label':'Überlassungsbedingungen gelesen','form':0,'checked':False},
                  {'target':'f0:e3','tag':'button','type':'submit','label':'Leseberechtigung anfordern','form':0}]
        view=observation(controls)
        view['url']='https://fixture.example/documents/model-selected'
        view['frames'][0]['text']='Dokumentenüberlassung. Leseberechtigung. Belegnummer: UNIT-43'
        self.assertEqual(access_action(view).target,'f0:e0')
        controls[0]['checked']=True
        self.assertEqual(access_action(view).value,'UNIT-43')
        controls[1]['filled']=True
        self.assertEqual(access_action(view).target,'f0:e2')
        controls[2]['checked']=True
        self.assertEqual(access_action(view).target,'f0:e3')
        controls[1]['label']=''
        controls[1]['name']='receipt'
        controls[1]['filled']=False
        self.assertEqual(access_action(view).value,'UNIT-43')
        view['url']='https://fixture.example/bid'
        self.assertIsNone(access_action(view))

    def test_mistyped_source_id_is_corrected_only_by_a_unique_selected_quote(self):
        worker=TenderAgent()
        worker.sources={'selected':{'source_id':'selected','text':'Quantity 12','site_id':'portal',
            'url':'https://fixture.example','document':None,'observed_at':'2027-01-25T09:00:00Z'},
            'unselected':{'source_id':'unselected','text':'Quantity 99','site_id':'portal',
            'url':'https://fixture.example','document':None,'observed_at':'2027-01-25T09:00:00Z'}}
        result=worker.evidence([SourceCitation(source_id='typo',excerpt='Quantity 12')],['selected'])
        self.assertEqual(result[0].id,'selected')
        with self.assertRaises(ValueError):
            worker.evidence([SourceCitation(source_id='typo',excerpt='Quantity 99')],['selected'])
        worker.sources['duplicate']={**worker.sources['selected'],'source_id':'duplicate'}
        with self.assertRaises(ValueError):
            worker.evidence([SourceCitation(source_id='typo',excerpt='Quantity 12')],['selected','duplicate'])

    def test_explicit_date_formats_are_parsed_without_selecting_a_revision(self):
        values=explicit_dates('Old 01/11 (MM/TT) 2027; New 17/01 (TT/MM) 2027; unknown 02/03; invalid 31/02 (TT/MM) 2027')
        self.assertEqual([item['iso_date'] for item in values],['2027-01-11','2027-01-17'])
        self.assertTrue(all('current' not in item for item in values))

    def test_citation_keeps_original_span_without_rejecting_invisible_formatting(self):
        source='Position V\u200bG-4711\n\nQuantity 12'
        quote=observed_quote(source,'VG-4711 Quantity 12')
        self.assertIn(quote,source)
        self.assertEqual(quote,'V\u200bG-4711\n\nQuantity 12')
        with self.assertRaises(ValueError):
            observed_quote(source,'VG-4711 Quantity 13')

    def test_toolbar_and_logout_do_not_confuse_login_form(self):
        fields = [
            {'target':'f0:e0','tag':'input','label':'Adresse','readonly':True,'filled':True},
            {'target':'f0:e1','tag':'button','label':'Abmelden','type':'submit','form':0},
            {'target':'f0:e2','tag':'input','label':'Benutzername','type':'text','form':1},
            {'target':'f0:e3','tag':'input','label':'Passwort','type':'password','form':1},
            {'target':'f0:e4','tag':'button','label':'Anmelden','type':'submit','form':1}]
        actions = login_actions(observation(fields))
        self.assertEqual([a.target for a in actions], ['f0:e2','f0:e3','f0:e4'])
        self.assertEqual([a.secret for a in actions], ['username','password',None])

    def test_slider_and_checkbox_are_separate_access_steps(self):
        slider = {'target':'f0:e0','type':'range','label':'Zum Entsperren ziehen','range_value':'0','max':'200'}
        checkbox = {'target':'f0:e1','type':'checkbox','label':'Ich bin kein Roboter','checked':False}
        view = observation([slider,checkbox])
        self.assertEqual(access_action(view).value, '200')
        slider['range_value'] = '200'
        self.assertEqual(access_action(view).target, 'f0:e1')
        self.assertTrue(unresolved_access(view))
        checkbox['checked'] = True
        self.assertIsNone(access_action(view))
        self.assertFalse(unresolved_access(view))

    def test_access_receipt_form_is_not_a_completed_login(self):
        self.assertTrue(unresolved_access(observation([
            {'target':'f0:e0','tag':'input','label':'Empfangsnummer','filled':True}])))

    def test_business_sliders_and_binding_agreements_are_not_automatic_access_actions(self):
        self.assertIsNone(access_action(observation([
            {'target':'f0:e0','type':'range','label':'Preis','range_value':'0','max':'100'},
            {'target':'f0:e1','tag':'button','label':'Accept Terms of Service'}])))

    def test_logged_in_business_tools_are_verified_without_a_model_guess(self):
        view = observation([{'target':'f0:e0','tag':'button','label':'Abmelden'},
                            {'target':'f0:e1','tag':'button','label':'Dokumente'}])
        self.assertTrue(business_access_ready(view,'supplier'))
        view['frames'][0]['controls'].append({'target':'f0:e2','tag':'input','type':'password'})
        self.assertFalse(business_access_ready(view,'supplier'))

    def test_readonly_receipt_flow_uses_only_the_observed_code_and_acknowledgement(self):
        view = observation([
            {'target':'f0:e0','tag':'input','label':'Empfangsnummer','form':0,'filled':False},
            {'target':'f0:e1','tag':'input','type':'checkbox','label':'Betriebsbedingungen gelesen','form':0,'checked':False},
            {'target':'f0:e2','tag':'button','type':'submit','label':'Weiter','form':0}])
        view['frames'][0]['text'] = 'Nur Anzeigezugriff. Empfangsnummer: UNIT-4711'
        self.assertEqual(access_action(view).value,'UNIT-4711')
        view['frames'][0]['controls'][0]['filled'] = True
        self.assertEqual(access_action(view).target,'f0:e1')
        view['frames'][0]['controls'][1]['checked'] = True
        self.assertEqual(access_action(view).target,'f0:e2')

    def test_missing_alias_is_routable_and_unknown_worker_is_rejected(self):
        self.assertEqual(MissingInformation(agent_id='tender-investigator',question='Read positions').agent_id,'tender')
        with self.assertRaises(ValueError):
            MissingInformation(agent_id='invented-agent',question='Read positions')

    def test_navigation_context_preserves_facts_without_repeated_quotations(self):
        e = Evidence(id='source',agent_id='tender',source_site='test',excerpt='A long quotation')
        record = TenderData(reference='RFQ',evidence=[e],positions=[TenderPosition(position_id='1',quantity=2,evidence=[e])])
        facts = compact_facts(record.model_dump(mode='json'))
        self.assertEqual(facts['positions'][0]['quantity'],'2')
        self.assertEqual(facts['source_ids'],['source'])
        self.assertNotIn('A long quotation',json.dumps(facts))
        schema = business_schema(TenderData.model_json_schema())
        self.assertNotIn('Evidence',schema['$defs'])
        self.assertNotIn('evidence',schema['properties'])
        self.assertNotIn('evidence',schema['$defs']['TenderPosition']['properties'])


class AsyncRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_citation_correction_is_bounded_and_still_requires_observed_text(self):
        for repaired in (True,False):
            worker=TenderAgent()
            source_id='tender:'+'a'*32
            worker.sources[source_id]={'source_id':source_id,'url':'https://fixture.example/',
                'document':None,'text':'Reference UNIT-RFQ','observed_at':'2026-10-10T00:00:00Z','site_id':'0'}
            calls=[]
            async def answer(context,system,payload,output_type,**kwargs):
                calls.append(payload)
                quote='Reference UNIT-RFQ' if repaired and len(calls)==2 else 'Invented reference'
                return output_type.model_validate({'output':{'reference':'UNIT-RFQ'},'status':'partial',
                    'citations':[{'source_id':source_id,'excerpt':quote}]})
            worker.ask=answer
            context=AgentContext(RUNTIME,RunConfig(),access_manager=manager(),logger=RunLogger(lambda _:None))
            if repaired:
                result=await worker.extract(context,AgentTask(),[source_id],final=False)
                self.assertEqual(result.evidence[0].excerpt,'Reference UNIT-RFQ')
            else:
                with self.assertRaises(ValueError):
                    await worker.extract(context,AgentTask(),[source_id],final=False)
                self.assertIsNone(worker.latest)
            self.assertEqual(len(calls),2)

    async def test_reference_publication_does_not_wait_for_positions_or_generate_full_records(self):
        worker=TenderAgent()
        worker.navigator=SimpleNamespace(page=SimpleNamespace(url=manager().for_role('tender').url))
        source=worker.remember({'text':'Listing RFQ-4711', 'url':worker.navigator.page.url},manager().for_role('tender'))
        model=SimpleNamespace(ask=AsyncMock(return_value=SimpleNamespace(structured=ReferenceExtraction(
            reference='RFQ-4711',citation={'source_id':source,'excerpt':'RFQ-4711'}))))
        report=AsyncMock()
        context=AgentContext(RUNTIME,RunConfig(),llm=model,access_manager=manager(),report=report)
        result=await worker.publish_reference(context,[source])
        self.assertEqual(result.output.reference,'RFQ-4711')
        self.assertEqual(result.output.positions,[])
        report.assert_awaited_once_with(result)
        self.assertIs(model.ask.call_args.kwargs['output_type'],ReferenceExtraction)

    async def test_dependency_wait_does_not_exhaust_the_active_worker_budget(self):
        waiting=asyncio.Event()
        def record(line):
            if json.loads(line)['event']=='dependency_wait': waiting.set()
        class Worker(BaseAgent):
            responsibility='test'
            def __init__(self,role): self.agent_id=role
            async def execute(self,task,context):
                task=await context.get_task(task)
                await asyncio.sleep(.01)
                return AgentResult(agent_id=self.agent_id,status=AgentStatus.partial,output=TechnicalRequirements())
        workers={role:Worker(role) for role in ('tender','supplier','erp','mail','pricing')}
        controller=Orchestrator(RUNTIME,SimpleNamespace(page_for=lambda _:object()),
            RunConfig(task_timeout=.15,retries=0),agents=workers,access_manager=manager(),logger=RunLogger(record))
        pending=asyncio.create_task(controller.execute('supplier',AgentTask()))
        await asyncio.wait_for(waiting.wait(),timeout=2)
        self.assertIsNone(controller.worker_timeouts['supplier'].when())
        await asyncio.sleep(.3)
        await controller.report(AgentResult(agent_id='tender',status=AgentStatus.partial,output=TenderData(reference='RFQ-4711')))
        result=await pending
        self.assertEqual(result.status,AgentStatus.partial)

    async def test_bid_dialog_is_observed_then_explicitly_confirmed(self):
        handlers={}
        page=SimpleNamespace(url=manager().for_role('tender').url,on=lambda event,handler:handlers.update({event:handler}))
        navigator=BrowserNavigator(page,manager().for_role('tender'))
        dialog=SimpleNamespace(type='confirm',message='Angebot verbindlich abgeben?',accept=AsyncMock(),dismiss=AsyncMock())
        navigator.allow_submission_dialogs=True
        handlers['dialog'](dialog)
        view=await navigator.observe()
        self.assertEqual(view['dialog']['message'],dialog.message)
        dialog.accept.assert_not_called()
        await navigator.act(BrowserAction(operation='confirm_dialog'))
        dialog.accept.assert_awaited_once()
        self.assertIsNone(navigator.pending_dialog)

    async def test_dialog_confirmation_is_unavailable_during_investigation(self):
        page=SimpleNamespace(on=lambda *args:None)
        navigator=BrowserNavigator(page,manager().for_role('tender'))
        with self.assertRaises(ValueError):
            await navigator.act(BrowserAction(operation='confirm_dialog'))

    async def test_structured_contract_is_visible_when_gateway_ignores_response_format(self):
        reply=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(
            content='{"ready":true}',tool_calls=[]))],usage=None)
        calls=AsyncMock(return_value=reply)
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=calls)))
        llm=LLMClient(client=client,logger=RunLogger(lambda _:None))
        await llm.ask('Check','Observed page',output_type=CheckReply)
        instruction=calls.call_args.kwargs['messages'][0]['content']
        self.assertIn('"properties"',instruction)
        self.assertIn('"ready"',instruction)
        self.assertIn('"boolean"',instruction)

    async def test_truncated_reply_increases_budget_without_parsing_partial_json(self):
        replies = [SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',message=SimpleNamespace(content='',tool_calls=[]))],usage=None),
                   SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='{"ready":true}',tool_calls=[]))],usage=None)]
        calls = AsyncMock(side_effect=replies)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=calls)))
        llm = LLMClient(client=client,logger=RunLogger(lambda _:None))
        with model_context(max_tokens=1024):
            reply = await llm.ask('Test','Test',output_type=CheckReply)
        self.assertTrue(reply.structured.ready)
        self.assertEqual([call.kwargs['max_tokens'] for call in calls.call_args_list],[1024,2048])

    async def test_unambiguous_login_is_batched_before_model_confirmation(self):
        login = observation([
            {'target':'f0:e0','tag':'input','label':'Benutzername','type':'text','form':0},
            {'target':'f0:e1','tag':'input','label':'Passwort','type':'password','form':0},
            {'target':'f0:e2','tag':'button','label':'Anmelden','type':'submit','form':0}])
        model = SimpleNamespace(ask=AsyncMock(return_value=SimpleNamespace(structured=SimpleNamespace(
            action=SimpleNamespace(operation='finish'), authenticated=True))))
        worker = TenderAgent()
        worker.navigator = SimpleNamespace(observe=AsyncMock(side_effect=[login,observation([])]),act=AsyncMock())
        context = AgentContext(RUNTIME,RunConfig(),llm=model,access_manager=manager(),logger=RunLogger(lambda _:None))
        await worker.authenticate(context)
        self.assertEqual(worker.navigator.act.await_count,3)
        self.assertEqual(model.ask.await_count,1)
        request = json.loads(model.ask.call_args.args[1])
        self.assertEqual(len(request['recent_steps']),3)
        self.assertNotIn('password-0',model.ask.call_args.args[1])

    async def test_model_call_limit_cancels_request_and_retains_agent_metadata(self):
        cancelled = asyncio.Event()
        async def never(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=never)))
        lines=[]
        llm=LLMClient(client=client,logger=RunLogger(lines.append))
        with model_context(agent='erp',phase='navigate',timeout=.02):
            with self.assertRaises(TimeoutError):
                await llm.ask('Test','Test')
        self.assertTrue(cancelled.is_set())
        start=json.loads(lines[0])
        self.assertEqual(start['agent'],'erp')
        self.assertEqual(start['phase'],'navigate')
