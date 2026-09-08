"""Simulator-local invoice and run models.

These models describe generated simulation data. Cross-lane decisions use the
canonical contracts in ``shared.contracts``.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field
from shared.enums import Action


class InvoiceCategory(str, Enum):
    TRAVEL = "travel"
    SUPPLIES = "supplies"
    SOFTWARE = "software"
    CONSULTING = "consulting"
    LOGISTICS = "logistics"


class SimulationPhase(str, Enum):
    GOOD = "good"
    DEGRADED = "degraded"
    RECOVERY = "recovery"


class Invoice(BaseModel):
    invoice_id: str = Field(default_factory=lambda: str(uuid4()))
    submitted_by: str
    vendor_name: str
    invoice_date: date
    category: InvoiceCategory
    amount: str
    description: str
    department: str | None = None
    cost_centre: str | None = None
    purchase_order: str | None = None
    phase: SimulationPhase = SimulationPhase.GOOD
    is_boundary_case: bool = False
    is_ambiguous_vendor: bool = False
    has_missing_fields: bool = False
    missing_field_names: list[str] = Field(default_factory=list)
    ground_truth_decision: Action | None = None
    ground_truth_reason: str = ""
    ground_truth_confidence: float = 1.0


class SimulationRunConfig(BaseModel):
    phase: SimulationPhase = SimulationPhase.GOOD
    invoice_count: int = 100
    seed: int = 42
    agent_id: str = "scripted-agent-001"
    submit: bool = False


class AgentOutcome(BaseModel):
    invoice_id: str
    agent_id: str
    action: Action
    reason: str
    confidence: float
    from_cache: bool = False
    cache_key: str | None = None
    is_correct: bool | None = None

    @property
    def decision(self) -> Action:
        return self.action


class SimulationRunResult:
    def __init__(self, config: SimulationRunConfig, total_invoices: int = 0) -> None:
        self.config = config
        self.total_invoices = total_invoices
        self.completed_at: datetime | None = None
        self.approved_count = 0
        self.rejected_count = 0
        self.escalated_count = 0
        self.correct_decisions = 0
        self.accuracy = None
        self.wilson_lower_bound = None
        self.cache_hits = 0
        self.llm_calls = 0
        self.errors: list[str] = []