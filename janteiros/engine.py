"""Motor de regras: a pure function from the notebook to the answer, the worksheet and the readiness.

All bid rules of the brief live here, in code, with Decimal arithmetic and HALF_UP rounding only where
the rules say so. The model never calculates; it only records what it read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from .normalize import (
    loose_id,
    norm_cert,
    norm_id,
    norm_mounting,
    parse_datetime,
    round_cents,
    to_decimal,
)
from .notebook import Fact

ZERO = Decimal("0")
ATTRS = ("bore_mm", "stroke_mm", "pressure_bar", "mounting", "certification")

# who may send which kind of record (local part + bidder mailbox domain); the brief states these for every scenario
DEFAULT_SENDERS = {
    "discount_approval": "leitung",
    "condition_rule": "kalkulation",
    "metal_index": "kalkulation",
    "setup_pool": "kalkulation",
    "reserve": "technik",
    "packaging": "logistik",
    "delivery": "logistik",
    "fx": "finanzen",
    "bonus": "finanzen",
}


@dataclass
class Plan:
    reference: str
    scenario_date: datetime
    bidder_company: str = ""
    bidder_vat: str = ""
    bidder_domain: str = ""
    senders: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SENDERS))


@dataclass
class PositionCalc:
    position: str
    requirement: str = ""
    quantity: int | None = None
    unit: str = ""
    attrs: dict[str, Any] = field(default_factory=dict)
    material: str | None = None
    material_status: str = "unknown"  # found | missing_confirmed | unknown | ambiguous
    base: Decimal | None = None
    surcharge_mass: Decimal | None = None
    setup: Decimal = ZERO
    pool: Decimal | None = ZERO
    cartons_cost: Decimal | None = None
    bonus: Decimal | None = None
    scope: str = "base_only"
    unit_price: Decimal | None = None
    lead_time: int | None = None
    notes: list[str] = field(default_factory=list)
    used: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class EngineResult:
    answer: dict
    positions: dict[str, PositionCalc]
    readiness: dict[str, str]
    missing: list[str]
    decision: str
    discount: Decimal
    notes: list[str]

    def worksheet(self) -> dict:
        out = {"decision": self.decision, "discount_percent": str(self.discount), "notes": self.notes, "missing": self.missing, "positions": {}}
        for p, c in self.positions.items():
            out["positions"][p] = {
                "requirement": c.requirement, "quantity": c.quantity, "unit": c.unit,
                "attrs": {k: str(v) for k, v in c.attrs.items()}, "material": c.material, "material_status": c.material_status,
                "base": str(c.base) if c.base is not None else None, "scope": c.scope,
                "surcharge_mass_rate": str(c.surcharge_mass) if c.surcharge_mass is not None else None,
                "setup": str(c.setup), "pool": str(c.pool) if c.pool is not None else None,
                "cartons_cost": str(c.cartons_cost) if c.cartons_cost is not None else None,
                "bonus": str(c.bonus) if c.bonus is not None else None,
                "unit_price": str(c.unit_price) if c.unit_price is not None else None, "lead_time": c.lead_time,
                "readiness": self.readiness.get(p), "notes": c.notes, "facts": c.used,
            }
        return out


def _dt(v) -> datetime | None:
    return parse_datetime(v) if v else None


def _num_key(s: str) -> tuple:
    d = to_decimal(s)
    return (0, d) if d is not None else (1, str(s))


def _pos_key(p: str) -> tuple:
    parts = []
    for chunk in str(p).replace("-", ".").split("."):
        d = to_decimal(chunk)
        parts.append((0, d) if d is not None else (1, chunk))
    return tuple(parts)


def _same_pos(a, b) -> bool:
    return loose_id(a) == loose_id(b) and loose_id(a) != ""


def _merge_materials(facts: list[Fact]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in facts:
        m = norm_id(f.data.get("material"))
        if not m:
            continue
        cur = out.setdefault(m, {"material": f.data.get("material"), "_facts": []})
        for k, v in f.data.items():
            if v is None or v == "" or k == "evidence":
                continue
            cur[k] = v
        cur["_facts"].append(f.id)
    return out


def _sender_ok(sender: str, local: str, domain: str) -> bool:
    s = (sender or "").strip().lower()
    if "@" not in s or not local:
        return False
    lp, dom = s.rsplit("@", 1)
    return lp == local.lower() and (not domain or dom == domain.lower())


def _latest(items: list[tuple[datetime | None, Any, Fact]]):
    """Pick the latest by date, then by tie-breaker; undated records sort first (never beat a dated one)."""
    if not items:
        return None
    floor = datetime(1900, 1, 1, tzinfo=timezone.utc)
    return max(items, key=lambda t: (t[0] or floor, t[1]))


def split_cents(total: Decimal, weights: dict[str, Decimal], rule: str = "largest_remainder", tie: str = "ascending") -> dict[str, Decimal]:
    """Allocate `total` EUR in cents proportionally to weights (floor, then largest remainders)."""
    cents = int((total * 100).to_integral_value())
    wsum = sum(weights.values())
    if wsum <= 0 or not weights:
        return {k: ZERO for k in weights}
    keys = sorted(weights, key=_pos_key)
    raw = {k: Decimal(cents) * weights[k] / wsum for k in keys}
    if rule == "round_each":
        alloc = {k: int(round_cents(raw[k] / 100) * 100) for k in keys}
    else:
        alloc = {k: int(raw[k].to_integral_value(rounding=ROUND_FLOOR)) for k in keys}
        rest = cents - sum(alloc.values())
        if rule == "remainder_to_last":
            alloc[keys[-1]] += rest
        elif rule == "remainder_to_first":
            alloc[keys[0]] += rest
        elif rule == "remainder_to_largest":
            alloc[max(keys, key=lambda k: weights[k])] += rest
        else:
            order = keys if tie != "descending" else list(reversed(keys))
            ranked = sorted(order, key=lambda k: -(raw[k] - alloc[k]))
            for k in ranked[: max(rest, 0)]:
                alloc[k] += 1
    return {k: Decimal(v) / 100 for k, v in alloc.items()}


def marginal_bands(total: Decimal, bands: list[dict]) -> Decimal:
    credit = ZERO
    for b in bands:
        lo = to_decimal(b.get("from_eur")) or ZERO
        hi = to_decimal(b.get("to_eur")) if b.get("to_eur") not in (None, "", "∞") else None
        pct = to_decimal(b.get("percent")) or ZERO
        top = total if hi is None else min(total, hi)
        if top > lo:
            credit += (top - lo) * pct / 100
    return round_cents(credit)


def compute(facts: list[Fact], plan: Plan) -> EngineResult:
    ref = norm_id(plan.reference)
    sd = plan.scenario_date
    notes: list[str] = []
    missing: list[str] = []
    by_kind: dict[str, list[Fact]] = {}
    for f in facts:
        by_kind.setdefault(f.kind, []).append(f)

    domain = (plan.bidder_domain or "").lower()
    if not domain:
        for f in by_kind.get("mail_identity", []):
            addr = (f.data.get("address") or "").lower()
            if "@" in addr:
                domain = addr.split("@", 1)[1]
    if not domain:
        missing.append("bidder mailbox domain (webmail identity)")

    # ---------- mail records: filter by exact reference, authorized sender, date ----------
    def mail_records(kind: str) -> list[tuple[datetime | None, Any, Fact]]:
        local = plan.senders.get(kind) or DEFAULT_SENDERS.get(kind, "")
        out = []
        for f in by_kind.get("mail_fact", []):
            d = f.data
            if d.get("kind") != kind:
                continue
            if norm_id(d.get("reference")) != ref:
                continue
            if not _sender_ok(d.get("sender", ""), local, domain):
                continue
            dt = _dt(d.get("date_utc"))
            if dt is not None and dt > sd:
                continue
            out.append((dt, (str(d.get("message_id") or ""), f.id), f))
        return out

    def latest_by_key(kind: str, key_field: str = "key") -> dict[str, Fact]:
        """Newest message wins per key; within one message the last entry for a key wins."""
        best: dict[str, tuple] = {}
        for dt, tb, f in mail_records(kind):
            k = loose_id(f.data.get(key_field))
            if not k:
                continue
            cand = (dt or datetime(1900, 1, 1, tzinfo=timezone.utc), tb, f)
            if k not in best or (cand[0], cand[1]) > (best[k][0], best[k][1]):
                best[k] = cand
        return {k: v[2] for k, v in best.items()}

    def latest_message_entries(kind: str) -> list[Fact]:
        """All entries of the newest qualifying message (used where the rule picks one message)."""
        recs = mail_records(kind)
        if not recs:
            return []
        top = _latest(recs)
        mid = top[2].data.get("message_id")
        return [f for _, _, f in recs if f.data.get("message_id") == mid] if mid else [top[2]]

    # ---------- tender: positions and Q&A ----------
    positions: dict[str, Fact] = {}
    for f in by_kind.get("position", []):
        p = str(f.data.get("position") or "").strip()
        if p:
            positions[p] = f
    if not positions:
        missing.append("tender positions")

    effects = []
    for f in by_kind.get("qa_effect", []):
        dt = _dt(f.data.get("date_utc"))
        if dt is not None and dt > sd:
            continue
        effects.append((dt, f))
    withdrawn = {loose_id(f.data.get("withdrawn_number")) for _, f in effects if f.data.get("kind") == "withdraw"}
    withdrawn.discard("")
    active = [(dt, f) for dt, f in effects if loose_id(f.data.get("qa_number")) not in withdrawn]

    def pos_effects(kind: str, pos: str) -> list[tuple[datetime | None, Fact]]:
        return [(dt, f) for dt, f in active if f.data.get("kind") == kind and _same_pos(f.data.get("position"), pos)]

    def latest_effect(kind: str, pos: str) -> Fact | None:
        items = [(dt, _num_key(f.data.get("qa_number")), f) for dt, f in pos_effects(kind, pos)]
        top = _latest(items)
        return top[2] if top else None

    # ---------- specification: governing revision and requirements ----------
    current = [f for f in by_kind.get("spec_revision", []) if f.data.get("is_current")]
    gov = None
    if current:
        gov_f = max(current, key=lambda f: (_dt(f.data.get("date")) or datetime(1900, 1, 1, tzinfo=timezone.utc), f.id))
        gov = gov_f.data.get("revision")
        if len({norm_id(f.data.get("revision")) for f in current}) > 1:
            notes.append(f"several revisions marked current; using {gov}")
    else:
        missing.append("current specification revision")
    reqs: dict[str, Fact] = {}
    for f in by_kind.get("requirement", []):
        if gov is None or norm_id(f.data.get("revision")) == norm_id(gov) or not f.data.get("revision"):
            if gov is not None and not f.data.get("revision"):
                continue
            reqs[loose_id(f.data.get("requirement"))] = f

    materials = _merge_materials(by_kind.get("material", []))
    stock: dict[str, Fact] = {}
    for f in by_kind.get("stock", []):
        stock[norm_id(f.data.get("material"))] = f
    conditions: dict[str, list[Fact]] = {}
    for f in by_kind.get("price_condition", []):
        conditions.setdefault(norm_id(f.data.get("material")), []).append(f)
    searches = by_kind.get("erp_search", [])

    reserves = latest_by_key("reserve")
    fx_by_key = latest_by_key("fx")
    pools = latest_by_key("setup_pool")
    packaging = latest_by_key("packaging")
    delivery = latest_by_key("delivery")
    bonuses = latest_by_key("bonus")
    rules = latest_by_key("condition_rule", "group")
    index_entries = latest_message_entries("metal_index")

    calcs: dict[str, PositionCalc] = {}
    for p in sorted(positions, key=_pos_key):
        pf = positions[p]
        c = PositionCalc(position=p, requirement=str(pf.data.get("requirement") or ""))
        c.used["position"] = [pf.id]
        calcs[p] = c
        req = reqs.get(loose_id(c.requirement))
        if req is None:
            c.notes.append(f"requirement {c.requirement} not found in governing revision {gov}")
            missing.append(f"requirement {c.requirement} (revision {gov})")
        else:
            c.used["requirement"] = [req.id]

        # 1. quantity
        unit_kind = pf.data.get("unit_kind") or "piece"
        c.unit = str(pf.data.get("unit") or "")
        qty = to_decimal(pf.data.get("quantity"))
        ch = latest_effect("quantity", p)
        if ch is not None:
            qty = to_decimal(ch.data.get("quantity"))
            c.used["quantity_change"] = [ch.id]
        if qty is not None and unit_kind == "set":
            pps = to_decimal(req.data.get("pieces_per_set")) if req else None
            if pps:
                qty = qty * pps
            else:
                qty = None
                c.notes.append("unit is a set but pieces per set are unknown")
                missing.append(f"pieces per set for {c.requirement}")
        if qty is not None and qty == qty.to_integral_value():
            c.quantity = int(qty)
        elif qty is not None:
            c.notes.append(f"non-integral quantity {qty}")

        # 2. final requirement attributes
        if req is not None:
            for a in ATTRS:
                c.attrs[a] = req.data.get(a)
            amends = [(dt, f) for dt, f in active if f.data.get("kind") == "amendment"
                      and loose_id(f.data.get("requirement")) == loose_id(c.requirement)
                      and norm_id(f.data.get("revision")) == norm_id(gov)]
            for a in ATTRS:
                items = [(dt, _num_key(f.data.get("qa_number")), f) for dt, f in amends if f.data.get("attribute") == a]
                top = _latest(items)
                if top:
                    c.attrs[a] = top[2].data.get("value")
                    c.used.setdefault("amendments", []).append(top[2].id)
            if req.data.get("stroke_is_usable"):
                key = loose_id(req.data.get("reserve_key"))
                rf = reserves.get(key) if key else None
                if rf is None and not key and len(reserves) == 1:
                    rf = next(iter(reserves.values()))
                if rf is not None and rf.data.get("revision") and norm_id(rf.data.get("revision")) != norm_id(gov):
                    c.notes.append(f"reserve {key} is for revision {rf.data.get('revision')}, not {gov}")
                    rf = None
                usable = to_decimal(c.attrs.get("stroke_mm"))
                res = to_decimal(rf.data.get("reserve_mm_per_end")) if rf else None
                if usable is not None and res is not None:
                    c.attrs["stroke_mm"] = usable + 2 * res
                    c.used["reserve"] = [rf.id]
                else:
                    c.attrs["stroke_mm"] = None
                    missing.append(f"end reserve {req.data.get('reserve_key')} (Technikfreigabe) for {c.requirement}")

        # 3. material
        if req is not None and all(c.attrs.get(a) not in (None, "") for a in ATTRS):
            exact = []
            for m, md in materials.items():
                if all(md.get(a) not in (None, "") for a in ATTRS) and _attrs_equal(c.attrs, md):
                    exact.append(md)
            active_m = [md for md in exact if md.get("is_active") is True]
            unknown_m = [md for md in exact if md.get("is_active") is None]
            if len(active_m) >= 1:
                pick = sorted(active_m, key=lambda md: (0 if loose_id(md.get("catalog_group")) == loose_id(req.data.get("catalog_group")) else 1, norm_id(md["material"])))[0]
                c.material = pick["material"]
                c.material_status = "found" if len(active_m) == 1 else "ambiguous"
                c.used["material"] = pick["_facts"]
                if len(active_m) > 1:
                    c.notes.append("several active exact materials: " + ", ".join(md["material"] for md in active_m))
            elif unknown_m:
                pick = unknown_m[0]
                c.material = pick["material"]
                c.material_status = "status_unknown"
                c.used["material"] = pick["_facts"]
                c.notes.append(f"material {pick['material']} matches but its status is unknown")
                missing.append(f"status of {pick['material']}")
            else:
                group = loose_id(req.data.get("catalog_group"))
                searched = [s for s in searches if s.data.get("complete") and group and (loose_id(s.data.get("catalog_group")) == group or group in loose_id(s.data.get("query")))]
                if searched:
                    listed = {norm_id(x) for s in searched for x in (s.data.get("results") or [])}
                    unseen = [x for x in listed if x not in materials or materials[x].get("is_active") is None
                              or any(materials[x].get(a) in (None, "") for a in ATTRS)]
                    if not listed and len(searched) < 2:
                        c.notes.append("one empty ERP search is not enough to confirm a missing material")
                        missing.append(f"second ERP search for {req.data.get('catalog_group')} ({c.requirement})")
                    elif not unseen:
                        c.material_status = "missing_confirmed"
                        c.notes.append(f"no active exact material in {req.data.get('catalog_group')} (checked {len(listed)})")
                    else:
                        c.notes.append(f"candidates not fully read: {', '.join(sorted(unseen))}")
                        missing.append(f"details of ERP candidates {', '.join(sorted(unseen))}")
                else:
                    missing.append(f"ERP search for {req.data.get('catalog_group')} ({c.requirement})")

    # ---------- per-material demand ----------
    demand: dict[str, int] = {}
    for c in calcs.values():
        if c.material and c.quantity is not None:
            demand[norm_id(c.material)] = demand.get(norm_id(c.material), 0) + c.quantity

    # ---------- 4. base price ----------
    for c in calcs.values():
        if not c.material or c.quantity is None:
            continue
        conds = conditions.get(norm_id(c.material), [])
        valid = [f for f in conds if _valid_on(f, sd)]
        if not valid:
            if conds:
                c.notes.append("no price condition valid on the scenario date")
            missing.append(f"price condition of {c.material} (ERP)")
            continue
        cf = max(valid, key=lambda f: (_dt(f.data.get("valid_from")) or datetime(1900, 1, 1, tzinfo=timezone.utc), f.id))
        c.used["condition"] = [cf.id]
        scales = [(to_decimal(s.get("from_qty")), to_decimal(s.get("price"))) for s in (cf.data.get("scales") or [])]
        scales = [(q, pr) for q, pr in scales if q is not None and pr is not None]
        reach = [(q, pr) for q, pr in scales if q <= c.quantity]
        if not reach:
            missing.append(f"price scales of {c.material} (ERP)")
            continue
        _, price = max(reach, key=lambda t: t[0])
        unit = to_decimal(cf.data.get("price_unit")) or Decimal(1)
        per_piece = price / unit
        cur = (cf.data.get("currency") or "EUR").upper()
        if cur == "EUR":
            c.base = round_cents(per_piece)
        else:
            fxk = loose_id(cf.data.get("fx_key"))
            fxf = fx_by_key.get(fxk) if fxk else None
            if fxf is None and not fxk:
                same = [f for f in fx_by_key.values() if (f.data.get("currency") or "").upper() == cur]
                fxf = same[0] if len(same) == 1 else None
            rate = to_decimal(fxf.data.get("eur_per_unit")) if fxf else None
            if rate is None:
                missing.append(f"exchange rate {cf.data.get('fx_key')} ({cur})")
                continue
            c.base = round_cents(per_piece * rate)
            c.used["fx"] = [fxf.id]

    # ---------- 5. mass surcharge per piece ----------
    for c in calcs.values():
        if not c.material:
            continue
        md = materials.get(norm_id(c.material), {})
        mass = to_decimal(md.get("surcharge_mass_g"))
        idx = loose_id(md.get("price_index"))
        if (mass is not None and mass == ZERO) or (mass is None and not idx):
            c.surcharge_mass = ZERO
            if mass is None:
                c.notes.append("material shows no surcharge mass nor price index: surcharge 0")
            continue
        if mass is None or not idx:
            missing.append(f"surcharge mass and price index of {c.material} (ERP)")
            continue
        rate = _index_rate(index_entries, idx, sd)
        if rate is None:
            missing.append(f"metal index {md.get('price_index')} rate on the scenario date")
            continue
        c.surcharge_mass = mass / 1000 * rate
        c.used["index"] = [f.id for f in index_entries if loose_id(f.data.get("index")) == idx]

    # ---------- 6. setup fees and pools ----------
    keys_of: dict[str, Fact | None] = {p: latest_effect("keys", p) for p in calcs}
    for p, c in calcs.items():
        sf = latest_effect("setup_fee", p)
        if sf is not None:
            c.setup = to_decimal(sf.data.get("amount_eur")) or ZERO
            c.used["setup_fee"] = [sf.id]
        elif c.material:
            erp_fee = to_decimal(materials.get(norm_id(c.material), {}).get("setup_fee_eur"))
            if erp_fee is not None:
                c.setup = erp_fee
                c.notes.append("ordinary setup fee taken from the ERP material")

    def key_of(p: str, field_name: str) -> str | None:
        kf = keys_of.get(p)
        if kf is None:
            return None
        v = kf.data.get(field_name)
        if v in (None, ""):
            return None
        return "none" if str(v).strip().lower() in ("none", "kein", "keine", "-", "null") else loose_id(v)

    pool_members: dict[str, list[str]] = {}
    for p in calcs:
        k = key_of(p, "pool_key")
        if k and k != "none":
            pool_members.setdefault(k, []).append(p)
    for k, members in pool_members.items():
        pf = pools.get(k)
        if pf is None:
            missing.append(f"setup pool {k} (Rüstpools mail)")
            for p in members:
                calcs[p].pool = None
            continue
        fee = to_decimal(pf.data.get("fee_eur"))
        weights = {}
        for p in members:
            c = calcs[p]
            md = materials.get(norm_id(c.material or ""), {})
            am = to_decimal(md.get("allocation_mass_g"))
            if c.quantity is None or am is None or fee is None:
                if c.material and am is None:
                    missing.append(f"allocation mass of {c.material} (ERP)")
                weights = None
                break
            weights[p] = c.quantity * am
        if weights is None:
            for p in members:
                calcs[p].pool = None
            continue
        alloc = split_cents(fee, weights, pf.data.get("allocation") or "largest_remainder", pf.data.get("tie_break") or "ascending")
        for p in members:
            calcs[p].pool = alloc[p]
            calcs[p].used["pool"] = [pf.id]

    # ---------- 7. packaging ----------
    for p, c in calcs.items():
        k = key_of(p, "packaging_key")
        if k == "none":
            c.cartons_cost = ZERO
            continue
        if not k:
            if keys_of.get(p) is not None:
                c.cartons_cost = ZERO
            continue
        tf = packaging.get(k)
        md = materials.get(norm_id(c.material or ""), {})
        per_carton = to_decimal(md.get("pieces_per_carton"))
        if tf is None:
            missing.append(f"packaging tariff {k} (Verpackungstarife mail)")
        if c.material and per_carton in (None, ZERO):
            missing.append(f"pieces per carton of {c.material} (ERP)")
        if tf is None or per_carton in (None, ZERO) or c.quantity is None:
            continue
        tariff = to_decimal(tf.data.get("eur_per_carton"))
        if tariff is None:
            continue
        cartons = math.ceil(Decimal(c.quantity) / per_carton)
        c.cartons_cost = tariff * cartons
        c.used["packaging"] = [tf.id]

    # ---------- 8./9. discount and condition groups ----------
    approvals = [(dt, tb, f) for dt, tb, f in mail_records("discount_approval") if f.data.get("approves") is not False]
    top = _latest(approvals)
    discount = ZERO
    threshold = None
    if top:
        af = top[2]
        discount = to_decimal(af.data.get("percent")) or ZERO
        threshold = to_decimal(af.data.get("min_order_value_eur"))
        notes.append(f"discount from message {af.data.get('message_id')} ({af.data.get('date_utc')}): {discount}%" + (f", min order {threshold}" if threshold is not None else ""))
    for p, c in calcs.items():
        gf = latest_effect("condition_group", p)
        if gf is None:
            continue
        g = loose_id(gf.data.get("group"))
        rf = rules.get(g)
        c.used["condition_group"] = [gf.id]
        if rf is None:
            c.notes.append(f"condition group {gf.data.get('group')} without a Konditionsregeln entry; base-only used")
            missing.append(f"Konditionsregeln for {gf.data.get('group')}")
            continue
        c.scope = rf.data.get("scope") or "base_only"
        c.used["condition_rule"] = [rf.id]

    # ---------- 10. project bonus ----------
    bonus_members: dict[str, list[str]] = {}
    for p in calcs:
        k = key_of(p, "bonus_key")
        if k and k != "none":
            bonus_members.setdefault(k, []).append(p)
        elif k == "none" or keys_of.get(p) is not None:
            calcs[p].bonus = ZERO
    for k, members in bonus_members.items():
        bf = bonuses.get(k)
        if bf is None:
            missing.append(f"project bonus {k} (Projektbonus mail)")
            continue
        values = {}
        for p in members:
            c = calcs[p]
            if c.base is None or c.quantity is None:
                values = None
                break
            values[p] = c.base * c.quantity
        if values is None:
            continue
        credit = marginal_bands(sum(values.values()), bf.data.get("bands") or [])
        alloc = split_cents(credit, values, "largest_remainder", bf.data.get("tie_break") or "ascending")
        for p in members:
            calcs[p].bonus = alloc[p]
            calcs[p].used["bonus"] = [bf.id]

    # ---------- discount threshold (undiscounted goods, no surcharges or setup) ----------
    missing_material = [c for c in calcs.values() if c.material_status == "missing_confirmed"]
    decision = "no_bid" if missing_material else "bid"
    if threshold is not None:
        if decision == "no_bid":
            discount = ZERO
        else:
            known = [c for c in calcs.values() if c.base is not None and c.quantity is not None]
            undisc = round_cents(sum((c.base * c.quantity for c in known), ZERO))
            if len(known) < len(calcs):
                notes.append(f"threshold check on {len(known)}/{len(calcs)} positions: {undisc}")
            if undisc < threshold:
                if len(known) == len(calcs):
                    discount = ZERO
                    notes.append(f"undiscounted value {undisc} below threshold {threshold}: 0 %")
                else:
                    notes.append("threshold not yet reached with partial data")
    d = discount / 100

    # ---------- 11. final unit price ----------
    for c in calcs.values():
        if c.base is None or c.quantity in (None, 0):
            continue
        s = c.surcharge_mass if c.surcharge_mass is not None else ZERO
        db = 1 if c.scope in ("base_only", "base_and_surcharge") else 0
        ds = 1 if c.scope == "base_and_surcharge" else 0
        extra = c.setup + (c.pool or ZERO) + (c.cartons_cost or ZERO) - (c.bonus or ZERO)
        c.unit_price = round_cents(c.base * (1 - db * d) + s * (1 - ds * d) + extra / c.quantity)

    # ---------- 12. lead time ----------
    for p, c in calcs.items():
        if not c.material or c.quantity is None:
            continue
        mkey = norm_id(c.material)
        need = demand.get(mkey, c.quantity)
        k = key_of(p, "delivery_key")
        if k and k != "none":
            df = delivery.get(k)
            if df is not None:
                cap = to_decimal(df.data.get("max_total_qty"))
                weeks = to_decimal(df.data.get("weeks"))
                if weeks is not None and norm_id(df.data.get("material")) == mkey and (cap is None or need <= cap):
                    c.lead_time = int(weeks)
                    c.used["delivery"] = [df.id]
                    continue
                c.notes.append(f"commitment {k} not applicable (material {df.data.get('material')}, cap {cap}, demand {need})")
            else:
                missing.append(f"delivery commitment {k} (Terminfreigabe mail)")
        md = materials.get(mkey, {})
        sf = stock.get(mkey)
        stock_wk, prod_wk = md.get("lead_time_stock_weeks"), md.get("lead_time_production_weeks")
        if sf is None or stock_wk is None or prod_wk is None:
            missing.append(f"stock and lead times of {c.material} (ERP)")
            continue
        avail = to_decimal(sf.data.get("available_qty"))
        if avail is None:
            continue
        c.lead_time = int(to_decimal(stock_wk) if avail >= need else to_decimal(prod_wk))
        c.used["stock"] = [sf.id]

    # ---------- 13. answer ----------
    readiness: dict[str, str] = {}
    out_positions = []
    for p, c in calcs.items():
        if c.material and c.quantity is not None and c.unit_price is not None and c.lead_time is not None:
            entry: dict[str, Any] = {
                "position": p,
                "material_number": c.material,
                "quantity": c.quantity,
                "unit_price": float(c.unit_price),
                "lead_time_weeks": c.lead_time,
                "base_unit_price": float(c.base),
            }
            complete = True
            if c.surcharge_mass is not None and c.pool is not None:
                entry["surcharge_per_unit"] = float(round_cents(c.surcharge_mass + (c.setup + c.pool) / c.quantity))
            else:
                complete = False
            if c.cartons_cost is not None:
                entry["packaging_per_unit"] = float(round_cents(c.cartons_cost / c.quantity))
            else:
                complete = False
            if c.bonus is not None:
                entry["rebate_total"] = float(c.bonus)
            else:
                complete = False
            readiness[p] = "complete" if complete else "partial"
            out_positions.append(entry)
        else:
            readiness[p] = "omit"
    total = round_cents(sum((Decimal(str(e["quantity"])) * Decimal(str(e["unit_price"])) for e in out_positions), ZERO))
    if decision == "no_bid":
        answer = {"tender_reference": plan.reference, "decision": "no_bid", "currency": "EUR",
                  "discount_percent": float(discount), "positions": [], "total_net": 0.0}
    else:
        answer = {"tender_reference": plan.reference, "decision": "bid", "currency": "EUR",
                  "discount_percent": float(discount), "positions": out_positions, "total_net": float(total)}
    return EngineResult(answer=answer, positions=calcs, readiness=readiness, missing=sorted(set(missing)),
                        decision=decision, discount=discount, notes=notes)


def _attrs_equal(want: dict, have: dict) -> bool:
    for a in ("bore_mm", "stroke_mm", "pressure_bar"):
        x, y = to_decimal(want.get(a)), to_decimal(have.get(a))
        if x is None or y is None or x != y:
            return False
    if norm_mounting(want.get("mounting")) != norm_mounting(have.get("mounting")):
        return False
    return norm_cert(want.get("certification")) == norm_cert(have.get("certification"))


def _valid_on(f: Fact, sd: datetime) -> bool:
    vf = _dt(f.data.get("valid_from"))
    vt_raw = f.data.get("valid_to")
    vt = _dt(vt_raw)
    if vt is not None and vt_raw and len(str(vt_raw).strip()) <= 10:
        vt = vt + timedelta(days=1) - timedelta(seconds=1)  # a date-only end includes the whole day
    if vf is not None and sd < vf:
        return False
    if vt is not None and sd > vt:
        return False
    return True


def _index_rate(entries: list[Fact], idx: str, sd: datetime) -> Decimal | None:
    rows = [f for f in entries if loose_id(f.data.get("index")) == idx]
    if not rows:
        return None
    windowed = [f for f in rows if f.data.get("valid_from") or f.data.get("valid_to")]
    if windowed:
        inside = [f for f in windowed if _valid_on(f, sd)]
        if inside:
            return to_decimal(inside[-1].data.get("rate_eur_per_kg"))
        return None
    return to_decimal(rows[-1].data.get("rate_eur_per_kg"))
