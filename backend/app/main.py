"""The FastAPI app. Every other lane talks through this (docs/lanes/vp.md,
responsibility 3: "The HTTP contract").

No database, no policy engine logic, no SQLAlchemy anywhere in this module
or anything it imports — this branch ships the contract with every
endpoint stubbed against canned fixtures (docs/DEADLINES.md, Tue 25 Aug).
Persistence lands separately (Fri 28 Aug onward).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from shared.constants import SCHEMA_VERSION

from app.api.v1 import agents, assistant, audit, decisions, health, recommendations, simulation
from app.errors import register_exception_handlers

app = FastAPI(
    title="Adaptive AI Governance Platform — Backend",
    description=(
        "The integration point for all four lanes: the Policy Engine, "
        "persistence and audit, the HTTP contract, and the hard ceiling. "
        "See docs/SYSTEM-EXPLAINED.md for the full design."
    ),
    version=SCHEMA_VERSION,
)

# The frontend's Next.js dev server (frontend/package.json: "dev": "next
# dev", default port 3000). No credentials/cookies are used yet — auth is a
# header, not a cookie (app/deps.py) — so a specific origin rather than "*"
# costs nothing and is one less thing to widen later.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

for router in (
    agents.router,
    decisions.router,
    recommendations.router,
    audit.router,
    simulation.router,
    assistant.router,
    health.router,
):
    app.include_router(router, prefix="/api/v1")
