"""Engine tests on an invented scenario (no value from any real scenario)."""

from datetime import datetime, timezone
from decimal import Decimal

from janteiros.engine import Plan, compute, marginal_bands, split_cents
from janteiros.notebook import Notebook

REF = "XT-2031-0420"
SD = datetime(2031, 3, 10, 12, 0, tzinfo=timezone.utc)
DOM = "acme-fluid.test"


class Builder:
    def __init__(self):
        self.nb = Notebook()

    def add(self, fact_kind, **data):
        return self.nb.add(fact_kind, data, agent="test", step=0, origin={})

    def mail(self, kind, sender, date, ref=REF, mid=None, **data):
        return self.add("mail_fact", kind=kind, sender=sender, date_utc=date, reference=ref,
                        message_id=mid or f"{kind}-{date}", **data)

    def qa(self, number, date, kind, **data):
        return self.add("qa_effect", qa_number=number, date_utc=date, kind=kind, **data)


def base_scenario(b: Builder):
    b.add("mail_identity", address=f"sales@{DOM}")
    b.add("position", position="1.1", quantity=10, unit="Stk", unit_kind="piece", requirement="R-1")
    b.add("position", position="1.2", quantity=4, unit="Satz", unit_kind="set", requirement="R-2")
    b.add("position", position="1.3", quantity=7, unit="Stk", unit_kind="piece", requirement="R-1")
    b.add("spec_revision", revision="V-2", is_current=True, date="2031-02-01")
    b.add("spec_revision", revision="V-1", is_current=False, date="2031-01-01")
    b.add("requirement", revision="V-1", requirement="R-1", bore_mm=40, stroke_mm=100, pressure_bar=160,
          mounting="Flansch", certification="keine", catalog_group="KG-1")
    b.add("requirement", revision="V-2", requirement="R-1", bore_mm=40, stroke_mm=120, pressure_bar=160,
          mounting="Flansch", certification="keine", catalog_group="KG-1")
    b.add("requirement", revision="V-2", requirement="R-2", bore_mm=70, stroke_mm=300, pressure_bar=250,
          mounting="Schwenkauge", certification="ISO 9001", catalog_group="KG-2", stroke_is_usable=True,
          reserve_key="RS-9", pieces_per_set=3)
    # Q&A: out of order numbers, a withdrawn change, an amendment for another revision
    b.qa("50", "2031-02-10T08:00:00Z", "quantity", position="1.1", quantity=12)
    b.qa("12", "2031-02-12T08:00:00Z", "quantity", position="1.1", quantity=11)  # lower number, later date: wins
    b.qa("60", "2031-02-13T08:00:00Z", "quantity", position="1.2", quantity=9)
    b.qa("61", "2031-02-14T08:00:00Z", "withdraw", withdrawn_number="60")
    b.qa("70", "2031-02-15T08:00:00Z", "amendment", revision="V-2", requirement="R-2", attribute="stroke_mm", value="290")
    b.qa("71", "2031-02-16T08:00:00Z", "amendment", revision="V-1", requirement="R-2", attribute="stroke_mm", value="999")
    b.qa("72", "2031-02-17T08:00:00Z", "condition_group", position="1.2", group="KG-X")
    b.qa("73", "2031-02-17T08:00:00Z", "setup_fee", position="1.3", amount_eur="30,00")
    for p, pool, bonus in (("1.1", "none", "none"), ("1.2", "PL-1", "BN-1"), ("1.3", "PL-1", "BN-1")):
        b.qa("80", "2031-02-18T08:00:00Z", "keys", position=p, packaging_key="PK-1", delivery_key=f"DL-{p}",
             pool_key=pool, bonus_key=bonus)
    # ERP
    b.add("erp_search", query="KG-1", catalog_group="KG-1", results=["M-11", "M-12"], complete=True)
    b.add("material", material="M-11", catalog_group="KG-1", bore_mm=40, stroke_mm=120, pressure_bar=160,
          mounting="Flansch", certification="keine", is_active=False, pieces_per_carton=4,
          allocation_mass_g=500, surcharge_mass_g=1000, price_index="IX-1",
          lead_time_stock_weeks=1, lead_time_production_weeks=6)
    b.add("material", material="M-12", catalog_group="KG-1", bore_mm=40, stroke_mm=120, pressure_bar=160,
          mounting="Flansch", certification="none", is_active=True, pieces_per_carton=4,
          allocation_mass_g=500, surcharge_mass_g=1000, price_index="IX-1",
          lead_time_stock_weeks=2, lead_time_production_weeks=8)
    b.add("material", material="M-21", catalog_group="KG-2", bore_mm=70, stroke_mm=310, pressure_bar=250,
          mounting="Schwenkauge", certification="ISO 9001", is_active=True, pieces_per_carton=5,
          allocation_mass_g=1500, surcharge_mass_g=2000, price_index="IX-1",
          lead_time_stock_weeks=3, lead_time_production_weeks=10)
    b.add("stock", material="M-12", available_qty=15)
    b.add("stock", material="M-21", available_qty=100)
    b.add("price_condition", material="M-12", valid_from="2031-01-01", valid_to="2031-12-31", currency="EUR",
          price_unit=1, scales=[{"from_qty": 1, "price": "100.00"}, {"from_qty": 10, "price": "90.00"}])
    b.add("price_condition", material="M-21", valid_from="2030-01-01", valid_to="2030-12-31", currency="EUR",
          price_unit=1, scales=[{"from_qty": 1, "price": "999.00"}])
    b.add("price_condition", material="M-21", valid_from="2031-01-01T00:00:00Z", valid_to="2031-03-10T12:00:00Z",
          currency="USD", fx_key="FX-1", price_unit=100,
          scales=[{"from_qty": 1, "price": "50000"}, {"from_qty": 12, "price": "45000"}])
    # mail
    b.mail("reserve", f"technik@{DOM}", "2031-02-20T08:00:00Z", key="RS-9", revision="V-2", reserve_mm_per_end=10)
    b.mail("fx", f"finanzen@{DOM}", "2031-02-20T08:00:00Z", key="FX-1", currency="USD", eur_per_unit="0.9")
    b.mail("fx", f"finanzen@{DOM}", "2031-03-11T08:00:00Z", key="FX-1", currency="USD", eur_per_unit="2.0")  # future
    b.mail("metal_index", f"kalkulation@{DOM}", "2031-02-20T08:00:00Z", mid="ix", index="IX-1",
           rate_eur_per_kg="1.00", valid_from="2031-01-01", valid_to="2031-03-09")
    b.mail("metal_index", f"kalkulation@{DOM}", "2031-02-20T08:00:00Z", mid="ix", index="IX-1",
           rate_eur_per_kg="1.50", valid_from="2031-03-10", valid_to="2031-03-31")
    b.mail("condition_rule", f"kalkulation@{DOM}", "2031-02-20T08:00:00Z", group="KG-X", scope="base_and_surcharge")
    b.mail("setup_pool", f"kalkulation@{DOM}", "2031-02-20T08:00:00Z", key="PL-1", fee_eur="100.01",
           allocation="largest_remainder", tie_break="ascending")
    b.mail("packaging", f"logistik@{DOM}", "2031-02-20T08:00:00Z", key="PK-1", eur_per_carton="2.00")
    b.mail("packaging", f"logistik@{DOM}", "2031-02-20T08:00:00Z", ref=REF + "-B", key="PK-1", eur_per_carton="50.00")
    b.mail("delivery", f"logistik@{DOM}", "2031-02-20T08:00:00Z", key="DL-1.1", material="M-12", weeks=1, max_total_qty=17)
    b.mail("delivery", f"logistik@{DOM}", "2031-02-20T08:00:00Z", key="DL-1.3", material="M-12", weeks=1, max_total_qty=20)
    b.mail("bonus", f"finanzen@{DOM}", "2031-02-20T08:00:00Z", key="BN-1",
           bands=[{"from_eur": 0, "to_eur": 1000, "percent": 0}, {"from_eur": 1000, "to_eur": None, "percent": 10}])
    b.mail("discount_approval", f"leitung@{DOM}", "2031-02-01T08:00:00Z", percent=8, approves=True)
    b.mail("discount_approval", f"leitung@other.test", "2031-03-01T08:00:00Z", percent=20, approves=True)
    b.mail("discount_approval", f"leitung@{DOM}", "2031-02-25T08:00:00Z", percent=10, approves=True, min_order_value_eur="100.00")


def run(b: Builder):
    return compute(b.nb.ok(), Plan(reference=REF, scenario_date=SD))


def test_full_invented_scenario():
    b = Builder()
    base_scenario(b)
    r = run(b)
    pos = {p["position"]: p for p in r.answer["positions"]}
    assert r.decision == "bid"
    assert r.answer["discount_percent"] == 10.0
    # 1.1: quantity 11 (later date beats higher number), blocked M-11 skipped
    assert pos["1.1"]["quantity"] == 11 and pos["1.1"]["material_number"] == "M-12"
    # 1.2: withdrawn change -> 4 Satz * 3 = 12 pieces; stroke 290 + 2*10 = 310 -> M-21
    assert pos["1.2"]["quantity"] == 12 and pos["1.2"]["material_number"] == "M-21"
    # base: USD 45000 per 100 -> 450 USD * 0.9 = 405.00 EUR (future FX ignored)
    assert pos["1.2"]["base_unit_price"] == 405.0
    assert pos["1.1"]["base_unit_price"] == 90.0 and pos["1.3"]["base_unit_price"] == 100.0
    # surcharge: index window containing the scenario date -> 1.50 EUR/kg
    assert pos["1.1"]["surcharge_per_unit"] == 1.5
    # packaging per position, never merged: 1.1 ceil(11/4)=3 cartons * 2.00 / 11
    assert pos["1.1"]["packaging_per_unit"] == round(6 / 11, 2)
    # shared stock M-12: 11 + 7 = 18 > 15 -> production; commitment 1.1 cap 17 fails, 1.3 cap 20 applies
    assert pos["1.1"]["lead_time_weeks"] == 8 and pos["1.3"]["lead_time_weeks"] == 1
    assert pos["1.2"]["lead_time_weeks"] == 3
    # total is the sum of quantity * rounded unit price
    total = sum(Decimal(str(p["quantity"])) * Decimal(str(p["unit_price"])) for p in r.answer["positions"])
    assert Decimal(str(r.answer["total_net"])) == total.quantize(Decimal("0.01"))


def test_pool_and_bonus_allocation():
    b = Builder()
    base_scenario(b)
    r = run(b)
    pos = {p["position"]: p for p in r.answer["positions"]}
    # pool 100.01 by qty * allocation mass: 1.2 -> 12*1500=18000, 1.3 -> 7*500=3500
    alloc = split_cents(Decimal("100.01"), {"1.2": Decimal(18000), "1.3": Decimal(3500)})
    assert sum(alloc.values()) == Decimal("100.01")
    c13 = r.positions["1.3"]
    assert c13.pool == alloc["1.3"] and c13.setup == Decimal("30.00")
    # bonus: goods 12*405 + 7*100 = 5560 -> 10% of 4560 = 456.00
    assert pos["1.2"]["rebate_total"] + pos["1.3"]["rebate_total"] == 456.0
    assert pos["1.1"]["rebate_total"] == 0.0
    # 1.2 is in a base+surcharge group: both discounted; setup/pool never
    c12 = r.positions["1.2"]
    expected = (Decimal("405") * Decimal("0.9") + Decimal("3.0") * Decimal("0.9")
                + (c12.pool + c12.cartons_cost - c12.bonus) / 12).quantize(Decimal("0.01"))
    assert pos["1.2"]["unit_price"] == float(expected)


def test_threshold_failure_gives_zero_without_fallback():
    b = Builder()
    base_scenario(b)
    b.mail("discount_approval", f"leitung@{DOM}", "2031-03-01T08:00:00Z", percent=12, approves=True, min_order_value_eur="999999")
    r = run(b)
    assert r.answer["discount_percent"] == 0.0


def test_missing_material_gives_no_bid():
    b = Builder()
    base_scenario(b)
    b.add("position", position="1.4", quantity=2, unit="Stk", unit_kind="piece", requirement="R-3")
    b.add("requirement", revision="V-2", requirement="R-3", bore_mm=99, stroke_mm=99, pressure_bar=99,
          mounting="Flansch", certification="keine", catalog_group="KG-3")
    b.add("erp_search", query="KG-3", catalog_group="KG-3", results=["M-31"], complete=True)
    b.add("material", material="M-31", bore_mm=99, stroke_mm=98, pressure_bar=99, mounting="Flansch",
          certification="keine", is_active=True)
    r = run(b)
    assert r.decision == "no_bid"
    assert r.answer["positions"] == [] and r.answer["total_net"] == 0.0
    assert r.answer["discount_percent"] == 0.0  # conditional approval yields 0 % for no_bid


def test_uninvestigated_material_is_omitted_not_no_bid():
    b = Builder()
    base_scenario(b)
    b.add("position", position="1.4", quantity=2, unit="Stk", unit_kind="piece", requirement="R-3")
    b.add("requirement", revision="V-2", requirement="R-3", bore_mm=99, stroke_mm=99, pressure_bar=99,
          mounting="Flansch", certification="keine", catalog_group="KG-3")
    r = run(b)
    assert r.decision == "bid"
    assert r.readiness["1.4"] == "omit"
    assert "1.4" not in {p["position"] for p in r.answer["positions"]}


def test_helpers():
    assert marginal_bands(Decimal("20000"), [{"from_eur": 0, "to_eur": 5000, "percent": 0},
                                             {"from_eur": 5000, "to_eur": 15000, "percent": 2},
                                             {"from_eur": 15000, "to_eur": None, "percent": 5}]) == Decimal("450.00")
    a = split_cents(Decimal("0.02"), {"b": Decimal(1), "a": Decimal(1), "c": Decimal(1)})
    assert a == {"a": Decimal("0.01"), "b": Decimal("0.01"), "c": Decimal("0.00")}
