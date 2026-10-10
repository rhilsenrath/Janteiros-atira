"""Deterministic resolution of the governing specification; the model only extracts published facts.

Rules (RFQ brief): use the requirement referenced by the position in the specification revision marked current
(rule 2); apply only Q&A amendments naming that revision and requirement, latest dated per attribute, all other
attributes inherited (rule 14); for a requirement given as usable travel ("nutzbarer Weg") add twice the per-end
reserve from the revision-specific Technikfreigabe to get the nominal stroke (rule 20); convert Satz with the
governing specification's pieces per set (rule 17).
"""
import re
from datetime import datetime, timezone
from decimal import Decimal
from core.contracts import (TechnicalRequirements, TechnicalRequirement, TenderData, CommercialPolicies,
    MissingInformation, PositionConversion)
from services.tender_resolution import normalize_id, normalize_reference, parse_quantity

NUMERIC = ("bore", "stroke", "pressure", "pieces_per_set")
ATTRIBUTES = (*NUMERIC, "mounting", "certification", "catalogue_group")
# Attribute names as written in amendments/specifications -> contract field (German and English).
FIELD_NAMES = [
    (re.compile(r"bohrung|kolben|bore|durchmesser", re.I), "bore"),
    (re.compile(r"hub|stellweg|stroke|weg\b", re.I), "stroke"),
    (re.compile(r"druck|pressure", re.I), "pressure"),
    (re.compile(r"befestigung|mounting|anbau", re.I), "mounting"),
    (re.compile(r"zertifi|certif", re.I), "certification"),
    (re.compile(r"st(ü|ue)ck\s*(pro|je)\s*satz|pieces\s*per\s*set", re.I), "pieces_per_set"),
    (re.compile(r"katalog|catalog", re.I), "catalogue_group"),
]
CURRENT = re.compile(r"\b(aktuell|gültig|gueltig|maßgeblich|massgeblich|current|governing|in kraft|freigegeben)\b", re.I)
NOT_CURRENT = re.compile(r"\b(ungültig|ungueltig|nicht|kein|ersetzt|veraltet|zurückgezogen|abgelaufen|"
                         r"superseded|obsolete|withdrawn|invalid|expired|entwurf|draft|alt)\b", re.I)
USABLE = re.compile(r"nutzbar|usable", re.I)
RESERVE = re.compile(r"(?:reserve|zuschlag|sicherheit|je\s+ende|pro\s+ende|per\s+end)[^0-9]{0,60}?"
                     r"(\d+(?:[.,]\d+)?)\s*mm", re.I)


def field_for(name: str | None) -> str | None:
    for pattern, field in FIELD_NAMES:
        if name and pattern.search(name):
            return field
    return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _number(identifier: str) -> tuple:
    digits = re.findall(r"\d+", identifier or "")
    return tuple(int(d) for d in digits) if digits else (0,)


def is_current(revision) -> bool | None:
    """Explicit marking only: a flag from the page or a status text such as 'aktuell'/'ersetzt'."""
    if revision.current is not None:
        return revision.current
    status = revision.status or ""
    if NOT_CURRENT.search(status):
        return False
    if CURRENT.search(status):
        return True
    return None


def governing(data: TechnicalRequirements, tender: TenderData | None = None) -> tuple[str | None, str | None, list[str]]:
    """The most recent revision the revision history marks valid (never simply the first or newest shown).
    Valid revisions dated after the scenario date do not count; several valid revisions are ordered by date."""
    scenario = tender.scenario_date if tender else None
    by_spec: dict[str, list] = {}
    for r in data.revisions:
        if is_current(r) and not (scenario and r.dated_at and _aware(r.dated_at) > _aware(scenario)):
            by_spec.setdefault(normalize_reference(r.specification_id), []).append(r)
    referenced = {normalize_reference(t) for p in (tender.positions if tender else [])
                  for t in re.split(r"[,;]\s*", p.specification_reference or "") if t}
    if len(by_spec) > 1 and referenced:
        by_spec = {k: v for k, v in by_spec.items() if k in referenced} or by_spec
    if len(by_spec) > 1:
        return None, None, ["resolution: valid revisions of several specifications and none is referenced by the tender"]
    if not by_spec:
        return None, None, []
    valid = list({normalize_reference(r.revision): r for r in next(iter(by_spec.values()))}.values())
    if len(valid) == 1:
        return valid[0].specification_id, valid[0].revision, []
    if all(r.dated_at for r in valid):
        latest = max(valid, key=lambda r: _aware(r.dated_at))
        return latest.specification_id, latest.revision, []
    labels = ", ".join(sorted(r.revision for r in valid))
    return None, None, [f"resolution: several revisions are marked valid ({labels}) and not all are dated"]


def _requirement_matches(amendment, requirement: TechnicalRequirement, spec: str | None) -> bool:
    """An amendment names the requirement (e.g. "LH-VG-2027-7093, A-2") or targets a linked position."""
    named = amendment.specification_reference or ""
    tokens = {normalize_reference(t) for t in re.split(r"[,;]\s*|\s+", named) if t.strip()}
    linked = bool(amendment.position_id and amendment.position_id in requirement.position_ids)
    if normalize_reference(requirement.requirement_id) in tokens:
        named_spec = [t for t in tokens if t != normalize_reference(requirement.requirement_id)]
        return not (spec and named_spec) or normalize_reference(spec) in named_spec
    if not named:
        return linked
    # Only the specification is named: it must be the governing one, the position link identifies the requirement.
    names_other_requirement = any(re.fullmatch(r"[a-z]\d+", t) for t in tokens)
    return linked and not names_other_requirement and (not spec or normalize_reference(spec) in tokens)


def reserve_from_policies(commercial: CommercialPolicies | None, revision: str | None,
                          reference: str | None, scenario: datetime | None) -> tuple[Decimal | None, str | None]:
    """Per-end reserve from the latest applicable Technikfreigabe for the governing revision (Mail Agent facts)."""
    if commercial is None or not revision:
        return None, None
    candidates = []
    for rule in commercial.rules:
        label = f"{rule.kind} {rule.subject or ''} {rule.key}"
        if not re.search(r"technikfreigabe|technical approval|technik", label, re.I):
            continue
        if rule.authorized is False:
            continue
        if reference and rule.tender_reference and normalize_reference(rule.tender_reference) != normalize_reference(reference):
            continue
        if scenario and rule.effective_at and _aware(rule.effective_at) > _aware(scenario):
            continue
        if normalize_reference(revision) not in normalize_reference(rule.text + " " + (rule.subject or "")):
            continue
        match = RESERVE.search(rule.text)
        if match:
            candidates.append((rule.effective_at or datetime.min.replace(tzinfo=timezone.utc), rule.key,
                               parse_quantity(match.group(1))))
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: _aware(c[0]))
    return candidates[-1][2], candidates[-1][1]


def resolve_specifications(data: TechnicalRequirements, tender: TenderData | None = None,
                           commercial: CommercialPolicies | None = None,
                           ) -> tuple[TechnicalRequirements, list[MissingInformation]]:
    """Recompute governing revision, effective requirements and conversions. Idempotent."""
    conflicts = [c for c in data.conflicts if not c.startswith("resolution:")]
    missing: list[MissingInformation] = []
    spec, revision, revision_conflicts = governing(data, tender)
    conflicts += revision_conflicts
    if not revision:
        missing.append(MissingInformation(agent_id="supplier", question=(
            "Read the revision history: status (valid or not) and date of every revision, to select the most "
            "recent valid revision")))

    positions = tender.positions if tender else []
    links: dict[str, list[str]] = {}
    for p in positions:
        if p.requirement_id:
            links.setdefault(normalize_id(p.requirement_id), []).append(p.position_id)

    amendments = [a for a in (tender.amendments if tender else [])
                  if a.change_type == "technical_amendment" and a.status in ("effective", "superseded")]
    reference = tender.reference if tender else None
    scenario = tender.scenario_date if tender else None

    # One record per requirement and revision: the same requirement can sit in both lists (it was kept apart
    # while the governing revision was still unknown) or be extracted twice; later non-empty fields win.
    combined: dict[tuple, TechnicalRequirement] = {}
    for r in [*data.other_requirements, *data.requirements]:
        key = (normalize_reference(r.requirement_id), normalize_reference(r.revision))
        previous = combined.get(key)
        if previous is not None:
            r = previous.model_copy(update={name: getattr(r, name) for name in type(r).model_fields
                                            if getattr(r, name) not in (None, [], {})})
        combined[key] = r
    requirements, others = [], []
    for r in combined.values():
        if not revision or (r.revision and normalize_reference(r.revision) != normalize_reference(revision)):
            others.append(r)  # other/unknown revision: kept as read, never applied
            continue
        originals = dict(r.original_values) or {
            field: str(getattr(r, field)) for field in ATTRIBUTES if getattr(r, field) is not None}
        if r.stroke_kind and "stroke_kind" not in originals:
            originals["stroke_kind"] = r.stroke_kind
        values: dict = {field: (parse_quantity(originals[field]) if field in NUMERIC else originals[field])
                        for field in ATTRIBUTES if field in originals}
        position_ids = sorted(set(r.position_ids) | set(links.get(normalize_id(r.requirement_id), [])))
        linked = r.model_copy(update={"position_ids": position_ids})

        # Rule 14: latest dated applicable amendment per attribute, named revision and requirement only.
        latest: dict[str, tuple] = {}
        for a in amendments:
            if not a.value or "=" not in a.value:
                continue
            if not a.revision or not revision or normalize_reference(a.revision) != normalize_reference(revision):
                continue
            if not _requirement_matches(a, linked, spec):
                continue
            name, new = a.value.split("=", 1)
            field = field_for(name) or normalize_id(name)
            order = (_aware(a.dated_at).timestamp() if a.dated_at else float("-inf"), _number(a.answer))
            if field not in latest or order > latest[field][0]:
                latest[field] = (order, a.answer, new.strip(), name.strip())
        applied, extra = [], dict(r.attributes)
        for field, (_, answer, new, name) in sorted(latest.items()):
            if field in NUMERIC:
                parsed = parse_quantity(new)
                if parsed is None:
                    conflicts.append(f"resolution: amendment {answer} value for {r.requirement_id} {name} is not numeric")
                    continue
                values[field] = parsed
            elif field in ("mounting", "certification", "catalogue_group"):
                values[field] = new
            else:
                extra[name] = new
            applied.append(f"{answer}:{name}={new}")

        # Rule 20: usable travel -> nominal stroke with the revision-specific reserve.
        kind = originals.get("stroke_kind") or r.stroke_kind
        usable = reserve = None
        if kind and USABLE.search(kind):
            usable = values.get("stroke")
            reserve, source = reserve_from_policies(commercial, revision or r.revision, reference, scenario)
            if reserve is None:
                values["stroke"] = None
                missing.append(MissingInformation(agent_id="mail", question=(
                    f"Provide the Technikfreigabe for {spec or 'the specification'} revision {revision or r.revision}, "
                    f"requirement {r.requirement_id}: the stroke reserve per end (mm) for the exact tender reference")))
            elif usable is not None:
                values["stroke"] = usable + 2 * reserve

        requirement = linked.model_copy(update={
            **{field: values.get(field) for field in ATTRIBUTES},
            "revision": r.revision or revision, "specification_id": r.specification_id or spec,
            "stroke_kind": kind,
            "original_values": originals, "applied_amendments": applied, "attributes": extra,
            "usable_travel": usable, "stroke_reserve_per_end": reserve})
        awaiting_reserve = usable is not None and reserve is None  # asked from Mail above
        for field in ("catalogue_group", "bore", "stroke", "pressure", "mounting", "certification"):
            if getattr(requirement, field) is None and not (field == "stroke" and awaiting_reserve):
                missing.append(MissingInformation(agent_id="supplier", question=(
                    f"Read {field} of requirement {r.requirement_id} in revision {revision or r.revision}")))
        requirements.append(requirement)

    known = {normalize_id(r.requirement_id) for r in requirements}
    for p in positions:
        if p.requirement_id and normalize_id(p.requirement_id) not in known and revision:
            missing.append(MissingInformation(agent_id="supplier", position_id=p.position_id, question=(
                f"Read requirement {p.requirement_id} (position {p.position_id}) in revision {revision}")))

    # Rule 17: Satz -> pieces with the governing specification's pieces per set.
    by_requirement = {normalize_id(r.requirement_id): r for r in requirements}
    conversions = []
    for p in positions:
        requirement = by_requirement.get(normalize_id(p.requirement_id or ""))
        per_set = requirement.pieces_per_set if requirement else None
        is_set = bool(p.unit and re.match(r"satz|set", p.unit, re.I))
        pieces = p.quantity if not is_set else (p.quantity * per_set if p.quantity is not None and per_set else None)
        if is_set and per_set is None and requirement is not None:
            missing.append(MissingInformation(agent_id="supplier", position_id=p.position_id, question=(
                f"Read pieces per set (Stück pro Satz) for requirement {p.requirement_id}")))
        conversions.append(PositionConversion(position_id=p.position_id, requirement_id=p.requirement_id,
            unit=p.unit, quantity=p.quantity, pieces_per_set=per_set if is_set else Decimal("1"), pieces=pieces))

    resolved = data.model_copy(update={"governing_specification": spec, "governing_revision": revision,
        "requirements": requirements, "other_requirements": others, "conversions": conversions,
        "conflicts": list(dict.fromkeys(conflicts))})
    return resolved, missing
