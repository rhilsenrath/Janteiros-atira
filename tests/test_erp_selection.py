import unittest
from core.contracts import MaterialCandidate,TechnicalRequirement
from tools.erp_selection import select_material


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.requirement=TechnicalRequirement(requirement_id='R',bore=80,stroke=200,
            pressure=210,mounting='Schwenkauge',certification='EN 1493',stroke_kind='nominal')

    def material(self,name,active=True):
        return MaterialCandidate(material_id=name,active=active,specification=self.requirement)

    def test_active_exact_match_selected_and_blocked_rejected(self):
        result=select_material(self.requirement,[self.material('blocked',False),self.material('active')])
        self.assertEqual(result.status,'selected')
        self.assertEqual(result.material_id,'active')
        self.assertIn('Material is blocked',result.reasons['blocked'])

    def test_each_required_attribute_must_match(self):
        for field,value in (('bore',90),('stroke',210),('pressure',200),
                            ('mounting','other'),('certification','other')):
            candidate=self.material('M').model_copy(update={'specification':
                self.requirement.model_copy(update={field:value})})
            self.assertEqual(select_material(self.requirement,[candidate]).status,'no_match')

    def test_unknown_status_or_specification_is_incomplete(self):
        self.assertEqual(select_material(self.requirement,[self.material('M',None)]).status,'incomplete')
        candidate=MaterialCandidate(material_id='M',active=True)
        self.assertEqual(select_material(self.requirement,[candidate]).status,'incomplete')

    def test_multiple_matches_and_unread_candidates_do_not_select(self):
        self.assertEqual(select_material(self.requirement,[self.material('A'),self.material('B')]).status,'ambiguous')
        result=select_material(self.requirement,[self.material('A')],complete=False)
        self.assertEqual(result.status,'incomplete')
        self.assertIsNone(result.material_id)

    def test_missing_requirement_and_usable_stroke_need_upstream_data(self):
        for update in ({'bore':None},{'stroke_kind':'usable'}):
            result=select_material(self.requirement.model_copy(update=update),[self.material('A')])
            self.assertEqual(result.status,'incomplete')
