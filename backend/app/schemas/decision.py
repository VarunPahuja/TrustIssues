"""Decision response and ingest-request models.

`DecisionRecordOut` mirrors `shared.contracts.DecisionRecord` field-for-field
(including `ground_truth` — present on the dataclass because "every synthetic
invoice carries a deterministic correct answer" while the simulator is the
only decision source; once real invoices arrive this field's meaning changes,
not its shape — see `shared/contracts.py` and ADR-0009).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from shared.enums import Action


class DecisionRecordOut(BaseModel):
    """Mirrors `shared.contracts.DecisionRecord` field-for-field."""

    model_config = ConfigDict(from_attributes=True)

    decision_id: str
    sequence: int
    invoice_id: str
    amount: int
    action: Action
    ground_truth: Action
    agent_id: str
    decided_at: datetime | None
    recommended_action: Action | None
    human_ruling: Action | None


class DecisionCreate(BaseModel):
    """Request body for `POST /api/v1/decisions` — the simulator's ingest path.

    Every field needed to construct a `DecisionRecord` (see
    `shared.contracts.DecisionRecord`), plus `reason`
    (cross-cutting rule: every state-changing endpoint requires one — here,
    why this decision is being submitted, e.g. which simulation run or
    invoice-processing batch produced it).
    """

    invoice_id: str
    amount: int = Field(gt=0)
    action: Action
    ground_truth: Action
    agent_id: str
    recommended_action: Action | None = Field(
        default=None,
        description=(
            "What the agent would have done had it been allowed to act. Only "
            "meaningful when `action` is ESCALATE: `shared.contracts."
            "DecisionRecord.has_human_ruling` requires both this and a later "
            "`human_ruling` before the pair counts toward human agreement."
        ),
    )
    reason: str = Field(min_length=1, description="Why this decision is being submitted")


class DecisionRuling(BaseModel):
    """Request body for `POST /api/v1/decisions/{decision_id}/ruling`.

    Records what a human decided about an escalated decision. Only ADMIN or
    REVIEWER may call this (`app/deps.py`) — ruling on an escalation is
    REVIEWER's job, the same reasoning as reviewing an audit sample
    (ADR-0009); AUDITOR stays read-only.

    A ruling is only half of the evidence: `human_agreement` compares it
    against the agent's own `recommended_action`, so a decision ingested
    without one can be ruled on but will not contribute to the trust score.
    """

    ruling: Action = Field(
        description="The human's verdict: APPROVE or REJECT. Never ESCALATE."
    )
    reason: str = Field(min_length=1, description="Why the human ruled this way")
