import unittest
from datetime import datetime,timezone
from decimal import Decimal
from core.contracts import PriceCondition,MaterialCandidate,MaterialSelection,TenderPosition,TechnicalRequirement
from tools.erp_selection import select_price


class PriceTests(unittest.TestCase):
    def setUp(self):
        self.date=datetime(2027,1,25,tzinfo=timezone.utc)
        self.requirement=TechnicalRequirement(requirement_id='R',pieces_per_set=2)
        self.position=TenderPosition(position_id='P',requirement_id='R',quantity=10,unit='ST')
        self.selection=MaterialSelection(requirement_id='R',status='selected',material_id='M')
        self.conditions=[PriceCondition(price=p,price_unit=100,minimum_quantity=q,currency='EUR',
            valid_from=datetime(2027,1,1,tzinfo=timezone.utc),
            valid_until=datetime(2027,12,31,tzinfo=timezone.utc)) for q,p in ((1,300000),(10,250000),(25,200000))]

    def run_selection(self,conditions=None,position=None,date=None,selection=None):
        return select_price(position or self.position,self.requirement,selection or self.selection,
            [MaterialCandidate(material_id='M',conditions=conditions if conditions is not None else self.conditions)],
            date or self.date)

    def test_highest_reached_scale_preserves_unit_and_unrounded_price(self):
        result=self.run_selection()
        self.assertEqual(result.status,'selected')
        self.assertEqual(result.condition.minimum_quantity,10)
        self.assertEqual(result.condition.price_unit,100)
        self.assertEqual(result.base_price_per_piece,Decimal('2500'))

    def test_date_bounds_are_inclusive_and_expired_conditions_are_ignored(self):
        for date in (self.conditions[0].valid_from,self.conditions[0].valid_until):
            self.assertEqual(self.run_selection(date=date).status,'selected')
        self.assertEqual(self.run_selection(date=datetime(2028,1,1,tzinfo=timezone.utc)).status,'no_match')

    def test_sets_convert_before_scale_selection(self):
        result=self.run_selection(position=self.position.model_copy(update={'quantity':Decimal('13'),'unit':'Satz'}))
        self.assertEqual(result.quantity_pieces,26)
        self.assertEqual(result.condition.minimum_quantity,25)

    def test_unknown_inputs_and_conflicting_prices_do_not_select(self):
        self.assertEqual(self.run_selection(position=self.position.model_copy(update={'unit':'unknown'})).status,'incomplete')
        conflict=self.conditions[1].model_copy(update={'price':Decimal('123')})
        self.assertEqual(self.run_selection(conditions=self.conditions+[conflict]).status,'ambiguous')
        self.assertEqual(self.run_selection(conditions=[self.conditions[0].model_copy(update={'price_unit':Decimal('0')})]).status,'incomplete')
        self.assertEqual(self.run_selection(selection=self.selection.model_copy(update={'status':'incomplete'})).status,'incomplete')

    def test_identical_duplicate_conditions_are_not_ambiguous(self):
        self.assertEqual(self.run_selection(conditions=self.conditions+[self.conditions[1]]).status,'selected')
