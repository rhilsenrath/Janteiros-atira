"""Deterministic resolution of tender amendments; the model only extracts published facts.

Rules (RFQ brief): the tender quantity applies unless a later Q&A answer changes it; an answer can withdraw an
earlier answer by its number, whose changes are then ignored and the latest remaining change applies, or the
original value if none remains. Q&A answers can also assign clause groups, commitment/packaging keys, shared
pools and scoped technical amendments.
"""
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from core.contracts import TenderData, Clarification, ClarificationChange, TenderAmendment, MissingInformation

KEY_FIELDS = ("condition_group", "packaging_key", "delivery_key", "setup_pool", "bonus_plan")
MEMBERSHIP = {"setup_pool": "setup_pool_member", "bonus_plan": "bonus_member"}
# Values that state non-membership although extracted as a value ("Kein Rüstpool", "no bonus").
NEGATION = re.compile(r"^\s*(kein|keine|keinen|ohne|no|none|nicht)\b", re.I)
_RESOLUTION = "resolution: "


def normalize_id(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _number(identifier: str) -> tuple:
    digits = re.findall(r"\d+", identifier or "")
    return tuple(int(d) for d in digits) if digits else (float("inf"),)


def parse_quantity(value: str | None) -> Decimal | None:
    """Published quantities: '1.200' and '1 200' are 1200, '2,5' is 2.5. Unparseable stays None."""
    if value is None:
        return None
    text = re.sub(r"[^\d.,\-]", "", str(value))
    if not re.search(r"\d", text):
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def normalize_reference(value: str | None) -> str:
    """Tender references compare without separators/case: "VG-2027-7093" == "vg 2027 7093"."""
    return re.sub(r"[^0-9a-z]", "", (value or "").casefold())


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _localize(value: datetime | None, zone: str | None) -> datetime | None:
    """Portal times are local to the portal's displayed timezone (e.g. Europe/Athens)."""
    if value is None or value.tzinfo is not None or not zone:
        return value
    try:
        return value.replace(tzinfo=ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        return value


def _order(clarification: Clarification) -> tuple:
    # Publication date first; the answer number breaks ties and orders undated answers.
    dated = clarification.dated_at
    if dated is None:
        timestamp = float("-inf")
    else:
        timestamp = (dated if dated.tzinfo else dated.replace(tzinfo=timezone.utc)).timestamp()
    return (timestamp, _number(clarification.identifier))


def _slot(change: ClarificationChange) -> tuple:
    """Changes in the same slot replace each other. Technical amendments replace each other per named
    attribute of the same specification/revision ("Hub/Stellweg = 485 mm" -> attribute "hub/stellweg");
    other notes accumulate."""
    position = normalize_id(change.position_id) if change.position_id else None
    if change.change_type == "technical_amendment" and change.value and "=" in change.value:
        attribute = normalize_id(change.value.split("=", 1)[0])
        return (change.change_type, normalize_id(change.specification_reference) or position,
                normalize_id(change.revision), position, attribute)
    if change.change_type in ("technical_amendment", "other"):
        return (change.change_type, position, change.change_id)
    return (change.change_type, position)


def withdrawn_answers(clarifications: list[Clarification]) -> dict[str, str]:
    """Map withdrawn answer -> withdrawing answer. A withdrawal by an answer that is itself withdrawn is void."""
    known = {normalize_id(c.identifier) for c in clarifications}
    withdrawn: dict[str, str] = {}
    for _ in range(len(clarifications) + 1):
        result: dict[str, str] = {}
        for clarification in sorted(clarifications, key=_order, reverse=True):
            own = normalize_id(clarification.identifier)
            if own in withdrawn:
                continue
            for target in clarification.withdraws:
                key = normalize_id(target)
                if key in known and key != own and key not in result:
                    result[key] = clarification.identifier
        if result == withdrawn:
            break
        withdrawn = result
    return withdrawn


def resolve_tender(data: TenderData) -> tuple[TenderData, list[MissingInformation]]:
    """Recompute effective position values, withdrawal flags and the amendment trail from extracted facts.

    Idempotent: resolving an already resolved record yields the same record, so incremental re-extraction
    and coordinator merges never apply an amendment twice.
    """
    zone = data.timezone
    conflicts = [c for c in data.conflicts if not c.startswith(_RESOLUTION)]
    missing: list[MissingInformation] = []

    clarifications = []
    for c in data.clarifications:
        changes, used = [], set()
        for change in c.changes:
            position = change.position_id or c.position_id
            change_id = f"{c.identifier}:{change.change_type}:{position or '*'}"
            suffix = 2
            while change_id in used:  # one answer can carry several notes of the same kind for a position
                change_id = f"{c.identifier}:{change.change_type}:{position or '*'}:{suffix}"
                suffix += 1
            used.add(change_id)
            changes.append(change.model_copy(update={"position_id": position, "change_id": change_id}))
        clarifications.append(c.model_copy(update={"dated_at": _localize(c.dated_at, zone), "changes": changes}))

    def not_applicable(c: Clarification, change: ClarificationChange | None = None) -> str | None:
        """Rule: only records for the exact tender reference, dated no later than the scenario date, apply."""
        named = (change.tender_reference if change and change.tender_reference else None) or c.tender_reference
        if named and data.reference and normalize_reference(named) != normalize_reference(data.reference):
            return f"names tender {named}, not {data.reference}"
        if c.dated_at and data.scenario_date and _aware(c.dated_at) > _aware(data.scenario_date):
            return "published after the scenario date"
        return None

    known = {normalize_id(c.identifier) for c in clarifications}
    for c in clarifications:
        for target in c.withdraws:
            if normalize_id(target) not in known:
                conflicts.append(f"{_RESOLUTION}answer {c.identifier} withdraws {target}, which was not extracted")
                missing.append(MissingInformation(agent_id="tender",
                    question=f"Read withdrawn answer {target} referenced by answer {c.identifier}"))
    # An answer that does not apply cannot withdraw others either.
    withdrawn = withdrawn_answers([c for c in clarifications if not not_applicable(c)])
    clarifications = sorted((c.model_copy(update={"withdrawn": normalize_id(c.identifier) in withdrawn,
        "withdrawn_by": withdrawn.get(normalize_id(c.identifier))}) for c in clarifications), key=_order)

    positions = {normalize_id(p.position_id) for p in data.positions}
    effective: dict[tuple, tuple[Clarification, ClarificationChange]] = {}
    undated: set[str] = set()
    for c in clarifications:
        for change in c.changes:
            if change.position_id and normalize_id(change.position_id) not in positions:
                conflicts.append(f"{_RESOLUTION}answer {c.identifier} changes unknown position {change.position_id}")
            if c.withdrawn or not_applicable(c, change):
                continue
            previous = effective.get(_slot(change))
            if previous and (previous[0].dated_at is None or c.dated_at is None):
                # Without both publication dates the order rests on answer numbers only: ask for the dates.
                undated.update(x.identifier for x in (previous[0], c) if x.dated_at is None)
            if previous and _order(previous[0]) == _order(c):
                conflicts.append(f"{_RESOLUTION}answers {previous[0].identifier} and {c.identifier} cannot be "
                                 f"ordered for {change.change_type} of {change.position_id or 'all positions'}")
            effective[_slot(change)] = (c, change)

    if undated:
        missing.append(MissingInformation(agent_id="tender", question="Read the publication dates of answers "
            + ", ".join(sorted(undated)) + "; their changes compete and are ordered by number until dated"))
    effective_ids = {change.change_id for _, change in effective.values()}
    amendments = []
    for c in clarifications:
        for change in c.changes:
            reason = not_applicable(c, change)
            if c.withdrawn:
                status, by = "withdrawn", c.withdrawn_by
            elif reason:
                status, by = "not_applicable", None
            elif change.change_id in effective_ids:
                status, by = "effective", None
            else:
                status, by = "superseded", effective[_slot(change)][0].identifier
            amendments.append(TenderAmendment(amendment_id=change.change_id, change_type=change.change_type,
                position_id=change.position_id, answer=c.identifier, dated_at=c.dated_at, value=change.value,
                unit=change.unit, specification_reference=change.specification_reference,
                revision=change.revision or c.specification_revision, status=status, replaced_by=by,
                reason=reason, evidence=c.evidence))

    resolved_positions = []
    for p in data.positions:
        key = normalize_id(p.position_id)
        # The BoQ value is kept in original_*; a model that only filled quantity/unit gave the BoQ value.
        unresolved = p.quantity_basis in (None, "original")
        original_quantity = p.original_quantity if p.original_quantity is not None else (
            p.quantity if unresolved else None)
        original_unit = p.original_unit or (p.unit if unresolved else None)
        update = {"original_quantity": original_quantity, "original_unit": original_unit,
                  "quantity": original_quantity, "unit": original_unit,
                  "quantity_basis": "original" if original_quantity is not None else None}
        if ("quantity", key) in effective:
            c, change = effective[("quantity", key)]
            amended = parse_quantity(change.value)
            if amended is None:
                conflicts.append(f"{_RESOLUTION}answer {c.identifier} quantity for {p.position_id} is not numeric")
            else:
                update.update(quantity=amended, unit=change.unit or original_unit,
                              quantity_basis=f"answer:{c.identifier}")
        for field in KEY_FIELDS:
            assigned = effective.get((field, key)) or effective.get((field, None))
            ineffective = {change.value for c in clarifications for change in c.changes
                           if change.change_type == field and normalize_id(change.position_id) in ("", key)}
            member = MEMBERSHIP.get(field)
            if assigned and (not assigned[1].applies or NEGATION.match(assigned[1].value or "")):
                update[field] = None  # explicitly not a member: no key, membership known to be False
                if member:
                    update[member] = False
            elif assigned and assigned[1].value:
                update[field] = assigned[1].value
                if member:
                    update[member] = True
            else:
                if getattr(p, field) in ineffective:
                    update[field] = None  # came from a withdrawn/inapplicable answer, not from the BoQ
                if member:
                    update[member] = None  # membership is only known from an effective answer
        fee = effective.get(("setup_fee", key))
        if fee:
            amount = parse_quantity(fee[1].value)
            update["setup_fee"] = amount
            if amount is None:
                conflicts.append(f"{_RESOLUTION}answer {fee[0].identifier} setup fee for {p.position_id} is not numeric")
        elif any(change.change_type == "setup_fee" and normalize_id(change.position_id) == key
                 for c in clarifications for change in c.changes):
            update["setup_fee"] = None  # only withdrawn/inapplicable answers stated a fee
        if update["quantity"] is None:
            missing.append(MissingInformation(agent_id="tender", position_id=p.position_id,
                question=f"Read the original quantity of position {p.position_id}"))
        if not update["unit"]:
            missing.append(MissingInformation(agent_id="tender", position_id=p.position_id,
                question=f"Read the unit (Stück/Satz) of position {p.position_id}"))
        if not (p.requirement_id or p.specification_reference):
            missing.append(MissingInformation(agent_id="tender", position_id=p.position_id,
                question=f"Read the specification/requirement reference of position {p.position_id}"))
        resolved_positions.append(p.model_copy(update=update))

    dates = [d.model_copy(update={"at": _localize(d.at, d.timezone or zone)}) for d in data.dates]
    resolved = data.model_copy(update={"positions": resolved_positions, "clarifications": clarifications,
        "amendments": amendments, "dates": dates, "conflicts": list(dict.fromkeys(conflicts))})
    return resolved, missing
