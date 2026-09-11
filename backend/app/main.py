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

from app.api.v1 import agents, audit, decisions, health, recommendations, simulation
from app.config import cors_allow_origins
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

# Which browser origins may call this API. `CORS_ALLOW_ORIGINS` (comma-
# separated) in a deployment; the Next.js dev server by default, so a local
# checkout needs no configuration. No credentials/cookies are used — auth is
# a header, not a cookie (app/deps.py) — so naming exact origins rather than
# "*" costs nothing and keeps the check meaningful.
#
# This is read at import time on purpose: the allowed origins are part of how
# the app is deployed, not a per-request decision, and a value that changed
# under a running process would make a CORS failure impossible to reproduce.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

for router in (agents.router, decisions.router, recommendations.router, audit.router, simulation.router, health.router):
    app.include_router(router, prefix="/api/v1")
