"""Deterministic reading of one model-selected ERP material record (its tabs, stock and condition pages).

The model chooses the catalogue search and the candidate; this helper parses the pages of that record into a
MaterialCandidate. Every value carries an Evidence whose excerpt is a verbatim line of its page, so the ERP agent's
citation checks verify it exactly like model citations. Anything not found stays None and is reported missing;
the model extraction remains the fallback. Numbers are read in the locale the page states (Zahlenformat).
"""
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from core.contracts import ERPSectionInspection, Evidence, MaterialCandidate, PriceCondition, TechnicalRequirement

TECHNICAL_LABELS = {"bohrung": "bore", "hub": "stroke", "betriebsdruck": "pressure", "befestigung": "mounting",
                    "zertifizierung": "certification", "bore": "bore", "stroke": "stroke", "pressure": "pressure",
                    "mounting": "mounting", "certification": "certification"}
ACTIVE_WORDS = re.compile(r"freigegeben|aktiv\b|active|released", re.I)
BLOCKED_WORDS = re.compile(r"gesperrt|inaktiv|blocked|inactive|gelöscht", re.I)


def number(text: str | None, locale: str | None = None) -> Decimal | None:
    """A number as printed, read in the stated locale (en-US 1,250.5; de-DE 1.250,5)."""
    if not text:
        return None
    raw = re.sub(r"[\s  ']", "", text)
    if locale == "en-US":
        raw = raw.replace(",", "")
    elif locale == "de-DE" or ("," in raw and "." not in raw and not re.fullmatch(r"\d{1,3}(,\d{3})+", raw)):
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def moment(text: str) -> datetime | None:
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})(?:,?\s*(\d{2}):(\d{2})(?::(\d{2}))?)?", text)
    if not match:
        return None
    day, month, year, hour, minute, second = match.groups()
    return datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0),
                    tzinfo=timezone.utc)


class RecordReader:
    def __init__(self, material_id: str, site_id: str = "erp"):
        self.material_id = material_id
        self.site_id = site_id
        self.found: dict[str, tuple] = {}  # field -> (value, evidence list)
        self.conditions: list[PriceCondition] = []
        self.sections: dict[str, list[Evidence]] = {"material": [], "inventory": [], "pricing": []}
        self.labels: dict[str, str] = {}

    def evidence(self, page, line, field, reference=None):
        return Evidence(id=page["source_id"], agent_id="erp", source_site=self.site_id, source_url=page.get("url"),
                        reference=reference or self.material_id, field=field, excerpt=line.strip())

    def keep(self, field, value, evidence):
        if value is not None and field not in self.found:
            self.found[field] = (value, evidence)

    def read(self, pages: list[dict]) -> MaterialCandidate:
        for page in pages:
            if self.material_id not in page.get("text", ""):
                continue
            lines = [line for line in page["text"].splitlines() if line.strip()]
            self.description(page, lines)
            self.technical(page, lines)
            self.status(page, lines)
            self.availability(page, lines)
            self.condition(page, lines)
        return self.candidate()

    # -- material description: Kataloggruppe, carton, masses, metal index --
    def description(self, page, lines):
        for line in lines:
            if not re.search(r"Kataloggruppe|Packinhalt|Zuschlagsmasse|Umlagegewicht|Preisindex", line):
                continue
            ev = lambda field: [self.evidence(page, line, field)]
            if m := re.search(r"Kataloggruppe\s+([A-Z]{1,4}-\d+)", line):
                self.keep("catalogue_group", m.group(1), ev("catalogue_group"))
            if m := re.search(r"Packinhalt\s+(\d+)\s*(?:Stück|ST)\s*/\s*Karton", line):
                self.keep("pieces_per_carton", Decimal(m.group(1)), ev("pieces_per_carton"))
            if m := re.search(r"Umlagegewicht\s+([\d.,]+)\s*(kg|g|t)\b", line):
                self.keep("allocation_mass", (number(m.group(1)), f"{m.group(1)} {m.group(2)}/Stück"),
                          ev("allocation_mass"))
            if m := re.search(r"Zuschlagsmasse\s+([\d.,]+)\s*(kg|g|t)\b", line):
                self.keep("surcharge_mass", (number(m.group(1)), f"{m.group(1)} {m.group(2)}/Stück"),
                          ev("surcharge_mass"))
            if m := re.search(r"Preisindex\s+([A-Z]{2,5}-\d+)", line):
                self.keep("metal_index_code", m.group(1), ev("metal_index_code"))

    # -- technical characteristics table (+ certification in an info control hint) --
    def technical(self, page, lines):
        for line in lines:
            cells = [c.strip() for c in line.split("\t")]
            name = re.sub(r"[^a-zäöüß]", "", cells[0].casefold())
            field = TECHNICAL_LABELS.get(name)
            if field and len(cells) >= 2 and cells[-1] not in ("", "—", "-"):
                value = cells[-1]
                unit = cells[-2] if len(cells) >= 3 and cells[-2] not in ("—", "-") else None
                parsed = number(value) if field in ("bore", "stroke", "pressure") else value
                if parsed is not None:
                    self.keep(field, (parsed, unit), [self.evidence(page, line, field)])
                    self.sections["material"].append(self.evidence(page, line, "material"))
            elif m := re.match(r"(Zertifizierung|Certification)\W*:\s*(.+?)\s*\(control hint\)$", line.strip(), re.I):
                self.keep("certification", (m.group(2).strip(), None), [self.evidence(page, line, "certification")])

    # -- status with its legend --
    def status(self, page, lines):
        code = legend_line = None
        for i, line in enumerate(lines):
            if m := re.match(r"\s*Materialstatus\s*:?\s*(\w+)?\s*(?:\(|$)", line):
                value = m.group(1) or (lines[i + 1].strip() if i + 1 < len(lines) else "")
                if value and re.fullmatch(r"\w{1,4}", value) and code is None:
                    code = (value, line)
            if re.search(r"\b\w{1,4}\s+(freigegeben|gesperrt|aktiv|inaktiv)", line, re.I) and \
                    re.search(r"freigegeben|aktiv", line, re.I):
                legend_line = line
        if code and legend_line:
            legend = dict((k, w) for k, w in re.findall(r"\b(\w{1,4})\s+([A-Za-zÄÖÜäöüß]+)", legend_line))
            word = legend.get(code[0])
            if word:
                active = True if ACTIVE_WORDS.search(word) else False if BLOCKED_WORDS.search(word) else None
                if active is not None:
                    self.keep("active", active, [self.evidence(page, code[1], "active"),
                                                 self.evidence(page, legend_line, "active")])

    # -- available stock and lead times --
    def availability(self, page, lines):
        for line in lines:
            if m := re.search(r"Verf.gbarer?\s+(?:Gesamt)?bestand\s*:?\s*([\d.,]+)\s*(?:Stück|ST)", line, re.I):
                self.keep("stock", number(m.group(1)), [self.evidence(page, line, "stock")])
                self.sections["inventory"].append(self.evidence(page, line, "inventory"))
            if m := re.search(r"Lieferzeit ab Lager\s*:?\s*(\d+)\s*(Wochen|Woche|Tage|Tag)", line, re.I):
                field = "stock_lead_weeks" if m.group(2).lower().startswith("woche") else "stock_lead_days"
                self.keep(field, int(m.group(1)), [self.evidence(page, line, field)])
            if m := re.search(r"Produktionslieferzeit\s*:?\s*(\d+)\s*(Wochen|Woche|Tage|Tag)", line, re.I):
                field = "production_lead_weeks" if m.group(2).lower().startswith("woche") else "production_lead_days"
                self.keep(field, int(m.group(1)), [self.evidence(page, line, field)])
            if m := re.search(r"R.stkosten[^:\n]*:\s*([\d.,]+)\s*EUR", line, re.I):
                self.keep("setup_fee", number(m.group(1)), [self.evidence(page, line, "setup_fee")])

    # -- one condition record with its quantity scales --
    def condition(self, page, lines):
        text = page["text"]
        record = re.search(r"Konditionssatz\s+(\S+)", text)
        rows = [line for line in lines if re.match(r"\s*[\d.,]+\s*(\([a-z]{2}-[A-Z]{2}\))?\t\s*[\d.,]+", line)]
        if not record or not rows:
            return
        def line_of(pattern):
            for i, line in enumerate(lines):
                if re.search(pattern, line, re.I):
                    nxt = lines[i + 1] if i + 1 < len(lines) else ""
                    return (line, nxt)
            return (None, None)
        locale_match = re.search(r"Zahlenformat\s*:?\s*([a-z]{2}-[A-Z]{2})", text)
        locale = locale_match.group(1) if locale_match else None
        condition_id = f"Konditionssatz {record.group(1)}"
        cid_line = next(line for line in lines if condition_id in line)
        start, start_value = line_of(r"^\s*Gültig von")
        end, end_value = line_of(r"^\s*Gültig bis")
        currency_line, _ = line_of(r"Währung\s*:")
        unit_line, _ = line_of(r"Preiseinheit\s*:")
        fx_line, _ = line_of(r"(Devisen|Umrechnungs|FX)[^:\n]*:")
        def from_line(line, value_line):
            if line is None:
                return None, None
            found = moment(line) or moment(value_line or "")
            return found, (line if moment(line) else value_line)
        valid_from, from_text = from_line(start, start_value)
        valid_until, until_text = from_line(end, end_value)
        currency = re.search(r"Währung\s*:\s*([A-Z]{3})", currency_line).group(1) if currency_line and \
            re.search(r"Währung\s*:\s*([A-Z]{3})", currency_line) else None
        unit = number(re.search(r"Preiseinheit\s*:\s*([\d.,]+)", unit_line).group(1)) if unit_line and \
            re.search(r"Preiseinheit\s*:\s*([\d.,]+)", unit_line) else None
        fx_key = re.search(r":\s*(\S+)", fx_line).group(1) if fx_line else (currency if currency == "EUR" else None)
        ev = lambda line, field: [self.evidence(page, line, field, condition_id)] if line else []
        shared = (ev(cid_line, "condition_id") + ev(from_text, "valid_from") + ev(until_text, "valid_until")
                  + ev(currency_line, "currency") + ev(unit_line, "price_unit")
                  + (ev(fx_line, "fx_key") if fx_line else ev(currency_line, "fx_key") if fx_key else []))
        for row in rows:
            cells = [re.sub(r"\([a-z]{2}-[A-Z]{2}\)", "", c).strip() for c in row.split("\t")]
            minimum, price = number(cells[0], locale), number(cells[1], locale)
            if minimum is None or price is None:
                continue
            if any(c.minimum_quantity == minimum and c.valid_from == valid_from for c in self.conditions):
                continue
            self.conditions.append(PriceCondition(condition_id=condition_id, price=price, currency=currency,
                price_unit=unit, minimum_quantity=minimum, valid_from=valid_from, valid_until=valid_until,
                fx_key=fx_key, evidence=shared + ev(row, "price") + ev(row, "minimum_quantity")))
        self.sections["pricing"].append(self.evidence(page, cid_line, "pricing"))

    # -- assemble --
    def value(self, field):
        return self.found.get(field, (None, []))

    def candidate(self) -> MaterialCandidate:
        spec_evidence, units, spec = [], {}, {}
        for field in ("bore", "stroke", "pressure", "mounting", "certification"):
            value, evidence = self.value(field)
            if value is not None:
                spec[field], unit = value
                if unit:
                    units[field] = unit
                spec_evidence += evidence
        group, group_evidence = self.value("catalogue_group")
        specification = TechnicalRequirement(requirement_id=self.material_id, catalogue_group=group, units=units,
                                             evidence=spec_evidence + group_evidence, **spec)
        attributes, evidence, fields = {}, list(group_evidence), {}
        for field in ("active", "stock", "stock_lead_weeks", "stock_lead_days", "production_lead_weeks",
                      "production_lead_days", "pieces_per_carton", "metal_index_code", "setup_fee"):
            value, ev = self.value(field)
            if value is not None:
                fields[field] = value
                evidence += ev
        for field in ("surcharge_mass", "allocation_mass"):
            value, ev = self.value(field)
            if value is not None:
                fields[field], attributes[field] = value
                evidence += ev
        missing = {
            "material": [f for f in ("active", "bore", "stroke", "pressure", "mounting", "certification")
                         if self.value(f)[0] is None],
            "inventory": [f for f in ("stock",) if self.value(f)[0] is None]
                         + ([] if self.value("stock_lead_weeks")[0] is not None or self.value("stock_lead_days")[0]
                            is not None else ["stock lead time"])
                         + ([] if self.value("production_lead_weeks")[0] is not None or
                            self.value("production_lead_days")[0] is not None else ["production lead time"]),
            "pricing": [] if self.conditions else ["condition records and scales"]}
        sections = [ERPSectionInspection(section=name, material_id=self.material_id,
                        section_label=_label(proof), search_verified=True, complete=not missing[name] and bool(proof),
                        missing=missing[name], evidence=[e.model_copy(update={
                            "reference": f"{self.material_id}:{name}"}) for e in proof[:1]])
                    for name, proof in self.sections.items() if proof]
        complete = len(sections) == 3 and all(x.complete for x in sections)
        return MaterialCandidate(material_id=self.material_id, specification=specification,
                                 conditions=self.conditions, attributes=attributes, sections=sections,
                                 evidence=evidence, records_complete=True if complete else None, **fields)


def _label(proof: list[Evidence]) -> str | None:
    return proof[0].excerpt if proof else None
