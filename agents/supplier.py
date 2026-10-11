"""Supplier portal worker: model-directed specification investigation plus deterministic resolution.

The shared WebsiteAgent loop authenticates with runtime credentials and lets the model choose every navigation
target. This class adds the supplier contract: governing (explicitly current) revision, effective requirements
per tender position after scoped Q&A amendments from the Tender Agent, nominal-stroke derivation from the Mail
Agent's Technikfreigabe, and Satz conversion (services.specification_resolution).
"""
from agents.website import WebsiteAgent
from core.contracts import TechnicalRequirements
from services.specification_resolution import resolve_specifications
from services.tender_resolution import normalize_id


class SupplierAgent(WebsiteAgent):
    agent_id = "supplier"
    responsibility = (
        "Find the buyer's technical specification for the tender on the supplier portal and extract, as "
        "published: every revision of it with its status/marking (current, superseded, draft) and date; and for "
        "every requirement in the revision explicitly marked current (and in other revisions you read): "
        "requirement id, catalogue group (Kataloggruppe), bore (Bohrung), stroke (Hub) and whether it is a "
        "nominal stroke or usable travel (nutzbarer Weg), pressure (Druck), mounting (Befestigung), "
        "certification (Zertifizierung), pieces per set (Stück pro Satz), units, further constraints and the "
        "page/section/table. Do not search the ERP, price or apply amendments yourself.")
    max_premature_finishes = 3
    incremental_extraction = True
    stall_steps = 20
    extraction_guidance = (
        " Supplier rules: list every specification revision of the revision history in revisions "
        "(specification_id, revision, title, status = the validity field exactly as displayed, current=true only "
        "when that field marks it valid/current, false when it says invalid/superseded, dated_at = its date, url). "
        "The first or newest document shown is NOT automatically governing; the application selects the most "
        "recent valid revision. Extract all requirements of the governing revision. Put each "
        "requirement with its revision and specification_id; values exactly as stated in that revision (numbers "
        "normalized in the locale stated next to the value, e.g. 'en-US' 1,250 = 1250 but 'de-DE' 1,250 = 1.25; "
        "without a stated locale German: 1.200 = 1200, 2,5 = 2.5) with units in units (e.g. {'bore': 'mm', 'pressure': 'bar'}); "
        "stroke_kind 'nominal' or 'nutzbarer Weg' as stated; additional matching constraints in constraints; "
        "the page/section/table in section. Leave original_values, applied_amendments, usable_travel, "
        "stroke_reserve_per_end, position_ids, conversions, governing_* and other_requirements to the "
        "application. Use a citation per requirement with reference = requirement id and one per revision "
        "status with reference = revision.")

    def finalize(self, output):
        if not isinstance(output, TechnicalRequirements):
            return output, []
        if self.latest and isinstance(self.latest.output, TechnicalRequirements):
            # Keep the values as stated in the revision when a later extraction restates effective values.
            known = {(normalize_id(r.requirement_id), normalize_id(r.revision)): r
                     for r in [*self.latest.output.requirements, *self.latest.output.other_requirements]}
            requirements = []
            for r in output.requirements:
                previous = known.get((normalize_id(r.requirement_id), normalize_id(r.revision)))
                if previous and previous.original_values and not r.original_values:
                    r = r.model_copy(update={"original_values": previous.original_values})
                requirements.append(r)
            output = output.model_copy(update={"requirements": requirements})
        task = getattr(self, "current_task", None)
        return resolve_specifications(output, task.tender if task else None,
                                      task.commercial if task else None)

    def covered(self, output):
        return bool(output.governing_revision and output.requirements) and all(
            r.catalogue_group and all(getattr(r, f) is not None
                for f in ("bore", "stroke", "pressure", "mounting", "certification"))
            for r in output.requirements)

    def derived_missing(self):
        output = self.latest.output if self.latest else None
        if not isinstance(output, TechnicalRequirements):
            return None
        return self.finalize(output)[1]

    def objective_met(self):
        """Governing revision identified and every requirement referenced by the tender read completely in it.
        Facts only other agents can supply (the Technikfreigabe reserve for usable travel) do not keep the
        supplier navigating: they are handed to the coordinator via open_requests()."""
        output = self.latest.output if self.latest else None
        task = getattr(self, "current_task", None)
        if not isinstance(output, TechnicalRequirements) or not output.governing_revision or not output.requirements:
            return False
        if task is None or task.tender is None or not task.tender.positions:
            return False  # without the tender positions coverage of every position cannot be shown
        missing = self.derived_missing() or []
        return not any(m.agent_id == self.agent_id for m in missing)

    def open_requests(self):
        return [m for m in (self.derived_missing() or []) if m.agent_id != self.agent_id]

    def core_covered(self):
        return self.latest is not None and isinstance(self.latest.output, TechnicalRequirements) \
            and self.covered(self.latest.output)

    def task_context(self, task):
        """Compact context: what the tender needs and which amendments/approvals exist, plus progress."""
        tender, commercial = task.tender, task.commercial
        found = self.latest.output if self.latest and isinstance(self.latest.output, TechnicalRequirements) else None
        return {
            'tender': None if tender is None else {
                'reference': tender.reference,
                'positions': [f"{p.position_id}: {p.quantity} {p.unit}, requirement {p.requirement_id}, "
                              f"specification {p.specification_reference}" for p in tender.positions],
                'technical_amendments': [f"{a.answer} ({a.dated_at}): {a.specification_reference} "
                                         f"{a.revision}: {a.value} [{a.status}]"
                                         for a in tender.amendments if a.change_type == 'technical_amendment']},
            'technikfreigabe_known': bool(commercial and any('technik' in f"{r.kind} {r.subject}".casefold()
                                                             for r in commercial.rules)),
            'found_so_far': None if found is None else {
                'revisions': [f"{r.specification_id} {r.revision}: {r.status} current={r.current}"
                              for r in found.revisions],
                'governing_revision': found.governing_revision,
                'requirements': [f"{r.requirement_id} ({r.revision}): group={r.catalogue_group} bore={r.bore} "
                                 f"stroke={r.stroke} {r.stroke_kind or ''} pressure={r.pressure} "
                                 f"mounting={r.mounting} cert={r.certification} per_set={r.pieces_per_set}"
                                 for r in found.requirements]},
            'still_missing': [m.question for m in (self.latest.missing if self.latest else [])][:8]}
