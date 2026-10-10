import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from decimal import Decimal

from agents.base import AgentContext
from agents.tender import TenderAgent
from agents.supplier import SupplierAgent
from agents.erp import ErpAgent
from agents.mail import MailAgent
from agents.website import AuthenticationStep
from core.config import RunConfig
from core.contracts import AgentTask, AgentStatus, TenderData, Evidence, TechnicalRequirements, TechnicalRequirement
from tools.navigation import BrowserAction, BrowserNavigator
from tools.logging import RunLogger
from tools.documents import read_document
from test_architecture import manager, RUNTIME

class ObservedBrowser:
    def __init__(self,page,site,secrets=(),**kwargs):
        self.site=site;self.page=SimpleNamespace(url=site.url,set_default_timeout=lambda _:None);self.actions=[]
    async def initialize(self):pass
    async def observe(self,offset=0):
        facts = {'0':'Tender UNIT-RFQ position fixture quantity 2 Stück.',
                 '1':'Requirement fixture revision test bore 10.',
                 '2':'Material fixture stock 0 active true.',
                 '3':'Policy fixture discount: fixture observed policy.'}
        return {'url':self.site.url,'frames':[{'text':facts[self.site.site_id]+' Account user-0 password-0',
                'controls':[]}], 'tabs':[], 'downloads':[]}
    async def act(self,action,**kwargs):self.actions.append(action);return None

class ExtractionModel:
    def __init__(self,role,deny_auth=False,fabricate=False):
        self.role=role;self.deny_auth=deny_auth;self.fabricate=fabricate;self.requests=[]
    async def ask(self,system,user,*,output_type=None,tools=None):
        self.requests.append(user);data=json.loads(user)
        if output_type is AuthenticationStep:
            answer={'action':{'operation':'finish'},'authenticated':not self.deny_auth}
        elif output_type is BrowserAction:
            answer={'operation':'finish','source_ids':[data['source_id']]}
        else:
            source=data['sources'][0]
            outputs={
                'tender':{'reference':'UNIT-RFQ','positions':[{'position_id':'fixture','quantity':'2','unit':'Stück'}]},
                'supplier':{'requirements':[{'requirement_id':'fixture','bore':'10','revision':'test'}]},
                'erp':{'materials':[{'material_id':'fixture','stock':'0','active':True}]},
                'mail':{'rules':[{'key':'fixture','kind':'discount','text':'fixture observed policy'}]}}
            answer={'output':outputs[self.role],'status':'complete','citations':[{
                'source_id':source['source_id'],'excerpt':'Invented quotation' if self.fabricate else source['text'].split(' Account')[0]}]}
        return SimpleNamespace(structured=output_type.model_validate(answer))

class WebsiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_production_worker_authenticates_and_extracts_from_observed_sources(self):
        for role,worker in [('tender',TenderAgent()),('supplier',SupplierAgent()),('erp',ErpAgent()),('mail',MailAgent())]:
            logs=[];reports=[];model=ExtractionModel(role)
            async def report(result):reports.append(result)
            context=AgentContext(RUNTIME,RunConfig(max_navigation_steps=2),page=object(),llm=model,
                access_manager=manager(),logger=RunLogger(logs.append),report=report)
            with patch('agents.website.BrowserNavigator',ObservedBrowser):
                result=await worker.execute(AgentTask(tender=TenderData(reference='UNIT-RFQ'),
                    technical=TechnicalRequirements(requirements=[TechnicalRequirement(requirement_id='fixture')])),context)
            self.assertEqual(result.status,AgentStatus.partial)
            self.assertEqual(result.evidence[0].source_site,manager().for_role(role).site_id)
            self.assertFalse(result.evidence[0].mock)
            self.assertIn('fixture',result.evidence[0].excerpt)
            self.assertEqual(reports,[result])
            self.assertNotIn('password-0',''.join(model.requests));self.assertNotIn('user-0',''.join(model.requests))
            self.assertNotIn('UNIT-RFQ',''.join(logs));self.assertNotIn('password-0',''.join(logs))
            self.assertTrue(any(json.loads(line)['event']=='authentication_succeeded' for line in logs))

    async def test_failed_login_never_fabricates_success(self):
        worker=TenderAgent();context=AgentContext(RUNTIME,RunConfig(),page=object(),llm=ExtractionModel('tender',deny_auth=True),
            access_manager=manager())
        with patch('agents.website.BrowserNavigator',ObservedBrowser):result=await worker.execute(AgentTask(),context)
        self.assertEqual(result.status,AgentStatus.failed);self.assertIsNone(result.output)

    async def test_fabricated_citation_is_rejected_and_steps_are_bounded(self):
        worker=TenderAgent();context=AgentContext(RUNTIME,RunConfig(max_navigation_steps=2),page=object(),
            llm=ExtractionModel('tender',fabricate=True),access_manager=manager())
        with patch('agents.website.BrowserNavigator',ObservedBrowser):result=await worker.execute(AgentTask(),context)
        self.assertEqual(result.status,AgentStatus.blocked);self.assertIsNone(result.output)
        self.assertTrue(result.missing)

    async def test_submission_and_tan_are_unavailable_operations(self):
        for worker in (TenderAgent(),MailAgent()):
            for action in ('submit','retrieve_signing_token'):
                result=await worker.execute(AgentTask(action=action),AgentContext(RUNTIME,RunConfig()))
                self.assertEqual(result.status,AgentStatus.blocked)

class NavigationTests(unittest.IsolatedAsyncioTestCase):
    def browser(self):
        site=manager().for_role('tender')
        control={'tag':'input','name':'random-login-field','type':'password','label':'Secret label','href':None}
        element=SimpleNamespace(fill=AsyncMock(),click=AsyncMock(),press=AsyncMock(),select_option=AsyncMock())
        async def evaluate(script):return site.url if "closest('form')" in script else control
        element.evaluate=evaluate
        frame=SimpleNamespace(locator=lambda selector:SimpleNamespace(nth=lambda index:element),
            get_by_label=lambda *args,**kwargs:SimpleNamespace(count=AsyncMock(return_value=0)))
        page=SimpleNamespace(url=site.url,on=lambda *args:None,context=SimpleNamespace(pages=[],route=AsyncMock()),
            goto=AsyncMock(),mouse=SimpleNamespace(wheel=AsyncMock()))
        page.context.pages=[page]
        nav=BrowserNavigator(page,site)
        nav.targets={'f0:e0':(frame,0,control)}
        return nav,element

    async def test_secret_injected_into_observed_control_and_not_allowed_in_investigation(self):
        nav,element=self.browser();action=BrowserAction(operation='fill',target='f0:e0',secret='password')
        with self.assertRaises(ValueError):await nav.act(action)
        await nav.act(action,authenticating=True)
        element.fill.assert_awaited_once_with('password-0',timeout=10000)

    async def test_unknown_target_guessed_url_cross_origin_refused(self):
        nav,_=self.browser()
        for action in [BrowserAction(operation='click',target='f9:unknown'),
                       BrowserAction(operation='goto',url='https://site-0.invalid/guessed'),
                       BrowserAction(operation='goto',url='https://evil.invalid')]:
            with self.assertRaises(ValueError):await nav.act(action)
        nav.page.goto.assert_not_called()

    async def test_page_request_guard_blocks_cross_site(self):
        nav,_=self.browser();await nav.initialize()
        handler=nav.page.context.route.call_args.args[1]
        route=SimpleNamespace(request=SimpleNamespace(url='https://evil.invalid/login'),continue_=AsyncMock(),abort=AsyncMock())
        await handler(route);route.abort.assert_awaited_once();route.continue_.assert_not_called()

    async def test_observed_identity_is_rechecked_before_action(self):
        nav,element=self.browser()
        async def changed(script):return {'tag':'button','name':None,'label':'different'}
        element.evaluate=changed
        with self.assertRaises(ValueError):await nav.act(BrowserAction(operation='click',target='f0:e0'))
        element.click.assert_not_called()

class DocumentTests(unittest.TestCase):
    def test_archive_is_listed_before_model_selected_member_is_read(self):
        from io import BytesIO
        from zipfile import ZipFile
        stream=BytesIO()
        with ZipFile(stream,'w') as archive:archive.writestr('selected.txt','Actual selected fixture content')
        data=stream.getvalue()
        self.assertEqual(read_document(data,'fixture.zip')['members'],['selected.txt'])
        self.assertEqual(read_document(data,'fixture.zip','selected.txt')['text'],'Actual selected fixture content')
        with self.assertRaises(ValueError):read_document(data,'fixture.zip','invented.txt')

    def test_unsupported_format_fails_instead_of_empty_success(self):
        with self.assertRaises(ValueError):read_document(b'not supported','fixture.exe')
        self.assertIn('document',read_document(b'<p>document</p>','fixture.html')['text'])
