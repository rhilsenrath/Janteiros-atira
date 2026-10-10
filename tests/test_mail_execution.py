import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pydantic import SecretStr

from agents.mail import MailAgent, MailPlan, MailExtraction
from core.config import RunConfig
from core.contracts import AgentTask, TenderData, AgentStatus
from tools.navigation import BrowserAction
from tools.mail_runtime import MailContext, MailLogger


class FakeMailbox:
    def __init__(self,page,site,secrets,**kwargs):
        self.page=SimpleNamespace(url=site.url+'/messages/message-one')
        self.targets={}
        self.actions=[]

    async def initialize(self):
        pass

    async def observe(self,offset=0):
        return {'url':self.page.url,'frames':[{'text':
            'Discount RFQ-1\n01/24 (MM/TT) 2027\nVon: director@actual.example\n'
            'Originalnachricht: Conditional discount 5 percent',
            'controls':[{'target':'f0:e0','label':'Abmelden'},
                        {'target':'f0:e1','label':'Suchbegriff'}]}]}

    async def act(self,action,**kwargs):
        self.actions.append(action)


class MailboxModel:
    def __init__(self):
        self.calls=[]
        self.navigations=0

    async def ask(self,system,user,*,output_type):
        data=json.loads(user)
        self.calls.append((output_type,data))
        if output_type is MailPlan:
            value={'scenario_date':'2027-01-25T09:00:00Z','topics':[
                {'topic_id':'discount','objective':'Read discount approvals','authorized_sender':'director@guessed.de'}]}
        elif output_type is BrowserAction:
            self.navigations+=1
            value={'operation':'inspect' if self.navigations==1 else 'finish',
                   'source_ids':[data['source_id']]}
            if self.navigations==2:
                assert data['investigation']['collected_facts'][0]['text']=='Conditional discount 5 percent'
        elif output_type is MailExtraction:
            source=data['sources'][0]['source_id']
            value={'rules':[{'key':'discount','kind':'conditional_discount','topic_id':'discount',
                'text':'Conditional discount 5 percent','authorized_sender':'director@actual.example',
                'authorized':True,'subject':'Discount RFQ-1','effective_at':'2027-01-24T16:00:00Z',
                'message_id':source,'citations':[{'source_id':source,'excerpt':'Conditional discount 5 percent'}]}],
                'topic_reviews':[{'topic_id':'discount','rule_keys':['discount'],
                    'search_complete':True,'source_ids':[source]}]}
        else:
            raise AssertionError('Unexpected model operation')
        return SimpleNamespace(structured=output_type.model_validate(value))


class MailExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mail_loop_plans_extracts_remembers_and_finishes(self):
        model=MailboxModel()
        site=SimpleNamespace(site_id='mail',url='https://mail.test',username=SecretStr('sales@actual.example'))
        access=SimpleNamespace(for_role=lambda _:site,secrets=lambda:['sales@actual.example','private-password'])
        artifacts={}
        reports=[]
        async def report(result):reports.append(result)
        context=MailContext(runtime={'brief':'Scenario date 2027-01-25T09:00:00Z. Discount authority director@ followed by bidder domain.'},
            config=RunConfig(max_navigation_steps=4),page=object(),llm=model,access_manager=access,
            logger=MailLogger(sink=lambda _:None),report=report,save_artifact=lambda name,data:artifacts.update({name:data}))
        worker=MailAgent()
        with patch('agents.mail.MailNavigator',FakeMailbox):
            result=await worker.execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(result.status,AgentStatus.complete)
        self.assertEqual(len(result.output.rules),1)
        self.assertEqual(result.output.rules[0].authorized_sender,'director@actual.example')
        self.assertEqual(worker.records[('message-one','discount')].message_id,'message-one')
        self.assertEqual(sum(t is MailExtraction for t,_ in model.calls),1)
        self.assertEqual(artifacts['mail-progress']['coverage']['discount']['state'],'verified')
        self.assertIn('mail-steps',artifacts)
        self.assertIn('mail-observations',artifacts)
        self.assertEqual(reports[-1].status,AgentStatus.complete)

    async def test_model_failure_does_not_discard_previous_verified_rules(self):
        # The typed investigator preserves the last publication when a later phase fails.
        model=MailboxModel()
        original=model.ask
        async def ask(system,user,*,output_type):
            if output_type is BrowserAction and model.navigations==1:
                raise TimeoutError()
            return await original(system,user,output_type=output_type)
        model.ask=ask
        site=SimpleNamespace(site_id='mail',url='https://mail.test',username=SecretStr('sales@actual.example'))
        context=MailContext(runtime={'brief':'Scenario date 2027-01-25T09:00:00Z. Discount authority director@.'},
            config=RunConfig(),page=object(),llm=model,
            access_manager=SimpleNamespace(for_role=lambda _:site,secrets=lambda:[]))
        with patch('agents.mail.MailNavigator',FakeMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(result.status,AgentStatus.partial)
        self.assertEqual(len(result.output.rules),1)

    async def test_invalid_navigation_recovers_and_preserves_error_artifact(self):
        model=MailboxModel()
        original=model.ask
        failed=False
        async def ask(system,user,*,output_type):
            nonlocal failed
            if output_type is BrowserAction and not failed:
                failed=True
                BrowserAction(operation='press',target='f0:e1',value='unsupported')
            return await original(system,user,output_type=output_type)
        model.ask=ask
        site=SimpleNamespace(site_id='mail',url='https://mail.test',username=SecretStr('sales@actual.example'))
        artifacts={}
        context=MailContext(runtime={'brief':'Scenario date 2027-01-25T09:00:00Z. Discount authority director@.'},
            config=RunConfig(max_navigation_steps=4),page=object(),llm=model,
            access_manager=SimpleNamespace(for_role=lambda _:site,secrets=lambda:[]),
            save_artifact=lambda name,value:artifacts.update({name:value}))
        with patch('agents.mail.MailNavigator',FakeMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(result.status,AgentStatus.complete)
        self.assertEqual(len(result.output.rules),1)
        self.assertEqual(artifacts['mail-model-errors'][0]['phase'],'navigate')

    async def test_topic_restart_closes_tab_authenticates_and_keeps_progress(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        worker=MailAgent()
        new_page=SimpleNamespace()
        browser_context=SimpleNamespace(new_page=AsyncMock(return_value=new_page))
        old_page=SimpleNamespace(context=browser_context,close=AsyncMock())
        worker.navigator=SimpleNamespace(page=old_page)
        worker.current_topic='discount'
        worker.coverage={'discount':{'state':'verified','rule_keys':['approval']},'clauses':{'state':'pending'}}
        worker.records={('message','approval'):SimpleNamespace()}
        worker.latest=SimpleNamespace(status=AgentStatus.partial)
        worker.history=[{'operation':'click','target':'old-target'}]
        worker.save_progress=lambda _:None
        worker.authenticate=AsyncMock()
        worker.emit=lambda *args,**kwargs:None
        site=SimpleNamespace(url='https://mail.test')
        context=SimpleNamespace(access_manager=SimpleNamespace(for_role=lambda _:site,secrets=lambda:[]),
            config=RunConfig())
        navigator=SimpleNamespace(initialize=AsyncMock())
        with patch('agents.mail.MailNavigator',return_value=navigator) as constructor:
            await worker.restart_mailbox(context)
        old_page.close.assert_awaited_once()
        browser_context.new_page.assert_awaited_once()
        self.assertIs(constructor.call_args.args[0],new_page)
        navigator.initialize.assert_awaited_once()
        worker.authenticate.assert_awaited_once_with(context)
        self.assertEqual(worker.restart_count,1)
        self.assertEqual(worker.history,[])
        self.assertIsNone(worker.current_topic)
        self.assertIn(('message','approval'),worker.records)
        self.assertEqual(worker.coverage['discount']['state'],'verified')
        self.assertEqual(worker.latest.status,AgentStatus.partial)
