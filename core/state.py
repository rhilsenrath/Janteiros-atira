"""Only the coordinator mutates state; tasks receive isolated typed snapshots."""
from dataclasses import dataclass, field
from core.config import RunConfig
from core.contracts import (AgentResult, AgentStatus, TenderData, TechnicalRequirements,
    ERPData, CommercialPolicies, BidResult, Evidence, MissingInformation, ValidationResult, SubmissionReceipt)

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
        self.evidence.extend(result.evidence)
        self.missing = [x for x in self.missing if x.agent_id != result.agent_id]
        self.missing.extend(result.missing)
        if result.error:
            self.errors.append(f"{result.agent_id}: {result.error}")
        for kind, name in [(TenderData, "tender"), (TechnicalRequirements, "technical"),
                           (ERPData, "erp"), (CommercialPolicies, "commercial"), (BidResult, "bid")]:
            if isinstance(result.output, kind):
                setattr(self, name, result.output.model_copy(deep=True))
        if hasattr(result.output, "evidence"):
            self.evidence.extend(result.output.evidence)
