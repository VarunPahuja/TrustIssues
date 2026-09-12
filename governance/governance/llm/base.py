"""What every provider client has to be, and the pacing they all share.

**Why more than one provider.** The strongest objection to this lane is that four
agents on the same base model are not four independent reviewers — they share a base
model, so they share its biases, and their errors correlate. The structural answer
(governance is advisory, so correlated error degrades the argument rather than the
safety) is true but defensive. Running the panel across genuinely different base models
is the actual mitigation: a Gemini risk agent and a Claude performance agent do not fail
the same way.

**Nothing here is required to run the project.** Gemini has a free tier and stays the
default; `anthropic` and `openai` are optional extras, and the whole system still runs
end to end with every key blank (docs/lanes/vc.md forbids introducing a paid service).
See ADR-0012.

A client's whole job is: take a `Prompt`, return the model's raw text. It does not
validate, parse, or build an `AgentOpinion` — that stays behind `parse_opinion()`, so
adding a provider cannot accidentally add a second validation path.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from governance.prompts.loader import Prompt

# Free-tier Gemini is roughly ten requests per minute, and it is the tightest limit of
# the three, so it sets the shared default. Each provider may raise it.
DEFAULT_MIN_INTERVAL_S = 6.0

# Measured, not guessed: three identical gemini-3.6-flash calls on 30 Aug 2026 returned
# in 9.9s, 15.6s and 33.3s. Latency on this model is wide enough that a 30s timeout sat
# inside the spread and failed roughly half the time — a recording run would have died
# partway through with quota already spent. 120s is well past the observed tail.
#
# This is not patience for its own sake. A timeout here aborts one *recording*, which is
# cheap to retry; live mode's own deadline is what protects the demo, and it is a
# separate number set against the panel's tolerance, not against Gemini's tail latency.
DEFAULT_TIMEOUT_S = 120.0

# Live mode's deadline, and deliberately a different number from the one above.
#
# The recording timeout is set against Gemini's tail latency: a recording is worth
# waiting for because the alternative is spending another day's quota. This one is set
# against a panel's patience. Nobody watching a demo waits two minutes to find out the
# network is down, and live mode's whole promise is that a valid recommendation still
# comes out — quickly enough to be worth having.
#
# 25s sits just under the slowest call measured on 30 Aug (33.3s), which is intentional:
# a call in that tail will fall back to its recording rather than stall the panel, and
# the recording is a real response to the same evidence. Losing the tail is cheaper than
# losing the room.
LIVE_TIMEOUT_S = 25.0

# Governance opinions should be reproducible enough that a recording is representative.
# Not zero: at temperature 0 a reasoning task tends to produce the same terse argument
# every time, and the panel's value comes from four agents actually differing.
DEFAULT_TEMPERATURE = 0.2

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def model_slug(provider: str, model: str) -> str:
    """A filesystem-safe identity for one provider/model pair.

    Model ids carry dots (`gemini-2.5-flash`) and the recording cache key is
    dot-delimited, so dots have to go or the key stops being parseable by eye.

    The provider is prefixed only when the model id does not already carry it —
    `gemini-2.5-flash` becomes `gemini-2-5-flash`, not `gemini-gemini-2-5-flash`, while
    OpenAI's `gpt-4o` becomes `openai-gpt-4o` because nothing in the id says who made it.
    """
    provider = provider.lower()
    slug = _SLUG_STRIP.sub("-", model.lower()).strip("-")
    if slug.startswith(f"{provider}-") or slug == provider:
        return slug
    return f"{provider}-{slug}"


@runtime_checkable
class LLMClient(Protocol):
    """One provider, one model. Prompt in, raw text out."""

    provider: str
    model: str

    def generate(self, prompt: Prompt, *, timeout_s: float | None = None) -> str: ...

    # Added for backend/app/services/assistant_llm.py (the read-only chat assistant).
    # `generate()` forces the AgentOpinion JSON schema into every request on all three
    # providers — right for a panel agent, wrong for a free-text answer. `generate_text`
    # shares the same client, config, Pacer and error-translation as `generate()`; it
    # just skips the structured-output constraint. Additive only: no existing behaviour
    # changes, and the coordinator/agents in this lane still call `generate()` exclusively.
    def generate_text(self, prompt: Prompt, *, timeout_s: float | None = None) -> str: ...

    @property
    def has_key(self) -> bool: ...

    @property
    def slug(self) -> str: ...


class Pacer:
    """Spaces requests by at least `min_interval` seconds.

    Deliberately a floor on the gap between calls rather than a token bucket. A bucket
    permits a burst, and a burst is exactly what a free tier punishes: the first four
    agents would go out instantly and the fifth would be rate-limited. Thread-safe
    because a parallel recording run would otherwise pace nothing at all.

    The clock and the sleep are injectable so a test can assert on the gaps this class
    *asks for* without waiting for them. Asserting on real elapsed time is what a test of
    a sleeping object usually does, and it is flaky by construction: the pacer's own
    guarantee is a floor, so the honest assertion `elapsed >= n * interval` has zero
    margin above the theoretical minimum and fails whenever `time.sleep` returns a hair
    early. What is worth pinning is the decision — how long it waited and when it
    declined to — not the operating system's timekeeping.
    """

    def __init__(
        self,
        min_interval: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_call: float | None = None

    def wait(self) -> float:
        """Block until the next call is allowed. Returns the seconds actually slept."""
        with self._lock:
            now = self._monotonic()
            if self._last_call is None:
                self._last_call = now
                return 0.0
            delay = max(0.0, self._min_interval - (now - self._last_call))
            if delay > 0:
                self._sleep(delay)
            self._last_call = self._monotonic()
            return delay
