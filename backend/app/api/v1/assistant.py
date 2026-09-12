"""Read-only assistant chat — `POST /api/v1/assistant/chat`.

This is a help panel, not a new way into the enforcement path. The project's whole
argument (docs/SYSTEM-EXPLAINED.md §2: "LLM reasons. Statistics provide evidence.
Policy Engine enforces. Humans authorize.") depends on the LLM never touching the
side that changes anything, and this endpoint is deliberately built so it cannot:
no tools, no function calling, nothing here writes to the database except the
ordinary commit `app.deps.get_session` already does for a request that changes
nothing (`app/services/assistant.py`'s module docstring goes further — reads the
latest trust evaluation instead of computing and persisting a fresh one, so even
that "ordinary" transaction has nothing to commit). `app/services/assistant_llm.py`
calls a model for text and nothing else; there is no path from a reply back into
`app/policy/`, `app/services/governance.py`, or a `policy_versions` write.

**Two scopes.** `agent_id` absent is the general scope: architecture plus the doc
index. `agent_id` present is agent-scoped: the same, plus one agent's full evidence
— trust evaluation, history, policy versions, recommendations with their governance
opinions, decisions, and audit log — fetched by that one id
(`app/services/assistant.py:build_agent_context`).

**Isolation is context scoping, not access control.** An agent-scoped conversation's
prompt contains exactly one agent's data because every query in `build_agent_context`
filters on `agent.id` at the database layer — there is no step that loads every
agent and asks the model to pick the right one. But nothing here checks whether the
caller is *allowed* to see that agent: any authenticated user who can open agent X's
detail page gets agent X's assistant, exactly as they already get agent X's trust
chart. Per-agent RBAC does not exist in this system (`app/deps.py` has three roles,
none of them scoped to an agent) — so this endpoint carries the same exposure as
every other agent-detail route today, no more and no less. Do not describe this as a
permission boundary; it is not one.

**Why this imports `governance.llm.*` directly instead of `governance.coordinator.
recommend()`.** `recommend()` is the four-agent panel workflow: it takes a
`TrustEvaluation` and returns a `Recommendation`, and every provider client's
`generate()` hard-codes the `AgentOpinion` JSON schema into the request — the wrong
shape for a chat reply, on all three providers. Building a second, parallel LLM
integration in `backend/` to work around that was rejected (the task's own framing);
routing free-text chat through the opinion schema by repurposing `reasoning` as the
reply was also rejected, since it caps every answer at 1200 characters and forces a
meaningless CONCUR/OBJECT/ABSTAIN verdict onto ordinary questions. The change made
instead — additive, in `governance/`, covered by that lane's own test suite —
was a `generate_text()` method on `LLMClient` and each provider client
(`governance/governance/llm/base.py`, `gemini.py`, `claude.py`, `openai_client.py`):
same client, same `Pacer`, same error hierarchy, same registry and recording store,
just without the structured-output constraint. Nothing about the four governance
agents' own behaviour changed; they still call `generate()` exclusively. See
`app/services/assistant_llm.py`'s module docstring for the reuse in full.
"""

from __future__ import annotations

from fastapi import APIRouter
from governance.llm.errors import GovernanceLLMError
from sqlalchemy.orm import Session

from app.deps import CurrentUserDep, DbSessionDep
from app.errors import (
    NOT_FOUND_RESPONSE,
    SERVICE_UNAVAILABLE_RESPONSE,
    not_found,
    service_unavailable,
)
from app.models import Agent
from app.schemas.assistant import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantMessage,
    AssistantSource,
)
from app.services.assistant import SYSTEM_PROMPT, build_agent_context, build_general_context
from app.services.assistant_llm import generate_reply
from app.services.doc_index import DocChunk

router = APIRouter(prefix="/assistant", tags=["assistant"])


def _get_agent_or_404(db: Session, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise not_found("agent_not_found", f"No agent {agent_id!r}.", {"agent_id": agent_id})
    return agent


def _render_user_prompt(context: str, messages: list[AssistantMessage]) -> str:
    """Assembled context, the conversation so far (everything but the last
    message), and the question the model is actually being asked right now.
    Splitting the last message out — rather than rendering the whole history
    undifferentiated — is what lets a follow-up like "what would it take to
    reach the next rung" read as a question in its own right rather than one
    more line of transcript.
    """
    *history, last = messages
    parts = [context]
    if history:
        convo = "\n".join(f"{m.role.capitalize()}: {m.content}" for m in history)
        parts.append(f"## Conversation so far\n{convo}")
    parts.append(f"## Question\n{last.content}")
    return "\n\n".join(parts)


def _sources_out(chunks: list[DocChunk]) -> list[AssistantSource]:
    return [AssistantSource(doc=c.doc, section=c.section) for c in chunks]


@router.post(
    "/chat",
    response_model=AssistantChatResponse,
    responses={**NOT_FOUND_RESPONSE, **SERVICE_UNAVAILABLE_RESPONSE},
)
def chat(body: AssistantChatRequest, user: CurrentUserDep, db: DbSessionDep) -> AssistantChatResponse:
    """Answer one question, in the requested scope. `agent_id` absent is the
    general scope; present, it must name a real agent (404 otherwise) and the
    reply is built from that agent's evidence alone (`app/services/assistant.py`).

    No role restriction — this is a read endpoint over data every stub role can
    already see elsewhere (agents, decisions, recommendations, audit log), the
    same reasoning `GET /agents/{id}` itself carries no `require_role`.
    """
    question = body.messages[-1].content

    if body.agent_id is not None:
        agent = _get_agent_or_404(db, body.agent_id)
        context, chunks = build_agent_context(db, agent, question)
        scope = "agent"
    else:
        context, chunks = build_general_context(question)
        scope = "general"

    user_prompt = _render_user_prompt(context, body.messages)

    try:
        result = generate_reply(scope, SYSTEM_PROMPT, user_prompt)
    except GovernanceLLMError as exc:
        # Mirrors app/services/governance.py:generate_recommendation's own handling
        # of a governance-layer failure: a 503 says "the assistant is unavailable
        # right now," which is the truth, rather than guessing at an answer with
        # nothing behind it.
        raise service_unavailable(
            "assistant_unavailable",
            f"The assistant could not answer in {scope!r} scope: {exc}",
            {"scope": scope, "agent_id": body.agent_id},
        ) from exc

    return AssistantChatResponse(reply=result.text, sources=_sources_out(chunks))
