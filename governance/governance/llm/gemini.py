"""The Gemini HTTP client.

Raw HTTP against the REST endpoint rather than the `google-genai` SDK. Three reasons,
in order of how much they matter here:

1. **The request is the design.** `response_schema` plus `response_mime_type` is the
   constrained-decoding claim this lane makes; having it visible in a dict a reader can
   point at is worth more in a viva than a method call that hides it.
2. **One fewer dependency.** `httpx` is already present via langgraph.
3. **The SDK's retry and safety defaults are its own.** This lane needs to decide what
   a rate limit means, because the answer feeds live mode's fallback rule.

**The key is sent as a header, never in the URL.** Query strings end up in proxy logs,
shell history, and error messages; `x-goog-api-key` does not.

Nothing here validates a response. `generate()` returns the model's text and stops, so
this module can be tested without Pydantic and the parser without a network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

from governance.llm.base import (
    DEFAULT_MIN_INTERVAL_S,
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
)
from governance.prompts.loader import Prompt
from governance.prompts.schema import gemini_response_schema

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# Flash: the cheapest model that follows a response schema reliably. Free tier only
# (docs/lanes/vc.md forbids a paid API), and free-tier quotas were cut sharply in Dec
# 2025 — verify against your own key rather than trusting a published number.
#
# 3.6, not 2.5: as of 30 Aug 2026 the API answers a 2.5-flash request with a 404 reading
# "no longer available to new users". A retired default is a 404 on every call, so this
# is checked by a live probe, not by a test — nothing in CI touches the network. If this
# 404s again, run one call by hand before a recording run and read the model the error
# names; the retirement notice is the only place that number is published.
DEFAULT_MODEL = "gemini-3.6-flash"

PROVIDER = "gemini"

# Requests per minute per model, read off the AI Studio rate-limit dashboard for this
# project on 2 Sept 2026 (aistudio.google.com/rate-limit — Google no longer publishes
# free-tier limits in the docs, so the dashboard is the only source, and it is per
# *project*, not per key).
#
# These differ by a factor of three between models on the same free tier, which is why
# pacing cannot be one shared constant. The Flash models allow 5 RPM; a 6s floor permits
# 10, so the previous shared default was silently twice the real limit and only survived
# because gemini-3.6-flash is slow enough (9.9-33.3s per call) that no run ever reached
# its own floor. A faster model would have turned that into 429s.
#
# RPD is the limit that actually binds a recording run and is not encoded here — the
# pacer cannot smooth a daily cap. It is recorded alongside for whoever plans the next
# run: Flash models 20/day, Flash-Lite models 500/day.
MODEL_RPM: dict[str, int] = {
    "gemini-3.7-flash": 5,  # 20 RPD
    "gemini-3.6-flash": 5,  # 20 RPD
    "gemini-3.5-flash": 5,  # 20 RPD
    "gemini-3-flash": 5,  # 20 RPD
    "gemini-2.5-flash": 5,  # 20 RPD — listed, but 404s for keys created recently
    "gemini-3.5-flash-lite": 15,  # 500 RPD
    "gemini-3.1-flash-lite": 15,  # 500 RPD
    "gemini-2.5-flash-lite": 10,  # 20 RPD
}

# How much of the nominal gap to add as headroom. The dashboard reports peak usage as a
# whole number against the limit, so a run pacing exactly at the limit has no margin for
# clock skew or a retry landing inside the same minute. 20% costs seconds on a 24-call
# run and is the difference between 4/5 and 5/5.
_PACING_HEADROOM = 1.2


def min_interval_for(model: str, *, fallback: float = DEFAULT_MIN_INTERVAL_S) -> float:
    """Seconds to leave between calls so `model` stays inside its requests-per-minute
    limit, with headroom.

    An unknown model falls back to the shared default rather than guessing generously:
    a new model is more likely to be preview-tier and *more* restricted, not less
    (ai.google.dev/gemini-api/docs/rate-limits). Being too slow costs a recording run
    some seconds; being too fast costs it the day's quota.
    """
    rpm = MODEL_RPM.get(model)
    if rpm is None or rpm <= 0:
        return fallback
    return (60.0 / rpm) * _PACING_HEADROOM


@dataclass(frozen=True, slots=True)
class GeminiConfig:
    """Everything the client needs, resolved once so nothing reads the environment later.

    `api_key` is read from `GEMINI_API_KEY` and may be empty. An empty key is not an
    error at construction time — stub and cached modes must work end to end with the
    variable unset (docs/lanes/vc.md), so the failure belongs at the moment a live call
    is actually attempted, not at import.
    """

    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    timeout_s: float = DEFAULT_TIMEOUT_S
    # None means "derive it from the model" (see `pacing_interval_s`). An explicit value
    # still wins, so tests can pace at zero and a future key on a paid tier can be told
    # its real limit without editing the table.
    min_interval_s: float | None = None

    @classmethod
    def from_env(cls, **overrides: object) -> GeminiConfig:
        """Build from the environment. Never raises on a missing key."""
        base: dict[str, object] = {
            "api_key": os.environ.get("GEMINI_API_KEY", "").strip(),
            "model": os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        }
        base.update(overrides)
        return cls(**base)  # type: ignore[arg-type]

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    @property
    def pacing_interval_s(self) -> float:
        """The gap this client actually paces with — explicit if given, else the one
        `self.model`'s requests-per-minute limit implies."""
        if self.min_interval_s is not None:
            return self.min_interval_s
        return min_interval_for(self.model)

    @property
    def endpoint(self) -> str:
        return f"{API_ROOT}/models/{self.model}:generateContent"


@dataclass
class GeminiClient:
    """One client, one model, one pacer.

    Holds no conversation state: each `generate()` is independent, because a governance
    agent's opinion must depend only on the evidence in front of it. Two evaluations in
    the same process must not be able to influence each other.
    """

    config: GeminiConfig = field(default_factory=GeminiConfig.from_env)
    provider: str = PROVIDER
    _pacer: Pacer = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._pacer = Pacer(self.config.pacing_interval_s)

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def has_key(self) -> bool:
        return self.config.has_key

    @property
    def slug(self) -> str:
        return model_slug(self.provider, self.config.model)

    def build_payload(self, prompt: Prompt) -> dict:
        """The exact JSON body sent to the API.

        Separate from `generate()` so a test can assert on the request shape without a
        network, and so the recording script can log precisely what produced a response.

        `responseSchema` is Gemini's OpenAPI-3.0 subset, not the Pydantic JSON Schema —
        see `gemini_response_schema()` for why the two differ. Sending it alongside
        `responseMimeType` is what makes malformed JSON structurally impossible rather
        than merely discouraged, which is why `parse_opinion()` still runs afterwards:
        constrained decoding guarantees shape, never sense.
        """
        return {
            "systemInstruction": {"parts": [{"text": prompt.system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt.user}]}],
            "generationConfig": {
                "temperature": self.config.temperature,
                "responseMimeType": "application/json",
                "responseSchema": gemini_response_schema(),
            },
        }

    def build_payload_text(self, prompt: Prompt) -> dict:
        """Same request as `build_payload`, minus the structured-output constraint.

        For a caller that wants prose, not an `AgentOpinion` — see `generate_text`.
        """
        return {
            "systemInstruction": {"parts": [{"text": prompt.system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt.user}]}],
            "generationConfig": {"temperature": self.config.temperature},
        }

    def generate(
        self,
        prompt: Prompt,
        *,
        client: httpx.Client | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """Send one prompt, return the model's raw text, constrained to the
        `AgentOpinion` schema (see `build_payload`).

        Paces itself first, so callers cannot accidentally burst. Raises a
        `GovernanceLLMError` subclass on every failure path; the caller decides what to
        do with `retryable`.

        `client` is injectable so tests can pass a transport rather than monkey-patching
        a module global.
        """
        return self._send(self.build_payload(prompt), client=client, timeout_s=timeout_s)

    def generate_text(
        self,
        prompt: Prompt,
        *,
        client: httpx.Client | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """Same call as `generate`, but the response is unconstrained prose —
        for a caller that isn't asking for an `AgentOpinion` (see `base.LLMClient`).
        """
        return self._send(self.build_payload_text(prompt), client=client, timeout_s=timeout_s)

    def _send(
        self,
        payload: dict,
        *,
        client: httpx.Client | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """The HTTP call both `generate` and `generate_text` make — pacing, the
        request itself, and error translation, shared so the two differ only in
        the payload they send.
        """
        if not self.config.has_key:
            raise LLMAuthError(
                "GEMINI_API_KEY is empty. Live calls need a key from Google AI Studio; "
                "put it in .env (which is gitignored) and never in a committed file. "
                "Stub and cached modes do not need one."
            )

        self._pacer.wait()
        headers = {
            "x-goog-api-key": self.config.api_key,
            "content-type": "application/json",
        }

        # Per call, not per client. Live mode needs a far shorter deadline than a
        # recording run, and giving it a client of its own would give it a `Pacer` of its
        # own — two pacers on one provider send at twice the rate the key allows.
        deadline = self.config.timeout_s if timeout_s is None else timeout_s

        owns_client = client is None
        http = client or httpx.Client(timeout=deadline)
        try:
            response = http.post(self.config.endpoint, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMTransportError(f"Gemini call timed out after {deadline}s: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMTransportError(f"Gemini call failed to complete: {exc}") from exc
        finally:
            if owns_client:
                http.close()

        _raise_for_status(response)
        return _extract_text(response)


def _raise_for_status(response: httpx.Response) -> None:
    """Turn an HTTP status into the exception whose `retryable` flag is correct."""
    status = response.status_code
    if status < 400:
        return

    detail = _error_message(response)

    if status == 429:
        raise LLMRateLimitError(
            f"Gemini rate limit hit (429): {detail}. The free tier allows roughly "
            f"{int(60 / DEFAULT_MIN_INTERVAL_S)} requests per minute.",
            retry_after=_retry_after(response),
        )
    if status in (401, 403):
        raise LLMAuthError(
            f"Gemini rejected the API key ({status}): {detail}. Check GEMINI_API_KEY in "
            f".env and that the key is entitled to {response.url.path.rsplit('/', 1)[-1]}."
        )
    if status >= 500:
        raise LLMTransportError(f"Gemini server error ({status}): {detail}")
    raise LLMResponseError(f"Gemini rejected the request ({status}): {detail}")


def _error_message(response: httpx.Response) -> str:
    """Pull the API's own message out, falling back to the body it actually sent."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300].strip() or "<empty body>"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return str(payload)[:300]


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _extract_text(response: httpx.Response) -> str:
    """Dig the text out of the candidates envelope.

    Every failure here is a `LLMResponseError` carrying the reason the API gave. A
    safety block returns HTTP 200 with no candidate text, which would otherwise surface
    much later as an empty-response parse failure with nothing explaining why.
    """
    try:
        payload = response.json()
    except ValueError as exc:
        raise LLMResponseError(f"Gemini returned a non-JSON body: {exc}") from exc

    candidates = payload.get("candidates") or []
    if not candidates:
        blocked = (payload.get("promptFeedback") or {}).get("blockReason")
        raise LLMResponseError(
            f"Gemini returned no candidates (blockReason={blocked!r}). The prompt was "
            f"filtered rather than answered."
        )

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise LLMResponseError(
            f"Gemini returned an empty candidate "
            f"(finishReason={candidate.get('finishReason')!r})."
        )
    return text
