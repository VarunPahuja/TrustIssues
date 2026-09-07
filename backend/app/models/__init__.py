"""SQLAlchemy models for every table in docs/lanes/vp.md's schema.

Import this package (or `app.models.base.Base`) to get `Base.metadata`
populated with every table — Alembic's `env.py` and `Base.metadata.create_all`
in tests both rely on that. `app.models.guards` is imported for its side
effect (registering the `before_flush` append-only / paired-limit-change hook
on every `Session`) — see that module's docstring. Its own imports reach
`Agent`/`PolicyVersion`/`AuditLogEntry` directly by submodule path, so it
registers correctly regardless of where this line sits relative to the
others below (ruff's import sort places it first, alphabetically).
"""

from __future__ import annotations

from app.models import guards  # noqa: F401  (registers the before_flush hook)
from app.models.agents import Agent
from app.models.approvals import Approval
from app.models.audit_log import AuditLogEntry
from app.models.audit_samples import AuditSample
from app.models.base import Base
from app.models.decisions import Decision
from app.models.invoices import Invoice
from app.models.policy_versions import PolicyVersion, apply_policy_version
from app.models.recommendations import Recommendation
from app.models.simulation_runs import SimulationRun
from app.models.trust_evaluations import TrustEvaluation
from app.models.users import User

__all__ = [
    "Agent",
    "Approval",
    "AuditLogEntry",
    "AuditSample",
    "Base",
    "Decision",
    "Invoice",
    "PolicyVersion",
    "Recommendation",
    "SimulationRun",
    "TrustEvaluation",
    "User",
    "apply_policy_version",
]
