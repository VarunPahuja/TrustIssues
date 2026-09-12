"""The Claude client, via the official `anthropic` SDK.

Optional. `anthropic` is not a hard dependency of this lane — Gemini's free tier is the
default and the project must run end to end with every key blank (docs/lanes/vc.md,
ADR-0012). The import is therefore lazy, and its absence is an actionable error rather
than an ImportError at module load.

**Two things differ from the Gemini client and are easy to get wrong:**

- **Structured output is `output_config.format`, not a `response_schema` field**, and it
  takes *strict JSON Schema* — `additionalProperties: false`, every property in
  `required` — which is a wider dialect than Gemini's OpenAPI subset. That is why
  `strict_json_schema()` exists separately from `gemini_response_schema()`.
- **`temperature` is rejected on Claude Opus 5.** Sampling parameters were removed on
  the current model family and sending one returns a 400. The shared
  `DEFAULT_TEMPERATURE` is deliberately not forwarded here.

Thinking is left at its default (adaptive, on) rather than configured. Only the final
text is recorded, and a governance opinion is exactly the kind of reasoning task the
default is tuned for.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from governance.llm.base import (
    DEFAULT_TIMEOUT_S,
    Pacer,
    model_slug,
)
from governance.llm.errors import (
    LLMAuthError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTransportError,
    ProviderUnavailableError,
)
from governance.prompts.loader import Prompt
from governance.prompts.schema import strict_json_schema

PROVIDER = "claude"

# Opus 5 unless overridden. Reasoning quality is the whole point of putting a second
# provider in the panel — a cheaper model that agrees with Gemini more often would
# defeat the reason for adding it.
DEFAULT_MODEL = "claude-opus-5"

# Room for the reasoning field's 1200-character ceiling plus thinking. Not large enough
# to need streaming.
DEFAULT_MAX_TOKENS = 4096


@dataclass(frozen=True, slots=True)
class ClaudeConfig:
    api_key: str = ""
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_s: float = DEFAULT_TIMEOUT_S
    # Paid tier, so the free-tier floor does not apply — but keep a small gap so a
    # recording run cannot hammer the endpoint.
    min_interval_s: float = 1.0

    @classmethod
    def from_env(cls, **overrides: object) -> ClaudeConfig:
        base: dict[str, object] = {
            "api_key": os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            "model": os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        }
        base.update(overrides)
        return cls(**base)  # type: ignore[arg-type]

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


@dataclass
class ClaudeClient:
    """One Anthropic client, one model, one pacer."""

    config: ClaudeConfig = field(default_factory=ClaudeConfig.from_env)
    provider: str = PROVIDER
    _pacer: Pacer = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._pacer = Pacer(self.config.min_interval_s)

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def has_key(self) -> bool:
        return self.config.has_key

    @property
    def slug(self) -> str:
        return model_slug(self.provider, self.config.model)

    def build_request(self, prompt: Prompt) -> dict:
        """The keyword arguments passed to `messages.create`.

        Separate from `generate()` so the request shape can be asserted on without a
        network, and so the recording script can log what produced a response.
        """
        return {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
            "output_config": {
                "format": {"type": "json_schema", "schema": strict_json_schema()}
            },
        }

    def build_request_text(self, prompt: Prompt) -> dict:
        """Same request as `build_request`, minus the structured-output constraint.

        For a caller that wants prose, not an `AgentOpinion` — see `generate_text`.
        """
        return {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
        }

    def generate(
        self,
        prompt: Prompt,
        *,
        client: object | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """Send one prompt, return Claude's raw text, constrained to the
        `AgentOpinion` schema (see `build_request`).

        `client` is injectable so tests can pass a stub without the SDK installed.
        """
        return self._send(self.build_request(prompt), client=client, timeout_s=timeout_s)

    def generate_text(
        self,
        prompt: Prompt,
        *,
        client: object | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """Same call as `generate`, but the response is unconstrained prose —
        for a caller that isn't asking for an `AgentOpinion` (see `base.LLMClient`).
        """
        return self._send(self.build_request_text(prompt), client=client, timeout_s=timeout_s)

    def _send(
        self,
        request: dict,
        *,
        client: object | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """The API call both `generate` and `generate_text` make — key check, pacing,
        and error translation, shared so the two differ only in the request they send.
        """
        if not self.config.has_key:
            raise LLMAuthError(
                "ANTHROPIC_API_KEY is empty. Claude is an optional provider — set "
                "GOVERNANCE_PROVIDER=gemini to use the free tier instead, or put a key "
                "in .env (which is gitignored)."
            )

        sdk = client if client is not None else self._build_client()
        self._pacer.wait()

        # Per call, so live mode's deadline does not require a second client for the
        # same provider — and therefore a second Pacer, which would let two agents send
        # at twice the rate the key allows.
        request = dict(request)
        if timeout_s is not None:
            request["timeout"] = timeout_s

        try:
            response = sdk.messages.create(**request)
        except Exception as exc:
            raise _translate(exc) from exc

        return _extract_text(response)

    def _build_client(self) -> object:
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderUnavailableError(
                "the 'anthropic' package is not installed. It is an optional extra: "
                "pip install 'governance[claude]'. Gemini needs no extra."
            ) from exc
        return anthropic.Anthropic(api_key=self.config.api_key, timeout=self.config.timeout_s)


def _translate(exc: Exception) -> Exception:
    """Map an SDK exception onto this lane's hierarchy, most specific first.

    Matched on class name rather than on imported types so this function works whether
    or not the SDK is installed — a test can raise a stand-in with the same name. The
    flag that matters downstream is `retryable`; live mode's fallback reads that rather
    than matching exception types of its own.
    """
    name = type(exc).__name__
    message = str(exc)

    if name == "RateLimitError":
        return LLMRateLimitError(f"Claude rate limit hit: {message}")
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return LLMAuthError(
            f"Claude rejected the API key: {message}. Check ANTHROPIC_API_KEY in .env."
        )
    if name in ("APITimeoutError", "APIConnectionError", "InternalServerError"):
        return LLMTransportError(f"Claude call failed to complete: {message}")
    if name == "BadRequestError":
        return LLMResponseError(f"Claude rejected the request: {message}")
    if name.endswith("Error") and name.startswith("API"):
        return LLMTransportError(f"Claude call failed: {message}")
    return exc


def _extract_text(response: object) -> str:
    """Pull the JSON text out of the content blocks.

    `output_config.format` guarantees the first text block holds valid JSON, but the
    response may also carry thinking blocks — so the type is checked rather than
    indexing blindly at position zero.
    """
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        raise LLMResponseError(
            f"Claude declined the request (refusal, category={category!r}). The prompt "
            f"was filtered rather than answered."
        )

    blocks = getattr(response, "content", None) or []
    text = "".join(
        getattr(block, "text", "") for block in blocks if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise LLMResponseError(
            f"Claude returned no text block (stop_reason={stop_reason!r})."
        )
    return text
