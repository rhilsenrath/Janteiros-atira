import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from agents.base import AgentContext
from agents.supplier import SupplierAgent
from agents.website import AuthenticationStep
from core.config import RunConfig
from core.contracts import (AgentResult, AgentStatus, AgentTask, CommercialPolicies, CommercialRule,
    SpecificationRevision, TechnicalRequirement, TechnicalRequirements, TenderAmendment, TenderData, TenderPosition)
from services.specification_resolution import resolve_specifications
from tools.navigation import BrowserAction
from test_architecture import RUNTIME, manager

SPEC = 'LH-VG-2027-7093'


def tender(*amendments):
    return TenderData(reference='VG-2027-7093', scenario_date=datetime(2027, 1, 25, 9, tzinfo=timezone.utc),
        positions=[TenderPosition(position_id='01.02', quantity='29', unit='Stk', requirement_id='A-2',
                                  specification_reference=SPEC),
                   TenderPosition(position_id='01.05', quantity='26', unit='Satz', requirement_id='A-5',
                                  specification_reference=SPEC)],
        amendments=list(amendments))


def amendment(answer, day, value, revision='R-6156', status='effective', reference=f'{SPEC}, A-2'):
    return TenderAmendment(amendment_id=f'{answer}:x', change_type='technical_amendment', answer=answer,
        dated_at=datetime(2027, 1, day, 8, tzinfo=timezone.utc), value=value, revision=revision,
        specification_reference=reference, position_id='01.02', status=status)


def requirement(rid, revision, **values):
    base = {'catalogue_group': 'HZ-DW', 'bore': '63', 'stroke': '500', 'pressure': '160', 'mounting': 'Flansch',
            'certification': 'CE', 'stroke_kind': 'nominal'}
    return TechnicalRequirement(requirement_id=rid, revision=revision, specification_id=SPEC, **{**base, **values})


def specification(*requirements, current='R-6156'):
    return TechnicalRequirements(
        revisions=[SpecificationRevision(specification_id=SPEC, revision='R-1001', status='ersetzt'),
                   SpecificationRevision(specification_id=SPEC, revision='R-6156',
                                         status='aktuell' if current == 'R-6156' else 'Entwurf')],
        requirements=list(requirements))


def by_id(data):
    return {r.requirement_id: r for r in data.requirements}


class ResolutionTests(unittest.TestCase):
    def test_explicitly_current_revision_governs_and_other_revisions_are_kept_apart(self):
        data = specification(requirement('A-2', 'R-1001', bore='50'), requirement('A-2', 'R-6156'),
                             requirement('A-5', 'R-6156', pieces_per_set='2'))
        resolved, missing = resolve_specifications(data, tender())
        self.assertEqual((resolved.governing_specification, resolved.governing_revision), (SPEC, 'R-6156'))
        self.assertEqual(by_id(resolved)['A-2'].bore, Decimal('63'))
        self.assertEqual([r.revision for r in resolved.other_requirements], ['R-1001'])
        self.assertEqual(by_id(resolved)['A-2'].position_ids, ['01.02'])
        self.assertEqual(missing, [])

    def test_no_explicit_marking_means_no_governing_revision(self):
        data = specification(requirement('A-2', 'R-6156'), current='none')
        resolved, missing = resolve_specifications(data, tender())
        self.assertIsNone(resolved.governing_revision)
        self.assertEqual(resolved.requirements, [])
        self.assertTrue(any('revision history' in m.question for m in missing))

    def test_most_recent_valid_revision_governs(self):
        day = lambda d: datetime(2026, 12, d, tzinfo=timezone.utc)
        revision = lambda label, status, d: SpecificationRevision(specification_id=SPEC, revision=label,
                                                                  status=status, dated_at=day(d))
        data = TechnicalRequirements(revisions=[
            revision('R-9711', 'ersetzt', 20), revision('R-4034', 'gültig', 5), revision('R-6156', 'gültig', 12),
            revision('R-7000', 'ungültig', 25), revision('R-8000', 'nicht gültig', 26)],
            requirements=[requirement('A-2', 'R-6156'), requirement('A-3', 'R-6156'), requirement('A-2', 'R-4034')])
        resolved, _ = resolve_specifications(data, tender())
        self.assertEqual(resolved.governing_revision, 'R-6156')
        self.assertEqual(sorted(r.requirement_id for r in resolved.requirements), ['A-2', 'A-3'])  # all of it
        self.assertEqual([r.revision for r in resolved.other_requirements], ['R-4034'])
        # A valid revision dated after the scenario date (2027-01-25) does not count.
        future = data.model_copy(update={'revisions': [*data.revisions, SpecificationRevision(
            specification_id=SPEC, revision='R-9999', status='gültig', dated_at=datetime(2027, 3, 1, tzinfo=timezone.utc))]})
        self.assertEqual(resolve_specifications(future, tender())[0].governing_revision, 'R-6156')
        # Several valid revisions without dates cannot be ordered: ask instead of guessing.
        undated = data.model_copy(update={'revisions': [r.model_copy(update={'dated_at': None}) for r in data.revisions]})
        resolved, missing = resolve_specifications(undated, tender())
        self.assertIsNone(resolved.governing_revision)
        self.assertTrue(any('not all are dated' in c for c in resolved.conflicts))

    def test_scoped_amendments_latest_per_attribute_for_governing_revision_only(self):
        t = tender(amendment('8149', 22, 'Hub/Stellweg = 485 mm', status='superseded'),
                   amendment('8150', 23, 'Hub/Stellweg = 482 mm'),
                   amendment('8170', 24, 'Druck = 250 bar', revision='R-1001'),   # other revision
                   amendment('8171', 24, 'Druck = 200 bar', status='withdrawn'),  # withdrawn
                   amendment('8172', 21, 'Befestigung = Fuß', reference=f'{SPEC}, A-4'))  # other requirement
        resolved, _ = resolve_specifications(specification(requirement('A-2', 'R-6156')), t)
        a2 = by_id(resolved)['A-2']
        self.assertEqual(a2.stroke, Decimal('482'))
        self.assertEqual((a2.pressure, a2.mounting, a2.bore), (Decimal('160'), 'Flansch', Decimal('63')))
        self.assertEqual(a2.original_values['stroke'], '500')
        self.assertEqual(a2.applied_amendments, ['8150:Hub/Stellweg=482 mm'])
        again, _ = resolve_specifications(resolved, t)
        self.assertEqual(by_id(again)['A-2'], a2)  # idempotent

    def test_amendment_naming_only_the_specification_applies_via_linked_positions(self):
        t = tender(amendment('8150', 23, 'Hub/Stellweg = 482 mm', reference=SPEC),
                   amendment('8151', 24, 'Druck = 999 bar', reference='LH-OTHER-1'))
        resolved, _ = resolve_specifications(specification(requirement('A-2', 'R-6156')), t)
        a2 = by_id(resolved)['A-2']
        self.assertEqual((a2.stroke, a2.pressure), (Decimal('482'), Decimal('160')))

    def test_requirement_kept_apart_earlier_is_not_duplicated(self):
        data = specification(requirement('A-2', 'R-6156', stroke_kind='nutzbarer Weg'))
        early = data.model_copy(update={'requirements': [], 'other_requirements': data.requirements,
                                        'revisions': []})
        resolved_early, _ = resolve_specifications(early, tender())
        self.assertEqual(len(resolved_early.other_requirements), 1)
        later = resolved_early.model_copy(update={'revisions': data.revisions,
                                                  'requirements': [requirement('A-2', 'R-6156', stroke_kind='nutzbarer Weg')]})
        resolved, _ = resolve_specifications(later, tender())
        self.assertEqual([r.requirement_id for r in resolved.requirements], ['A-2'])
        self.assertEqual(resolved.other_requirements, [])
        self.assertEqual(by_id(resolved)['A-2'].stroke_kind, 'nutzbarer Weg')

    def test_usable_travel_needs_technikfreigabe_reserve(self):
        t = tender(amendment('8150', 23, 'Hub/Stellweg = 482 mm'))
        data = specification(requirement('A-2', 'R-6156', stroke='490', stroke_kind='nutzbarer Weg'))
        resolved, missing = resolve_specifications(data, t)
        self.assertIsNone(by_id(resolved)['A-2'].stroke)
        self.assertEqual(by_id(resolved)['A-2'].usable_travel, Decimal('482'))
        self.assertTrue(any(m.agent_id == 'mail' and 'R-6156' in m.question for m in missing))
        approvals = CommercialPolicies(rules=[
            CommercialRule(key='m1', kind='Technikfreigabe', subject='Technikfreigabe R-6156',
                           text='Für R-6156 gilt eine Reserve je Ende von 9 mm.', authorized=True,
                           tender_reference='VG-2027-7093', effective_at=datetime(2027, 1, 20, tzinfo=timezone.utc)),
            CommercialRule(key='m2', kind='Technikfreigabe', subject='Technikfreigabe R-6156',
                           text='Reserve je Ende 4 mm für R-6156.', authorized=True, tender_reference='VG-2027-7093',
                           effective_at=datetime(2027, 1, 22, tzinfo=timezone.utc)),
            CommercialRule(key='m3', kind='Technikfreigabe', subject='Technikfreigabe R-6156',
                           text='Reserve je Ende 20 mm für R-6156.', authorized=True,
                           tender_reference='VG-2027-7093', effective_at=datetime(2027, 2, 1, tzinfo=timezone.utc))])
        resolved, missing = resolve_specifications(data, t, approvals)
        a2 = by_id(resolved)['A-2']
        self.assertEqual((a2.usable_travel, a2.stroke_reserve_per_end, a2.stroke), (Decimal('482'), Decimal('4'), Decimal('490')))
        self.assertFalse(any(m.agent_id == 'mail' for m in missing))

    def test_satz_converted_with_pieces_per_set_and_missing_values_reported(self):
        data = specification(requirement('A-2', 'R-6156'), requirement('A-5', 'R-6156', pieces_per_set='2'))
        resolved, _ = resolve_specifications(data, tender())
        conversions = {c.position_id: c for c in resolved.conversions}
        self.assertEqual(conversions['01.05'].pieces, Decimal('52'))
        self.assertEqual(conversions['01.02'].pieces, Decimal('29'))
        _, missing = resolve_specifications(specification(requirement('A-2', 'R-6156', certification=None)), tender())
        questions = ' '.join(m.question for m in missing)
        self.assertIn('certification of requirement A-2', questions)
        self.assertIn('requirement A-5', questions)


class RunnerTests(unittest.TestCase):
    def test_console_summary_handles_every_output_kind(self):
        import sys
        sys.path.insert(0, 'scripts')
        import run_tender
        self.assertIn('governing=R-6156', run_tender.summary(TechnicalRequirements(governing_revision='R-6156')))
        self.assertIn('reference=VG', run_tender.summary(TenderData(reference='VG')))
        self.assertIn('kind=None', run_tender.summary(None))
        self.assertIs(run_tender.AGENTS['supplier']['cls'], SupplierAgent)


class SourceTests(unittest.TestCase):
    def test_values_outside_visible_text_are_citable(self):
        agent = SupplierAgent()
        agent.navigator = SimpleNamespace(page=SimpleNamespace(url='https://p/viewer'))
        observation = {'url': 'https://p/viewer', 'frames': [{'text': 'Referenz Anforderung Bohrung (mm)',
            'dom_text': [{'kind': 'canvas', 'text': 'Bohrung (mm): 80', 'near': 'A-1'}],
            'image_text': ['Prüfcode 4711'],
            'controls': [{'label': 'Belegnummer', 'value': '64431', 'type': None},
                         {'label': 'Passwort', 'value': 'x', 'type': 'password'}]}]}
        source = agent.sources[agent.remember(observation, SimpleNamespace(site_id='s'))]
        self.assertIn('Bohrung (mm): 80', source['text'])
        self.assertIn('Prüfcode 4711', source['text'])
        self.assertIn('Belegnummer: 64431', source['text'])
        self.assertNotIn('Passwort', source['text'])


class AccessFormTests(unittest.TestCase):
    def test_set_action_keys_and_form_state_counts_as_effect(self):
        self.assertEqual(BrowserAction(operation='set', target='f0:e1', value='true').operation, 'set')
        with self.assertRaises(ValueError):
            BrowserAction(operation='set', target='f0:e1')
        for key in ('ArrowRight', 'ArrowLeft', 'Home', 'End', 'Space'):
            self.assertEqual(BrowserAction(operation='press', target='f0:e1', value=key).value, key)
        before = {'url': 'u', 'frames': [{'text': 'Unterlagen', 'controls': [
            {'label': 'Kenntnis genommen', 'checked': False, 'value': None, 'disabled': False}]}]}
        after = {'url': 'u', 'frames': [{'text': 'Unterlagen', 'controls': [
            {'label': 'Kenntnis genommen', 'checked': True, 'value': None, 'disabled': False}]}]}
        self.assertNotEqual(SupplierAgent.content(before), SupplierAgent.content(after))


class Portal:
    pages = {'list': 'Lastenheft LH-VG-2027-7093 Revision R-1001 Status: ersetzt. Revision R-6156 Status: aktuell.',
             'spec': 'R-6156 A-2 Kataloggruppe HZ-DW Bohrung 63 mm Hub 500 mm Druck 160 bar Befestigung Flansch '
                     'Zertifizierung CE. A-5 Kataloggruppe HZ-SW Bohrung 40 mm Hub 200 mm Druck 160 bar '
                     'Befestigung Gelenk Zertifizierung CE Stück pro Satz 2.'}

    def __init__(self, page, site, secrets=(), **kwargs):
        self.site = site; self.page = SimpleNamespace(url=site.url, set_default_timeout=lambda _: None)
        self.current = 'list'

    async def initialize(self): pass

    async def observe(self, offset=0):
        return {'url': f'{self.site.url}/{self.current}', 'frames': [{'text': self.pages[self.current],
                'controls': []}], 'tabs': [], 'downloads': []}

    async def act(self, action, **kwargs):
        if action.operation == 'click':
            self.current = 'spec'


class Model:
    def __init__(self): self.step = 0; self.extractions = []

    async def ask(self, system, user, *, output_type=None, tools=None):
        data = json.loads(user)
        if output_type is AuthenticationStep:
            return SimpleNamespace(structured=AuthenticationStep(action=BrowserAction(operation='finish'), authenticated=True))
        if output_type is BrowserAction:
            self.step += 1
            plan = [BrowserAction(operation='inspect', source_ids=[data['source_id']]),
                    BrowserAction(operation='click', target='f0:e1'),
                    BrowserAction(operation='inspect', source_ids=[data['source_id']])]
            return SimpleNamespace(structured=plan[min(self.step, 3) - 1])
        self.extractions.append(data)
        source = data['sources'][0]
        if 'Status' in source['text']:
            answer = {'output': {'revisions': [
                {'specification_id': SPEC, 'revision': 'R-1001', 'status': 'ersetzt'},
                {'specification_id': SPEC, 'revision': 'R-6156', 'status': 'aktuell', 'current': True}]},
                'citations': [{'source_id': source['source_id'], 'excerpt': 'Revision R-6156 Status: aktuell.',
                               'reference': 'R-6156'}]}
        else:
            req = lambda rid, group, bore, stroke, mount, **x: {'requirement_id': rid, 'revision': 'R-6156',
                'specification_id': SPEC, 'catalogue_group': group, 'bore': bore, 'stroke': stroke, 'pressure': '160',
                'mounting': mount, 'certification': 'CE', 'stroke_kind': 'nominal', **x}
            answer = {'status': 'complete', 'output': {'requirements': [
                req('A-2', 'HZ-DW', '63', '500', 'Flansch'), req('A-5', 'HZ-SW', '40', '200', 'Gelenk', pieces_per_set='2')]},
                'citations': [{'source_id': source['source_id'], 'excerpt': 'A-2 Kataloggruppe HZ-DW', 'reference': 'A-2'},
                              {'source_id': source['source_id'], 'excerpt': 'A-5 Kataloggruppe HZ-SW', 'reference': 'A-5'}]}
        return SimpleNamespace(structured=output_type.model_validate(answer))


class SupplierAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_investigation_applies_tender_amendments_and_ends_when_complete(self):
        reports = []
        async def report(result): reports.append(result)
        task = AgentTask(tender=tender(amendment('8150', 23, 'Hub/Stellweg = 482 mm')))
        async def get_task(_): return task
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=10), page=object(), llm=Model(),
                               access_manager=manager(), report=report, get_task=get_task)
        with patch('agents.website.BrowserNavigator', Portal):
            result = await SupplierAgent().execute(task, context)
        self.assertEqual(result.status, AgentStatus.complete)
        self.assertEqual(result.output.governing_revision, 'R-6156')
        a2 = by_id(result.output)['A-2']
        self.assertEqual((a2.stroke, a2.original_values['stroke']), (Decimal('482'), '500'))
        self.assertEqual(a2.position_ids, ['01.02'])
        self.assertEqual({c.position_id: c.pieces for c in result.output.conversions}['01.05'], Decimal('52'))
        self.assertEqual(reports[0].output.governing_revision, 'R-6156')  # revision published before requirements
        self.assertTrue(all(e.source_url.endswith('/spec') for e in a2.evidence))
        self.assertNotIn('"evidence"', json.dumps(context.llm.extractions[0]['task']))


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_ends_when_only_other_agents_can_supply_missing_facts(self):
        class UsableTravel(Model):
            async def ask(self, system, user, *, output_type=None, tools=None):
                reply = await super().ask(system, user, output_type=output_type, tools=tools)
                structured = reply.structured
                if hasattr(structured, 'output') and structured.output.requirements:
                    reqs = [r.model_copy(update={'stroke_kind': 'nutzbarer Weg'}) if r.requirement_id == 'A-2' else r
                            for r in structured.output.requirements]
                    structured = structured.model_copy(update={'output': structured.output.model_copy(
                        update={'requirements': reqs})})
                return SimpleNamespace(structured=structured)
        task = AgentTask(tender=tender(amendment('8150', 23, 'Hub/Stellweg = 482 mm')))
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=30), page=object(), llm=UsableTravel(),
                               access_manager=manager())
        with patch('agents.website.BrowserNavigator', Portal):
            result = await SupplierAgent().execute(task, context)
        self.assertEqual(result.status, AgentStatus.partial)
        self.assertTrue(result.missing and all(m.agent_id == 'mail' for m in result.missing))
        self.assertIsNone(by_id(result.output)['A-2'].stroke)
        self.assertEqual(by_id(result.output)['A-2'].usable_travel, Decimal('482'))
        self.assertLess(context.llm.step, 6)


if __name__ == '__main__':
    unittest.main()
