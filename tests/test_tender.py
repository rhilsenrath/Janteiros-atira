import json
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.base import AgentContext
from agents.tender import TenderAgent
from agents.website import AuthenticationStep
from core.config import RunConfig
from core.contracts import (AgentStatus, AgentTask, BidPosition, BidResult, Clarification, ClarificationChange,
    CommercialPolicies, Evidence, TenderData, TenderDate, TenderPosition, is_synthetic)
from core.state import RunState
from services.tender_resolution import parse_quantity, resolve_tender, withdrawn_answers
from tools.logging import RunLogger
from tools.navigation import BrowserAction
from test_architecture import RUNTIME, manager


def answer(identifier, day, *changes, withdraws=()):
    return Clarification(identifier=identifier, text=f'answer {identifier}', dated_at=datetime(2026, 9, day, 10),
        withdraws=list(withdraws), changes=[ClarificationChange(**c) for c in changes])


def tender(*clarifications, positions=None):
    positions = positions or [TenderPosition(position_id='10', original_quantity='100', original_unit='Stück',
                                             requirement_id='R-1'),
                              TenderPosition(position_id='20', original_quantity='4', original_unit='Satz',
                                             specification_reference='SPEC-B')]
    return TenderData(reference='UNIT-REF', timezone='Europe/Athens', positions=positions,
                      clarifications=list(clarifications), qa_complete=True)


def effective(data):
    return {p.position_id: p for p in data.positions}


class ResolutionTests(unittest.TestCase):
    def test_original_quantity_applies_without_answers(self):
        resolved, missing = resolve_tender(tender())
        self.assertEqual(effective(resolved)['10'].quantity, Decimal('100'))
        self.assertEqual(effective(resolved)['10'].quantity_basis, 'original')
        self.assertEqual(effective(resolved)['20'].unit, 'Satz')
        self.assertEqual(missing, [])

    def test_latest_answer_changes_quantity_and_keeps_original(self):
        data = tender(answer('A-02', 5, {'change_type': 'quantity', 'position_id': '10', 'value': '120'}),
                      answer('A-05', 9, {'change_type': 'quantity', 'position_id': '10', 'value': '1.250'}))
        position = effective(resolve_tender(data)[0])['10']
        self.assertEqual((position.original_quantity, position.quantity), (Decimal('100'), Decimal('1250')))
        self.assertEqual(position.quantity_basis, 'answer:A-05')
        trail = {a.answer: a for a in resolve_tender(data)[0].amendments}
        self.assertEqual((trail['A-02'].status, trail['A-02'].replaced_by), ('superseded', 'A-05'))
        self.assertEqual(trail['A-05'].status, 'effective')

    def test_withdrawn_answer_falls_back_to_latest_remaining_change(self):
        data = tender(answer('A-02', 5, {'change_type': 'quantity', 'position_id': '10', 'value': '120'}),
                      answer('A-05', 9, {'change_type': 'quantity', 'position_id': '10', 'value': '150'}),
                      answer('A-07', 11, withdraws=['A-05']))
        resolved = resolve_tender(data)[0]
        self.assertEqual(effective(resolved)['10'].quantity, Decimal('120'))
        withdrawn = {c.identifier: (c.withdrawn, c.withdrawn_by) for c in resolved.clarifications}
        self.assertEqual(withdrawn['A-05'], (True, 'A-07'))
        self.assertEqual(withdrawn['A-02'], (False, None))

    def test_withdrawing_the_only_change_restores_tender_quantity(self):
        data = tender(answer('A-05', 9, {'change_type': 'quantity', 'position_id': '10', 'value': '150'}),
                      answer('A-07', 11, withdraws=['a-05']))
        position = effective(resolve_tender(data)[0])['10']
        self.assertEqual((position.quantity, position.quantity_basis), (Decimal('100'), 'original'))

    def test_withdrawn_withdrawal_has_no_effect(self):
        clarifications = [answer('A-01', 1, {'change_type': 'quantity', 'position_id': '10', 'value': '90'}),
                          answer('A-02', 2, withdraws=['A-01']), answer('A-03', 3, withdraws=['A-02'])]
        self.assertEqual(withdrawn_answers(clarifications), {'a-02': 'A-03'})
        self.assertEqual(effective(resolve_tender(tender(*clarifications))[0])['10'].quantity, Decimal('90'))

    def test_commercial_keys_and_technical_amendments(self):
        data = tender(answer('A-03', 4, {'change_type': 'packaging_key', 'position_id': '10', 'value': 'VP-2'},
                                     {'change_type': 'delivery_key', 'position_id': '10', 'value': 'TF-9'},
                                     {'change_type': 'condition_group', 'position_id': '20', 'value': 'KG-B'},
                                     {'change_type': 'setup_pool', 'position_id': '20', 'value': 'POOL-1'},
                                     {'change_type': 'technical_amendment', 'position_id': '20',
                                      'specification_reference': 'SPEC-B', 'revision': 'C'}),
                      answer('A-04', 6, {'change_type': 'packaging_key', 'position_id': '10', 'value': 'VP-3'}),
                      answer('A-06', 8, withdraws=['A-04']))
        resolved = resolve_tender(data)[0]
        positions = effective(resolved)
        self.assertEqual((positions['10'].packaging_key, positions['10'].delivery_key), ('VP-2', 'TF-9'))
        self.assertEqual((positions['20'].condition_group, positions['20'].setup_pool), ('KG-B', 'POOL-1'))
        technical = [a for a in resolved.amendments if a.change_type == 'technical_amendment']
        self.assertEqual((technical[0].revision, technical[0].status), ('C', 'effective'))
        # Resolution is idempotent: a withdrawn key disappears even from previously resolved output.
        again = resolve_tender(resolved.model_copy(update={'positions': [
            p.model_copy(update={'packaging_key': 'VP-3'}) for p in resolved.positions]}))[0]
        self.assertEqual(effective(again)['10'].packaging_key, 'VP-2')
        self.assertEqual(resolve_tender(resolved)[0], resolved)

    def test_technical_amendments_replace_per_attribute_and_notes_accumulate(self):
        amend = lambda value: {'change_type': 'technical_amendment', 'position_id': '20', 'value': value,
                               'specification_reference': 'LH-1, A-2', 'revision': 'R-1'}
        data = tender(answer('A-10', 3, amend('Hub/Stellweg = 485 mm'), amend('Druck = 160 bar')),
                      answer('A-11', 4, amend('Hub/Stellweg = 482 mm'),
                             {'change_type': 'other', 'position_id': '20', 'value': 'note one'},
                             {'change_type': 'other', 'position_id': '20', 'value': 'note two'}))
        resolved, _ = resolve_tender(data)
        status = {(a.answer, a.value): a.status for a in resolved.amendments}
        self.assertEqual(status[('A-10', 'Hub/Stellweg = 485 mm')], 'superseded')
        self.assertEqual(status[('A-10', 'Druck = 160 bar')], 'effective')
        self.assertEqual(status[('A-11', 'Hub/Stellweg = 482 mm')], 'effective')
        self.assertEqual(status[('A-11', 'note one')], status[('A-11', 'note two')])
        self.assertEqual(resolved.conflicts, [])
        undated = tender(answer('A-1', 1, {'change_type': 'quantity', 'position_id': '10', 'value': '5'}),
                         Clarification(identifier='A-2', text='x', changes=[ClarificationChange(
                             change_type='quantity', position_id='10', value='6')]))
        self.assertTrue(any('publication dates' in m.question for m in resolve_tender(undated)[1]))

    def test_records_after_scenario_date_or_for_other_references_do_not_apply(self):
        data = tender(answer('A-02', 5, {'change_type': 'quantity', 'position_id': '10', 'value': '120'}),
                      answer('A-05', 20, {'change_type': 'quantity', 'position_id': '10', 'value': '150'}),
                      answer('A-06', 21, withdraws=['A-02']),
                      answer('A-07', 6, {'change_type': 'quantity', 'position_id': '20', 'value': '9',
                                         'tender_reference': 'VG-2027-9999'}))
        data = data.model_copy(update={'scenario_date': datetime(2026, 9, 10, tzinfo=None)})
        resolved, _ = resolve_tender(data)
        positions = effective(resolved)
        self.assertEqual(positions['10'].quantity, Decimal('120'))  # A-05 is future-dated, A-06 cannot withdraw
        self.assertEqual(positions['20'].quantity, Decimal('4'))    # A-07 names another tender
        trail = {a.answer: a for a in resolved.amendments}
        self.assertEqual(trail['A-05'].status, 'not_applicable')
        self.assertIn('scenario date', trail['A-05'].reason)
        self.assertIn('VG-2027-9999', trail['A-07'].reason)
        self.assertFalse({c.identifier: c.withdrawn for c in resolved.clarifications}['A-02'])
        # Same reference written differently still applies.
        same = tender(answer('A-08', 6, {'change_type': 'quantity', 'position_id': '20', 'value': '7',
                                         'tender_reference': 'vg 2027 unit ref'}))
        same = same.model_copy(update={'reference': 'VG-2027-UNIT-REF'})
        self.assertEqual(effective(resolve_tender(same)[0])['20'].quantity, Decimal('7'))

    def test_membership_bonus_plan_and_setup_fee(self):
        data = tender(answer('A-01', 3, {'change_type': 'setup_pool', 'position_id': '10', 'value': 'RP-1'},
                                     {'change_type': 'bonus_plan', 'position_id': '10', 'value': 'PB-7'},
                                     {'change_type': 'setup_fee', 'position_id': '10', 'value': '57,50 EUR'}),
                      answer('A-02', 4, {'change_type': 'setup_pool', 'position_id': '20', 'applies': False},
                                     {'change_type': 'bonus_plan', 'position_id': '20', 'value': 'Kein Projektbonus'}))
        positions = effective(resolve_tender(data)[0])
        self.assertEqual((positions['10'].setup_pool, positions['10'].setup_pool_member), ('RP-1', True))
        self.assertEqual((positions['10'].bonus_plan, positions['10'].bonus_member), ('PB-7', True))
        self.assertEqual(positions['10'].setup_fee, Decimal('57.50'))
        self.assertEqual((positions['20'].setup_pool, positions['20'].setup_pool_member), (None, False))
        self.assertEqual((positions['20'].bonus_plan, positions['20'].bonus_member), (None, False))
        self.assertIsNone(positions['20'].setup_fee)
        withdrawn = tender(*data.clarifications, answer('A-03', 5, withdraws=['A-01']))
        self.assertIsNone(effective(resolve_tender(withdrawn)[0])['10'].setup_fee)
        self.assertIsNone(effective(resolve_tender(withdrawn)[0])['10'].setup_pool_member)

    def test_scenario_date_comes_from_the_brief(self):
        from agents.tender import scenario_date_from_brief
        agent = TenderAgent(); agent.brief = 'Prepare a bid. The scenario date is 2027-01-25T09:00:00Z; ...'
        output, _ = agent.finalize(tender())
        self.assertEqual(output.scenario_date, scenario_date_from_brief(agent.brief))
        self.assertEqual(output.scenario_date.utcoffset().total_seconds(), 0)

    def test_missing_and_inconsistent_information_is_explicit(self):
        data = tender(answer('A-02', 5, {'change_type': 'quantity', 'position_id': '99', 'value': '3'}),
                      answer('A-03', 6, withdraws=['A-01']),
                      positions=[TenderPosition(position_id='10')])
        resolved, missing = resolve_tender(data)
        self.assertIsNone(effective(resolved)['10'].quantity)
        questions = ' '.join(m.question for m in missing)
        self.assertIn('original quantity', questions); self.assertIn('unit', questions)
        self.assertIn('A-01', questions)
        self.assertTrue(any('unknown position 99' in c for c in resolved.conflicts))

    def test_dates_are_localized_to_the_portal_timezone_and_numbers_parsed(self):
        data = tender().model_copy(update={'dates': [TenderDate(label='Angebotsfrist', text='20.10.2026 12:00',
                                                                at=datetime(2026, 10, 20, 12))]})
        self.assertEqual(resolve_tender(data)[0].dates[0].at.utcoffset().total_seconds(), 3 * 3600)
        for text, value in (('1.200', '1200'), ('2,5', '2.5'), ('1 500 Stück', '1500'), ('12', '12')):
            self.assertEqual(parse_quantity(text), Decimal(value))
        self.assertIsNone(parse_quantity('unbekannt'))


class PortalBrowser:
    """Observations stand in for live pages; the model chooses every step."""
    pages = {
        'detail': 'Vergabenummer UNIT-REF Titel Zylinder Pos. 10 Menge 100 Stück Anforderung R-1',
        'qa': 'Antwort A-05 vom 09.09.2026: Pos. 10 neue Menge 150 Stück. '
              'Antwort A-07 vom 11.09.2026: Antwort A-05 wird zurückgezogen.',
    }

    def __init__(self, page, site, secrets=(), **kwargs):
        self.site = site; self.page = SimpleNamespace(url=site.url, set_default_timeout=lambda _: None)
        self.current = 'detail'; self.actions = []

    async def initialize(self): pass

    async def observe(self, offset=0):
        return {'url': f'{self.site.url}/{self.current}', 'frames': [{'text': self.pages[self.current],
                'controls': []}], 'tabs': [], 'downloads': []}

    async def act(self, action, **kwargs):
        self.actions.append(action.operation)
        if action.operation == 'click':
            self.current = 'qa'


class PortalModel:
    def __init__(self): self.step = 0; self.extractions = []

    async def ask(self, system, user, *, output_type=None, tools=None):
        data = json.loads(user)
        if output_type is AuthenticationStep:
            answer = {'action': {'operation': 'finish'}, 'authenticated': True}
        elif output_type is BrowserAction:
            self.step += 1
            answer = [{'operation': 'inspect', 'source_ids': [data['source_id']]},
                      {'operation': 'click', 'target': 'f0:e1'},
                      {'operation': 'finish', 'source_ids': [data['source_id']]}][self.step - 1]
        else:
            self.extractions.append(data)
            qa = [s for s in data['sources'] if 'Antwort' in s['text']]
            source = qa[0] if qa else data['sources'][0]
            if not qa:
                answer = {'output': {'reference': 'UNIT-REF', 'positions': [{'position_id': '10',
                          'original_quantity': '100', 'original_unit': 'Stück', 'requirement_id': 'R-1'}]},
                          'citations': [{'source_id': source['source_id'], 'excerpt': 'Vergabenummer UNIT-REF'},
                                        {'source_id': source['source_id'], 'excerpt': 'Pos. 10 Menge 100 Stück',
                                         'reference': '10'}]}
            else:
                # Restates only the effective quantity; the agent must keep the verified original.
                answer = {'status': 'complete', 'output': {'reference': 'UNIT-REF', 'qa_complete': True,
                    'positions': [{'position_id': '10', 'quantity': '150', 'unit': 'Stück', 'requirement_id': 'R-1'}],
                    'clarifications': [
                        {'identifier': 'A-05', 'text': 'Pos. 10 neue Menge 150 Stück.', 'dated_at': '2026-09-09',
                         'changes': [{'change_type': 'quantity', 'position_id': '10', 'value': '150', 'unit': 'Stück'}]},
                        {'identifier': 'A-07', 'text': 'Antwort A-05 wird zurückgezogen.', 'dated_at': '2026-09-11',
                         'withdraws': ['A-05']}]},
                    'citations': [{'source_id': source['source_id'], 'reference': 'A-05',
                                   'excerpt': 'Antwort A-05 vom 09.09.2026: Pos. 10 neue Menge 150 Stück.'},
                                  {'source_id': source['source_id'], 'reference': 'A-07',
                                   'excerpt': 'Antwort A-07 vom 11.09.2026: Antwort A-05 wird zurückgezogen.'}]}
        return SimpleNamespace(structured=output_type.model_validate(answer))


class TenderAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_publication_and_withdrawal_resolution_with_provenance(self):
        reports = []; logs = []
        async def report(result): reports.append(result)
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=5), page=object(), llm=PortalModel(),
                               access_manager=manager(), logger=RunLogger(logs.append), report=report)
        with patch('agents.website.BrowserNavigator', PortalBrowser):
            result = await TenderAgent().execute(AgentTask(), context)
        # The reference was published before the investigation finished, so Mail could start.
        self.assertEqual(reports[0].output.reference, 'UNIT-REF')
        self.assertEqual(reports[0].status, AgentStatus.partial)
        position = result.output.positions[0]
        self.assertEqual((position.original_quantity, position.quantity), (Decimal('100'), Decimal('100')))
        self.assertEqual(position.quantity_basis, 'original')
        answers = {c.identifier: c for c in result.output.clarifications}
        self.assertTrue(answers['A-05'].withdrawn)
        self.assertEqual([e.reference for e in answers['A-07'].evidence], ['A-07'])
        self.assertTrue(all(e.source_url.endswith('/qa') for e in answers['A-05'].evidence))
        # Configurator not inspected yet: explicitly partial with a targeted question, never complete.
        self.assertEqual(result.status, AgentStatus.partial)
        self.assertTrue(any('configurator' in m.question for m in result.missing))
        state = RunState({}, RunConfig())
        for item in reports: state.collect(item)
        self.assertEqual(state.tender.positions[0].quantity, Decimal('100'))

    async def test_submit_bid_refuses_incomplete_mismatched_or_unsigned_bids(self):
        agent = TenderAgent()
        data = resolve_tender(tender())[0]
        evidence = [Evidence(id='tender:x', agent_id='tender', source_site='0')]
        good = BidResult(complete=True, decision='bid', tender_reference='UNIT-REF', evidence=evidence,
                         positions=[BidPosition(position='10', material_number='M', quantity='100')])
        context = AgentContext(RUNTIME, RunConfig(enable_submission=True))
        self.assertEqual(agent.bid_issues(good, data), [])
        cases = [(BidResult(), 'incomplete'),
                 (good.model_copy(update={'tender_reference': 'OTHER'}), 'reference'),
                 (good.model_copy(update={'positions': [good.positions[0].model_copy(update={'quantity': Decimal('5')})]}), 'differs'),
                 (good.model_copy(update={'mock': True}), 'Synthetic')]
        for bid, reason in cases:
            result = await agent.execute(AgentTask(action='submit', bid=bid, tender=data,
                commercial=CommercialPolicies(signing_token='123456')), context)
            self.assertEqual(result.status, AgentStatus.blocked); self.assertIn(reason, result.error)
        unsigned = await agent.submit_bid(good, None, context, tender=data)
        self.assertIn('TAN', unsigned.error)
        disabled = await agent.submit_bid(good, CommercialPolicies(signing_token='1').signing_token,
                                          AgentContext(RUNTIME, RunConfig()), tender=data)
        self.assertIn('not explicitly enabled', disabled.error)
        ready = await agent.submit_bid(good, CommercialPolicies(signing_token='1').signing_token, context, tender=data)
        self.assertEqual(ready.status, AgentStatus.blocked); self.assertIn('not implemented', ready.error)

    async def test_incomplete_finish_is_sent_back_while_steps_remain(self):
        class EarlyFinish(PortalModel):
            async def ask(self, system, user, *, output_type=None, tools=None):
                data = json.loads(user)
                if output_type is BrowserAction:
                    self.step += 1
                    self.feedback = getattr(self, 'feedback', []) + [data.get('feedback')]
                    answer = {'operation': 'finish', 'source_ids': [data['source_id']]}
                    return SimpleNamespace(structured=BrowserAction.model_validate(answer))
                return await super().ask(system, user, output_type=output_type, tools=tools)
        model = EarlyFinish(); logs = []
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=12), page=object(), llm=model,
                               access_manager=manager(), logger=RunLogger(logs.append))
        with patch('agents.website.BrowserNavigator', PortalBrowser):
            result = await TenderAgent().execute(AgentTask(), context)
        self.assertEqual(model.step, TenderAgent.max_premature_finishes + 1)
        self.assertEqual(model.feedback[1]['finish_rejected'][:13], 'Investigation')
        self.assertTrue(model.feedback[1]['still_missing'])
        self.assertEqual(result.status, AgentStatus.partial)
        self.assertEqual(sum(json.loads(l)['event'] == 'finish_rejected' for l in logs), 3)

    async def test_identical_action_on_unchanged_page_is_blocked_after_two_attempts(self):
        class Repeating(PortalModel):
            async def ask(self, system, user, *, output_type=None, tools=None):
                data = json.loads(user)
                if output_type is BrowserAction:
                    self.feedback = getattr(self, 'feedback', []) + [data.get('feedback')]
                    return SimpleNamespace(structured=BrowserAction(operation='scroll'))
                return await super().ask(system, user, output_type=output_type, tools=tools)
        model = Repeating(); logs = []
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=4), page=object(), llm=model,
                               access_manager=manager(), logger=RunLogger(logs.append))
        with patch('agents.website.BrowserNavigator', PortalBrowser):
            await TenderAgent().execute(AgentTask(), context)
        self.assertIn('repetition_blocked', model.feedback[3])
        self.assertEqual(sum(json.loads(l)['event'] == 'repetition_blocked' for l in logs), 2)

    async def test_unchanged_content_is_not_extracted_twice(self):
        class Reinspecting(PortalModel):
            async def ask(self, system, user, *, output_type=None, tools=None):
                data = json.loads(user)
                if output_type is BrowserAction:
                    self.feedback = getattr(self, 'feedback', []) + [data.get('feedback')]
                    return SimpleNamespace(structured=BrowserAction(operation='inspect', source_ids=[data['source_id']]))
                return await super().ask(system, user, output_type=output_type, tools=tools)
        model = Reinspecting()
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=3), page=object(), llm=model,
                               access_manager=manager())
        with patch('agents.website.BrowserNavigator', PortalBrowser):
            await TenderAgent().execute(AgentTask(), context)
        self.assertEqual(len(model.extractions), 1)
        self.assertIn('already_extracted', model.feedback[2])

    async def test_focused_reinspection_extracts_again_with_focus(self):
        class Refocusing(PortalModel):
            async def ask(self, system, user, *, output_type=None, tools=None):
                data = json.loads(user)
                if output_type is BrowserAction:
                    self.step += 1
                    actions = [BrowserAction(operation='inspect', source_ids=[data['source_id']]),
                               BrowserAction(operation='inspect', source_ids=[data['source_id']]),
                               BrowserAction(operation='inspect', source_ids=[data['source_id']],
                                             value='publication date of answer A-05')]
                    return SimpleNamespace(structured=actions[min(self.step, 3) - 1])
                return await super().ask(system, user, output_type=output_type, tools=tools)
        model = Refocusing()
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=3), page=object(), llm=model,
                               access_manager=manager())
        with patch('agents.website.BrowserNavigator', PortalBrowser):
            await TenderAgent().execute(AgentTask(), context)
        self.assertEqual(len(model.extractions), 2)  # plain repeat skipped, focused repeat extracted
        self.assertEqual(model.extractions[1]['focus'], 'publication date of answer A-05')

    def test_invisible_characters_and_served_document_names(self):
        from tools.navigation import clean, document_name
        self.assertEqual(clean('V\u200bG\u200b-2027\u2060-7093'), 'VG-2027-7093')
        self.assertTrue(document_name('https://p/tenders/X/documents/0', {'content-type': 'text/csv'}).endswith('.csv'))
        self.assertEqual(document_name('https://p/d/1', {'content-disposition': 'attachment; filename="LV.zip"'}),
                         'LV.zip')

    async def test_incremental_extraction_sends_only_new_sources_and_compact_previous_output(self):
        model = PortalModel()
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=5), page=object(), llm=model,
                               access_manager=manager())
        with patch('agents.website.BrowserNavigator', PortalBrowser):
            result = await TenderAgent().execute(AgentTask(), context)
        first, second = model.extractions
        self.assertEqual(len(second['sources']), 1)
        self.assertIn('Antwort', second['sources'][0]['text'])
        self.assertNotIn('evidence', json.dumps(second['previous_output']))
        # Facts from the first extraction survive the merge although only new sources were sent.
        self.assertEqual(result.output.reference, 'UNIT-REF')
        self.assertEqual(result.output.positions[0].original_quantity, Decimal('100'))

    async def test_same_site_frame_url_becomes_navigable(self):
        from tools.navigation import BrowserNavigator
        site = manager().for_role('tender')
        body = SimpleNamespace(inner_text=AsyncMock(return_value='Bieterfragen'))
        def frame(url):
            return SimpleNamespace(url=url, locator=lambda selector: SimpleNamespace(
                inner_text=body.inner_text, evaluate_all=AsyncMock(return_value=[])))
        main, qa, foreign = frame(site.url + '/t/1'), frame(site.url + '/t/1/questions-content'), frame('https://evil.invalid/x')
        page = SimpleNamespace(url=main.url, frames=[main, qa, foreign], on=lambda *a: None,
                               context=SimpleNamespace(pages=[]))
        page.context.pages = [page]
        navigator = BrowserNavigator(page, site)
        await navigator.observe()
        self.assertIn(qa.url, navigator.links)
        self.assertNotIn(foreign.url, navigator.links)

    async def test_loaded_content_is_extracted_before_leaving_the_page(self):
        class LoadMoreBrowser(PortalBrowser):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs); self.current = 'qa_first'
                self.pages = {**PortalBrowser.pages,
                    'qa_first': 'Antwort A-05 vom 09.09.2026: Pos. 10 neue Menge 150 Stück.'}
            async def act(self, action, **kwargs):
                self.actions.append(action.operation)
                if action.operation == 'click':
                    self.current = 'qa'  # "Weitere laden": same URL, more answers
            async def observe(self, offset=0):
                return {'url': f'{self.site.url}/questions', 'frames': [{'text': self.pages[self.current],
                        'controls': []}], 'tabs': [], 'downloads': []}
        class LoadThenLeave(PortalModel):
            async def ask(self, system, user, *, output_type=None, tools=None):
                data = json.loads(user)
                if output_type is BrowserAction:
                    self.step += 1
                    action = [BrowserAction(operation='click', target='f0:e1'), BrowserAction(operation='back')]
                    return SimpleNamespace(structured=action[min(self.step, 2) - 1])
                return await super().ask(system, user, output_type=output_type, tools=tools)
        model = LoadThenLeave(); logs = []
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=3), page=object(), llm=model,
                               access_manager=manager(), logger=RunLogger(logs.append))
        with patch('agents.website.BrowserNavigator', LoadMoreBrowser):
            result = await TenderAgent().execute(AgentTask(), context)
        events = [json.loads(line)['event'] for line in logs]
        self.assertEqual(events.count('auto_extracted'), 1)
        self.assertIn('A-07', {c.identifier for c in result.output.clarifications})

    async def test_back_skips_login_pages_and_visited_pages_are_navigable(self):
        from tools.navigation import BrowserNavigator
        site = manager().for_role('tender')
        history = [site.url + '/tenders/X', site.url + '/login', site.url + '/consent', site.url + '/tenders/X/doc']
        class Page:
            url = history[-1]; frames = []
            def on(self, *a): pass
            async def go_back(self, **kwargs):
                history.pop(); Page.url = history[-1]; return object()
            async def goto(self, url, **kwargs): Page.url = url
        page = Page(); page.context = SimpleNamespace(pages=[page])
        navigator = BrowserNavigator(page, site)
        navigator.auth_paths = {'/login', '/consent'}
        navigator.visited = [site.url + '/tenders/X']
        await navigator.act(BrowserAction(operation='back'))
        self.assertEqual(Page.url, site.url + '/tenders/X')
        await navigator.act(BrowserAction(operation='goto', url=site.url + '/tenders/X'))
        with self.assertRaises(ValueError):
            await navigator.act(BrowserAction(operation='goto', url=site.url + '/never-seen'))

    async def test_closed_tab_is_replaced_by_a_new_tab_on_the_portal(self):
        from tools.navigation import BrowserNavigator
        site = manager().for_role('tender')
        fresh = SimpleNamespace(url='about:blank', frames=[], on=lambda *a: None, is_closed=lambda: False)
        async def goto(url, **kwargs): fresh.url = url
        fresh.goto = goto
        context = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=fresh))
        fresh.context = context
        dead = SimpleNamespace(url=site.url, frames=[], on=lambda *a: None, is_closed=lambda: True, context=context)
        context.pages = [dead]
        navigator = BrowserNavigator(dead, site)
        navigator.visited = [site.url + '/tenders/X']
        observation = await navigator.observe()
        self.assertIs(navigator.page, fresh)
        self.assertEqual(observation['url'], site.url + '/tenders/X')

    def test_outcome_memory_warns_then_blocks_trap_controls(self):
        agent = TenderAgent(); logs = []
        context = AgentContext(RUNTIME, RunConfig(), logger=RunLogger(logs.append))
        trap = {'tag': 'a', 'label': 'Unterlagen herunterladen', 'href': 'https://p/logout-trap'}
        good = {'tag': 'a', 'label': 'Unterlagen', 'href': 'https://p/docs'}
        agent.navigator = SimpleNamespace(targets={'f0:e1': (None, 1, trap), 'f0:e2': (None, 2, good)})
        click = lambda target: BrowserAction(operation='click', target=target)
        key = agent.outcome_key(click('f0:e1'))
        page = {'url': 'https://p/t', 'frames': [{'text': 'Tender'}]}
        pending = (key, 'https://p/t', agent.content(page))
        agent.judge(context, pending, page, logged_out=True)
        # One logout can be an ordinary session expiry: warn, but do not block yet.
        self.assertFalse(agent.untrusted(key))
        self.assertEqual(agent.warnings(), [{'target': 'f0:e1', 'label': 'Unterlagen herunterladen',
                                             'history': ['logged_out']}])
        agent.judge(context, pending, page, logged_out=True)
        self.assertTrue(agent.untrusted(key))
        self.assertFalse(agent.untrusted(agent.outcome_key(click('f0:e2'))))
        # Error pages and controls without any effect are recognised generically.
        error = ('url', 'https://p/fake')
        agent.judge(context, (error, 'https://p/t', 0), {**page, 'url': 'https://p/fake', 'http_status': 404}, False)
        agent.judge(context, (error, 'https://p/t', 0), {**page, 'url': 'https://p/fake', 'http_status': 500}, False)
        self.assertTrue(agent.untrusted(error))
        dead = ('control', 'button', 'Weiter', None)
        for _ in range(3):
            agent.judge(context, (dead, 'https://p/t', agent.content(page)), page, False)
        self.assertTrue(agent.untrusted(dead))
        self.assertIn('error_page', [json.loads(l)['operation'] for l in logs])

    def test_fully_extracted_pages_are_not_revisited_and_progress_is_compact(self):
        agent = TenderAgent()
        page = {'url': 'https://p/t', 'frames': [{'text': 'Tender detail', 'overlay_text': None}]}
        agent.page_log['https://p/t'] = {'visits': 2, 'extracted': True, 'added_facts': True,
                                         'content': agent.content(page)}
        self.assertFalse(agent.exhausted('https://p/t'))  # content not extracted yet
        agent.extracted_pages.add(agent.content(page))
        self.assertTrue(agent.exhausted('https://p/t'))
        self.assertTrue(agent.page_overview()[0]['fully_covered'])
        # Overlay text (random notices) does not make a page look new.
        noisy = {'url': 'https://p/t', 'frames': [{'text': 'Tender detail\nNotice', 'overlay_text': 'Notice'}]}
        self.assertEqual(agent.content(noisy), agent.content(page))
        context = agent.task_context(AgentTask(tender=resolve_tender(tender())[0]))
        self.assertEqual(context['found_so_far']['reference'], 'UNIT-REF')
        self.assertNotIn('evidence', json.dumps(context))

    async def test_model_cannot_mark_extraction_synthetic_or_supply_evidence(self):
        class MockFlagging(PortalModel):
            async def ask(self, system, user, *, output_type=None, tools=None):
                if output_type is AuthenticationStep:
                    return await super().ask(system, user, output_type=output_type, tools=tools)
                if output_type is BrowserAction:
                    data = json.loads(user)
                    return SimpleNamespace(structured=BrowserAction(operation='finish', source_ids=[data['source_id']]))
                schema = json.dumps(output_type.model_json_schema())
                self.schema_has_application_fields = '"mock"' in schema or '"evidence"' in schema
                source = json.loads(user)['sources'][0]
                fabricated = {'id': 'invented', 'agent_id': 'tender', 'source_site': 'x', 'mock': True}
                answer = {'output': {'reference': 'UNIT-REF', 'evidence': [fabricated], 'positions': [
                              {'position_id': '10', 'original_quantity': '100', 'evidence': [fabricated]}]},
                          'citations': [{'source_id': source['source_id'], 'excerpt': 'Vergabenummer UNIT-REF'}]}
                return SimpleNamespace(structured=output_type.model_validate(answer))
        model = MockFlagging()
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=1), page=object(), llm=model,
                               access_manager=manager())
        with patch('agents.website.BrowserNavigator', PortalBrowser):
            result = await TenderAgent().execute(AgentTask(), context)
        self.assertFalse(model.schema_has_application_fields)
        self.assertEqual(result.output.reference, 'UNIT-REF')
        self.assertFalse(is_synthetic(result))
        self.assertTrue(all(e.id != 'invented' for e in result.output.positions[0].evidence))

    async def test_investigation_ends_when_objective_met_or_stalled(self):
        class Endless(PortalModel):
            async def ask(self, system, user, *, output_type=None, tools=None):
                if output_type is BrowserAction:
                    self.step += 1
                    data = json.loads(user)
                    if self.step == 1:
                        return SimpleNamespace(structured=BrowserAction(operation='inspect', source_ids=[data['source_id']]))
                    return SimpleNamespace(structured=BrowserAction(operation='scroll'))
                return await super().ask(system, user, output_type=output_type, tools=tools)
        class Done(TenderAgent):
            def objective_met(self): return self.latest is not None
        logs = []
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=50), page=object(), llm=Endless(),
                               access_manager=manager(), logger=RunLogger(logs.append))
        with patch('agents.website.BrowserNavigator', PortalBrowser):
            result = await Done().execute(AgentTask(), context)
        self.assertEqual(result.status, AgentStatus.complete)
        self.assertIn('objective_complete', [json.loads(l)['event'] for l in logs])
        self.assertLess(context.llm.step, 5)
        # Core facts covered but nothing new keeps coming: stop after stall_steps instead of the step limit.
        class Stalling(TenderAgent):
            stall_steps = 3
            def core_covered(self): return self.latest is not None
        logs.clear(); model = Endless()
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=50), page=object(), llm=model,
                               access_manager=manager(), logger=RunLogger(logs.append))
        with patch('agents.website.BrowserNavigator', PortalBrowser):
            result = await Stalling().execute(AgentTask(), context)
        self.assertEqual(result.status, AgentStatus.partial)
        self.assertIn('No new facts', result.error)
        self.assertLess(model.step, 10)

    def test_reload_is_a_validated_navigation_action(self):
        self.assertEqual(BrowserAction(operation='reload').operation, 'reload')
        self.assertEqual(BrowserAction(operation='expand').operation, 'expand')
        self.assertEqual(BrowserAction(operation='reveal', target='f0:e1').target, 'f0:e1')
        with self.assertRaises(ValueError):
            BrowserAction(operation='reveal')


if __name__ == '__main__':
    unittest.main()
