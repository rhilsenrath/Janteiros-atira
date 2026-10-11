"""Pricing & Decision Agent: the brief's rules on a hand-calculated scenario, edge cases and bid/no-bid decisions.

Hand calculation of the main scenario (scenario date 2027-01-25, approved discount 10 %):
  01.01  25 pcs M1 scale>=20 95.00 EUR | surcharge 0.5 kg x 5.00 = 2.50 | base only -> 85.50 + 2.50
         cartons ceil(25/10)=3 x 1.20 = 3.60 -> 0.144/pc                     unit 88.144 -> 88.14, stock 1 wk
  01.02  10 Satz x 2 = 20 pcs M2 scale>=1 2600 USD/100 x 0.9 = 23.40 | surcharge 1.5 kg x 5.00 = 7.50
         K-1 base+surcharge -> 21.06 + 6.75 | setup 30 (Q&A) + pool 68.97 + cartons 5 x 1.20 - bonus 7.29
         = 97.68 / 20 = 4.884                                                unit 32.694 -> 32.69, production 8
  01.03  9 pcs M2 23.40 | K-2 none -> 23.40 + 7.50 | setup 10 (ERP) + pool 31.03 + 3 x 1.20 - bonus 3.28
         = 41.35 / 9 = 4.5944..                                             unit 35.4944 -> 35.49, commitment 3
  pool 100.00 by pieces x allocation mass: 40 : 18 -> 68.9655 / 31.0344 -> floor 68.96 + 31.03, +1 ct to 01.02
  bonus on 468.00 + 210.60 = 678.60: 300 x 1 % + 378.60 x 2 % = 10.572 -> 10.57 -> 7.2896 / 3.2804
        -> floor 7.28 + 3.28, +1 ct to 01.02
  threshold: 25 x 95.00 + 20 x 23.40 + 9 x 23.40 = 3053.60 >= 500
  total: 25 x 88.14 + 20 x 32.69 + 9 x 35.49 = 2203.50 + 653.80 + 319.41 = 3176.71
"""
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

from agents.base import AgentContext
from agents.pricing import PARTIAL_SUBMISSION, PricingAgent
from core.config import RunConfig
from core.contracts import (AgentStatus, AgentTask, CommercialPolicies, CommercialRule, ERPData, Evidence,
    MaterialCandidate, PriceCondition, SpecificationRevision, TechnicalRequirement, TechnicalRequirements,
    TenderData, TenderPosition)
from services.bid_validator import BidValidator
from services.commercial_terms import (AuthorityReading, Band, CommercialReading, ResolvedTerms, Term, TermReading,
    Window, norm)
from services.pricing_engine import PricingEngine, PricingInput, allocate_cents, marginal_credit
from test_architecture import RUNTIME

REF, SPEC = "VG-1", "LH-VG-1"
SCENARIO = datetime(2027, 1, 25, 9, tzinfo=timezone.utc)


def ev(name):
    return [Evidence(id=f"fixture:{name}", agent_id="test", source_site="fixture", excerpt=name)]


def day(text):
    return datetime.fromisoformat(text)


def requirement(rid, bore, stroke, **extra):
    return TechnicalRequirement(requirement_id=rid, revision="R-2", specification_id=SPEC, catalogue_group=f"GR-{rid}",
        bore=D(bore), stroke=D(stroke), pressure=D("160"), mounting="Flansch", certification="EN 1493",
        stroke_kind="nominal", evidence=ev(rid), **extra)


def technical(*requirements):
    return TechnicalRequirements(evidence=ev("spec"), revisions=[
        SpecificationRevision(specification_id=SPEC, revision="R-1", status="ersetzt", current=False),
        SpecificationRevision(specification_id=SPEC, revision="R-2", status="gültig", current=True)],
        requirements=list(requirements or (requirement("A-1", "50", "200"),
                                           requirement("A-2", "63", "482", pieces_per_set=D("2")))))


def position(pid, qty, unit, rid, **keys):
    return TenderPosition(position_id=pid, quantity=D(qty), unit=unit, requirement_id=rid,
                          specification_reference=SPEC, evidence=ev(pid), **keys)


def tender(*positions):
    return TenderData(reference=REF, scenario_date=SCENARIO, evidence=ev("tender"), positions=list(positions or (
        position("01.01", "25", "Stück", "A-1", packaging_key="VP-1"),
        position("01.02", "10", "Satz", "A-2", condition_group="K-1", packaging_key="VP-1", setup_pool="RP-1",
                 setup_pool_member=True, bonus_plan="PB-1", bonus_member=True, setup_fee=D("30")),
        position("01.03", "9", "Stk", "A-2", condition_group="K-2", packaging_key="VP-1", delivery_key="TZ-1",
                 setup_pool="RP-1", setup_pool_member=True, bonus_plan="PB-1", bonus_member=True))))


def condition(cid, price, minimum, currency="EUR", unit="1", fx="EUR", start="2026-12-01", end="2027-12-31"):
    return PriceCondition(condition_id=cid, price=D(price), currency=currency, price_unit=D(unit),
                          minimum_quantity=D(minimum), valid_from=day(start), valid_until=day(end), fx_key=fx)


def material(mid, spec, *conditions, active=True, **values):
    base = dict(stock=D("30"), stock_lead_weeks=1, production_lead_weeks=6, records_complete=True)
    return MaterialCandidate(material_id=mid, active=active, specification=spec, conditions=list(conditions),
                             evidence=ev(mid), **{**base, **values})


def erp(*materials, searched=("GR-A-1", "GR-A-2")):
    m1 = material("M1", requirement("M1", "50", "200"), condition("C1-1", "100.00", "1"),
                  condition("C1-2", "95.00", "20"), condition("OLD", "50.00", "1", start="2026-01-01",
                                                                     end="2026-12-31"),
                  surcharge_mass=D("500"), attributes={"surcharge_mass": "500 g/Stück"}, metal_index_code="MET-1",
                  pieces_per_carton=D("10"))
    m2 = material("M2", requirement("M2", "63", "482"),
                  condition("C2-1", "2600", "1", currency="USD", unit="100", fx="FX-USD"),
                  condition("C2-2", "2500", "25", currency="USD", unit="100", fx="FX-USD"),
                  surcharge_mass=D("1.5"), metal_index_code="MET-1", allocation_mass=D("2"),
                  attributes={"surcharge_mass": "1,5 kg/Stück", "allocation_mass": "2 kg/Stück"},
                  pieces_per_carton=D("4"), stock=D("25"), production_lead_weeks=8, setup_fee=D("10"))
    blocked = material("M3", requirement("M3", "50", "200"), condition("C3", "1.00", "1"), active=False)
    other = material("M4", requirement("M4", "50", "250"), condition("C4", "1.00", "1"))
    return ERPData(evidence=ev("erp"), searched_catalogue_groups=list(searched),
                   materials=list(materials or (m1, m2, blocked, other)))


def term(topic, key=None, **values):
    return Term(topic=topic, key=key, rule_key=f"mail-{topic}-{key}", dated_at=SCENARIO, **values)


def terms(discount=D("10"), minimum=D("500"), drop=(), **override):
    items = {
        "condition_rules|K-1": term("condition_rules", "K-1", clause="base_and_surcharge"),
        "condition_rules|K-2": term("condition_rules", "K-2", clause="none"),
        "metal_index|MET-1": term("metal_index", "MET-1", mass_unit="kg", windows=[
            Window(valid_from=day("2027-01-01"), valid_until=day("2027-01-20"), rate=D("4.00")),
            Window(valid_from=day("2027-01-21"), valid_until=day("2027-02-28"), rate=D("5.00"))]),
        "packaging_tariff|VP-1": term("packaging_tariff", "VP-1", rate=D("1.20")),
        "fx_rate|FX-USD": term("fx_rate", "FX-USD", rate=D("0.9"), fx_direction="eur_per_unit", currency="USD"),
        "setup_pool|RP-1": term("setup_pool", "RP-1", pool_fee=D("100.00"), allocation_method="largest_remainder",
                                tie_break="position_ascending"),
        "project_bonus|PB-1": term("project_bonus", "PB-1", bands=[Band(lower=D("0"), upper=D("300"), percent=D("1")),
                                                                    Band(lower=D("300"), percent=D("2"))]),
        "delivery_commitment|TZ-1": term("delivery_commitment", "TZ-1", material="M2", ceiling=D("30"), weeks=3),
        **override}
    by_key = {f"{slot.split('|')[0]}|{norm(slot.split('|')[1])}": t for slot, t in items.items() if slot not in drop}
    approval = term("discount_approval", approves=True, percent=discount, minimum_order_value=minimum) \
        if discount is not None else None
    return ResolvedTerms(discount=approval, discount_checked=True, by_key=by_key)


def evaluate(tender_data=None, technical_data=None, erp_data=None, resolved=None):
    return PricingEngine().evaluate(PricingInput(tender=tender_data or tender(), technical=technical_data or technical(),
        erp=erp_data or erp(), commercial=CommercialPolicies(evidence=ev("mail")),
        terms=resolved if resolved is not None else terms()))


def by_position(bid):
    return {p.position: p for p in bid.positions}


class PricingRuleTests(unittest.TestCase):
    def test_hand_calculated_bid(self):
        outcome = evaluate()
        bid = outcome.bid
        self.assertTrue(bid.complete, outcome.requests)
        self.assertEqual((bid.decision, bid.tender_reference, bid.discount_percent), ("bid", REF, D("10")))
        lines = by_position(bid)
        expected = {  # unit, pieces, material, lead, base, surcharge/pc, packaging/pc, rebate
            "01.01": ("88.14", "25", "M1", 1, "95.00", "2.50", "0.14", "0.00"),
            "01.02": ("32.69", "20", "M2", 8, "23.40", "12.45", "0.30", "7.29"),
            "01.03": ("35.49", "9", "M2", 3, "23.40", "12.06", "0.40", "3.28")}
        for pid, (unit, pieces, mid, lead, base, surcharge, packaging, rebate) in expected.items():
            line = lines[pid]
            self.assertEqual((line.unit_price, line.quantity, line.material_number, line.lead_time_weeks),
                             (D(unit), D(pieces), mid, lead), pid)
            self.assertEqual((line.base_unit_price, line.surcharge_per_unit, line.packaging_per_unit,
                              line.rebate_total), (D(base), D(surcharge), D(packaging), D(rebate)), pid)
        self.assertEqual(bid.total_net, D("3176.71"))
        self.assertEqual(bid.total_net, sum((p.quantity * p.unit_price for p in bid.positions), D("0")))
        audit = {line.position: line for line in outcome.lines}
        self.assertEqual((audit["01.02"].shared_setup, audit["01.03"].shared_setup), (D("68.97"), D("31.03")))
        self.assertEqual(audit["01.03"].lead_basis, "commitment TZ-1")
        self.assertTrue(BidValidator().validate(bid, {}, tender=tender()).valid)

    def test_discount_threshold_is_inclusive_and_failure_means_zero(self):
        self.assertEqual(evaluate(resolved=terms(minimum=D("3053.60"))).bid.discount_percent, D("10"))
        bid = evaluate(resolved=terms(minimum=D("3053.61"))).bid
        self.assertEqual(bid.discount_percent, D("0"))
        self.assertEqual(by_position(bid)["01.01"].unit_price, D("97.64"))  # 95 + 2.50 + 0.144, undiscounted
        self.assertEqual(evaluate(resolved=terms(discount=None)).bid.discount_percent, D("0"))  # no approval

    def test_scale_is_per_position_and_boundary_inclusive(self):
        exactly = tender(position("01.01", "20", "Stk", "A-1"))
        below = tender(position("01.01", "19", "Stk", "A-1"))
        self.assertEqual(by_position(evaluate(exactly).bid)["01.01"].base_unit_price, D("95.00"))
        self.assertEqual(by_position(evaluate(below).bid)["01.01"].base_unit_price, D("100.00"))
        # M2: 29 pooled pieces would reach the 25-scale, but each position stays on its own quantity.
        self.assertEqual(by_position(evaluate().bid)["01.02"].base_unit_price, D("23.40"))

    def test_shared_stock_covers_the_pooled_demand_or_nobody_uses_it(self):
        two = tender(position("01.01", "20", "Stk", "A-1"), position("01.02", "15", "Stk", "A-1"))
        bid = evaluate(two).bid  # 20 + 15 = 35 > stock 30, though each alone is covered
        self.assertEqual([p.lead_time_weeks for p in bid.positions], [6, 6])
        covered = tender(position("01.01", "20", "Stk", "A-1"), position("01.02", "10", "Stk", "A-1"))
        self.assertEqual([p.lead_time_weeks for p in evaluate(covered).bid.positions], [1, 1])

    def test_commitment_needs_matching_material_and_pooled_ceiling(self):
        resolved = terms(**{"delivery_commitment|TZ-1": term("delivery_commitment", "TZ-1", material="M2",
                                                             ceiling=D("28"), weeks=3)})
        self.assertEqual(by_position(evaluate(resolved=resolved).bid)["01.03"].lead_time_weeks, 8)  # 29 > 28
        resolved = terms(**{"delivery_commitment|TZ-1": term("delivery_commitment", "TZ-1", material="M1",
                                                             ceiling=D("99"), weeks=3)})
        self.assertEqual(by_position(evaluate(resolved=resolved).bid)["01.03"].lead_time_weeks, 8)

    def test_cent_allocation_rules(self):
        weights = {"01.03": D("1"), "01.02": D("1"), "01.01": D("1")}
        self.assertEqual(allocate_cents(D("1.00"), weights),
                         {"01.01": D("0.34"), "01.02": D("0.33"), "01.03": D("0.33")})  # tie: ascending
        self.assertEqual(allocate_cents(D("1.00"), weights, "largest_remainder", "position_descending")["01.03"],
                         D("0.34"))
        last = allocate_cents(D("1.00"), weights, "round_last_absorbs")
        self.assertEqual((last["01.01"], last["01.03"], sum(last.values())), (D("0.33"), D("0.34"), D("1.00")))

    def test_marginal_bonus_bands(self):
        bands = [Band(lower=D("0"), upper=D("300"), percent=D("1")), Band(lower=D("300"), percent=D("2"))]
        self.assertEqual(marginal_credit(D("678.60"), bands), D("10.57"))
        self.assertEqual(marginal_credit(D("250"), bands), D("2.50"))

    def test_no_bid_only_when_the_search_confirms_no_active_match(self):
        only_blocked = erp(*[m for m in erp().materials if m.material_id != "M1"])
        outcome = evaluate(erp_data=only_blocked)
        bid = outcome.bid
        self.assertTrue(outcome.confirmed_no_bid)
        self.assertEqual((bid.decision, bid.positions, bid.total_net, bid.complete), ("no_bid", [], D("0"), True))
        self.assertEqual(bid.discount_percent, D("0"))  # conditional approval yields 0 % for no_bid
        self.assertEqual(evaluate(erp_data=only_blocked, resolved=terms(minimum=None)).bid.discount_percent, D("10"))
        self.assertTrue(BidValidator().validate(bid, {}, tender=tender()).valid)
        # A candidate of the searched group with unread certification: it cannot be ruled out yet.
        unread = erp(*[m.model_copy(update={"specification": m.specification.model_copy(update={
            "catalogue_group": "GR-A-1", "certification": None})}) if m.material_id == "M3" else m
            for m in only_blocked.materials])
        outcome = evaluate(erp_data=unread)
        self.assertFalse(outcome.confirmed_no_bid)
        self.assertTrue(any("M3" in r.question for r in outcome.requests))
        # The group was never searched completely: an incomplete investigation, not a no_bid.
        unsearched = only_blocked.model_copy(update={"searched_catalogue_groups": ["GR-A-2"]})
        outcome = evaluate(erp_data=unsearched)
        self.assertFalse(outcome.confirmed_no_bid)
        self.assertNotEqual(outcome.bid.decision, "no_bid")
        self.assertTrue(any(r.agent_id == "erp" and "GR-A-1" in r.question for r in outcome.requests))

    def test_missing_facts_become_targeted_requests_and_partial_submission(self):
        outcome = evaluate(resolved=terms(drop=("packaging_tariff|VP-1",)))
        self.assertFalse(outcome.bid.complete)
        self.assertEqual(sorted(outcome.bid.omitted_positions), ["01.01", "01.02", "01.03"])
        self.assertTrue(any(r.agent_id == "mail" and "VP-1" in r.question for r in outcome.requests))
        no_tariff = tender(position("01.01", "25", "Stück", "A-1"),
                           position("01.02", "9", "Stk", "A-2", packaging_key="VP-9"))
        outcome = evaluate(no_tariff)
        self.assertTrue(outcome.partial_possible)
        self.assertEqual(outcome.bid.omitted_positions, ["01.02"])
        self.assertFalse(BidValidator().validate(outcome.bid, {}, tender=no_tariff).valid)
        partial = PricingEngine().partial_submission(outcome.bid)
        self.assertTrue(partial.complete)
        self.assertEqual(partial.total_net, D("25") * by_position(partial)["01.01"].unit_price)
        self.assertIn("01.02", partial.notes)
        self.assertTrue(BidValidator().validate(partial, {}, tender=no_tariff).valid)

    def test_unknown_material_status_or_unsearched_requirement_is_never_guessed(self):
        unknown = erp(*[m.model_copy(update={"active": None}) if m.material_id == "M1" else m
                        for m in erp().materials])
        outcome = evaluate(erp_data=unknown)
        self.assertIn("01.01", outcome.bid.omitted_positions)
        self.assertTrue(any("M1" in r.question and "status" in r.question for r in outcome.requests))

    def test_metal_rate_uses_the_window_containing_the_scenario_date(self):
        early = TenderData.model_validate({**tender().model_dump(), "scenario_date": datetime(2027, 1, 10,
                                                                                               tzinfo=timezone.utc)})
        line = by_position(evaluate(early).bid)["01.01"]
        self.assertEqual(line.surcharge_per_unit, D("2.00"))  # 0.5 kg x 4.00 in the earlier window

    def test_validator_rejects_inconsistent_bids(self):
        bid = evaluate().bid
        wrong_total = bid.model_copy(update={"total_net": bid.total_net + D("0.01")})
        self.assertIn("Total is inconsistent", BidValidator().validate(wrong_total, {}, tender=tender()).issues)
        missing = bid.model_copy(update={"positions": bid.positions[:2], "total_net": sum(
            (p.quantity * p.unit_price for p in bid.positions[:2]), D("0"))})
        self.assertFalse(BidValidator().validate(missing, {}, tender=tender()).valid)
        self.assertFalse(BidValidator().validate(bid, {}).valid)  # no tender to check against


class FakeModel:
    def __init__(self, reading):
        self.reading = reading
        self.calls = []

    async def ask(self, system, user, *, output_type=None, tools=None):
        self.calls.append(user)
        return SimpleNamespace(structured=self.reading)


class PricingAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_reads_messages_and_applies_only_the_authorized_exact_reference_approval(self):
        def rule(key, sender, date, ref, text):
            return CommercialRule(key=key, kind="mail", text=text, authorized_sender=sender, tender_reference=ref,
                                  effective_at=datetime.fromisoformat(date), evidence=ev(key))
        rules = [rule("m1", "leitung@linden.example", "2027-01-10T08:00:00+00:00", REF, "Freigabe VG-1: 8 % Rabatt."),
                 rule("m2", "vertrieb@linden.example", "2027-01-20T08:00:00+00:00", REF, "Freigabe VG-1: 15 % Rabatt."),
                 rule("m3", "leitung@linden.example", "2027-01-21T08:00:00+00:00", "VG-2", "Freigabe VG-2: 12 % Rabatt."),
                 rule("m4", "leitung@linden.example", "2027-02-01T08:00:00+00:00", REF, "Freigabe VG-1: 9 % Rabatt.")]
        reading = CommercialReading(
            authorities=[AuthorityReading(topic="discount_approval", sender_local_part="leitung",
                                          quote="leitung@ followed by the bidder's mailbox domain")],
            terms=[TermReading(rule_key=key, topic="discount_approval", tender_reference=ref, approves=True,
                               percent=pct, quote=f"{pct} % Rabatt")
                   for key, ref, pct in (("m1", REF, "8"), ("m2", REF, "15"), ("m3", "VG-2", "12"), ("m4", REF, "9"))])
        commercial = CommercialPolicies(evidence=ev("mail"), mailbox_address="vertrieb@linden.example", rules=rules)
        single = tender(position("01.01", "25", "Stück", "A-1"))
        plain = erp(material("M1", requirement("M1", "50", "200"), condition("C", "40.00", "1"), stock=D("100")))
        model = FakeModel(reading)
        brief = "Use the most recent message from the sales director (leitung@ followed by the bidder's mailbox domain)"
        context = AgentContext({**RUNTIME, "brief": brief}, RunConfig(), llm=model)
        result = await PricingAgent().execute(AgentTask(action="price", tender=single, technical=technical(),
                                                        erp=plain, commercial=commercial), context)
        self.assertEqual(result.status, AgentStatus.complete, result.missing)
        self.assertEqual(result.output.discount_percent, D("8"))  # m2 unauthorized, m3 other ref, m4 future
        self.assertEqual(result.output.positions[0].unit_price, D("36.80"))
        self.assertEqual(result.output.total_net, D("920.00"))
        self.assertEqual(len(model.calls), 1)

    async def test_agent_requests_follow_ups_and_finalizes_a_partial_submission(self):
        commercial = CommercialPolicies(evidence=ev("mail"), mailbox_address="vertrieb@linden.example")
        partial_tender = tender(position("01.01", "25", "Stück", "A-1"),
                                position("01.02", "9", "Stk", "A-2", packaging_key="VP-9"))
        agent = PricingAgent()
        agent.engine = SimpleNamespace(evaluate=lambda inputs: PricingEngine().evaluate(
            inputs.model_copy(update={"terms": terms()})), partial_submission=PricingEngine.partial_submission)
        context = AgentContext(RUNTIME, RunConfig())
        task = AgentTask(action="price", tender=partial_tender, technical=technical(), erp=erp(), commercial=commercial)
        result = await agent.execute(task, context)
        self.assertEqual(result.status, AgentStatus.partial)
        self.assertTrue(any(m.agent_id == "mail" and "VP-9" in m.question for m in result.missing))
        result = await agent.execute(task.model_copy(update={"question": PARTIAL_SUBMISSION}), context)
        self.assertEqual(result.status, AgentStatus.complete)
        self.assertEqual(result.output.omitted_positions, ["01.02"])


if __name__ == "__main__":
    unittest.main()
