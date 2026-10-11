"""ERP extraction completeness, exact matching and incremental condition provenance."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
from agents.erp import ErpAgent, exact_match
from core.contracts import (AgentTask, ERPData, Evidence, MaterialCandidate, PriceCondition,
                            TechnicalRequirement, TechnicalRequirements, PositionConversion, ERPSectionInspection)
from core.state import merge_progress
from agents.website import AuthenticationStepLimit


def requirement(**changes):
    return TechnicalRequirement(requirement_id='r', catalogue_group='group', bore='50', stroke='100',
        pressure='200', mounting='flange', certification='CE', units={'bore': 'mm', 'stroke': 'mm',
        'pressure': 'bar'}, **changes)


def evidence(reference):
    return Evidence(id='erp:' + 'a' * 32, agent_id='erp', source_site='erp', reference=reference,
                    excerpt='test observation')


def condition(**changes):
    values = dict(condition_id='c', price='12', minimum_quantity='1', currency='EUR', price_unit='1',
                  valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                  valid_until=datetime(2026, 12, 31, tzinfo=timezone.utc), fx_key='fx', evidence=[evidence('c')])
    return PriceCondition(**{**values, **changes})


def material(**changes):
    spec = requirement().model_copy(update={'evidence': [evidence('m')]})
    values = dict(material_id='m', active=True, specification=spec, stock='0', stock_lead_days=2,
        production_lead_weeks=3, conditions=[condition(), condition(minimum_quantity='10', price='11')],
        setup_fee='0', surcharge_mass='2', metal_index_code='metal', allocation_mass='3',
        pieces_per_carton='4', records_complete=True, evidence=[evidence('m')],
        sections=[ERPSectionInspection(section=section, material_id='m', section_label=section,
            search_verified=True, complete=True, evidence=[evidence('m:' + section)])
            for section in ('material', 'inventory', 'pricing')])
    return MaterialCandidate(**{**values, **changes})


class ErpTests(unittest.TestCase):
    def setUp(self):
        self.agent = ErpAgent()
        self.agent.current_task = AgentTask(technical=TechnicalRequirements(requirements=[requirement()],
            conversions=[PositionConversion(position_id='1', requirement_id='r', pieces='12')]))

    def output(self, **changes):
        return ERPData(materials=[material(**changes)], searched_catalogue_groups=['group'])

    def test_complete_material_and_explicit_zero_are_accepted(self):
        self.assertTrue(self.agent.covered(self.output()))
        self.assertEqual(self.agent.finalize(self.output())[1], [])

    def test_each_technical_attribute_group_status_and_unit_must_match(self):
        for field, value in [('bore', 51), ('stroke', 101), ('pressure', 201),
                             ('mounting', 'foot'), ('certification', 'other'), ('catalogue_group', 'other')]:
            candidate = material(specification=requirement().model_copy(update={field: value}))
            self.assertFalse(exact_match(candidate, requirement()), field)
        self.assertFalse(exact_match(material(active=False), requirement()))
        self.assertFalse(exact_match(material(active=None), requirement()))
        self.assertFalse(exact_match(material(specification=requirement().model_copy(
            update={'units': {'bore': 'cm', 'stroke': 'mm', 'pressure': 'bar'}})), requirement()))

    def test_missing_records_do_not_count_as_complete(self):
        # The live ERP shows no setup fee for a material: ordinary setup comes from the tender Q&A instead.
        self.assertTrue(self.agent.covered(self.output(setup_fee=None)))
        for field in ('stock', 'surcharge_mass', 'metal_index_code', 'allocation_mass',
                      'pieces_per_carton', 'records_complete'):
            output = self.output(**{field: None})
            self.assertFalse(self.agent.covered(output), field)
            self.assertTrue(self.agent.finalize(output)[1])
        self.assertFalse(self.agent.covered(self.output(stock_lead_days=None)))
        self.assertFalse(self.agent.covered(self.output(production_lead_weeks=None)))
        self.assertFalse(self.agent.covered(self.output(conditions=[])))
        self.assertFalse(self.agent.covered(self.output().model_copy(update={'searched_catalogue_groups': []})))

    def test_unresolved_candidate_cannot_be_silently_excluded(self):
        unresolved = material(material_id='unknown', active=None)
        output = self.output().model_copy(update={'materials': [material(), unresolved]})
        self.assertFalse(self.agent.covered(output))
        unresolved = material(material_id='unknown', specification=requirement().model_copy(update={'bore': None}))
        self.assertFalse(self.agent.covered(output.model_copy(update={'materials': [material(), unresolved]})))

    def test_all_condition_fields_and_evidence_are_required(self):
        for field in ('condition_id', 'price', 'minimum_quantity', 'valid_from', 'valid_until',
                      'currency', 'price_unit', 'fx_key'):
            self.assertFalse(self.agent.covered(self.output(conditions=[condition(**{field: None})])), field)
        self.assertFalse(self.agent.covered(self.output(conditions=[condition(evidence=[])])))

    def test_quantities_and_requirements_come_from_task(self):
        self.agent.current_task = AgentTask()
        missing = self.agent.finalize(self.output())[1]
        self.assertTrue(all(m.agent_id == 'supplier' for m in missing))
        self.assertFalse(self.agent.covered(self.output()))
        task = AgentTask(technical=TechnicalRequirements(requirements=[requirement()],
            conversions=[PositionConversion(position_id='1', requirement_id='r', pieces='12')]))
        self.assertEqual(self.agent.task_context(task)['technical']['conversions'][0]['pieces'], '12')

    def test_scale_updates_merge_without_losing_other_rows(self):
        old = self.output()
        incoming = ERPData(materials=[MaterialCandidate(material_id='m', conditions=[condition(price='13')])])
        merged = merge_progress(old, incoming)
        self.assertEqual(len(merged.materials[0].conditions), 2)
        self.assertEqual(str(merged.materials[0].conditions[0].price), '13')
        self.assertEqual(merged.materials[0].stock, 0)
        future = condition(valid_from=datetime(2027, 1, 1, tzinfo=timezone.utc))
        merged = merge_progress(merged, ERPData(materials=[MaterialCandidate(material_id='m', conditions=[future])]))
        self.assertEqual(len(merged.materials[0].conditions), 3)

    def test_condition_citations_are_scoped_to_condition_id(self):
        citations = [evidence('m'), evidence('c'), evidence('other')]
        attached = self.agent.attach(self.output(), citations)
        self.assertEqual([e.reference for e in attached.materials[0].conditions[0].evidence], ['c'])
        self.assertEqual([e.reference for e in attached.materials[0].evidence], ['m'])


class AuthenticationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_step_limit_retries_from_current_page(self):
        worker = ErpAgent()
        with patch('agents.website.WebsiteAgent.authenticate', new=AsyncMock(
                side_effect=[AuthenticationStepLimit(), None])) as authenticate:
            await worker.authenticate(SimpleNamespace(logger=None), 'resume investigation')
        self.assertEqual(authenticate.await_count, 2)
        self.assertEqual(authenticate.await_args.args[1], 'resume investigation')

    async def test_recovery_is_bounded_and_other_errors_are_not_retried(self):
        for error, expected in ((AuthenticationStepLimit(), 3), (RuntimeError('not confirmed'), 1)):
            with patch('agents.website.WebsiteAgent.authenticate', new=AsyncMock(side_effect=error)) as authenticate:
                with self.assertRaises(type(error)):
                    await ErpAgent().authenticate(SimpleNamespace(logger=None))
            self.assertEqual(authenticate.await_count, expected)


class NavigationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.worker = ErpAgent()
        self.worker.navigator = SimpleNamespace(page=SimpleNamespace(url='https://erp.test/search'), targets={
            'q': (None, 0, {'tag': 'input', 'label': 'Bezeichnung enthält', 'value': 'group-a'}),
            'search': (None, 1, {'tag': 'button', 'label': 'Suchen'})})

    def test_expiry_does_not_ban_search_and_query_outcomes_are_separate(self):
        from tools.navigation import BrowserAction
        action = BrowserAction(operation='click', target='search')
        old_key = self.worker.outcome_key(action)
        self.worker.outcomes[old_key] = ['logged_out', 'logged_out']
        self.assertFalse(self.worker.untrusted(old_key))
        self.worker.outcomes[old_key] = ['no_effect'] * 3
        self.assertTrue(self.worker.untrusted(old_key))
        self.worker.navigator.targets['q'][2]['value'] = 'group-b'
        new_key = self.worker.outcome_key(action)
        self.assertNotEqual(old_key, new_key)
        self.assertFalse(self.worker.untrusted(new_key))
        self.assertEqual(self.worker.warnings(), [])

    def test_overlay_query_and_results_count_as_progress(self):
        def observation(value, result):
            return {'frames': [{'text': 'ERP', 'overlay_text': result, 'controls': [
                {'in_overlay': True, 'label': 'Search', 'value': value}]}]}
        self.assertNotEqual(self.worker.content(observation('', '0 results')),
                            self.worker.content(observation('group-a', '0 results')))
        self.assertNotEqual(self.worker.content(observation('group-a', '0 results')),
                            self.worker.content(observation('group-a', '2 results material-a material-b')))

    def test_reusable_forms_are_not_exhausted_and_empty_progress_is_bounded(self):
        from core.contracts import AgentResult, AgentStatus
        self.worker.page_log['https://erp.test/search'] = {'visits': 5, 'extracted': True, 'content': 123}
        self.worker.extracted_pages.add(123)
        self.assertFalse(self.worker.exhausted('https://erp.test/search'))
        self.worker.latest = AgentResult(agent_id='erp', status=AgentStatus.partial, output=ERPData())
        self.assertTrue(self.worker.core_covered())
        self.assertEqual(self.worker.stall_steps, 20)


class TargetSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_schema_accepts_only_live_ids(self):
        from tools.navigation import BrowserAction
        from pydantic import ValidationError
        worker = ErpAgent()
        schemas = []
        async def ask(system, user, *, output_type):
            schemas.append(output_type)
            return SimpleNamespace(structured=output_type(operation='click', target='f0:e1'))
        context = SimpleNamespace(llm=SimpleNamespace(ask=ask), access_manager=SimpleNamespace(secrets=lambda: []))
        action = await worker.ask(context, 'Choose a live action',
            {'observation': {'frames': [{'controls': [{'target': 'f0:e1'}]}]}}, BrowserAction)
        self.assertEqual(action.target, 'f0:e1')
        with self.assertRaises(ValidationError):
            schemas[0](operation='click', target='a[label=Search]')




class LookupValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalogue_group_cannot_be_used_as_material_number(self):
        from tools.navigation import BrowserAction
        from pydantic import ValidationError
        schemas = []
        async def ask(system, user, *, output_type):
            schemas.append(output_type)
            return SimpleNamespace(structured=output_type(operation='click', target='f0:e1'))
        context = SimpleNamespace(llm=SimpleNamespace(ask=ask), access_manager=SimpleNamespace(secrets=lambda: []))
        await ErpAgent().ask(context, 'Choose a live action', {
            'task': {'technical': {'requirements': [{'catalogue_group': 'G', 'requirement_id': 'r'}]}},
            'observation': {'frames': [{'controls': [{'target': 'f0:e1', 'tag': 'input', 'label': 'Material', 'value': ''}, {'target': 'f0:e2', 'tag': 'a', 'label': 'F4 search'}]}]}}, BrowserAction)
        for action in ({'operation': 'fill', 'target': 'f0:e1', 'value': 'G'},
                       {'operation': 'fill', 'target': 'f0:e1', 'value': 'r'},
                       {'operation': 'fill', 'target': 'f0:e2', 'value': 'G'},
                       {'operation': 'inspect', 'target': 'f0:e1'},
                       {'operation': 'press', 'target': 'f0:e1', 'value': 'F4'}):
            with self.assertRaises(ValidationError):
                schemas[0](**action)
        self.assertEqual(schemas[0](operation='fill', target='f0:e1', value='verified-material').value,
                         'verified-material')


class SectionWorkflowTests(unittest.TestCase):
    setUp = ErpTests.setUp
    output = ErpTests.output

    def test_all_three_sections_are_required(self):
        for section in ('material', 'inventory', 'pricing'):
            inspections = [s for s in material().sections if s.section != section]
            output = self.output(sections=inspections)
            self.assertFalse(self.agent.covered(output))
            self.assertFalse(self.agent.finalize(output)[0].materials[0].records_complete)
            self.assertTrue(any(section in m.question for m in self.agent.finalize(output)[1]))

    def test_wrong_material_unverified_results_and_unscoped_evidence_are_incomplete(self):
        for update in ({'material_id': 'other'}, {'search_verified': False}, {'complete': False},
                       {'evidence': [evidence('m')]}, {'missing': ['scale page inaccessible']}):
            inspections = material().sections
            inspections[2] = inspections[2].model_copy(update=update)
            self.assertFalse(self.agent.covered(self.output(sections=inspections)))

    def test_sections_merge_without_losing_prior_material_facts(self):
        old = ERPData(materials=[material(sections=material().sections[:1])])
        incoming = ERPData(materials=[MaterialCandidate(material_id='m', sections=material().sections[1:])])
        merged = merge_progress(old, incoming)
        self.assertEqual({s.section for s in merged.materials[0].sections}, {'material', 'inventory', 'pricing'})
        self.assertEqual(len(merged.materials[0].conditions), 2)
        self.assertTrue(self.agent.covered(merged.model_copy(update={'searched_catalogue_groups': ['group']})))
        self.agent.latest = SimpleNamespace(output=merged, missing=[])
        worklist = self.agent.task_context(self.agent.current_task)['material_worklist']
        self.assertEqual(worklist[0]['missing_sections'], [])

    def test_unreferenced_supplier_requirement_does_not_block_relevant_positions(self):
        task = self.agent.current_task
        self.agent.current_task = task.model_copy(update={'technical': task.technical.model_copy(
            update={'requirements': [requirement(), requirement().model_copy(update={'requirement_id': 'unreferenced'})]})})
        self.assertTrue(self.agent.covered(self.output()))

    def test_section_evidence_must_show_material_and_actual_heading(self):
        self.agent.sources = {'erp:' + 'a' * 32: {'text': 'm material inventory'}}
        output, missing = self.agent.finalize(self.output())
        pricing = next(s for s in output.materials[0].sections if s.section == 'pricing')
        self.assertFalse(pricing.complete)
        self.assertFalse(output.materials[0].records_complete)
        self.assertTrue(any('pricing' in m.question for m in missing))

class ExtractionSafetyTests(unittest.TestCase):
    setUp = ErpTests.setUp
    output = ErpTests.output
    def test_physical_stock_is_not_accepted_as_available_stock(self):
        self.agent.sources = {'erp:' + 'a' * 32: {'text': 'm material inventory pricing Physical stock: 0'}}
        output, _ = self.agent.finalize(self.output())
        self.assertIsNone(output.materials[0].stock)
        self.assertFalse(output.materials[0].records_complete)

    def test_available_stock_requires_a_scoped_stock_quote(self):
        quote = evidence('m').model_copy(update={'field': 'stock', 'excerpt': 'Verfügbarer Gesamtbestand: 0 Stück'})
        self.agent.sources = {'erp:' + 'a' * 32: {'text': 'm material inventory pricing Verfügbarer Gesamtbestand: 0 Stück'}}
        output, _ = self.agent.finalize(self.output(evidence=[quote]))
        self.assertEqual(output.materials[0].stock, 0)

    def test_rejected_candidate_is_not_sent_for_repeated_commercial_investigation(self):
        from core.contracts import AgentResult, AgentStatus
        candidate = material(specification=requirement().model_copy(update={'stroke': 101}))
        self.agent.latest = AgentResult(agent_id='erp', status=AgentStatus.partial, output=ERPData(materials=[candidate]))
        work = self.agent.task_context(self.agent.current_task)['material_worklist'][0]
        self.assertTrue(work['rejected'])
        self.assertEqual(work['missing_sections'], [])
        self.assertEqual(work['definite_mismatches']['r'], ['stroke'])


class QuoteSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_ids_and_verbatim_quotes_are_validated_before_extraction_returns(self):
        from pydantic import create_model, ValidationError
        from agents.website import SourceCitation
        schemas = []
        schema = create_model('ErpExtraction', output=(ERPData, ...), citations=(list[SourceCitation], ...))
        async def ask(system, user, *, output_type):
            schemas.append(output_type)
            return SimpleNamespace(structured=output_type(output=ERPData(), citations=[
                {'source_id': 'source-a', 'excerpt': 'Actual ERP text'}]))
        context = SimpleNamespace(llm=SimpleNamespace(ask=ask), access_manager=SimpleNamespace(secrets=lambda: []))
        await ErpAgent().ask(context, 'Extract observed facts', {'sources': [
            {'source_id': 'source-a', 'text': 'Actual ERP text'}]}, schema)
        for citation in ({'source_id': 'guessed-source', 'excerpt': 'Actual ERP text'},
                         {'source_id': 'source-a', 'excerpt': 'Summarized ERP text'}):
            with self.assertRaises(ValidationError):
                schemas[0](output=ERPData(), citations=[citation])

class FailedExtractionRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_extraction_is_not_cached_as_success(self):
        from agents.base import AgentContext
        from core.config import RunConfig
        from core.contracts import AgentResult, AgentStatus
        from tools.navigation import BrowserAction
        from test_architecture import manager, RUNTIME
        from test_website_integration import ObservedBrowser
        class Worker(ErpAgent):
            attempts = 0
            async def authenticate(self, context, situation=None):
                pass
            async def ask(self, context, system, data, output_type):
                return BrowserAction(operation='inspect', source_ids=[data['source_id']])
            async def extract(self, context, task, source_ids, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise ValueError('Invalid quote')
                self.latest = AgentResult(agent_id='erp', status=AgentStatus.partial, output=ERPData())
                return self.latest
        worker = Worker()
        context = AgentContext(RUNTIME, RunConfig(max_navigation_steps=2), page=object(),
                               llm=object(), access_manager=manager())
        with patch('agents.website.BrowserNavigator', ObservedBrowser):
            await worker.execute(AgentTask(technical=TechnicalRequirements(requirements=[requirement()])), context)
        self.assertEqual(worker.attempts, 2)
        self.assertEqual(len(worker.extracted), 1)

class PricingProvenanceTests(unittest.TestCase):
    def test_invented_prices_from_inventory_are_discarded(self):
        worker = ErpAgent()
        source_id = 'erp:' + 'a' * 32
        url = 'https://erp.test/mmbe?material=m'
        quote = evidence('m').model_copy(update={'source_url': url, 'excerpt': 'Material: m'})
        worker.sources[source_id] = {'text': 'MMBE Material: m Physical stock: 103', 'url': url}
        record = material(conditions=[condition(price='123.45', evidence=[quote])])
        output, missing = worker.finalize(ERPData(materials=[record]))
        self.assertEqual(output.materials[0].conditions, [])
        self.assertTrue(any('unsupported price condition' in m.question for m in missing))

    def test_price_value_must_appear_in_its_own_condition_quote(self):
        worker = ErpAgent()
        url = 'https://erp.test/vk13?material=m'
        source_id = 'erp:' + 'a' * 32
        worker.sources[source_id] = {'text': 'm Konditionssatz c Preis: 200,00 EUR Zahlenformat: de-DE', 'url': url}
        quotes = [evidence('c').model_copy(update={'field': field, 'excerpt': text, 'source_url': url})
                  for field, text in [('condition_id', 'Konditionssatz c'), ('price', 'Preis: 200,00 EUR')]]
        output, _ = worker.finalize(ERPData(materials=[material(conditions=[condition(price='123.45', evidence=quotes)])]))
        self.assertIsNone(output.materials[0].conditions[0].price)
        output, _ = worker.finalize(ERPData(materials=[material(conditions=[condition(price='200', evidence=quotes)])]))
        self.assertEqual(output.materials[0].conditions[0].price, 200)

    def test_evidence_from_another_material_is_not_attached(self):
        worker = ErpAgent()
        wrong = evidence('m').model_copy(update={'source_url': 'https://erp.test/mm03?material=other'})
        attached = worker.attach(material(), [wrong])
        self.assertEqual(attached.evidence, [])
        self.assertEqual(attached.specification.evidence, [])

    def test_explicit_locale_distinguishes_decimal_and_thousands(self):
        from agents.erp import quoted_value
        from decimal import Decimal
        self.assertTrue(quoted_value(Decimal('1250'), 'Preis: 1,250 en-US'))
        self.assertFalse(quoted_value(Decimal('1.25'), 'Preis: 1,250 en-US'))
        self.assertTrue(quoted_value(Decimal('1.25'), 'Preis: 1,250 de-DE'))

    def test_missing_report_rewording_does_not_count_as_progress(self):
        worker = ErpAgent()
        first = material(sections=[s.model_copy(update={'missing': ['unread']}) for s in material().sections])
        second = material(sections=[s.model_copy(update={'missing': ['still unread']}) for s in material().sections])
        self.assertEqual(worker.progress_facts(ERPData(materials=[first])), worker.progress_facts(ERPData(materials=[second])))


if __name__ == '__main__':
    unittest.main()


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    """One material at a time, every listed search result read before a group counts as searched."""

    def setUp(self):
        self.agent = ErpAgent()
        self.agent.current_task = AgentTask(technical=TechnicalRequirements(requirements=[requirement()],
            conversions=[PositionConversion(position_id='1', requirement_id='r', pieces='12')]))
        self.agent.record_search_results({'url': 'https://erp.test/search?transaction=MM03&q=group', 'frames': [
            {'controls': [{'target': 'f0:e1', 'label': 'm', 'href': 'https://erp.test/mm03?material=m'},
                          {'target': 'f0:e2', 'label': 'QQ-0001', 'href': 'https://erp.test/mm03?material=QQ-0001'},
                          {'target': 'f0:e3', 'label': 'Abbrechen', 'href': 'https://erp.test/'}]}]})

    def test_search_results_are_recorded_per_query(self):
        self.assertEqual(set(self.agent.results()['group']['materials']), {'m', 'QQ-0001'})

    def test_group_counts_as_searched_only_when_every_listed_result_is_read(self):
        output, missing = self.agent.finalize(ERPData(materials=[material()], searched_catalogue_groups=['group']))
        self.assertEqual(output.searched_catalogue_groups, [])
        self.assertTrue(any('QQ-0001 not yet read' in m.question for m in missing))
        other = material(material_id='QQ-0001', active=False, conditions=[], sections=[], records_complete=None,
                         evidence=[evidence('QQ-0001')])
        output, _ = self.agent.finalize(ERPData(materials=[material(), other], searched_catalogue_groups=['group']))
        self.assertEqual(output.searched_catalogue_groups, ['group'])

    def test_next_step_finishes_one_material_then_opens_the_next_result(self):
        task = self.agent.current_task
        unread = material(active=None, specification=requirement().model_copy(update={'certification': None}))
        step = self.agent.next_step(task, ERPData(materials=[unread]))
        self.assertEqual((step['material'], step['section']), ('m', 'material'))
        self.assertIn('certification info control', step['instruction'])
        incomplete = material(sections=[], records_complete=None)
        self.assertEqual(self.agent.next_step(task, ERPData(materials=[incomplete]))['section'], 'material')
        step = self.agent.next_step(task, ERPData(materials=[material()]))
        self.assertEqual((step['material'], step['url']), ('QQ-0001', 'https://erp.test/mm03?material=QQ-0001'))
        fresh = ErpAgent()
        step = fresh.next_step(task, ERPData())
        self.assertEqual(step['group'], 'group')

    async def test_actions_on_another_material_are_refused_until_the_focus_is_done(self):
        from tools.navigation import BrowserAction
        from pydantic import ValidationError
        schemas = []
        async def ask(system, user, *, output_type):
            schemas.append(output_type)
            return SimpleNamespace(structured=output_type(operation='click', target='f0:e1'))
        context = SimpleNamespace(llm=SimpleNamespace(ask=ask), access_manager=SimpleNamespace(secrets=lambda: []))
        focus = {'material': 'm', 'section': 'inventory', 'instruction': 'm matches exactly: open its inventory'}
        await ErpAgent().ask(context, 'Choose a live action', {'task': {'next_step': focus}, 'observation': {
            'url': 'https://erp.test/mm03?material=m', 'frames': [{'controls': [
                {'target': 'f0:e1', 'tag': 'a', 'label': 'MMBE', 'href': 'https://erp.test/mmbe?material=m'},
                {'target': 'f0:e2', 'tag': 'a', 'label': 'QQ-0001', 'href': 'https://erp.test/mm03?material=QQ-0001'},
                {'target': 'f0:e3', 'tag': 'input', 'label': 'Material', 'value': 'm'}]}]}}, BrowserAction)
        self.assertEqual(schemas[0](operation='click', target='f0:e1').target, 'f0:e1')
        for action in ({'operation': 'click', 'target': 'f0:e2'},
                       {'operation': 'fill', 'target': 'f0:e3', 'value': 'QQ-0001'}):
            with self.assertRaises(ValidationError):
                schemas[0](**action)


class AttributeEqualityTests(unittest.TestCase):
    def test_only_the_no_certification_wording_is_unified(self):
        from services.specification_resolution import same_attribute
        self.assertTrue(same_attribute('certification', 'Keine', 'keine gefordert'))
        self.assertFalse(same_attribute('certification', 'Keine', 'EN 1493'))
        self.assertFalse(same_attribute('certification', 'EN 1493', 'EN 1494'))
        self.assertTrue(same_attribute('stroke', '300', '300.0'))
        self.assertFalse(same_attribute('mounting', 'Flansch', 'Schwenkauge'))

    def test_focus_lists_unvisited_pages_of_that_material(self):
        agent = ErpAgent()
        agent.navigator = SimpleNamespace(visited=['https://erp.test/mm03?material=m'], seen_links={
            'https://erp.test/mm03?material=m': 'MM03', 'https://erp.test/mm03?material=m&tab=mrp': 'Disposition',
            'https://erp.test/mmbe?material=m': 'MMBE', 'https://erp.test/mm03?material=x': 'other'})
        self.assertEqual([p['label'] for p in agent.unvisited_pages('m')], ['Disposition', 'MMBE'])


class RecordCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_material_record_is_read_completely_and_only_that_material(self):
        base = 'https://erp.test'
        site_links = {  # page -> links it shows
            f'{base}/mm03?material=m': {f'{base}/mm03?material=m&tab=planning': 'Disposition',
                                        f'{base}/mmbe?material=m': 'MMBE', f'{base}/vk13?material=m': 'VK13',
                                        f'{base}/mm03?material=other': 'other', f'{base}/logout?material=m': 'Abmelden'},
            f'{base}/mmbe?material=m': {f'{base}/mmbe?material=m&level=stock': 'Lagerort'},
            f'{base}/vk13?material=m': {f'{base}/vk13?material=m&page=2': 'Nächste Seite'}}

        class Navigator:
            def __init__(self):
                self.seen_links, self.visited, self.auth_paths, self.opened = {}, [], set(), []
                self.url = f'{base}/mm03?material=m'
                self.seen_links.update(site_links[self.url])

            async def act(self, action):
                self.url = action.url
                self.opened.append(action.url)

            async def observe(self):
                self.seen_links.update(site_links.get(self.url, {}))
                return {'url': self.url, 'frames': [{'text': f'page {self.url}', 'controls': []}]}

        agent = ErpAgent()
        agent.navigator = Navigator()
        agent.sources = {}
        agent.remember = lambda page, site: f"erp:{len(agent.sources):032x}" if agent.sources.update(
            {f"erp:{len(agent.sources):032x}": {'text': page['url']}}) is None else None
        agent.record_search_results({'url': f'{base}/search?q=group', 'frames': [
            {'controls': [{'label': 'm', 'href': f'{base}/mm03?material=m'}]}]})
        agent.extract = AsyncMock()
        agent.emit = lambda *args, **kwargs: None
        feedback = await agent.after_observation(None, AgentTask(), {'url': f'{base}/mm03?material=m', 'frames': []},
                                                 'erp:start', None)
        self.assertIn('record_collected', feedback)
        self.assertEqual(set(agent.navigator.opened), {
            f'{base}/mm03?material=m&tab=planning', f'{base}/mmbe?material=m', f'{base}/vk13?material=m',
            f'{base}/mmbe?material=m&level=stock', f'{base}/vk13?material=m&page=2'})
        sources = agent.extract.await_args.args[2]
        self.assertEqual(len(sources), 6)  # the opened page plus five record pages, in one extraction
        self.assertIsNone(await agent.after_observation(None, AgentTask(), {'url': f'{base}/mm03?material=m',
                                                                            'frames': []}, 'erp:again', None))
