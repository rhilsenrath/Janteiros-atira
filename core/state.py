"""Only the coordinator mutates state; tasks receive isolated typed snapshots."""
from dataclasses import dataclass, field
from pydantic import BaseModel
from core.config import RunConfig
from core.contracts import (AgentResult, AgentStatus, TenderData, TechnicalRequirements,
    ERPData, CommercialPolicies, BidResult, Evidence, MissingInformation, ValidationResult, SubmissionReceipt)

def merge_progress(previous, incoming):
    """Merge partial records without losing previously observed fields or mixing revisions."""
    if previous is None or type(previous) is not type(incoming):
        return incoming
    if isinstance(incoming, BaseModel):
        if hasattr(incoming, "revision") and incoming.revision and previous.revision != incoming.revision:
            return incoming
        updates = {}
        for name in type(incoming).model_fields:
            old, new = getattr(previous, name), getattr(incoming, name)
            updates[name] = old if new is None else merge_progress(old, new)
        return incoming.model_copy(update=updates)
    if isinstance(incoming, list):
        def identity(value):
            if isinstance(value, Evidence): return (value.id, value.field, value.excerpt)
            if getattr(value, "specification_id", None) and getattr(value, "revision", None) \
                    and not getattr(value, "requirement_id", None):
                return ("revision", value.specification_id, value.revision)
            # Specific ids first: one position can carry several changes/amendments.
            for name in ("change_id", "amendment_id", "identifier", "position_id", "requirement_id",
                         "material_id", "url", "key", "label"):
                if name == "url" and getattr(value, "revision", None):
                    continue  # revisions of one specification can share a viewer URL
                key = getattr(value, name, None)
                if key is not None:
                    return (name, key, getattr(value, "effective_at", None), getattr(value, "revision", None))
            return repr(value)
        merged = {identity(value): value for value in previous}
        for value in incoming:
            key = identity(value)
            merged[key] = merge_progress(merged.get(key), value)
        return list(merged.values())
    return incoming

@dataclass
class RunState:
    metadata: dict
    config: RunConfig
    tender: TenderData | None = None
    technical: TechnicalRequirements | None = None
    erp: ERPData | None = None
    commercial: CommercialPolicies | None = None
    bid: BidResult | None = None
    validation: ValidationResult | None = None
    submission_receipt: SubmissionReceipt | None = None
    statuses: dict[str, AgentStatus] = field(default_factory=dict)
    results: list[AgentResult] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    missing: list[MissingInformation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def collect(self, result: AgentResult):
        self.statuses[result.agent_id] = result.status
        self.results.append(result)
        known = {(e.id, e.field, e.excerpt) for e in self.evidence}
        for evidence in result.evidence:
            key = (evidence.id, evidence.field, evidence.excerpt)
            if key not in known:
                self.evidence.append(evidence)
                known.add(key)
        self.missing = [x for x in self.missing if x.agent_id != result.agent_id]
        self.missing.extend(result.missing)
        if result.error:
            self.errors.append(f"{result.agent_id}: {result.error}")
        for kind, name in [(TenderData, "tender"), (TechnicalRequirements, "technical"),
                           (ERPData, "erp"), (CommercialPolicies, "commercial"), (BidResult, "bid")]:
            if isinstance(result.output, kind):
                output = result.output
                if result.status in (AgentStatus.partial, AgentStatus.blocked, AgentStatus.failed):
                    output = merge_progress(getattr(self, name), output)
                setattr(self, name, output.model_copy(deep=True))
        if hasattr(result.output, "evidence"):
            for evidence in result.output.evidence:
                key = (evidence.id, evidence.field, evidence.excerpt)
                if key not in known:
                    self.evidence.append(evidence)
                    known.add(key)
