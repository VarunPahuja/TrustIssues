"""Recommendation response models and the approve/reject request bodies.

`AgentOpinionOut` and `RecommendationOut` mirror `shared.contracts`'s
`AgentOpinion` and `Recommendation` field-for-field — including `clamped`
and `clamped_from`, so the hard ceiling (docs/lanes/vp.md, responsibility
4) is visible in the API response itself, not just in the audit log.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from shared.enums import Direction, OpinionVerdict, RecommendationStatus


class AgentOpinionOut(BaseModel):
    """Mirrors `shared.contracts.AgentOpinion` field-for-field."""

    model_config = ConfigDict(from_attributes=True)

    agent_name: str
    verdict: OpinionVerdict
    reasoning: str
    concerns: list[str]
    confidence: float


class RecommendationOut(BaseModel):
    """Mirrors `shared.contracts.Recommendation` field-for-field."""

    model_config = ConfigDict(from_attributes=True)

    recommendation_id: str
    agent_id: str
    schema_version: str

    direction: Direction
    proposed_limit: int
    proposed_rung: int
    rationale: str

    opinions: list[AgentOpinionOut]
    has_dissent: bool
    confidence: float

    governance_mode: str
    status: RecommendationStatus
    trust_evaluation_ref: str | None
    generated_at: datetime | None

    clamped: bool
    clamped_from: int | None

    # Not on `shared.contracts.Recommendation` — that dataclass has no
    # `reason_codes` field to mirror, and `shared/` is frozen, so this is a
    # backend-local addition rather than a treaty field. Derived at read
    # time from `clamped` (`app.services.governance.recommendation_out`),
    # the same way `has_dissent`/`confidence`/`proposed_rung` are already
    # derived from `agent_opinions` rather than stored — see
    # `app/models/recommendations.py`'s docstring for why this table
    # prefers deriving over storing redundant data that could disagree with
    # its own source. Currently ever contains at most one code
    # (`shared.reason_codes.RECOMMENDATION_CLAMPED`) — a list, not a single
    # optional field, so a second recommendation-level reason code never
    # needs a shape change to add.
    reason_codes: list[str] = Field(default_factory=list)


class RecommendationDecision(BaseModel):
    """Request body for both `POST .../approve` and `POST .../reject`.

    `reason` is mandatory on both — approving *and* rejecting are mutations,
    and the cross-cutting rule doesn't carve out an exception for the
    "yes" path. Only ADMIN may call either endpoint (see `app/deps.py`,
    `Role.ADMIN` docstring: "Reviewer explicitly cannot approve").
    """

    reason: str = Field(min_length=1, description="Why this decision was made")
