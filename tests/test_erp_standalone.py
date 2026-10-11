import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from agents.erp_standalone import StandaloneErpAgent,ErpContext,ErpExtraction,ErpNavigationChoice,ErpSearchChoice,ErpOptionChoice
from core.access import SiteAccess
from core.config import RunConfig
from core.contracts import AgentStatus,MaterialCandidate
from tools.run_erp import ErpAccess,parser,load_task
from tools.erp_navigation import ErpNavigator
from tools.navigation import BrowserAction
from tools.erp_values import number,prices,available_stock,material_fields,missing_fields,canonical_attributes


class FakeErp:
    instances=[]
    def __init__(self,page,site,*args,**kwargs):
        self.page=page;self.site=site;self.expired=False
        self.instances.append(self)
    async def initialize(self):self.page.url=self.site.url
    async def observe(self,offset=0):
        root=self.site.url.rstrip('/')
        controls=[{'target':'logout','label':'Abmelden'},{'target':'transaction','label':'Transaktionscode'}]
        text='SAP Easy Access'
        if '/search' in self.page.url:
            text='2 Treffer'
            controls += [{'target':'q','tag':'input','label':'Bezeichnung enthält'},
                         {'target':'submit','tag':'button','label':'Suchen'},
                         {'target':'one','href':root+'/mm03?material=M1','label':'M1'},
                         {'target':'two','href':root+'/mm03?material=M2','label':'M2'}]
        elif '?material=' in self.page.url:
            material=self.page.url.split('material=')[1].split('&')[0]
            text=f'Materialnummer {material}\nBestand 12 Stück\nPreis 10 EUR\n01 = aktiv'
            controls += [{'target':'technical','href':root+'/mm03?material='+material+'&tab=technical'},
                         {'target':'planning','href':root+'/mm03?material='+material+'&tab=planning'},
                         {'target':'stock','href':root+'/mmbe?material='+material},
                         {'target':'price','href':root+'/vk13?material='+material}]
            if '/mmbe?' in self.page.url:
                controls.append({'target':'locations','label':'Hauptwerk öffnen',
                                 'href':root+'/mmbe?material='+material+'&level=locations'})
                if 'level=locations' in self.page.url:
                    controls.append({'target':'stock-detail','label':'Fertigwaren öffnen',
                                     'href':root+'/mmbe?material='+material+'&level=stock'})
                if 'level=stock' in self.page.url:text += '\nFrei verwendbar\n7'
            if '/vk13?' in self.page.url:
                controls.append({'target':'scales','label':'Mengenstaffeln öffnen',
                                 'href':root+'/vk13?material='+material+'&page=1&view=scales'})
        elif self.page.url.endswith('/mm03'):
            controls.append({'target':'search','href':root+'/search?transaction=MM03','label':'F4 Material suchen'})
        else:controls.append({'target':'mm03','href':root+'/mm03','label':'MM03'})
        return {'url':self.page.url,'frames':[{'text':text,'controls':controls}]}
    async def act(self,action,**kwargs):
        if action.operation=='goto':self.page.url=action.url


class ErpTests(unittest.IsolatedAsyncioTestCase):
    def setup_context(self):
        task=load_task(parser().parse_args([]))
        site=SiteAccess(site_id='erp',url='https://erp.test/',username='test-erp-user',password='secret-erp-password')
        artifacts={}
        async def ask(system,user,*,output_type):
            data=json.loads(user)
            if output_type is ErpSearchChoice:
                return SimpleNamespace(structured=ErpSearchChoice(
                    search_text=task.technical.requirements[0].catalogue_group,reason='Search observed group field'))
            if issubclass(output_type,ErpOptionChoice):
                urls=[option['option_id'] for option in data['options']]
                if data['phase'] in ('transaction','search_entry','material_entry'):urls=urls[:1]
                if data['phase']=='candidates':urls=urls[:data['limit']]
                return SimpleNamespace(structured=output_type(option_ids=urls,reason='Choose from live options'))
            return SimpleNamespace(structured=ErpExtraction(material=MaterialCandidate(
                material_id=data['material_id'],stock=12),passage_ids=[data['passages'][0]['passage_id']],
                missing=['Full pricing not yet verified']))
        context=ErpContext(runtime={},config=RunConfig(),page=SimpleNamespace(url=site.url),
            llm=SimpleNamespace(ask=ask),access_manager=ErpAccess(site),
            save_artifact=lambda name,data:artifacts.update({name:data}))
        return task,context,artifacts

    async def test_basic_run_reads_two_materials_and_saves_partial_progress(self):
        task,context,artifacts=self.setup_context()
        with patch('agents.erp_standalone.ErpNavigator',FakeErp):
            result=await StandaloneErpAgent().execute(task,context)
        self.assertEqual(result.status,AgentStatus.partial)
        self.assertEqual([m.material_id for m in result.output.materials],['M1','M2'])
        self.assertTrue(result.output.materials[0].evidence)
        self.assertTrue(all(r['status']=='saved' for r in artifacts['erp-material-runs']))
        urls=[s['observation']['url'] for s in artifacts['erp-steps']]
        self.assertTrue(any('/mmbe?' in u for u in urls))
        self.assertTrue(any('/vk13?' in u for u in urls))
        self.assertTrue(any('level=locations' in u for u in urls))
        self.assertTrue(any('level=stock' in u for u in urls))
        self.assertEqual(result.output.materials[0].stock,7)
        self.assertTrue(any('view=scales' in u for u in urls))
        self.assertIn('erp-progress',artifacts)
        self.assertIn('erp-navigation-decisions',artifacts)
        phases=[d['phase'] for d in artifacts['erp-navigation-decisions']]
        self.assertIn('search_input',phases)
        self.assertIn('material_pages',phases)
        self.assertIn('stock_details',phases)
        self.assertIn('price_details',phases)

    async def test_second_material_failure_preserves_first(self):
        task,context,artifacts=self.setup_context()
        ask=context.llm.ask
        async def fail_second(system,user,*,output_type):
            if output_type is ErpExtraction and json.loads(user)['material_id']=='M2':raise ValueError('test extraction failure')
            return await ask(system,user,output_type=output_type)
        context.llm.ask=fail_second
        with patch('agents.erp_standalone.ErpNavigator',FakeErp):
            result=await StandaloneErpAgent().execute(task,context)
        self.assertEqual([m.material_id for m in result.output.materials],['M1'])
        self.assertEqual(artifacts['erp-material-runs'][1]['status'],'failed')

    async def test_unknown_passage_does_not_publish_invented_evidence(self):
        task,context,_=self.setup_context()
        original=context.llm.ask
        async def ask(*args,**kwargs):
            if kwargs.get('output_type') is ErpSearchChoice or issubclass(kwargs['output_type'],ErpOptionChoice):return await original(*args,**kwargs)
            return SimpleNamespace(structured=ErpExtraction(material=MaterialCandidate(material_id='M1'),passage_ids=['invented']))
        context.llm.ask=ask
        with patch('agents.erp_standalone.ErpNavigator',FakeErp):
            result=await StandaloneErpAgent().execute(task,context)
        self.assertFalse(result.output.materials)

    async def test_model_navigation_rejects_unobserved_urls_and_invented_search(self):
        task,context,artifacts=self.setup_context()
        worker=StandaloneErpAgent()
        context.llm.ask=AsyncMock(return_value=SimpleNamespace(structured=ErpOptionChoice(
            option_ids=['unobserved'],reason='wrong')))
        with self.assertRaises(ValueError):
            await worker.choose(context,task,'material_pages',{'frames':[]},['https://erp.test/mm03'])
        self.assertEqual(artifacts['erp-navigation-decisions'][0]['status'],'rejected')
        self.assertIn('unobserved',artifacts['erp-navigation-decisions'][0]['error'])
        context.llm.ask=AsyncMock(return_value=SimpleNamespace(structured=ErpSearchChoice(
            search_text='invented-group',reason='wrong')))
        with self.assertRaises(ValueError):
            await worker.choose(context,task,'search_input',{'frames':[]},[],search=True)

    async def test_navigation_and_search_have_separate_schemas(self):
        self.assertNotIn('search_text',ErpNavigationChoice.model_fields)
        self.assertNotIn('urls',ErpSearchChoice.model_fields)
        task,context,artifacts=self.setup_context()
        choice=await StandaloneErpAgent().choose(context,task,'transaction',{'frames':[]},['https://erp.test/mm03'])
        self.assertEqual(choice.urls,['https://erp.test/mm03'])
        self.assertEqual(artifacts['erp-navigation-decisions'][0]['status'],'accepted')

    async def test_entry_schema_requires_one_url_and_rejection_is_recorded(self):
        task,context,artifacts=self.setup_context()
        context.llm.ask=AsyncMock(return_value=SimpleNamespace(structured=ErpOptionChoice(
            option_ids=['o1','o2'],reason='entry')))
        with self.assertRaises(ValueError):
            await StandaloneErpAgent().choose(context,task,'transaction',{'frames':[]},
                ['https://erp.test/mm03','https://erp.test/mmbe'])
        schema=context.llm.ask.call_args.kwargs['output_type']
        for ids in ([],['o1','o2']):
            with self.assertRaises(ValueError):schema(option_ids=ids,reason='entry')
        self.assertEqual(artifacts['erp-navigation-decisions'][0]['status'],'rejected')

    async def test_delayed_notice_retry_resolves_new_target(self):
        from playwright.async_api import TimeoutError as BrowserTimeoutError
        _,context,artifacts=self.setup_context()
        worker=StandaloneErpAgent()
        def ready(target):return {'frames':[{'controls':[{'target':target,'tag':'button','label':'Suchen'}]}]}
        blocked={'frames':[{'controls':[{'target':'close','tag':'button','label':'Schließen'}]}]}
        worker.clear_access=AsyncMock(side_effect=[ready('old'),ready('new')])
        worker.navigator=SimpleNamespace(act=AsyncMock(side_effect=[BrowserTimeoutError('blocked'),None]),
            observe=AsyncMock(return_value=blocked))
        await worker.act_control(context,'search','click','button',r'Suchen')
        self.assertEqual([c.args[0].target for c in worker.navigator.act.await_args_list],['old','new'])
        self.assertTrue(any(s['phase']=='search_timeout' for s in artifacts['erp-steps']))

    async def test_click_timeout_without_notice_is_not_retried(self):
        from playwright.async_api import TimeoutError as BrowserTimeoutError
        _,context,_=self.setup_context()
        worker=StandaloneErpAgent()
        ready={'frames':[{'controls':[{'target':'submit','tag':'button','label':'Suchen'}]}]}
        worker.clear_access=AsyncMock(return_value=ready)
        worker.navigator=SimpleNamespace(act=AsyncMock(side_effect=BrowserTimeoutError('timeout')),
            observe=AsyncMock(return_value=ready))
        with self.assertRaises(BrowserTimeoutError):
            await worker.act_control(context,'search','click','button',r'Suchen')
        self.assertEqual(worker.navigator.act.await_count,1)

    async def test_skipped_candidate_keeps_material_selection_incomplete(self):
        task,context,_=self.setup_context()
        original=context.llm.ask
        async def ask(system,user,*,output_type):
            response=await original(system,user,output_type=output_type)
            if issubclass(output_type,ErpOptionChoice) and json.loads(user)['phase']=='candidates':
                return SimpleNamespace(structured=response.structured.model_copy(update={'option_ids':response.structured.option_ids[:1]}))
            return response
        context.llm.ask=ask
        with patch('agents.erp_standalone.ErpNavigator',FakeErp):
            result=await StandaloneErpAgent().execute(task,context)
        self.assertEqual(result.output.selections[0].status,'incomplete')

    async def test_stock_options_schema_excludes_links_from_other_stages(self):
        task,context,artifacts=self.setup_context()
        original=context.llm.ask
        async def ask(system,user,*,output_type):
            data=json.loads(user)
            self.assertIn('Only inspect available stock',data['objective'])
            self.assertEqual(data['options'],[{'option_id':'o1','url':'https://erp.test/mmbe?level=locations'}])
            for invalid in ('o2','https://erp.test/mm03'):
                with self.assertRaises(ValueError):output_type(option_ids=[invalid],reason='wrong')
            return await original(system,user,output_type=output_type)
        context.llm.ask=ask
        result=await StandaloneErpAgent().choose(context,task,'stock_details',
            {'frames':[{'controls':[{'href':'https://erp.test/mm03'}]}]},
            ['https://erp.test/mmbe?level=locations'])
        self.assertEqual(result.urls,['https://erp.test/mmbe?level=locations'])
        self.assertEqual(artifacts['erp-navigation-decisions'][0]['choice']['option_ids'],['o1'])

    async def test_expired_session_reauthenticates_then_returns_to_same_url(self):
        task,context,_=self.setup_context()
        worker=StandaloneErpAgent()
        actions=[]
        worker.navigator=SimpleNamespace(act=AsyncMock(side_effect=lambda action:actions.append(action.url)))
        login={'frames':[{'controls':[{'type':'password'}]}]}
        ready={'frames':[{'controls':[]}]}
        worker.clear_access=AsyncMock(side_effect=[login,ready])
        worker.authenticate=AsyncMock()
        await worker.open_url(context,'https://erp.test/mm03?material=M1')
        self.assertEqual(worker.relogins,1)
        self.assertEqual(actions,['https://erp.test/mm03?material=M1']*2)
        worker.authenticate.assert_awaited_once()

    async def test_unobserved_and_write_navigation_are_rejected(self):
        navigator=object.__new__(ErpNavigator)
        navigator.page=SimpleNamespace(url='https://erp.test/',goto=AsyncMock())
        navigator.observed_urls={'https://erp.test/mm02'}
        navigator.allowed=lambda url:True
        with self.assertRaises(ValueError):await navigator.act(BrowserAction(operation='goto',url='https://erp.test/mm03'))
        with self.assertRaises(ValueError):await navigator.act(BrowserAction(operation='goto',url='https://erp.test/mm02'))
        navigator.page.goto.assert_not_awaited()

    async def test_access_is_erp_only_and_fixture_has_one_group(self):
        task,context,_=self.setup_context()
        self.assertEqual(task.technical.requirements[0].catalogue_group,'GR-8923')
        with self.assertRaises(ValueError):context.access_manager.for_role('mail')

    async def test_null_attribute_is_omitted_without_losing_material(self):
        response=ErpExtraction.model_validate({'material':{'material_id':'M1',
            'allocation_mass':None,'attributes':{'allocation_mass':None,'mass_unit':'g/piece'}},
            'passage_ids':['p1']})
        self.assertIsNone(response.material.allocation_mass)
        self.assertEqual(response.material.attributes,{'mass_unit':'g/piece'})
        with self.assertRaises(ValueError):
            ErpExtraction.model_validate({'material':{'material_id':'M1','attributes':{'mass_unit':123}},'passage_ids':['p1']})

    async def test_locale_prices_are_parsed_without_model_guessing(self):
        from decimal import Decimal
        self.assertEqual(number('270,566','en-US'),Decimal('270566'))
        self.assertEqual(number('2.930,65','de-DE'),Decimal('2930.65'))
        source={'url':'https://erp.test/vk13?material=M1&view=scales','text':
                'Gültig von\n01.01.2027, 00:00:00 UTC\nGültig bis\n31.12.2027, 23:59:59 UTC\n'
                'Währung: EUR\nPreiseinheit: 100 ST · Zahlenformat: en-US\n'
                '10 (en-US)\t254,332 (en-US)'}
        condition=prices(source)[0]
        self.assertEqual(condition.price,Decimal('254332'))
        self.assertEqual(condition.price_unit,100)
        self.assertEqual(condition.minimum_quantity,10)
        with self.assertRaises(ValueError):number('270,56','en-US')

    async def test_physical_and_inspection_stock_are_not_available_stock(self):
        self.assertIsNone(available_stock({'url':'https://erp.test/mmbe?level=locations','text':'Frei verwendbar 99'}))
        self.assertIsNone(available_stock({'url':'https://erp.test/mmbe?level=inspection','text':'Frei verwendbar 99'}))
        self.assertIsNone(available_stock({'url':'https://erp.test/mmbe?level=stock','text':'Physischer Bestand 99'}))

    async def test_observed_fields_recover_missing_specification_and_masses(self):
        fields=material_fields([{'url':'https://erp.test/mm03?material=M1','text':
            'Kataloggruppe GR-8923; Packinhalt 11 Stück/Karton; Umlagegewicht 3100 g/Stück '
            '(nicht Zuschlagsmasse); Zuschlagsmasse 2125 g/Stück; EN 1493\n'
            'Bohrung\tHYD_PRD\tmm\t80\nHub\tHYD_PRD\tmm\t200\n'
            'Betriebsdruck\tHYD_PRD\tbar\t210\nBefestigung\tHYD_PRD\t—\tSchwenkauge'}],'TEST')
        material=MaterialCandidate(material_id='M1',stock=0,
            stock_lead_weeks=2,production_lead_weeks=7,**fields)
        self.assertEqual(material.allocation_mass,3100)
        self.assertEqual(material.surcharge_mass,2125)
        self.assertEqual(material.specification.bore,80)
        self.assertEqual(material.specification.mounting,'Schwenkauge')
        self.assertEqual(material.pieces_per_carton,11)
        pending=missing_fields(material)
        self.assertIn('setup_fee',pending)
        self.assertFalse(any('lead' in p or 'mass' in p or 'specification' in p for p in pending))

    async def test_fee_requires_label_and_locale_and_preserves_zero(self):
        source={'url':'https://erp.test/mm03?tab=basic','text':'Rüstkosten: 0,00 EUR\nZahlenformat: de-DE'}
        self.assertEqual(material_fields([source],'TEST')['setup_fee'],0)
        source['text']='Rüstkosten: 1.234,50 EUR'
        self.assertNotIn('setup_fee',material_fields([source],'TEST'))

    async def test_status_is_decoded_from_observed_code_and_legend(self):
        source={'url':'https://erp.test/mm03?tab=planning',
                'text':'Materialstatus\n03\n01 freigegeben · 03 gesperrt\nMaterialstatus: 03'}
        self.assertIs(material_fields([source],'TEST')['active'],False)
        source['text']='Materialstatus: 01\n01 freigegeben · 03 gesperrt'
        self.assertIs(material_fields([source],'TEST')['active'],True)
        source['text']='Materialstatus: 03'
        self.assertIsNone(material_fields([source],'TEST')['active'])
        source['text']='Materialstatus: 03\nMaterialstatus: 01\n01 freigegeben · 03 gesperrt'
        self.assertIsNone(material_fields([source],'TEST')['active'])

    async def test_known_numeric_attributes_are_moved_before_validation(self):
        response=ErpExtraction.model_validate({'material':{'material_id':'M1',
            'attributes':{'pieces_per_carton':7,'allocation_mass':875,'mass_unit':'g'}},
            'passage_ids':['p1']})
        self.assertEqual(response.material.pieces_per_carton,7)
        self.assertEqual(response.material.allocation_mass,875)
        self.assertEqual(response.material.attributes,{'mass_unit':'g'})
        response=ErpExtraction.model_validate({'material':{'material_id':'M1',
            'pieces_per_carton':11,'attributes':{'pieces_per_carton':7}},'passage_ids':['p1']})
        self.assertEqual(response.material.pieces_per_carton,11)
        for invalid in (True,float('inf')):
            with self.assertRaises(ValueError):
                ErpExtraction.model_validate({'material':{'material_id':'M1',
                    'attributes':{'pieces_per_carton':invalid}},'passage_ids':['p1']})

    async def test_duplicate_attributes_do_not_keep_alternative_mass_units(self):
        material=MaterialCandidate(material_id='M1',allocation_mass=875,surcharge_mass=2125,
            pieces_per_carton=7,attributes={'allocation_mass':'0.875 kg per piece',
            'surcharge_mass':'2.125 kg per piece','allocation_mass_unit':'g',
            'surcharge_mass_unit':'g','allocation_mass_units':'kg','mass_unit':'kg',
            'pack_content':'7 pieces per carton','description':'Hydraulic cylinder'})
        self.assertEqual(canonical_attributes(material),{'allocation_mass_unit':'g',
            'surcharge_mass_unit':'g','description':'Hydraulic cylinder'})
        unknown=MaterialCandidate(material_id='M2',attributes={'allocation_mass':'not confirmed',
            'pack_content':'unknown','mass_unit':'g'})
        self.assertEqual(canonical_attributes(unknown),unknown.attributes)
