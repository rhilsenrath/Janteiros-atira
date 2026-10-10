"""Commercial terms from the sales mailbox: model interpretation in, deterministic selection out.

The model only reads mail prose into typed `TermReading`s, each with a verbatim quote of its message. This module
verifies those quotes and numbers against the message text and then decides, without the model, which record
applies: the sender must be the topic's authorized local part at the bidder's mailbox domain, the record must name
the EXACT tender reference, be dated no later than the scenario date, and the latest such record per topic and key
wins. A newer record replaces an older one; nothing older is revived (e.g. by a failed discount threshold).
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from pydantic import BaseModel, Field
from core.contracts import CommercialPolicies, CommercialRule, Record

Topic = Literal["discount_approval", "condition_rules", "metal_index", "packaging_tariff", "delivery_commitment",
                "fx_rate", "setup_pool", "project_bonus", "technical_release", "other"]
PRICING_TOPICS = ("discount_approval", "condition_rules", "metal_index", "packaging_tariff", "delivery_commitment",
                  "fx_rate", "setup_pool", "project_bonus")
Clause = Literal["base_only", "base_and_surcharge", "none"]


# ---- model output (strings for numbers: JSON floats must never carry money) ----

class WindowReading(BaseModel):
    valid_from: str | None = Field(None, description="ISO date YYYY-MM-DD, inclusive; null if open")
    valid_until: str | None = Field(None, description="ISO date YYYY-MM-DD, inclusive; null if open")
    rate: str = Field(description="plain decimal, e.g. 4.85")


class BandReading(BaseModel):
    lower: str = Field(description="band start in EUR, plain decimal (0 for the first band)")
    upper: str | None = Field(None, description="band end in EUR, plain decimal; null for the open last band")
    percent: str = Field(description="percent for the portion inside this band, plain decimal")


class TermReading(BaseModel):
    rule_key: str = Field(description="the `key` of the message this term was read from")
    topic: Topic
    key: str | None = Field(None, description="the code the term defines (K-…, MET-…, VP-…, TZ-…, FX key, RP-…, "
                                              "PB-…); null for a discount approval")
    tender_reference: str | None = Field(None, description="tender reference the message names for this term, "
                                                           "exactly as written")
    quote: str = Field(description="verbatim excerpt of the message that states every value of this term")
    approves: bool | None = Field(None, description="discount_approval: true when the message approves a discount")
    percent: str | None = Field(None, description="discount_approval: approved percent, plain decimal")
    minimum_order_value: str | None = Field(None, description="discount_approval: minimum undiscounted net order "
                                                              "value in EUR required for the discount, if any")
    clause: Clause | None = Field(None, description="condition_rules: what the order discount applies to for "
                                                    "this Konditionsgruppe")
    rate: str | None = Field(None, description="metal_index: EUR per mass unit; packaging_tariff: EUR per carton; "
                                               "fx_rate: the stated rate")
    windows: list[WindowReading] = Field(default_factory=list, description="rates with validity windows, when the "
                                                                           "message lists several")
    rate_currency: str | None = Field(None, description="currency of the rate when not EUR (metal_index, packaging)")
    mass_unit: str | None = Field(None, description="metal_index: mass the rate refers to, e.g. kg, 100 kg, t")
    fx_direction: Literal["eur_per_unit", "units_per_eur"] | None = Field(
        None, description="fx_rate: eur_per_unit when the rate is EUR per 1 foreign unit, units_per_eur when it is "
                          "foreign units per 1 EUR")
    currency: str | None = Field(None, description="fx_rate: the foreign currency code")
    fixed_fee: str | None = Field(None, description="packaging_tariff: one-off fee per position in EUR, if stated")
    material: str | None = Field(None, description="delivery_commitment: material number the commitment is for")
    ceiling: str | None = Field(None, description="delivery_commitment: maximum pooled pieces (inclusive)")
    weeks: int | None = Field(None, description="delivery_commitment: committed lead time in weeks")
    pool_fee: str | None = Field(None, description="setup_pool: the one shared pool fee in EUR")
    allocation_method: Literal["largest_remainder", "round_last_absorbs"] | None = Field(
        None, description="setup_pool: cent rule; largest_remainder = floor to cents, distribute the remaining "
                          "cents by largest remainder; round_last_absorbs = round each, the last one takes the "
                          "difference")
    tie_break: Literal["position_ascending", "position_descending", "weight_descending"] | None = None
    bands: list[BandReading] = Field(default_factory=list, description="project_bonus: marginal bands")
    bonus_basis: Literal["base", "base_and_surcharge"] | None = Field(
        None, description="project_bonus: goods value the bands apply to (default undiscounted base)")


class AuthorityReading(BaseModel):
    topic: Topic
    sender_local_part: str = Field(description="local part before @ that the brief authorizes for this topic")
    quote: str = Field(description="verbatim brief excerpt naming it")


class CommercialReading(BaseModel):
    authorities: list[AuthorityReading] = Field(default_factory=list)
    terms: list[TermReading] = Field(default_factory=list)


# ---- resolved, typed terms (Decimal only) ----

class Window(Record):
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    rate: Decimal


class Band(Record):
    lower: Decimal
    upper: Decimal | None = None
    percent: Decimal


class Term(Record):
    topic: str
    key: str | None = None
    rule_key: str
    sender: str | None = None
    dated_at: datetime | None = None
    approves: bool | None = None
    percent: Decimal | None = None
    minimum_order_value: Decimal | None = None
    clause: str | None = None
    rate: Decimal | None = None
    windows: list[Window] = Field(default_factory=list)
    rate_currency: str | None = None
    mass_unit: str | None = None
    fx_direction: str | None = None
    currency: str | None = None
    fixed_fee: Decimal | None = None
    material: str | None = None
    ceiling: Decimal | None = None
    weeks: int | None = None
    pool_fee: Decimal | None = None
    allocation_method: str | None = None
    tie_break: str | None = None
    bands: list[Band] = Field(default_factory=list)
    bonus_basis: str | None = None

    def rate_on(self, day: datetime | None) -> Decimal | None:
        """The rate whose inclusive window contains the day, not the last listed; a plain rate otherwise."""
        if not self.windows:
            return self.rate
        if day is None:
            return None
        hits = [w.rate for w in self.windows if within(day, w.valid_from, w.valid_until)]
        return hits[0] if len(set(hits)) == 1 else None


class ResolvedTerms(Record):
    """The applicable record per topic/key, plus what could not be settled."""
    discount: Term | None = None  # latest approving director message, if any
    discount_checked: bool = False  # authorized approvals were searched (None discount then means 0 %)
    by_key: dict[str, Term] = Field(default_factory=dict)  # "<topic>|<key>" -> applicable term
    issues: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)  # targeted requests for the mail agent

    def get(self, topic: str, key: str | None) -> Term | None:
        return self.by_key.get(f"{topic}|{norm(key)}") if key else None


# ---- helpers ----

def norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def within(day: datetime, start: datetime | None, end: datetime | None) -> bool:
    """Inclusive on both endpoints, compared by calendar date (validities are published as days)."""
    d = day.date()
    return (start is None or start.date() <= d) and (end is None or d <= end.date())


def decimal(text) -> Decimal | None:
    if text is None or str(text).strip() == "":
        return None
    try:
        value = Decimal(str(text).strip().replace(" ", ""))
    except InvalidOperation:
        raise ValueError(f"not a plain decimal: {text!r}") from None
    if not value.is_finite():
        raise ValueError(f"not finite: {text!r}")
    return value


def parse_day(text) -> datetime | None:
    if not text:
        return None
    text = str(text).strip()
    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"not a date: {text!r}") from None


NUMBER = re.compile(r"\d[\d.,'\u202f\u00a0]*\d|\d")  # a plain space separates numbers


def numbers_in(text: str) -> set[Decimal]:
    """Every number in the text, read both as de-DE (1.234,5) and en-US (1,234.5), so the check never depends on
    guessing the locale."""
    found = set()
    for raw in NUMBER.findall(text or ""):
        raw = re.sub(r"['\u202f\u00a0]", "", raw)
        for decimal_mark, group_mark in ((",", "."), (".", ",")):
            candidate = raw.replace(group_mark, "").replace(decimal_mark, ".")
            if candidate.count(".") <= 1:
                try:
                    found.add(Decimal(candidate).normalize())
                except InvalidOperation:
                    pass
        try:
            found.add(Decimal(raw.replace(",", "").replace(".", "")).normalize())
        except InvalidOperation:
            pass
    return found


def stated(value: Decimal | None, quote: str) -> bool:
    return value is None or value.normalize() in numbers_in(quote)


def term_from_reading(reading: TermReading, rule: CommercialRule) -> tuple[Term | None, str | None]:
    """Typed term, or why the reading was rejected (quote not in the message, number not in the quote)."""
    if norm(reading.quote) not in norm(rule.text) or len(norm(reading.quote)) < 3:
        return None, f"{reading.topic} {reading.key or ''} from {rule.key}: quote not found in the message"
    try:
        windows = [Window(valid_from=parse_day(w.valid_from), valid_until=parse_day(w.valid_until),
                          rate=decimal(w.rate)) for w in reading.windows]
        bands = [Band(lower=decimal(b.lower), upper=decimal(b.upper), percent=decimal(b.percent))
                 for b in reading.bands]
        term = Term(topic=reading.topic, key=reading.key, rule_key=rule.key, sender=rule.authorized_sender,
                    dated_at=rule.effective_at, approves=reading.approves, percent=decimal(reading.percent),
                    minimum_order_value=decimal(reading.minimum_order_value), clause=reading.clause,
                    rate=decimal(reading.rate), windows=windows, rate_currency=reading.rate_currency,
                    mass_unit=reading.mass_unit, fx_direction=reading.fx_direction, currency=reading.currency,
                    fixed_fee=decimal(reading.fixed_fee), material=reading.material,
                    ceiling=decimal(reading.ceiling), weeks=reading.weeks, pool_fee=decimal(reading.pool_fee),
                    allocation_method=reading.allocation_method, tie_break=reading.tie_break, bands=bands,
                    bonus_basis=reading.bonus_basis)
    except ValueError as error:
        return None, f"{reading.topic} {reading.key or ''} from {rule.key}: {error}"
    values = [term.percent, term.minimum_order_value, term.rate, term.fixed_fee, term.ceiling, term.pool_fee,
              *(w.rate for w in windows), *(b.percent for b in bands), *(b.lower for b in bands if b.lower),
              *(b.upper for b in bands), Decimal(term.weeks) if term.weeks is not None else None]
    unstated = [str(v) for v in values if not stated(v, reading.quote)]
    if unstated:
        return None, (f"{reading.topic} {reading.key or ''} from {rule.key}: value(s) {', '.join(unstated)} "
                      f"not stated in the quote")
    return term, None


def sender_parts(address: str | None) -> tuple[str, str] | None:
    match = re.search(r"([^\s<>@\"']+)@([^\s<>@\"']+)", address or "")
    return (match.group(1).casefold(), match.group(2).casefold().rstrip(".>")) if match else None


def resolve_terms(policies: CommercialPolicies | None, reading: CommercialReading | None, reference: str | None,
                  scenario: datetime | None, brief: str | None = None) -> ResolvedTerms:
    """Select the applicable record per topic/key: authorized sender, exact reference, dated on or before the
    scenario date, latest wins. Ambiguities become questions, never guesses."""
    issues, questions = [], []
    if policies is None or reading is None:
        return ResolvedTerms(questions=["Investigate the exact-reference commercial messages (discount approval, "
                                        "Konditionsregeln, Preisindizes, Verpackungstarife, Terminfreigabe, "
                                        "Devisenfixing, Rüstpools, Projektbonus)"])
    authority = {}
    for a in reading.authorities:
        local = a.sender_local_part.strip().casefold().rstrip("@")
        if brief is not None and f"{local}@" not in brief.casefold():
            issues.append(f"{a.topic}: sender {local}@ is not named in the brief")
            continue
        authority.setdefault(a.topic, local)
    domain = (sender_parts(policies.mailbox_address) or (None, None))[1]
    if domain is None:
        issues.append("bidder mailbox domain unknown: relying on the mail agent's authorization flags")
        questions.append("Report the bidder's own mailbox address shown in the webmail (its domain authorizes "
                         "senders)")
    rules = {r.key: r for r in policies.rules}
    candidates: dict[str, list[Term]] = {}
    for item in reading.terms:
        if item.topic not in PRICING_TOPICS:
            continue
        rule = rules.get(item.rule_key)
        if rule is None:
            issues.append(f"{item.topic} {item.key or ''}: unknown message {item.rule_key}")
            continue
        term, problem = term_from_reading(item, rule)
        if term is None:
            issues.append(problem)
            questions.append(f"Re-read message {rule.key} ({rule.subject or item.topic}) and its exact values")
            continue
        named = item.tender_reference or rule.tender_reference
        if not reference or norm(named) != norm(reference):
            issues.append(f"{rule.key}: names {named or 'no reference'}, not exactly {reference}")
            continue
        local = authority.get(item.topic)
        sender = sender_parts(rule.authorized_sender)
        if local is None:
            issues.append(f"{item.topic}: the brief's authorized sender is unknown")
            continue
        if sender is None:
            issues.append(f"{rule.key}: sender address unknown")
            questions.append(f"Read the sender address of message {rule.key} ({rule.subject or item.topic})")
            continue
        if sender[0] != local or (domain is not None and sender[1] != domain) or (
                domain is None and rule.authorized is not True):
            issues.append(f"{rule.key}: sender {rule.authorized_sender} is not authorized for {item.topic}")
            continue
        if rule.effective_at is None:
            questions.append(f"Read the date of message {rule.key} ({rule.subject or item.topic})")
            issues.append(f"{rule.key}: undated, cannot order it")
            continue
        if scenario is not None and rule.effective_at > scenario:
            issues.append(f"{rule.key}: dated after the scenario date")
            continue
        if item.topic == "discount_approval":
            if term.approves is not True:
                continue
            candidates.setdefault("discount_approval|", []).append(term)
        elif term.key:
            candidates.setdefault(f"{item.topic}|{norm(term.key)}", []).append(term)
    by_key, discount = {}, None
    for slot, terms in candidates.items():
        latest = max(t.dated_at for t in terms)
        newest = [t for t in terms if t.dated_at == latest]
        distinct = {t.model_dump_json(exclude={"rule_key", "sender", "dated_at"}) for t in newest}
        if len(distinct) > 1:
            issues.append(f"{slot}: conflicting records dated {latest.isoformat()}")
            questions.append(f"Resolve conflicting {slot.split('|')[0]} records dated {latest.date()}")
            continue
        if slot == "discount_approval|":
            discount = newest[0]
        else:
            by_key[slot] = newest[0]
    checked = "discount_approval" in authority
    return ResolvedTerms(discount=discount, discount_checked=checked, by_key=by_key, issues=issues,
                         questions=list(dict.fromkeys(questions)))
