"""The arc's online mode — submitting decisions and rulings to a real backend.

Submission is deliberately a side effect: the arc still evaluates from its own
in-memory records, so the demo story is identical online and offline. These
tests pin that property, plus the failure handling that makes a dropped
decision visible instead of silent.

A stub client stands in for the backend here; the live path is exercised by
running `simulator arc --submit` against a real server.
"""

from __future__ import annotations

from shared.enums import Action

from simulator.arc import ArcRunner, default_script


class StubClient:
    """Records what the arc sends, and can be told to fail on demand."""

    def __init__(self, fail_decisions_at=(), fail_rulings_at=()):
        self.decisions: list[dict] = []
        self.rulings: list[dict] = []
        self._fail_decisions_at = set(fail_decisions_at)
        self._fail_rulings_at = set(fail_rulings_at)
        # Indexed by call, not by success: a failed call still consumes an
        # index, otherwise one configured failure would repeat forever.
        self._decision_calls = 0
        self._ruling_calls = 0

    def submit_decision(self, invoice, outcome, agent_id, reason, recommended_action=None):
        n = self._decision_calls
        self._decision_calls += 1
        if n in self._fail_decisions_at:
            raise RuntimeError(f"API error 500 on decision {n}")
        self.decisions.append(
            {
                "invoice_id": invoice.invoice_id,
                "action": outcome.action,
                "agent_id": agent_id,
                "reason": reason,
                "recommended_action": recommended_action,
            }
        )
        return {"decision_id": f"dec-stub-{n:05d}"}

    def submit_ruling(self, decision_id, ruling, reason):
        n = self._ruling_calls
        self._ruling_calls += 1
        if n in self._fail_rulings_at:
            raise RuntimeError(f"API error 404 on ruling {n}")
        self.rulings.append({"decision_id": decision_id, "ruling": ruling, "reason": reason})
        return {"decision_id": decision_id, "human_ruling": ruling.value}


def _run(client=None, count=40):
    runner = ArcRunner(seed=42, count=count, auto_approve=True, api_client=client)
    for beat in default_script(count):
        runner.run_phase(beat.phase, beat.count)
    return runner


# ---------------------------------------------------------------------------
# Offline stays offline
# ---------------------------------------------------------------------------


def test_no_client_means_no_submission_attempted():
    runner = _run(client=None)
    assert runner.submitted == 0
    assert runner.ruled == 0
    assert runner.submit_failures == []
    assert runner.records, "the arc still produces decisions offline"


def test_the_story_is_identical_online_and_offline():
    """Submission is a side effect. If attaching a backend changed the arc's
    own evaluation, the demo would no longer be reproducible offline."""
    offline = _run(client=None)
    online = _run(client=StubClient())

    def shape(r):
        return [
            (d.sequence, d.action, d.ground_truth, d.recommended_action, d.human_ruling)
            for d in r.records
        ]

    assert shape(offline) == shape(online)


# ---------------------------------------------------------------------------
# What gets sent
# ---------------------------------------------------------------------------


def test_every_decision_is_submitted():
    client = StubClient()
    runner = _run(client=client)
    assert runner.submitted == len(runner.records)
    assert len(client.decisions) == len(runner.records)


def test_escalations_carry_the_recommendation_and_acted_decisions_do_not():
    """`recommended_action` is half the human-agreement evidence, and it is
    only meaningful on an escalation — the backend 422s it as ESCALATE."""
    client = StubClient()
    _run(client=client)
    for sent in client.decisions:
        if sent["action"] is Action.ESCALATE:
            assert sent["recommended_action"] in (Action.APPROVE, Action.REJECT)
        else:
            assert sent["recommended_action"] is None


def test_every_escalation_gets_a_ruling():
    client = StubClient()
    runner = _run(client=client)
    escalations = [r for r in runner.records if r.human_ruling is not None]
    assert escalations, "the arc must produce escalations"
    assert runner.ruled == len(escalations)
    assert len(client.rulings) == len(escalations)


def test_rulings_are_never_escalate():
    client = StubClient()
    _run(client=client)
    for sent in client.rulings:
        assert sent["ruling"] in (Action.APPROVE, Action.REJECT)


def test_the_reason_identifies_the_run():
    """Every state-changing endpoint requires a reason; it should say which run
    and which decision produced it, or the audit log cannot be traced back."""
    client = StubClient()
    runner = _run(client=client)
    for sent in client.decisions:
        assert runner.run_id in sent["reason"]
        assert f"seed={runner.seed}" in sent["reason"]


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_a_failed_decision_is_recorded_not_raised():
    """One rejected decision must not abort a 1,500-decision arc — but it must
    not vanish either, because a run that drops decisions is not reproducible."""
    client = StubClient(fail_decisions_at=(5, 17))
    runner = _run(client=client)
    assert len(runner.submit_failures) == 2
    assert runner.submitted == len(runner.records) - 2
    assert all("API error 500" in f for f in runner.submit_failures)


def test_a_failed_ruling_is_recorded_not_raised():
    client = StubClient(fail_rulings_at=(3,))
    runner = _run(client=client)
    assert len(runner.submit_failures) == 1
    assert "ruling" in runner.submit_failures[0]


def test_a_failed_decision_skips_its_ruling():
    """A ruling needs the backend's decision id. If the decision never landed
    there is nothing to rule on, and attempting it would only produce a second,
    misleading failure."""
    client = StubClient(fail_decisions_at=(0,))
    runner = _run(client=client)
    assert len(runner.submit_failures) == 1, "one failure, not two"


def test_submission_failures_do_not_change_the_arc_verdict():
    """The arc evaluates its own records, so a backend outage degrades
    reporting, never the demo's conclusions."""
    clean = _run(client=StubClient())
    broken = _run(client=StubClient(fail_decisions_at=range(200)))
    assert [d.action for d in clean.records] == [d.action for d in broken.records]
    assert broken.submit_failures, "the failures are still reported"
