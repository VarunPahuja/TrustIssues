"""Calling a model for the assistant chat endpoint — mode switching, provider
resolution, and the recording-backed cache, all reused from `governance/`
rather than reimplemented (see `app/api/v1/assistant.py`'s module docstring for
why a second LLM integration was rejected).

**What's reused, and what's new.** `governance.llm.registry` (provider choice
and client construction), `governance.llm.recording` (the `Recording`
dataclass, `RecordingStore`, `cache_key_for`, `prompt_fingerprint`), and
`governance.modes` (mode validation) are used exactly as governance itself
uses them. The one addition, `LLMClient.generate_text()`
(`governance/governance/llm/base.py` and each provider client), exists
because `generate()` hard-codes the `AgentOpinion` JSON schema into every
request on all three providers — right for a governance panel agent, wrong
for a free-text chat reply. That method was added to the shared lane rather
than duplicated here.

**A separate recording store, not governance's.** `RECORDING_DIR` points at
`backend/app/data/assistant_recordings/`, not `governance/recordings/` — the
assistant's cached Q&A pairs are a backend concern (keyed by whatever a user
actually asked plus this agent's live evidence), not part of the governance
panel's fixed demo-scenario recordings. The `RecordingStore`/`Recording`
*code* is shared; the directory is not.

**Provider is not pinned to Gemini.** `build_client("assistant")` resolves
through `GOVERNANCE_PROVIDER_ASSISTANT` (if set), then `GOVERNANCE_PROVIDER`,
then the gemini default — the same precedence any governance panel agent
gets. Point `GOVERNANCE_PROVIDER_ASSISTANT` at `claude` or `openai` and the
assistant follows, no code change.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from governance.llm.base import LIVE_TIMEOUT_S, LLMClient
from governance.llm.errors import GovernanceLLMError, RecordingMissError, RecordingStaleError
from governance.llm.recording import (
    RecordingStore,
    build_recording,
    cache_key_for,
    prompt_fingerprint,
)
from governance.llm.registry import build_client
from governance.modes import CACHED, LIVE, STUB, resolve_mode
from governance.prompts.loader import Prompt

# backend/app/services/assistant_llm.py -> backend/app/data/assistant_recordings/
RECORDING_DIR = Path(__file__).resolve().parents[1] / "data" / "assistant_recordings"

PROMPT_VERSION = "v1"

# Assistant defaults to `live`, deliberately the opposite of `GOVERNANCE_MODE`'s
# `stub` default (governance/governance/modes.py). Governance's default protects an
# unconfigured environment from an accidental API call in a workflow that mutates
# state; this assistant makes no request a person didn't just type, so the safer
# default here is the one that actually answers the question. `cached` is one env
# var away for a presentation on a metered key (task brief: "the free tier is 20
# requests per day and a rate limit mid-presentation would be bad").
DEFAULT_ASSISTANT_MODE = LIVE

# Assistant name used for provider resolution and the recording namespace — distinct
# from any of the four governance agent names (risk/performance/compliance/audit) so
# `GOVERNANCE_PROVIDER_ASSISTANT` and this feature's recordings never collide with
# theirs.
AGENT_NAME_PREFIX = "assistant"


@dataclass(frozen=True, slots=True)
class AssistantReply:
    text: str
    mode: str  # what actually answered: "stub" | "cached" | "live" | "live+cached"


def resolve_assistant_mode(explicit: str | None = None) -> str:
    """Which mode serves this request: an explicit override, else `ASSISTANT_MODE`,
    else `DEFAULT_ASSISTANT_MODE`. Reuses `governance.modes.resolve_mode` purely for
    its validation (raises on a typo instead of silently picking a mode nobody
    asked for) — the env var name and the default are the assistant's own.
    """
    if explicit is not None:
        return resolve_mode(explicit)
    return resolve_mode(os.environ.get("ASSISTANT_MODE", DEFAULT_ASSISTANT_MODE))


def _build_prompt(scope: str, system: str, user: str) -> Prompt:
    """`scope` is `"general"` or `"agent"` — folded into the agent name so a general
    question and an agent-scoped one never collide in the recording cache even if
    they happen to render identical text.
    """
    evidence_hash = hashlib.sha256(user.encode("utf-8")).hexdigest()[:16]
    return Prompt(
        agent_name=f"{AGENT_NAME_PREFIX}-{scope}",
        version=PROMPT_VERSION,
        system=system,
        user=user,
        evidence_hash=evidence_hash,
    )


def generate_reply(
    scope: str,
    system: str,
    user: str,
    *,
    mode: str | None = None,
    store: RecordingStore | None = None,
) -> AssistantReply:
    """Produce one assistant reply for the assembled `system`/`user` prompt text.

    Mirrors `governance.agents.llm_backed.opine_with_provenance`'s mode handling —
    `stub` makes no LLM call, `cached` replays a recording, `live` calls the
    provider and falls back to a recording on any failure — but for plain text
    instead of a validated `AgentOpinion`, since there is no schema to fall back
    on validating here.
    """
    resolved = resolve_mode(mode) if mode is not None else resolve_assistant_mode()
    store = store or RecordingStore(directory=RECORDING_DIR)
    prompt = _build_prompt(scope, system, user)

    if resolved == STUB:
        return AssistantReply(text=_stub_reply(user), mode=STUB)

    if resolved == CACHED:
        return AssistantReply(text=_load_cached(prompt, store), mode=CACHED)

    client = build_client(AGENT_NAME_PREFIX)
    text, failure = _try_live(client, prompt)
    if text is not None:
        # Cache every successful live answer under its own recording, so a demo
        # rehearsed once in `live` mode can be replayed reliably in `cached` mode
        # without a second round of API calls or a second day's quota.
        store.save(
            build_recording(prompt, text, client.model, provider=client.provider, model_slug=client.slug)
        )
        return AssistantReply(text=text, mode=LIVE)

    try:
        cached_text = _load_cached(prompt, store, model_slug=client.slug)
    except RecordingMissError as miss:
        raise RecordingMissError(
            f"live call failed ({failure}) and there is no recording to fall back to. {miss}",
            cache_key=miss.cache_key,
        ) from miss
    return AssistantReply(text=cached_text, mode=f"{LIVE}+{CACHED}")


def _load_cached(prompt: Prompt, store: RecordingStore, *, model_slug: str | None = None) -> str:
    """Replay a recorded response for `prompt`, or raise `RecordingMissError`/
    `RecordingStaleError` — the same two failure modes
    `governance.agents.llm_backed.opine_via_model` guards against, for the same
    reason: a stale recording (prompt text edited without a version bump) would
    otherwise replay an answer to a question nobody is currently asking, looking
    perfectly healthy while doing it.
    """
    slug = model_slug if model_slug is not None else build_client(AGENT_NAME_PREFIX).slug
    cache_key = cache_key_for(prompt, slug)
    recording = store.load(cache_key)

    found = prompt_fingerprint(prompt)
    if recording.prompt_sha != found:
        raise RecordingStaleError(
            f"recording {cache_key!r} was made from different prompt text "
            f"(recorded {recording.prompt_sha}, current {found}). The system prompt or "
            f"context-assembly text changed without a version bump.",
            cache_key=cache_key,
            expected=recording.prompt_sha,
            found=found,
        )
    return recording.response_text


def _try_live(client: LLMClient, prompt: Prompt) -> tuple[str | None, str]:
    """Attempt the live call, retrying once if the failure says it is worth
    retrying. Mirrors `governance.agents.llm_backed._try_live`, minus the
    `OpinionParseError` branch — there is no schema for a chat reply to fail.
    """
    failure = ""
    for attempt in (1, 2):
        try:
            return client.generate_text(prompt, timeout_s=LIVE_TIMEOUT_S), ""
        except GovernanceLLMError as exc:
            failure = f"{type(exc).__name__}: {exc}"
            if not exc.retryable or attempt == 2:
                return None, failure
    return None, failure


def _stub_reply(user: str) -> str:
    """No LLM call, ever — the same guarantee `GOVERNANCE_MODE=stub` makes.

    Returns the assembled context itself (trimmed), rather than hand-written
    filler: it is real, correct-scope evidence with nothing invented, it makes
    `stub` mode genuinely useful for a quick check, and — because it is built
    from exactly the same context a live/cached call would receive — it is also
    what the cross-agent isolation test asserts against: a leak would show up
    here as directly as anywhere.
    """
    limit = 12000
    excerpt = user if len(user) <= limit else user[:limit] + "\n...[truncated for stub mode]"
    return (
        "[stub mode — no LLM call was made] Here is the context assembled for this "
        f"question:\n\n{excerpt}"
    )
