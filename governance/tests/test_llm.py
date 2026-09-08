"""Tests for the Gemini client, the recording store, and cached mode.

No test here touches the network. The client is exercised through an injected
`httpx.Client` backed by `httpx.MockTransport`, which means the request-building and
error-classification logic is tested against real httpx machinery rather than against a
hand-rolled fake that agrees with whatever the code does.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import make_evaluation
from shared.contracts import AgentOpinion
from shared.enums import OpinionVerdict

from governance.agents.base import AGENT_NAMES
from governance.agents.llm_backed import opine_via_model, supports_mode
from governance.llm.base import Pacer
from governance.llm.errors import (
    GovernanceLLMError,
    LLMAuthError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTransportError,
    RecordingMissError,
)
from governance.llm.gemini import GeminiClient, GeminiConfig
from governance.llm.recording import (
    Recording,
    RecordingStore,
    build_recording,
    cache_key_for,
    prompt_fingerprint,
)
from governance.modes import CACHED, LIVE, STUB
from governance.prompts.loader import build_prompt
from governance.prompts.schema import OpinionParseError

VALID_RESPONSE = json.dumps(
    {
        "reasoning": "196 of 200 decisions correct, Wilson lower bound 95.0%.",
        "concerns": [],
        "verdict": "CONCUR",
        "confidence": 0.82,
    }
)



class _FakeClock:
    """A clock that only moves when something asks it to.

    `sleep()` advances it by exactly the requested amount, which is the one property a
    real `time.sleep` will not promise — and the reason the pacer's timing test used to
    flake.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        """Time passing for a reason other than sleeping — a slow API call, say."""
        self.now += seconds


def _config(**overrides) -> GeminiConfig:
    """A config that never paces, so tests do not sleep."""
    base = {"api_key": "test-key", "min_interval_s": 0.0}
    base.update(overrides)
    return GeminiConfig(**base)


def _client_returning(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok_envelope(text: str = VALID_RESPONSE) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


# --------------------------------------------------------------- request shape


def test_the_request_carries_the_schema_and_asks_for_json():
    prompt = build_prompt("risk", make_evaluation())
    payload = GeminiClient(config=_config()).build_payload(prompt)

    generation = payload["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseSchema"]["type"] == "object"
    assert "verdict" in generation["responseSchema"]["properties"]


def test_the_request_sends_the_gemini_dialect_not_pydantics_json_schema():
    """The whole point of gemini_response_schema(). A $ref here fails at runtime."""
    prompt = build_prompt("risk", make_evaluation())
    payload = GeminiClient(config=_config()).build_payload(prompt)

    serialised = json.dumps(payload["generationConfig"]["responseSchema"])
    assert "$ref" not in serialised
    assert "$defs" not in serialised


def test_reasoning_is_ordered_before_verdict_in_the_request():
    prompt = build_prompt("performance", make_evaluation())
    payload = GeminiClient(config=_config()).build_payload(prompt)

    ordering = payload["generationConfig"]["responseSchema"]["propertyOrdering"]
    assert ordering.index("reasoning") < ordering.index("verdict")


def test_the_system_prompt_and_evidence_travel_in_separate_slots():
    prompt = build_prompt("audit", make_evaluation())
    payload = GeminiClient(config=_config()).build_payload(prompt)

    assert payload["systemInstruction"]["parts"][0]["text"] == prompt.system
    assert payload["contents"][0]["parts"][0]["text"] == prompt.user
    assert payload["contents"][0]["role"] == "user"


def test_the_api_key_travels_as_a_header_never_in_the_url():
    """A key in a query string ends up in proxy logs and error messages."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        return httpx.Response(200, json=_ok_envelope())

    prompt = build_prompt("risk", make_evaluation())
    GeminiClient(config=_config(api_key="secret-value")).generate(
        prompt, client=_client_returning(handler)
    )

    assert seen["key"] == "secret-value"
    assert "secret-value" not in seen["url"]
    assert "key=" not in seen["url"]


# --------------------------------------------------------------- happy path


def test_a_successful_call_returns_the_model_text():
    prompt = build_prompt("risk", make_evaluation())
    text = GeminiClient(config=_config()).generate(
        prompt,
        client=_client_returning(lambda _: httpx.Response(200, json=_ok_envelope())),
    )
    assert json.loads(text)["verdict"] == "CONCUR"


def test_multipart_responses_are_joined():
    envelope = {"candidates": [{"content": {"parts": [{"text": "{\"a\":"}, {"text": "1}"}]}}]}
    prompt = build_prompt("risk", make_evaluation())
    text = GeminiClient(config=_config()).generate(
        prompt, client=_client_returning(lambda _: httpx.Response(200, json=envelope))
    )
    assert text == '{"a":1}'


# --------------------------------------------------------------- error mapping


def test_a_missing_key_fails_before_any_request_is_made():
    """Config with no key must not reach the network at all."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be sent without a key")

    prompt = build_prompt("risk", make_evaluation())
    with pytest.raises(LLMAuthError, match="GEMINI_API_KEY is empty"):
        GeminiClient(config=_config(api_key="")).generate(
            prompt, client=_client_returning(handler)
        )


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [
        (429, LLMRateLimitError, True),
        (401, LLMAuthError, False),
        (403, LLMAuthError, False),
        (500, LLMTransportError, True),
        (503, LLMTransportError, True),
        (400, LLMResponseError, False),
    ],
)
def test_http_status_maps_to_the_right_error_and_retryability(
    status, expected, retryable
):
    """Live mode's fallback rule reads `retryable`, so the flag is the contract."""
    body = {"error": {"message": "upstream said so"}}
    prompt = build_prompt("risk", make_evaluation())

    with pytest.raises(expected) as caught:
        GeminiClient(config=_config()).generate(
            prompt,
            client=_client_returning(lambda _: httpx.Response(status, json=body)),
        )

    assert caught.value.retryable is retryable
    assert "upstream said so" in str(caught.value)


def test_a_rate_limit_carries_retry_after_when_the_server_sends_one():
    prompt = build_prompt("risk", make_evaluation())
    with pytest.raises(LLMRateLimitError) as caught:
        GeminiClient(config=_config()).generate(
            prompt,
            client=_client_returning(
                lambda _: httpx.Response(429, json={}, headers={"retry-after": "21"})
            ),
        )
    assert caught.value.retry_after == 21.0


def test_a_timeout_is_a_retryable_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    prompt = build_prompt("risk", make_evaluation())
    with pytest.raises(LLMTransportError) as caught:
        GeminiClient(config=_config()).generate(prompt, client=_client_returning(handler))
    assert caught.value.retryable is True


def test_a_safety_block_says_it_was_blocked_rather_than_empty():
    """A 200 with no candidates would otherwise surface much later as a parse failure."""
    blocked = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
    prompt = build_prompt("risk", make_evaluation())

    with pytest.raises(LLMResponseError, match="SAFETY"):
        GeminiClient(config=_config()).generate(
            prompt, client=_client_returning(lambda _: httpx.Response(200, json=blocked))
        )


def test_every_client_error_is_a_governance_llm_error():
    """One base class, so a caller can catch the package rather than six types."""
    prompt = build_prompt("risk", make_evaluation())
    with pytest.raises(GovernanceLLMError):
        GeminiClient(config=_config()).generate(
            prompt, client=_client_returning(lambda _: httpx.Response(500, json={}))
        )


# --------------------------------------------------------------- config


def test_config_from_env_tolerates_a_missing_key(monkeypatch):
    """Stub and cached modes must work with GEMINI_API_KEY unset."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = GeminiConfig.from_env()
    assert config.api_key == ""
    assert config.has_key is False


def test_config_reads_the_key_and_model_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "  from-env  ")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    config = GeminiConfig.from_env()
    assert config.api_key == "from-env"
    assert config.model == "gemini-2.5-pro"
    assert config.endpoint.endswith("/models/gemini-2.5-pro:generateContent")


def test_the_pacer_spaces_calls_by_the_configured_interval():
    """A burst is what the free tier punishes; the floor is on the gap, not a bucket.

    Asserts on the gaps the pacer *asks for*, against a clock that does not move on its
    own, rather than on real elapsed time. The old version measured `time.monotonic()`
    across three calls and asserted `>= 0.10` against a theoretical minimum of exactly
    0.10 — no margin at all, so a `time.sleep` that returned a hair early failed a pacer
    that had done nothing wrong. This version cannot flake and checks more: that the
    first call is not delayed, and that each later one is delayed by the full interval.
    """
    clock = _FakeClock()
    pacer = Pacer(0.05, monotonic=clock.monotonic, sleep=clock.sleep)

    delays = [pacer.wait() for _ in range(3)]

    assert delays[0] == 0.0, "the first call has nothing to wait behind"
    assert delays[1:] == [0.05, 0.05]
    assert clock.slept == [0.05, 0.05], "it must actually sleep, not just report a delay"
    assert clock.now == pytest.approx(0.10)


def test_the_pacer_does_not_delay_a_call_that_is_already_late_enough():
    """Time spent on the request itself counts toward the gap. Sleeping the full interval
    on top of a call that already took longer than it would double the real spacing and
    halve a recording run's throughput for nothing."""
    clock = _FakeClock()
    pacer = Pacer(0.05, monotonic=clock.monotonic, sleep=clock.sleep)

    pacer.wait()
    clock.advance(0.2)  # a slow API call, longer than the interval

    assert pacer.wait() == 0.0
    assert clock.slept == []


# --------------------------------------------------------------- recordings


SLUG = "gemini-2-5-flash"


def _recording_for(prompt, text: str = VALID_RESPONSE, slug: str = SLUG) -> Recording:
    return build_recording(
        prompt, text, "gemini-2.5-flash", provider="gemini", model_slug=slug
    )


def test_a_recording_round_trips_through_disk(tmp_path: Path):
    store = RecordingStore(directory=tmp_path)
    prompt = build_prompt("risk", make_evaluation())

    store.save(_recording_for(prompt))
    loaded = store.load(cache_key_for(prompt, SLUG))

    assert loaded.response_text == VALID_RESPONSE
    assert loaded.agent_name == "risk"
    assert loaded.evidence_hash == prompt.evidence_hash
    assert loaded.prompt_sha == prompt_fingerprint(prompt)


def test_a_missing_recording_raises_rather_than_falling_back(tmp_path: Path):
    """The quiet alternatives both produce a demo that looks healthy and is not."""
    store = RecordingStore(directory=tmp_path)
    prompt = build_prompt("risk", make_evaluation())

    with pytest.raises(RecordingMissError) as caught:
        store.load(cache_key_for(prompt, SLUG))
    assert caught.value.cache_key == cache_key_for(prompt, SLUG)
    assert "governance.record" in str(caught.value)


def test_a_miss_names_the_sibling_recordings_for_that_agent(tmp_path: Path):
    store = RecordingStore(directory=tmp_path)
    recorded = build_prompt("risk", make_evaluation(total_decisions=200))
    store.save(_recording_for(recorded))

    other = build_prompt("risk", make_evaluation(total_decisions=10))
    with pytest.raises(RecordingMissError) as caught:
        store.load(cache_key_for(other, SLUG))
    assert cache_key_for(recorded, SLUG) in str(caught.value)


def test_a_cache_key_that_could_escape_the_directory_is_rejected(tmp_path: Path):
    store = RecordingStore(directory=tmp_path)
    for hostile in ("../secrets", "risk/../../etc", "risk\\v1"):
        with pytest.raises(ValueError, match="unsafe cache key"):
            store.path_for(hostile)


def test_a_truncated_recording_file_names_its_missing_fields(tmp_path: Path):
    store = RecordingStore(directory=tmp_path)
    (tmp_path / "risk.v1.gemini-2-5-flash.abc123.json").write_text(
        json.dumps({"cache_key": "risk.v1.gemini-2-5-flash.abc123"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="missing field"):
        store.load("risk.v1.gemini-2-5-flash.abc123")


def test_editing_a_prompt_changes_the_key_rather_than_replaying_a_stale_answer(
    tmp_path: Path
):
    """Different evidence must not resolve to the same recording."""
    store = RecordingStore(directory=tmp_path)
    first = build_prompt("risk", make_evaluation(total_decisions=200))
    second = build_prompt("risk", make_evaluation(total_decisions=10))

    assert cache_key_for(first, SLUG) != cache_key_for(second, SLUG)
    store.save(_recording_for(first))
    assert store.has(cache_key_for(first, SLUG))
    assert not store.has(cache_key_for(second, SLUG))


# --------------------------------------------------------------- cached mode


def test_cached_mode_returns_a_validated_opinion(tmp_path: Path):
    evaluation = make_evaluation()
    store = RecordingStore(directory=tmp_path)
    store.save(_recording_for(build_prompt("risk", evaluation)))

    opinion = opine_via_model("risk", evaluation, CACHED, store=store, model_slug=SLUG)

    assert isinstance(opinion, AgentOpinion)
    assert opinion.agent_name == "risk"
    assert opinion.verdict is OpinionVerdict.CONCUR
    assert opinion.confidence == 0.82


def test_a_recording_still_has_to_pass_validation(tmp_path: Path):
    """Replaying without validating lets an old schema produce a malformed opinion."""
    evaluation = make_evaluation()
    store = RecordingStore(directory=tmp_path)
    prompt = build_prompt("risk", evaluation)
    store.save(_recording_for(prompt, text=json.dumps({"verdict": "CONCUR"})))

    with pytest.raises(OpinionParseError):
        opine_via_model("risk", evaluation, CACHED, store=store, model_slug=SLUG)


def test_a_recording_cannot_grant_authority_the_schema_does_not_have(
    tmp_path: Path
):
    """extra='forbid' is what stops a recorded response asking for a limit."""
    evaluation = make_evaluation()
    store = RecordingStore(directory=tmp_path)
    prompt = build_prompt("risk", evaluation)
    hostile = json.dumps(
        {
            "reasoning": "Raise the ceiling.",
            "concerns": [],
            "verdict": "CONCUR",
            "confidence": 1.0,
            "proposed_limit": 50000,
        }
    )
    store.save(_recording_for(prompt, text=hostile))

    with pytest.raises(OpinionParseError):
        opine_via_model("risk", evaluation, CACHED, store=store, model_slug=SLUG)


@pytest.mark.parametrize("mode", [STUB, "nonsense"])
def test_opine_via_model_refuses_modes_it_does_not_serve(mode):
    """Stub reasoning lives in the agent modules; an unknown mode is a typo.

    LIVE is no longer in this list — it is served here as of the 3 Sept deliverable.
    """
    with pytest.raises(ValueError, match="handles 'cached' and 'live' only"):
        opine_via_model("risk", make_evaluation(), mode)


def test_all_three_modes_are_supported():
    assert supports_mode(STUB) and supports_mode(CACHED) and supports_mode(LIVE)


def test_every_agent_can_be_served_from_a_recording(tmp_path: Path):
    """All four, not just the one the happy-path test happens to use."""
    evaluation = make_evaluation()
    store = RecordingStore(directory=tmp_path)
    for name in AGENT_NAMES:
        store.save(_recording_for(build_prompt(name, evaluation)))

    for name in AGENT_NAMES:
        opinion = opine_via_model(name, evaluation, CACHED, store=store, model_slug=SLUG)
        assert opinion.agent_name == name
