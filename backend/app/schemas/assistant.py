"""Request/response models for `POST /api/v1/assistant/chat`.

No `shared/` equivalent — the assistant is a backend-local feature (a help
panel over the API's own data), not a cross-lane contract the trust or
governance lanes need to agree on.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AssistantMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class AssistantChatRequest(BaseModel):
    """`agent_id` absent (or `null`) is the general scope; present, it is the
    agent-scoped conversation — see `app/api/v1/assistant.py` for what each
    scope fetches.
    """

    messages: list[AssistantMessage] = Field(min_length=1)
    agent_id: str | None = None


class AssistantSource(BaseModel):
    """One documentation citation: a doc name (e.g. `"ADR-0006"`) and the
    heading within it the excerpt came from. Mirrors `app.services.doc_index.
    DocChunk`'s two identifying fields — see that module for why this is a
    stub search today, swapped for `vc/assistant-retrieval`'s real index later.
    """

    doc: str
    section: str


class AssistantChatResponse(BaseModel):
    reply: str
    sources: list[AssistantSource]
