import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pydantic import SecretStr

from agents.mail import MailAgent, MailPlan, MailExtraction, MailDecision, MailSearch, MailChoice
from core.config import RunConfig
from core.contracts import AgentTask, TenderData, AgentStatus
from tools.navigation import BrowserAction
from tools.mail_runtime import MailContext, MailLogger


class FakeMailbox:
    def __init__(self,page,site,secrets,**kwargs):
        self.page=page
        self.page.url=site.url+'/messages/message-one'
        self.targets={}
        self.actions=[]

    async def initialize(self):
        pass

    async def observe(self,offset=0):
        return {'url':self.page.url,'frames':[{'text':
            'Discount RFQ-1\n01/24 (MM/TT) 2027\nVon: director@actual.example\n'
            'Originalnachricht: Conditional discount 5 percent',
            'controls':[{'target':'f0:e0','label':'Abmelden'},
                        {'target':'f0:e1','label':'Suchbegriff','tag':'input'}]}]}

    async def act(self,action,**kwargs):
        self.actions.append(action)
        if action.operation=='fill':self.query=action.value
        if action.operation=='press':
            from urllib.parse import urlencode
            self.page.url='https://mail.test/?'+urlencode({'q':self.query,'message':'message-one'})


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
        elif output_type is MailSearch:
            value={'terms':'Rabattfreigabe'}
        elif output_type is MailChoice:
            value={'operation':'finish'}
        elif output_type is MailDecision:
            self.navigations+=1
            value={'topic_id':'discount','stage':'extract' if self.navigations==1 else 'finish',
                   'operation':'inspect' if self.navigations==1 else 'finish',
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
    async def test_passage_selection_copies_original_without_model_transcription(self):
        from agents.mail import MailCitation
        model=MailboxModel()
        original=model.ask
        async def ask(system,user,*,output_type):
            result=await original(system,user,output_type=output_type)
            if output_type is MailExtraction:
                data=json.loads(user)
                passage=next(p for p in data['citation_passages'] if 'Conditional discount 5 percent' in p['text'])
                result.structured.rules[0].citations[:]=[MailCitation(passage_ids=[passage['passage_id']])]
            return result
        model.ask=ask
        context,_,_=self.context(model)
        with patch('agents.mail.MailNavigator',FakeMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(len(result.output.rules),1)
        self.assertEqual(result.output.rules[0].citations[0].excerpt,'Originalnachricht: Conditional discount 5 percent')

    async def test_rejection_history_retains_both_failed_attempts(self):
        from agents.mail import MailCitation
        model=MailboxModel()
        original=model.ask
        async def ask(system,user,*,output_type):
            result=await original(system,user,output_type=output_type)
            if output_type is MailExtraction:
                result.structured.rules[0].citations[:]=[MailCitation(passage_ids=['not-observed'])]
            return result
        model.ask=ask
        context,_,artifacts=self.context(model)
        with patch('agents.mail.MailNavigator',FakeMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertFalse(result.output.rules)
        history=artifacts['mail-extraction-rejections']
        self.assertEqual([r['attempt'] for r in history],[1,2])
        self.assertEqual([r['topic_id'] for r in history],['discount','discount'])
        self.assertEqual(history[1]['citation_correction']['rejected_passage_ids'],['not-observed'])

    async def test_controller_binds_valid_quote_without_trusting_model_source(self):
        for supplied_source in ('', 'invented-source'):
            with self.subTest(source=supplied_source):
                model=MailboxModel()
                original=model.ask
                calls=[]
                async def ask(system,user,*,output_type):
                    result=await original(system,user,output_type=output_type)
                    if output_type is MailExtraction:
                        calls.append(json.loads(user))
                        citation=result.structured.rules[0].citations[0]
                        result.structured.rules[0].citations[0]=citation.model_copy(update={'source_id':supplied_source})
                    return result
                model.ask=ask
                context,_,_=self.context(model)
                with patch('agents.mail.MailNavigator',FakeMailbox):
                    result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
                self.assertEqual(len(calls),1)
                self.assertEqual(len(result.output.rules),1)
                citation=result.output.rules[0].citations[0]
                self.assertEqual(citation.source_id,calls[0]['sources'][0]['source_id'])
                self.assertEqual(citation.excerpt,'Conditional discount 5 percent')

    async def test_wrong_topic_is_corrected_with_explicit_expected_identifier(self):
        model=MailboxModel()
        original=model.ask
        extraction_calls=[]
        async def ask(system,user,*,output_type):
            result=await original(system,user,output_type=output_type)
            if output_type is MailExtraction:
                data=json.loads(user)
                extraction_calls.append(data)
                self.assertEqual(data['required_topic_id'],'discount')
                self.assertIn("ONLY allowed topic_id",system)
                self.assertEqual([t['topic_id'] for t in data['investigation']['topics']],['discount'])
                if len(extraction_calls)==1:
                    result.structured.rules[0].topic_id='packaging'
                else:
                    self.assertEqual(data['feedback']['required_topic_id'],'discount')
                    self.assertIn('packaging',data['feedback']['error'])
                    self.assertIn("topic_id='discount'",data['feedback']['instruction'])
            return result
        model.ask=ask
        context,_,artifacts=self.context(model)
        with patch('agents.mail.MailNavigator',FakeMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(len(extraction_calls),2)
        self.assertEqual(len(result.output.rules),1)
        self.assertEqual(artifacts['mail-topic-runs'][0]['status'],'saved')

    async def test_dialog_appearing_during_search_choice_is_closed_before_search(self):
        instances=[]
        class DialogMailbox(FakeMailbox):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs)
                self.dialog=False
                instances.append(self)
            async def observe(self,offset=0):
                observation=await super().observe(offset)
                if self.dialog:
                    observation['frames'][0]['controls'] += [
                        {'target':'accept','label':'Akzeptieren und fortfahren','in_dialog':True},
                        {'target':'close','label':'Schließen','in_dialog':True}]
                return observation
            async def act(self,action,**kwargs):
                if action.operation=='click' and action.target=='close':
                    self.actions.append(action)
                    self.dialog=False
                    return
                if action.operation in ('fill','press') and self.dialog:
                    raise AssertionError('Search attempted beneath dialog')
                await super().act(action,**kwargs)
        model=MailboxModel()
        original=model.ask
        async def ask(system,user,*,output_type):
            result=await original(system,user,output_type=output_type)
            if output_type is MailSearch:instances[0].dialog=True
            return result
        model.ask=ask
        context,_,_=self.context(model)
        with patch('agents.mail.MailNavigator',DialogMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(len(result.output.rules),1)
        self.assertEqual(instances[0].actions[0].target,'close')
        self.assertFalse(any(a.target=='accept' for a in instances[0].actions))

    async def test_empty_inbox_opens_archive_and_repeats_search(self):
        instances=[]
        class FolderMailbox(FakeMailbox):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs)
                self.folder='inbox'
                self.expanded=False
                instances.append(self)
            async def observe(self,offset=0):
                controls=[{'target':'search','tag':'input','label':'Suchbegriff'},
                          {'target':'folders','tag':'button','label':'Postfachordner'},
                          {'target':'logout','label':'Abmelden'}]
                if self.expanded:controls.append({'target':'archive','label':'Archiv'})
                return {'url':self.page.url,'frames':[{'text':'Keine Nachrichten gefunden','controls':controls}]}
            async def act(self,action,**kwargs):
                from urllib.parse import urlencode
                self.actions.append(action)
                if action.operation=='fill':self.query=action.value
                elif action.operation=='press':self.page.url='https://mail.test/?'+urlencode({'folder':self.folder,'q':self.query})
                elif action.target=='folders':self.expanded=True
                elif action.target=='archive':
                    self.folder='archive'
                    self.expanded=False
                    self.page.url='https://mail.test/?folder=archive'
        context,_,_=self.context(MailboxModel())
        context.runtime['brief']='Scenario date 2027-01-25T09:00:00Z. Discount authority director@.'
        with patch('agents.mail.MailNavigator',FolderMailbox):
            await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        actions=instances[0].actions
        self.assertEqual([a.target for a in actions if a.operation=='click'],['folders','archive'])
        self.assertEqual([a.value for a in actions if a.operation=='fill'],['RFQ-1 Rabattfreigabe']*2)

    async def test_original_button_cannot_bypass_candidate_selection(self):
        worker=MailAgent()
        worker.reference='RFQ-1'
        observation={'frames':[{'controls':[
            {'target':'original','label':'Original öffnen','href':'https://mail.test/messages/wrong'},
            {'target':'right','label':'Preisindizes RFQ-1','href':'https://mail.test/?message=right'}]}]}
        self.assertEqual([c['target'] for c in worker.topic_controls(observation,'review_results')],['right'])

    def context(self,model):
        pages=[]
        browser_context=SimpleNamespace()
        def new_page():
            page=SimpleNamespace(url='https://mail.test',context=browser_context)
            page.closed=False
            async def close():page.closed=True
            page.close=AsyncMock(side_effect=close)
            page.is_closed=lambda:page.closed
            pages.append(page)
            return page
        browser_context.new_page=AsyncMock(side_effect=new_page)
        page=new_page()
        site=SimpleNamespace(site_id='mail',url='https://mail.test',username=SecretStr('sales@actual.example'))
        artifacts={}
        context=MailContext(runtime={'brief':'Scenario date 2027-01-25T09:00:00Z. Discount authority director@. Subject Rabattfreigabe.'},
            config=RunConfig(max_navigation_steps=8),page=page,llm=model,
            access_manager=SimpleNamespace(for_role=lambda _:site,secrets=lambda:[]),
            save_artifact=lambda name,value:artifacts.update({name:value}))
        return context,pages,artifacts

    async def test_basic_topic_saves_rules_then_closes_tab(self):
        model=MailboxModel()
        context,pages,artifacts=self.context(model)
        with patch('agents.mail.MailNavigator',FakeMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(result.status,AgentStatus.complete)
        self.assertEqual(len(result.output.rules),1)
        self.assertEqual(model.navigations,0)
        self.assertTrue(pages[0].closed)
        self.assertEqual(artifacts['mail-topic-runs'][0]['status'],'saved')

    async def test_first_topic_failure_closes_tab_and_attempts_second_once(self):
        from agents.mail import MailTopic
        model=MailboxModel()
        original=model.ask
        seen=[]
        async def ask(system,user,*,output_type):
            data=json.loads(user)
            if output_type is MailPlan:
                return SimpleNamespace(structured=MailPlan(scenario_date='2027-01-25T09:00:00Z',topics=[
                    MailTopic(topic_id='discount',objective='Read discount'),MailTopic(topic_id='clauses',objective='Read Konditionsregeln')]))
            if output_type is MailSearch:
                topic=data['topic']['topic_id']
                seen.append(topic)
                if topic=='discount':raise TimeoutError()
                return SimpleNamespace(structured=MailSearch(terms='Konditionsregeln'))
            return await original(system,user,output_type=output_type)
        model.ask=ask
        context,pages,artifacts=self.context(model)
        with patch('agents.mail.MailNavigator',FakeMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(seen,['discount','clauses'])
        self.assertEqual(len(pages),2)
        self.assertTrue(all(p.closed for p in pages))
        self.assertEqual(result.status,AgentStatus.partial)
        self.assertEqual(artifacts['mail-topic-runs'][0]['status'],'failed')
        self.assertNotEqual(artifacts['mail-topic-runs'][1]['status'],'running')

    async def test_listing_without_original_does_not_call_extraction_model(self):
        worker=MailAgent()
        worker.sources={'listing':{'source_id':'listing','url':'https://mail.test/?q=RFQ-1+Rabatt&message=one',
                                  'text':'Rabattfreigabe RFQ-1. Original öffnen'}}
        worker.ask=AsyncMock()
        with self.assertRaisesRegex(ValueError,'Original body unavailable'):
            await worker.extract(SimpleNamespace(),AgentTask(tender=TenderData(reference='RFQ-1')),['listing'])
        worker.ask.assert_not_awaited()

    async def test_controller_submits_search_before_extraction(self):
        model=MailboxModel()
        context,pages,artifacts=self.context(model)
        with patch('agents.mail.MailNavigator',FakeMailbox):
            worker=MailAgent()
            await worker.execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        actions=[s['action']['operation'] for s in worker.trace if s['phase']=='search']
        self.assertEqual(actions,['fill','press'])
        self.assertEqual(sum(t is MailSearch for t,_ in model.calls),1)
        self.assertEqual(sum(t is MailExtraction for t,_ in model.calls),1)

    async def test_result_controls_exclude_signing_and_readonly(self):
        worker=MailAgent()
        observation={'frames':[{'controls':[
            {'target':'sign','label':'Signatur TAN','href':'https://mail.test/messages/signing-1'},
            {'target':'valid','label':'Rabattfreigabe RFQ-1','href':'https://mail.test/?message=approval'},
            {'target':'archive','label':'Archiv','href':'https://mail.test/?folder=archive'},
            {'target':'address','label':'Adresse','readonly':True}]}]}
        self.assertEqual([c['target'] for c in worker.topic_controls(observation,'review_results')],['valid','archive'])

    async def test_observation_loop_stops_without_extraction(self):
        model=MailboxModel()
        original=model.ask
        async def ask(system,user,*,output_type):
            if output_type is MailChoice:return SimpleNamespace(structured=MailChoice(operation='observe'))
            return await original(system,user,output_type=output_type)
        model.ask=ask
        context,pages,artifacts=self.context(model)
        class ListingMailbox(FakeMailbox):
            async def observe(self,offset=0):
                value=await super().observe(offset)
                value['url']='https://mail.test/?q=RFQ-1+Rabattfreigabe'
                value['frames'][0]['text']='Results RFQ-1'
                return value
        with patch('agents.mail.MailNavigator',ListingMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(result.status,AgentStatus.partial)
        self.assertEqual(sum(t is MailExtraction for t,_ in model.calls),0)
        self.assertTrue(pages[0].closed)
        self.assertEqual(artifacts['mail-topic-runs'][0]['status'],'incomplete')

    async def test_repeated_reference_removed_and_subject_not_translated(self):
        model=MailboxModel()
        original=model.ask
        async def ask(system,user,*,output_type):
            if output_type is MailSearch:
                return SimpleNamespace(structured=MailSearch(terms='RFQ-1 RFQ-1 Rabattfreigabe'))
            return await original(system,user,output_type=output_type)
        model.ask=ask
        context,pages,artifacts=self.context(model)
        with patch('agents.mail.MailNavigator',FakeMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        from urllib.parse import urlsplit,parse_qs
        self.assertEqual(parse_qs(urlsplit(pages[0].url).query)['q'],['RFQ-1 Rabattfreigabe'])
        self.assertEqual(len(result.output.rules),1)

    async def test_descriptive_search_is_corrected_before_submission(self):
        model=MailboxModel()
        original=model.ask
        searches=[]
        async def ask(system,user,*,output_type):
            if output_type is MailSearch:
                searches.append(json.loads(user))
                return SimpleNamespace(structured=MailSearch(terms='discount approval' if len(searches)==1 else 'Rabattfreigabe'))
            return await original(system,user,output_type=output_type)
        model.ask=ask
        context,pages,artifacts=self.context(model)
        with patch('agents.mail.MailNavigator',FakeMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(len(searches),2)
        self.assertIn('correction',searches[1])
        self.assertEqual(len(result.output.rules),1)

    async def test_invisible_subject_in_control_label_is_recognized(self):
        model=MailboxModel()
        context,pages,artifacts=self.context(model)
        context.runtime['brief']='Scenario date 2027-01-25T09:00:00Z. Discount authority director@.'
        class LabelMailbox(FakeMailbox):
            async def observe(self,offset=0):
                value=await super().observe(offset)
                value['frames'][0]['controls'].append({'target':'subject','label':'\u200b'.join('Rabattfreigabe RFQ-1')})
                return value
        with patch('agents.mail.MailNavigator',LabelMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(len(result.output.rules),1)

    async def test_similar_tender_candidate_is_filtered(self):
        worker=MailAgent()
        worker.reference='RFQ-1'
        observation={'frames':[{'controls':[
            {'target':'other','label':'Rabattfreigabe RFQ-1-A','href':'https://mail.test/messages/wrong'},
            {'target':'right','label':'Rabattfreigabe RFQ-1','href':'https://mail.test/messages/right'}]}]}
        self.assertEqual([c['target'] for c in worker.topic_controls(observation,'review_results')],['right'])

    async def test_bad_citation_is_corrected_on_same_original(self):
        model=MailboxModel()
        original=model.ask
        extractions=0
        async def ask(system,user,*,output_type):
            nonlocal extractions
            result=await original(system,user,output_type=output_type)
            if output_type is MailExtraction:
                extractions+=1
                if extractions==1:
                    rule=result.structured.rules[0]
                    from agents.website import SourceCitation
                    rule=rule.model_copy(update={'citations':[SourceCitation(source_id='unselected',excerpt='Invented discount 99 percent')]})
                    result.structured=result.structured.model_copy(update={'rules':[rule]})
                else:
                    feedback=json.loads(user)['feedback']
                    self.assertEqual(feedback['citation_correction']['rejected_excerpt'],'Invented discount 99 percent')
                    self.assertIn('Conditional discount 5 percent',feedback['citation_correction']['available_text'][0]['text'])
            return result
        model.ask=ask
        context,pages,artifacts=self.context(model)
        with patch('agents.mail.MailNavigator',FakeMailbox):
            result=await MailAgent().execute(AgentTask(tender=TenderData(reference='RFQ-1')),context)
        self.assertEqual(extractions,2)
        self.assertEqual(len(result.output.rules),1)
