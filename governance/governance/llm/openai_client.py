"""The OpenAI client, via the official `openai` SDK.

Optional, on the same terms as Claude — Gemini's free tier stays the default and the
project runs end to end with every key blank (docs/lanes/vc.md, ADR-0012). The import is
lazy so a missing package is an actionable error rather than an ImportError at load.

Structured output is `response_format={"type": "json_schema", ...}` with `strict: true`,
which takes the same strict-JSON-Schema dialect Claude does — `additionalProperties:
false`, every property in `required`. That is why one `strict_json_schema()` serves both
and Gemini needs its own.

The named schema is required by the API, so it is named for what it is: an agent
opinion, versioned alongside the prompts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from governance.llm.base import (
    DEFAULT_TEMPERATURE,
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

PROVIDER = "openai"

DEFAULT_MODEL = "gpt-4o"
DEFAULT_MAX_TOKENS = 2048
SCHEMA_NAME = "agent_opinion"


@dataclass(frozen=True, slots=True)
class OpenAIConfig:
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_s: float = DEFAULT_TIMEOUT_S
    min_interval_s: float = 1.0

    @classmethod
    def from_env(cls, **overrides: object) -> OpenAIConfig:
        base: dict[str, object] = {
            "api_key": os.environ.get("OPENAI_API_KEY", "").strip(),
            "model": os.environ.get("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        }
        base.update(overrides)
        return cls(**base)  # type: ignore[arg-type]

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


@dataclass
class OpenAIClient:
    """One OpenAI client, one model, one pacer."""

    config: OpenAIConfig = field(default_factory=OpenAIConfig.from_env)
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
        """The keyword arguments passed to `chat.completions.create`."""
        return {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": SCHEMA_NAME,
                    "strict": True,
                    "schema": strict_json_schema(),
                },
            },
        }

    def build_request_text(self, prompt: Prompt) -> dict:
        """Same request as `build_request`, minus the structured-output constraint.

        For a caller that wants prose, not an `AgentOpinion` — see `generate_text`.
        """
        return {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
        }

    def generate(
        self,
        prompt: Prompt,
        *,
        client: object | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """Constrained to the `AgentOpinion` schema (see `build_request`)."""
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
                "OPENAI_API_KEY is empty. OpenAI is an optional provider — set "
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
            response = sdk.chat.completions.create(**request)
        except Exception as exc:
            raise _translate(exc) from exc

        return _extract_text(response)

    def _build_client(self) -> object:
        try:
            import openai
        except ImportError as exc:
            raise ProviderUnavailableError(
                "the 'openai' package is not installed. It is an optional extra: "
                "pip install 'governance[openai]'. Gemini needs no extra."
            ) from exc
        return openai.OpenAI(api_key=self.config.api_key, timeout=self.config.timeout_s)


def _translate(exc: Exception) -> Exception:
    """Map an SDK exception onto this lane's hierarchy. See `claude._translate`."""
    name = type(exc).__name__
    message = str(exc)

    if name == "RateLimitError":
        return LLMRateLimitError(f"OpenAI rate limit hit: {message}")
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return LLMAuthError(
            f"OpenAI rejected the API key: {message}. Check OPENAI_API_KEY in .env."
        )
    if name in ("APITimeoutError", "APIConnectionError", "InternalServerError"):
        return LLMTransportError(f"OpenAI call failed to complete: {message}")
    if name == "BadRequestError":
        return LLMResponseError(f"OpenAI rejected the request: {message}")
    if name.endswith("Error") and name.startswith("API"):
        return LLMTransportError(f"OpenAI call failed: {message}")
    return exc


def _extract_text(response: object) -> str:
    """Pull the message content out of the first choice.

    A `length` finish reason is raised rather than returned: a truncated JSON object
    would fail `parse_opinion()` a moment later with a confusing message, and the real
    cause — `max_tokens` too low — would not appear anywhere in it.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise LLMResponseError("OpenAI returned no choices.")

    choice = choices[0]
    finish = getattr(choice, "finish_reason", None)
    if finish == "length":
        raise LLMResponseError(
            f"OpenAI truncated the response at max_tokens={DEFAULT_MAX_TOKENS}. The JSON "
            f"is incomplete; raise max_tokens rather than trying to parse it."
        )
    if finish == "content_filter":
        raise LLMResponseError(
            "OpenAI filtered the response rather than answering it (finish_reason="
            "'content_filter')."
        )

    text = (getattr(getattr(choice, "message", None), "content", None) or "").strip()
    if not text:
        raise LLMResponseError(f"OpenAI returned an empty message (finish_reason={finish!r}).")
    return text
