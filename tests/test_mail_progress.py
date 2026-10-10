import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pydantic import SecretStr, ValidationError
from agents.mail import MailAgent, MailPlan, MailTopic, TopicReview, MailRule as CommercialRule
from core.contracts import AgentTask, TenderData, CommercialPolicies, AgentStatus, AgentResult, Evidence
from tools.navigation import BrowserAction


class MailProgressTests(unittest.TestCase):
    def setUp(self):
        self.worker = MailAgent()
        self.worker.plan = MailPlan(scenario_date='2027-01-25T09:00:00Z', topics=[
            MailTopic(topic_id='discount', objective='Discount', authorized_sender='director@example.test'),
            MailTopic(topic_id='fx', objective='Exchange rate', authorized_sender='finance@example.test')])
        self.worker.coverage = {t.topic_id: {'objective':t.objective,'state':'pending','rule_keys':[]} for t in self.worker.plan.topics}
        self.task = AgentTask(tender=TenderData(reference='RFQ-1'))
        self.context = SimpleNamespace(save_artifact=None)

    def source(self, key='one', date='01/24', body='Conditional discount 5 percent', sender='director@example.test'):
        source = {'source_id':key,'url':f'https://mail.test/messages/{key}',
            'text':f'RFQ-1\n{date} (MM/TT) 2027\nVon: {sender}\nOriginalnachricht: {body}'}
        self.worker.sources[key] = source
        return source

    def rule(self, **updates):
        return CommercialRule(key='discount', kind='conditional_discount', topic_id='discount', text='Conditional discount 5 percent',
            subject='Discount RFQ-1', tender_reference='RFQ-1', authorized=True,
            evidence=[Evidence(id='one',agent_id='mail',source_site='mail',excerpt='Conditional discount 5 percent')],
            authorized_sender='director@example.test', effective_at='2027-01-24T16:00:00Z', **updates)

    def process(self, rules, sources, reviews=()):
        response = SimpleNamespace(topic_reviews=list(reviews))
        return self.worker.process_extraction(self.context,self.task,response,
            CommercialPolicies(rules=rules, approved_discount=5),[],AgentStatus.complete,[],sources)

    def test_observed_authority_does_not_depend_on_model_boolean(self):
        source=self.source()
        rule=self.rule().model_copy(update={'authorized':False,'authorized_sender':None})
        result=self.process([rule],[source])[0]
        self.assertEqual(len(result.rules),1)
        self.assertTrue(result.rules[0].authorized)
        self.assertEqual(result.rules[0].authorized_sender,'director@example.test')

    def test_mismatched_envelope_authority_is_not_repaired(self):
        source=self.source(sender='stranger@example.test')
        rule=self.rule().model_copy(update={'authorized':False,'authorized_sender':None})
        self.assertEqual(self.process([rule],[source])[0].rules,[])

    def test_observed_other_reference_reports_reference_not_sender_error(self):
        source=self.source()
        source['text']=source['text'].replace('RFQ-1','RFQ-1-A')
        self.process([self.rule()],[source])
        self.assertEqual(self.worker.last_rejections[0]['reason'],'reference_mismatch')

    def test_pending_topics_survive_successful_discount_extraction(self):
        source = self.source()
        review = TopicReview(topic_id='discount',rule_keys=['discount'],search_complete=True,source_ids=['one'])
        output,_,status,missing = self.process([self.rule()],[source],[review])
        self.assertEqual(status,AgentStatus.partial)
        self.assertEqual([m.question for m in missing],['Complete topic fx: Exchange rate'])
        self.assertIsNone(output.approved_discount)
        self.worker.latest = AgentResult(agent_id='mail', status=status, output=output, missing=missing)
        self.assertIn('Conditional discount 5 percent',str(self.worker.investigation_context()['collected_facts']))

    def test_future_original_rejected_even_without_effective_date(self):
        source = self.source(date='01/26')
        rule = self.rule().model_copy(update={'effective_at':None})
        output,_,_,missing = self.process([rule],[source])
        self.assertEqual(output.rules,[])
        self.assertEqual(self.worker.discarded[0]['reason'],'future_record')
        self.assertEqual(len(missing),2)

    def test_listing_and_wrong_reference_do_not_become_rules(self):
        source = self.source()
        source['text']='Discount RFQ-1 director@example.test 24.01.2027'
        rule = self.rule().model_copy(update={'text':'Discount RFQ-1'})
        self.assertEqual(self.process([rule],[source])[0].rules,[])
        source = self.source()
        rule = self.rule().model_copy(update={'tender_reference':'RFQ-1-A'})
        self.assertEqual(self.process([rule],[source])[0].rules,[])

    def test_duplicate_message_uses_observed_timezone(self):
        source = self.source()
        self.worker.sources['list']={'source_id':'list','url':'https://mail.test/?message=one',
            'text':'Nachricht: Discount RFQ-1 director@example.test | Eingang: 24.01.2027, 17:00\nZeitzone: Europe/Berlin'}
        a=self.rule().model_copy(update={'effective_at':datetime.fromisoformat('2027-01-24T17:00:00+00:00')})
        b=self.rule()
        rules=self.process([a,b],[source])[0].rules
        self.assertEqual(len(rules),1)
        self.assertEqual(rules[0].effective_at.utcoffset().total_seconds(),3600)
        self.assertEqual(rules[0].effective_at.hour,17)

    def test_repeat_extraction_blocked_but_completion_reuses_facts(self):
        self.worker.extracted_sources.add('one')
        with self.assertRaises(ValueError):
            self.worker.validate_investigation_action(BrowserAction(operation='inspect',source_ids=['one']))
        self.worker.latest=AgentResult(agent_id='mail',status=AgentStatus.partial,output=CommercialPolicies(rules=[self.rule()]))
        self.assertIsNone(self.worker.finish_collected())
        for state in self.worker.coverage.values():state['state']='verified'
        self.assertEqual(self.worker.finish_collected().status,AgentStatus.complete)

    def test_unobserved_review_cannot_mark_topic_complete(self):
        source=self.source()
        review=TopicReview(topic_id='discount',rule_keys=['discount'],search_complete=True,source_ids=['invented'])
        self.process([self.rule()],[source],[review])
        self.assertNotEqual(self.worker.coverage['discount']['state'],'verified')

    def test_rule_requires_citation_of_original_body(self):
        source=self.source()
        rule=self.rule().model_copy(update={'evidence':[]})
        self.assertEqual(self.process([rule],[source])[0].rules,[])
        self.assertEqual(self.worker.discarded[0]['reason'],'rule_body_not_cited')

    def test_search_must_be_submitted_before_overwriting_query(self):
        self.worker.history=[{'operation':'fill','target':'f0:e1'}]
        self.worker.navigator=SimpleNamespace(targets={'f0:e1':(None,1,{'label':'Suchbegriff'})})
        with self.assertRaises(ValueError):
            self.worker.validate_investigation_action(BrowserAction(operation='fill',target='f0:e1',value='new query'))

    def test_plan_schema_excludes_other_agent_topics(self):
        with self.assertRaises(ValidationError):
            MailTopic(topic_id='erp_material_matching',objective='Search ERP')
        with self.assertRaises(ValidationError):
            MailTopic(topic_id='decision_bid',objective='Retrieve TAN')

    def test_observation_id_and_missing_reference_recovered_from_original(self):
        source=self.source()
        rule=self.rule().model_copy(update={'message_id':'one','tender_reference':None})
        result=self.process([rule],[source])[0]
        self.assertEqual(len(result.rules),1)
        self.assertEqual(result.rules[0].message_id,'one')
        self.assertEqual(result.rules[0].tender_reference,'RFQ-1')

    def test_failed_extractions_are_bounded_and_report_expected_values(self):
        source=self.source()
        rule=self.rule().model_copy(update={'tender_reference':'RFQ-OTHER'})
        self.process([rule],[source])
        self.process([rule],[source])
        with self.assertRaises(ValueError):
            self.worker.validate_investigation_action(BrowserAction(operation='inspect',source_ids=['one']))
        self.assertEqual(self.worker.investigation_context()['last_rejections'][0]['expected_reference'],'RFQ-1')
        self.assertEqual(self.worker.investigation_context()['last_rejections'][0]['reason'],'reference_mismatch')


class MailPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_domain_comes_from_runtime_account_not_company_guess(self):
        worker=MailAgent()
        worker.run_key='current'
        worker.ask=AsyncMock(return_value=MailPlan(scenario_date='2027-01-25T09:00:00Z',topics=[
            MailTopic(topic_id='discount',objective='Read discount approval messages',authorized_sender='director@invented.de')]))
        site=SimpleNamespace(username=SecretStr('sales@actual.example'))
        context=SimpleNamespace(access_manager=SimpleNamespace(for_role=lambda _:site),
            runtime={'brief':'Scenario date 2027-01-25T09:00:00Z. Discount authority director@ followed by bidder domain.'},save_artifact=None)
        task=AgentTask(tender=TenderData(reference='RFQ-1'))
        await worker.prepare_investigation(context,task)
        self.assertEqual(worker.plan.topics[0].authorized_sender,'director@actual.example')
        self.assertEqual(worker.mailbox_domain,'actual.example')
        self.assertNotIn('sales@actual.example',str(worker.ask.await_args))
        await worker.prepare_investigation(context,task)
        self.assertEqual(worker.ask.await_count,1)

    async def test_foreign_work_rejected_even_under_mail_topic_id(self):
        worker=MailAgent()
        worker.ask=AsyncMock(return_value=MailPlan(scenario_date='2027-01-25T09:00:00Z',topics=[
            MailTopic(topic_id='discount',objective='Search ERP for material price scales')]))
        context=SimpleNamespace(access_manager=SimpleNamespace(for_role=lambda _:SimpleNamespace(username=SecretStr('sales@actual.example'))),
            runtime={'brief':'Scenario date 2027-01-25T09:00:00Z'},save_artifact=None)
        with self.assertRaises(ValueError):
            await worker.prepare_investigation(context,AgentTask(tender=TenderData(reference='RFQ-1')))

    async def test_equivalent_timestamp_and_mail_about_erp_are_allowed(self):
        worker=MailAgent()
        worker.ask=AsyncMock(return_value=MailPlan(scenario_date='2027-01-25T10:00:00+01:00',topics=[
            MailTopic(topic_id='technical',objective='Search emails for ERP specification approval')]))
        context=SimpleNamespace(access_manager=SimpleNamespace(for_role=lambda _:SimpleNamespace(username=SecretStr('sales@actual.example'))),
            runtime={'brief':'Scenario date 2027-01-25T09:00:00Z'},save_artifact=None)
        await worker.prepare_investigation(context,AgentTask(tender=TenderData(reference='RFQ-1')))
        self.assertEqual(worker.ask.await_count,1)

    async def test_invalid_plan_is_logged_and_repaired_without_upstream_agents(self):
        worker=MailAgent()
        invalid=MailPlan(scenario_date='2027-01-25T09:00:00Z',topics=[
            MailTopic(topic_id='discount',objective='Search ERP for prices',authorized_sender='invented@example.test')])
        valid=MailPlan(scenario_date='2027-01-25T09:00:00Z',topics=[
            MailTopic(topic_id='discount',objective='Read discount approval emails')])
        worker.ask=AsyncMock(side_effect=[invalid,valid])
        artifacts={}
        context=SimpleNamespace(access_manager=SimpleNamespace(for_role=lambda _:SimpleNamespace(username=SecretStr('sales@actual.example'))),
            runtime={'brief':'Scenario date 2027-01-25T09:00:00Z'},save_artifact=lambda name,value:artifacts.update({name:value}))
        await worker.prepare_investigation(context,AgentTask(tender=TenderData(reference='RFQ-1')))
        self.assertEqual(worker.ask.await_count,2)
        self.assertEqual(len(artifacts['mail-plan-attempts'][0]['errors']),2)
        self.assertEqual(artifacts['mail-plan-attempts'][1]['errors'],[])
        self.assertIn('validation_errors',worker.ask.await_args.args[2])
        self.assertIsNone(worker.plan.topics[0].authorized_sender)

    async def test_subject_local_part_authorities_from_brief(self):
        worker=MailAgent()
        worker.ask=AsyncMock(return_value=MailPlan(scenario_date='2027-01-25T09:00:00Z',topics=[
            MailTopic(topic_id='technical',objective='Read technical approvals',authorized_sender='technik@guessed.test'),
            MailTopic(topic_id='packaging',objective='Read packaging tariffs',authorized_sender='logistik@guessed.test'),
            MailTopic(topic_id='fx',objective='Read currency fixing',authorized_sender='finanzen@guessed.test')]))
        context=SimpleNamespace(access_manager=SimpleNamespace(for_role=lambda _:SimpleNamespace(username=SecretStr('sales@actual.example'))),
            runtime={'brief':'Scenario date 2027-01-25T09:00:00Z. Authorized subjects/senders (local part + bidder mailbox domain): Technikfreigabe/technik, Verpackungstarife/logistik, Devisenfixing/finanzen.'},save_artifact=None)
        await worker.prepare_investigation(context,AgentTask(tender=TenderData(reference='RFQ-1')))
        self.assertEqual([t.authorized_sender for t in worker.plan.topics],
                         ['technik@actual.example','logistik@actual.example','finanzen@actual.example'])
        self.assertEqual(worker.ask.await_count,1)

    async def test_validation_failure_artifact_records_field_without_secret(self):
        worker=MailAgent()
        try:
            BrowserAction(operation='press',target='f0:e1',value='unsupported')
        except ValidationError as error:
            failure=error
        artifacts={}
        context=SimpleNamespace(llm=SimpleNamespace(ask=AsyncMock(side_effect=failure)),
            access_manager=SimpleNamespace(secrets=lambda:['private-password']),
            save_artifact=lambda name,value:artifacts.update({name:value}))
        with self.assertRaises(ValidationError):
            await worker.ask(context,'Navigate',{},BrowserAction,step=15)
        self.assertEqual(artifacts['mail-model-errors'][0]['step'],15)
        self.assertIn('Unsupported key',artifacts['mail-model-errors'][0]['errors'][0]['msg'])
        self.assertNotIn('input',artifacts['mail-model-errors'][0]['errors'][0])


class MailMessageIdentityTests(unittest.TestCase):
    def test_changed_observation_does_not_repeat_extracted_message(self):
        worker=MailAgent()
        worker.sources={'old':{'url':'https://mail.test/messages/approval'},
                        'new':{'url':'https://mail.test/?folder=inbox&message=approval'},
                        'other':{'url':'https://mail.test/messages/other'}}
        worker.extracted_sources={'old'}
        with self.assertRaises(ValueError):
            worker.validate_investigation_action(BrowserAction(operation='inspect',source_ids=['new']))
        with self.assertRaises(ValueError):
            worker.validate_investigation_action(BrowserAction(operation='goto',url='https://mail.test/messages/approval'))
        worker.validate_investigation_action(BrowserAction(operation='inspect',source_ids=['other']))

    def test_same_failed_action_is_bounded_but_different_return_is_allowed(self):
        worker=MailAgent()
        worker.navigator=SimpleNamespace(page=SimpleNamespace(url='https://mail.test/messages/one'))
        action=BrowserAction(operation='goto',url='https://mail.test/tools')
        worker.failed_actions[worker.action_signature(action)]=2
        with self.assertRaisesRegex(ValueError,'failed twice'):
            worker.validate_investigation_action(action)
        worker.validate_investigation_action(BrowserAction(operation='goto',url='https://mail.test/?q=RFQ-1'))



    def test_rejected_message_cannot_be_retried_under_another_observation(self):
        worker=MailAgent()
        worker.sources={'new':{'url':'https://mail.test/messages/wrong'}}
        worker.message_attempts={'wrong':{'rejected_attempts':2,'reasons':['reference_mismatch']}}
        with self.assertRaisesRegex(ValueError,'rejected twice'):
            worker.validate_investigation_action(BrowserAction(operation='inspect',source_ids=['new']))
        worker.validate_investigation_action(BrowserAction(operation='goto',url='https://mail.test/?q=RFQ-1+Konditionsregeln'))
