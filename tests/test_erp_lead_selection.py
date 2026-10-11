import unittest
from core.contracts import MaterialCandidate,MaterialSelection,TenderPosition,TechnicalRequirement
from tools.erp_selection import select_lead_times


class LeadTests(unittest.TestCase):
    def setUp(self):
        self.material=MaterialCandidate(material_id='M',stock=31,stock_lead_weeks=2,production_lead_weeks=7)
        self.req=TechnicalRequirement(requirement_id='R',pieces_per_set=2)
        self.sel=MaterialSelection(requirement_id='R',status='selected',material_id='M')

    def run_selection(self,quantities,unit='ST',material=None):
        return select_lead_times([TenderPosition(position_id=str(i),requirement_id='R',quantity=q,unit=unit)
            for i,q in enumerate(quantities)],[self.req],[self.sel],[material or self.material])

    def test_stock_covers_demand_including_exact_boundary(self):
        for quantity in (10,31):
            lead=self.run_selection([quantity])[0]
            self.assertEqual((lead.status,lead.basis,lead.duration,lead.unit),('selected','stock',2,'weeks'))

    def test_shared_demand_uses_production_for_every_position(self):
        result=self.run_selection([20,20])
        self.assertTrue(all((r.demand_pieces,r.basis,r.duration)==(40,'production',7) for r in result))

    def test_sets_are_converted_before_pooling(self):
        result=self.run_selection([8,8],unit='Satz')
        self.assertTrue(all(r.demand_pieces==32 and r.basis=='production' for r in result))

    def test_unknown_quantity_stock_and_applicable_time_stay_pending(self):
        self.assertTrue(all(r.status=='incomplete' for r in self.run_selection([10,None])))
        for changes in ({'stock':None},{'stock_lead_weeks':None},{'stock_lead_days':3}):
            self.assertEqual(self.run_selection([10],material=self.material.model_copy(update=changes))[0].status,'incomplete')

    def test_original_days_and_zero_duration_are_preserved(self):
        lead=self.run_selection([10],material=self.material.model_copy(update={'stock_lead_weeks':None,'stock_lead_days':0}))[0]
        self.assertEqual((lead.status,lead.duration,lead.unit),('selected',0,'days'))
