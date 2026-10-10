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
    position_id: str
    quantity: Decimal | None = None
    unit: str | None = None
    requirement_id: str | None = None
    condition_group: str | None = None
    packaging_key: str | None = None
    delivery_key: str | None = None
    setup_pool: str | None = None

class TenderData(Provenanced):
    kind: Literal["tender"] = "tender"
    reference: str | None = None
    scenario_date: datetime | None = None
    positions: list[TenderPosition] = Field(default_factory=list)

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

class TechnicalRequirements(Provenanced):
    kind: Literal["technical"] = "technical"
    requirements: list[TechnicalRequirement] = Field(default_factory=list)

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
    setup_fee: Decimal | None = None
    surcharge_mass: Decimal | None = None
    allocation_mass: Decimal | None = None
    pieces_per_carton: Decimal | None = None

class ERPData(Provenanced):
    kind: Literal["erp"] = "erp"
    materials: list[MaterialCandidate] = Field(default_factory=list)

class CommercialRule(Provenanced):
    key: str
    kind: str
    text: str
    authorized_sender: str | None = None
    effective_at: datetime | None = None
    supersedes: str | None = None

class CommercialPolicies(Provenanced):
    kind: Literal["commercial"] = "commercial"
    rules: list[CommercialRule] = Field(default_factory=list)
    approved_discount: Decimal | None = None
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
    action: Literal["investigate", "price", "submit", "reinvestigate"] = "investigate"
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
