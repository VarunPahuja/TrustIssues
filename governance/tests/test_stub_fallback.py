"""The opt-in cached -> stub fallback.

Cached recordings key on a hash of the whole prompt, so one replays only for
the exact evaluation it was made from. Any agent the recordings were not built
against misses, and a miss is a hard error by design — a silent substitution
would hide that the panel was asked a question nothing had answered.

That default is right for correctness and wrong for a live demo that must not
stop, so the fallback exists behind a switch. These tests pin both sides: off
means raise, on means fall back *and say so*.
"""

from __future__ import annotations

import pytest
from shared.contracts import AgentContext, TrustEvaluation
from shared.enums import AgentState

from governance.coordinator import recommend
from governance.llm.errors import RecordingMissError
from governance.modes import STUB_FALLBACK_ENV


def _unrecorded_evaluation() -> TrustEvaluation:
    """An evaluation with numbers no recording was ever made from.

    Built through the real trust engine rather than hand-assembled, so the
    prompt — and therefore the recording key — is shaped exactly as a live one.
    """
    from trust_engine.evaluate import evaluate

    return evaluate(
        [],
        AgentContext(
            current_limit=500,
            decisions_since_last_change=0,
            decisions_since_clawback=None,
            state=AgentState.PROBATION,
        ),
    )


def test_a_miss_raises_when_the_fallback_is_off(monkeypatch):
    monkeypatch.delenv(STUB_FALLBACK_ENV, raising=False)
    with pytest.raises(RecordingMissError):
        recommend(_unrecorded_evaluation(), mode="cached", trust_evaluation_ref="t-1")


def test_a_miss_falls_back_when_the_fallback_is_on(monkeypatch):
    monkeypatch.setenv(STUB_FALLBACK_ENV, "1")
    rec = recommend(_unrecorded_evaluation(), mode="cached", trust_evaluation_ref="t-2")
    assert rec.opinions, "a recommendation still comes out"
    assert len(rec.opinions) == 4, "all four agents still speak"


def test_the_fallback_is_labelled_not_hidden(monkeypatch):
    """The whole justification for allowing this is that it stays visible."""
    monkeypatch.setenv(STUB_FALLBACK_ENV, "1")
    rec = recommend(_unrecorded_evaluation(), mode="cached", trust_evaluation_ref="t-3")
    assert rec.governance_mode == "cached+stub", rec.governance_mode
    assert "stub" in rec.rationale.lower(), "the rationale names the fallback too"


def test_the_direction_is_unaffected_by_which_text_wrote_it(monkeypatch):
    """The trust engine decides; the panel writes it up. Falling back changes
    the prose, never the verdict — that is why this is safe to allow at all."""
    evaluation = _unrecorded_evaluation()

    monkeypatch.setenv(STUB_FALLBACK_ENV, "1")
    fell_back = recommend(evaluation, mode="cached", trust_evaluation_ref="t-4")

    monkeypatch.delenv(STUB_FALLBACK_ENV, raising=False)
    plain_stub = recommend(evaluation, mode="stub", trust_evaluation_ref="t-5")

    assert fell_back.direction == plain_stub.direction
    assert fell_back.proposed_limit == plain_stub.proposed_limit


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_switch_values(monkeypatch, value):
    monkeypatch.setenv(STUB_FALLBACK_ENV, value)
    rec = recommend(_unrecorded_evaluation(), mode="cached", trust_evaluation_ref="t-6")
    assert rec.governance_mode == "cached+stub"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_anything_else_leaves_the_loud_default_in_place(monkeypatch, value):
    """A typo in the switch must not silently enable it."""
    monkeypatch.setenv(STUB_FALLBACK_ENV, value)
    with pytest.raises(RecordingMissError):
        recommend(_unrecorded_evaluation(), mode="cached", trust_evaluation_ref="t-7")


def test_stub_mode_itself_is_untouched(monkeypatch):
    """The switch is scoped to cached-mode misses. Plain stub mode never
    consults it and never reports a fallback."""
    monkeypatch.setenv(STUB_FALLBACK_ENV, "1")
    rec = recommend(_unrecorded_evaluation(), mode="stub", trust_evaluation_ref="t-8")
    assert rec.governance_mode == "stub"
