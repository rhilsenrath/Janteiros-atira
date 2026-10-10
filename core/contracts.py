"""Typed integration boundary. Unknown values stay None; evidence travels with records."""
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr

class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

class AgentStatus(StrEnum):
    pending = "pending"
    running = "running"
    complete = "complete"
    partial = "partial"
    blocked = "blocked"
    failed = "failed"
    cancelled = "cancelled"

class Evidence(Record):
    id: str
    agent_id: str
    source_site: str
    source_url: str | None = None
    document: str | None = None
    reference: str | None = None
    revision: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    applicable_at: datetime | None = None
    field: str | None = None
    excerpt: str | None = None
    mock: bool = False

class Provenanced(Record):
    evidence: list[Evidence] = Field(default_factory=list)

class TenderPosition(Provenanced):
    """`quantity`/`unit` and the commercial keys are the effective values resolved by
    services.tender_resolution; `original_*` keep the bill-of-quantities values."""
    position_id: str
    description: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    original_quantity: Decimal | None = None
    original_unit: str | None = None
    quantity_basis: str | None = None  # "original" or "answer:<identifier>" once resolved
    specification_reference: str | None = None
    requirement_id: str | None = None
    condition_group: str | None = None
    packaging_key: str | None = None
    delivery_key: str | None = None
    setup_pool: str | None = None
    setup_pool_member: bool | None = None  # False: explicitly no shared setup pool ("kein Rüstpool")
    bonus_plan: str | None = None
    bonus_member: bool | None = None  # False: explicitly no project bonus ("kein Projektbonus")
    setup_fee: Decimal | None = None  # ordinary one-off setup fee for this position, net EUR, from Q&A

TenderChangeType = Literal["quantity", "condition_group", "packaging_key", "delivery_key", "setup_pool",
                           "bonus_plan", "setup_fee", "technical_amendment", "other"]

class ClarificationChange(Record):
    """One effect of a bidder Q&A answer on one position (or the whole tender when position_id is None)."""
    change_id: str | None = None  # assigned by resolution: "<answer>:<type>:<position>"
    change_type: TenderChangeType
    position_id: str | None = None
    value: str | None = None  # new quantity / key / pool id / free text, as published
    unit: str | None = None
    specification_reference: str | None = None
    revision: str | None = None
    applies: bool = True  # False: the answer states the position is NOT a member (no pool, no bonus)
    tender_reference: str | None = None  # reference named in the answer for this change, if any

class Clarification(Provenanced):
    identifier: str  # answer number as published, e.g. "A-07"
    text: str  # complete answer text
    question: str | None = None
    position_id: str | None = None
    dated_at: datetime | None = None
    date_text: str | None = None  # publication date exactly as displayed (row, tooltip)
    tender_reference: str | None = None  # reference the answer names, if any
    withdraws: list[str] = Field(default_factory=list)
    specification_revision: str | None = None
    changes: list[ClarificationChange] = Field(default_factory=list)
    withdrawn: bool | None = None  # resolved: withdrawn by a later effective answer
    withdrawn_by: str | None = None

class TenderAmendment(Provenanced):
    """Resolved audit trail of a change: which answer set what, and whether it is still effective."""
    amendment_id: str
    change_type: TenderChangeType
    position_id: str | None = None
    answer: str
    dated_at: datetime | None = None
    value: str | None = None
    unit: str | None = None
    specification_reference: str | None = None
    revision: str | None = None
    status: Literal["effective", "superseded", "withdrawn", "not_applicable"]
    replaced_by: str | None = None
    reason: str | None = None  # why a change is not applicable (other reference, after scenario date)

class TenderDate(Provenanced):
    label: str  # e.g. "Angebotsfrist", "Fragenfrist"
    text: str  # as displayed, including the portal's timezone note
    at: datetime | None = None
    timezone: str | None = None

class SubmissionRequirements(Provenanced):
    configurator_fields: list[str] = Field(default_factory=list)
    signing_procedure: str | None = None
    steps: list[str] = Field(default_factory=list)
    partial_submission: str | None = None

class DocumentReference(Provenanced):
    title: str
    url: str | None = None
    revision: str | None = None
    current: bool | None = None

class TenderData(Provenanced):
    kind: Literal["tender"] = "tender"
    reference: str | None = None
    title: str | None = None
    buyer: str | None = None
    scenario_date: datetime | None = None
    timezone: str | None = None
    dates: list[TenderDate] = Field(default_factory=list)
    positions: list[TenderPosition] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)
    qa_complete: bool | None = None  # every Q&A page/answer was read, not only the first page
    amendments: list[TenderAmendment] = Field(default_factory=list)
    submission: SubmissionRequirements | None = None
    documents: list[DocumentReference] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

class TechnicalRequirement(Provenanced):
    requirement_id: str
    revision: str | None = None
    catalogue_group: str | None = None
    bore: Decimal | None = None
    stroke: Decimal | None = None
    pressure: Decimal | None = None
    mounting: str | None = None
    certification: str | None = None
    pieces_per_set: Decimal | None = None
    stroke_kind: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)

class TechnicalRequirements(Provenanced):
    kind: Literal["technical"] = "technical"
    requirements: list[TechnicalRequirement] = Field(default_factory=list)
    documents: list[DocumentReference] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

class PriceCondition(Provenanced):
    price: Decimal | None = None
    currency: str | None = None
    price_unit: Decimal | None = None
    minimum_quantity: Decimal | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    fx_key: str | None = None

class MaterialCandidate(Provenanced):
    material_id: str
    active: bool | None = None
    specification: TechnicalRequirement | None = None
    conditions: list[PriceCondition] = Field(default_factory=list)
    stock: Decimal | None = None
    stock_lead_days: int | None = None
    production_lead_days: int | None = None
    stock_lead_weeks: int | None = None
    production_lead_weeks: int | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    setup_fee: Decimal | None = None
    surcharge_mass: Decimal | None = None
    allocation_mass: Decimal | None = None
    pieces_per_carton: Decimal | None = None

class ERPData(Provenanced):
    kind: Literal["erp"] = "erp"
    materials: list[MaterialCandidate] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

class CommercialRule(Provenanced):
    key: str
    kind: str
    text: str
    authorized_sender: str | None = None
    effective_at: datetime | None = None
    supersedes: str | None = None
    tender_reference: str | None = None
    subject: str | None = None
    authorized: bool | None = None
    valid_until: datetime | None = None

class CommercialPolicies(Provenanced):
    kind: Literal["commercial"] = "commercial"
    rules: list[CommercialRule] = Field(default_factory=list)
    approved_discount: Decimal | None = None
    conflicts: list[str] = Field(default_factory=list)
    signing_token: SecretStr | None = Field(default=None, exclude=True, repr=False)

class BidPosition(Provenanced):
    position: str
    material_number: str
    quantity: Decimal
    unit_price: Decimal | None = None
    lead_time_weeks: int | None = None
    base_unit_price: Decimal | None = None
    surcharge_per_unit: Decimal | None = None
    packaging_per_unit: Decimal | None = None
    rebate_total: Decimal | None = None

class BidResult(Provenanced):
    kind: Literal["bid"] = "bid"
    complete: bool = False
    mock: bool = False
    decision: Literal["bid", "no_bid"] | None = None
    tender_reference: str | None = None
    discount_percent: Decimal | None = None
    positions: list[BidPosition] = Field(default_factory=list)
    total_net: Decimal | None = None
    missing: list[str] = Field(default_factory=list)

class SubmissionReceipt(Record):
    kind: Literal["receipt"] = "receipt"
    simulated: bool = False
    confirmed: bool = False
    reference: str | None = None

class ValidationResult(Record):
    valid: bool
    issues: list[str] = Field(default_factory=list)

class MissingInformation(Record):
    agent_id: str
    question: str
    position_id: str | None = None

class AgentTask(Record):
    action: Literal["investigate", "price", "submit", "reinvestigate", "retrieve_signing_token"] = "investigate"
    question: str | None = None
    tender: TenderData | None = None
    technical: TechnicalRequirements | None = None
    erp: ERPData | None = None
    commercial: CommercialPolicies | None = None
    bid: BidResult | None = None

AgentOutput = Annotated[TenderData | TechnicalRequirements | ERPData | CommercialPolicies | BidResult | SubmissionReceipt,
    Field(discriminator="kind")]

class AgentResult(Record):
    agent_id: str
    status: AgentStatus
    output: AgentOutput | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    missing: list[MissingInformation] = Field(default_factory=list)
    error: str | None = None
    retryable: bool = False
    mock: bool = False


def is_synthetic(value) -> bool:
    """Inspect nested typed records so synthetic evidence cannot hide in a child record."""
    if isinstance(value, BaseModel):
        return bool(getattr(value, "mock", False)) or any(
            is_synthetic(getattr(value, field)) for field in type(value).model_fields)
    if isinstance(value, (list, tuple)):
        return any(is_synthetic(item) for item in value)
    return False
