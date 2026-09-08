"""Pacing is per model, because the free tier's limits are.

The AI Studio dashboard for this project on 2 Sept 2026 reports 5 RPM for the Flash
models and 15 RPM for the Flash-Lite ones — a threefold spread inside one free tier.
A single shared floor cannot be right for both: the 6s default permitted 10 requests a
minute against a 5 RPM limit, and only survived because gemini-3.6-flash is slow enough
(9.9-33.3s per call) that no run ever reached its own floor.

None of this can be tested against the live API — CI never touches the network — so
these tests pin the arithmetic and the fallback, and the numbers in `MODEL_RPM` carry
the dashboard reading and its date in a comment.
"""

from __future__ import annotations

from governance.llm.base import DEFAULT_MIN_INTERVAL_S
from governance.llm.gemini import (
    DEFAULT_MODEL,
    MODEL_RPM,
    GeminiClient,
    GeminiConfig,
    min_interval_for,
)


def test_a_five_rpm_model_is_paced_slower_than_one_call_every_twelve_seconds():
    """5 RPM is one call per 12s. The old shared 6s default was twice too fast."""
    interval = min_interval_for("gemini-3.6-flash")

    assert interval >= 12.0
    assert interval > DEFAULT_MIN_INTERVAL_S


def test_a_fifteen_rpm_model_is_allowed_to_go_faster():
    """Flash-Lite allows 15 RPM. Pacing it at the Flash rate would waste an hour on a
    500-call day for no reason."""
    lite = min_interval_for("gemini-3.5-flash-lite")
    flash = min_interval_for("gemini-3.6-flash")

    assert lite < flash
    assert lite >= 4.0  # 60/15, before headroom


def test_every_listed_model_stays_inside_its_own_limit():
    """The arithmetic, for every row in the table at once: pacing at `interval` must
    not permit more than `rpm` calls in a minute."""
    for model, rpm in MODEL_RPM.items():
        interval = min_interval_for(model)
        calls_per_minute = 60.0 / interval
        assert calls_per_minute <= rpm, (
            f"{model}: pacing at {interval:.1f}s permits {calls_per_minute:.1f} "
            f"calls/min against a limit of {rpm}"
        )


def test_an_unknown_model_falls_back_rather_than_guessing():
    """A model not in the table is more likely to be preview-tier and *more* restricted,
    so the fallback must not be faster than the shared default."""
    assert min_interval_for("gemini-99-nonexistent") == DEFAULT_MIN_INTERVAL_S
    assert min_interval_for("gemini-99-nonexistent", fallback=30.0) == 30.0


def test_an_explicit_interval_still_wins():
    """Tests pace at zero, and a key on a paid tier has limits this table doesn't know."""
    assert GeminiConfig(min_interval_s=0.0).pacing_interval_s == 0.0
    assert GeminiConfig(min_interval_s=99.0).pacing_interval_s == 99.0


def test_the_interval_follows_the_model_when_none_is_given():
    flash = GeminiConfig(model="gemini-3.6-flash")
    lite = GeminiConfig(model="gemini-3.5-flash-lite")

    assert flash.pacing_interval_s == min_interval_for("gemini-3.6-flash")
    assert lite.pacing_interval_s == min_interval_for("gemini-3.5-flash-lite")
    assert lite.pacing_interval_s < flash.pacing_interval_s


def test_the_client_paces_with_the_resolved_interval():
    """The client must read the resolved value, not the raw field — which is None by
    default and would have made `Pacer` compare against nothing."""
    client = GeminiClient(config=GeminiConfig(model="gemini-3.5-flash-lite"))

    assert client._pacer._min_interval == min_interval_for("gemini-3.5-flash-lite")


def test_the_default_model_is_one_we_know_the_limit_for():
    """A default whose limit isn't in the table would pace on the fallback and quietly
    lose the protection this module exists for."""
    assert DEFAULT_MODEL in MODEL_RPM
