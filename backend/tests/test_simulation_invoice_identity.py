"""An invoice id must name exactly one invoice.

`_create_decision` never overwrites an existing invoice's `amount` or
`ground_truth_action` — an invoice is a fact recorded once. That rule is only
safe if an invoice id is a function of everything that decides the invoice's
content. It wasn't: amounts come from `randint(10, max(current_limit * 2,
1000))`, so `current_limit` decides the amount and, through the shared `rng`
stream, every ground truth drawn after it — while the id was built from
(agent, phase, seed, index) alone.

So a re-run at a new limit produced decisions whose recorded ground truth
belonged to a *different* invoice, and accuracy was scored against the wrong
answer key. Observed live on agent-01: after two clawbacks took it from INR
2,500 to the floor, all 20 invoices in its critical-error window still held
amounts up to INR 4,859 drawn at the old limit, and a phantom critical error
in there pinned drift to CRITICAL so no good run could clear it.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Decision, Invoice
from app.schemas.simulation import SimulationPhase
from app.services.simulation import generate_decision_plan

_LADDER = (500, 1000, 2500, 5000, 10000)


def _plan(limit: int, *, seed: int = 42, count: int = 40, phase=SimulationPhase.GOOD):
    return generate_decision_plan(
        agent_id="agent-01", phase=phase, seed=seed, count=count, current_limit=limit
    )


class TestIdDeterminesContent:
    def test_the_same_inputs_still_reproduce_exactly(self):
        """The determinism guarantee this lane rests on, unchanged."""
        assert [
            (d.invoice_id, d.amount, d.action, d.ground_truth) for d in _plan(2500)
        ] == [(d.invoice_id, d.amount, d.action, d.ground_truth) for d in _plan(2500)]

    def test_two_limits_never_share_an_invoice_id(self):
        """The actual fix. Every rung of the ladder against every other."""
        ids_by_limit = {limit: {d.invoice_id for d in _plan(limit)} for limit in _LADDER}
        for a in _LADDER:
            for b in _LADDER:
                if a >= b:
                    continue
                shared = ids_by_limit[a] & ids_by_limit[b]
                assert not shared, (
                    f"limits {a} and {b} share invoice ids {sorted(shared)[:3]} — "
                    "the same id would name two different invoices"
                )

    def test_an_id_never_names_two_different_invoices(self):
        """The invariant stated directly: across every limit, an id maps to one
        (amount, ground_truth) pair and only ever that one."""
        seen: dict[str, tuple[int, object]] = {}
        for limit in _LADDER:
            for d in _plan(limit):
                content = (d.amount, d.ground_truth)
                if d.invoice_id in seen:
                    assert seen[d.invoice_id] == content, (
                        f"{d.invoice_id} means {seen[d.invoice_id]} at one limit "
                        f"and {content} at another"
                    )
                seen[d.invoice_id] = content

    def test_the_limit_is_visible_in_the_id(self):
        """Not cosmetic — this is what makes the collision impossible, so it is
        worth failing loudly if someone reformats it away."""
        for limit in _LADDER:
            assert all(f"-L{limit}-" in d.invoice_id for d in _plan(limit))

    def test_a_changed_limit_really_does_change_the_amounts(self):
        """Guards the premise. If the limit stopped affecting content, putting
        it in the id would be pointless noise — and this test should then be
        deleted along with it, deliberately rather than by accident."""
        low = {d.invoice_id.rsplit("-", 1)[1]: d.amount for d in _plan(500)}
        high = {d.invoice_id.rsplit("-", 1)[1]: d.amount for d in _plan(10000)}
        assert any(low[i] != high[i] for i in low), "limit no longer affects amounts"


class TestAgainstTheDatabase:
    """The plan being right is not enough — the bug was in what got persisted."""

    def _run(self, client, headers, phase: str, count: int = 40, seed: int = 42):
        resp = client.post(
            "/api/v1/simulation/runs",
            headers=headers,
            json={
                "agent_id": "agent-01",
                "phase": phase,
                "invoice_count": count,
                "seed": seed,
                "reason": f"invoice identity {phase}",
            },
        )
        assert resp.status_code == 201, resp.text
        run_id = resp.json()["run_id"]
        import time

        for _ in range(120):
            row = client.get(
                f"/api/v1/simulation/runs/{run_id}", headers=headers
            ).json()
            if row["status"] != "running":
                return row
            time.sleep(0.05)
        raise AssertionError("simulation run did not finish")

    def test_stored_ground_truth_matches_the_plan_that_produced_it(
        self, client, admin_headers, db_engine
    ):
        """The failure this file exists for. Two degraded runs move the limit,
        then a good run: every decision the good run recorded must be scored
        against its own invoice, not one a previous limit wrote."""
        self._run(client, admin_headers, "degraded")
        self._run(client, admin_headers, "degraded")
        self._run(client, admin_headers, "good", count=40, seed=42)

        with Session(db_engine) as session:
            rows = session.execute(
                select(Decision, Invoice)
                .join(Invoice, Decision.invoice_id == Invoice.id)
                .where(Decision.agent_id == "agent-01")
                .order_by(Decision.sequence.desc())
                .limit(40)
            ).all()

        assert rows, "the good run recorded nothing"
        for decision, invoice in rows:
            limit_tag = invoice.id.rsplit("-", 2)[1]
            assert limit_tag.startswith("L"), invoice.id
            limit = int(limit_tag[1:])
            expected = {
                d.invoice_id: d
                for d in generate_decision_plan(
                    agent_id="agent-01",
                    phase=SimulationPhase.GOOD
                    if "-good-" in invoice.id
                    else SimulationPhase.DEGRADED,
                    seed=42,
                    count=40,
                    current_limit=limit,
                )
            }
            planned = expected.get(invoice.id)
            if planned is None:
                continue
            assert invoice.amount == planned.amount, (
                f"{invoice.id}: stored amount {invoice.amount} != planned "
                f"{planned.amount} — a stale invoice row was reused"
            )
            assert invoice.ground_truth_action == planned.ground_truth, (
                f"{invoice.id}: stored ground truth {invoice.ground_truth_action} "
                f"!= planned {planned.ground_truth} — accuracy would be scored "
                "against the wrong answer key"
            )

    def test_no_stored_invoice_exceeds_the_limit_it_was_drawn_at(
        self, client, admin_headers, db_engine
    ):
        """A cheap check on the same corruption from the other side. Amounts are
        drawn from `max(limit * 2, 1000)`, so a row holding more than that for
        the limit named in its own id came from somewhere else."""
        self._run(client, admin_headers, "degraded")
        self._run(client, admin_headers, "good", count=40)

        with Session(db_engine) as session:
            invoices = session.execute(
                select(Invoice).where(Invoice.id.like("sim-agent-01-%"))
            ).scalars().all()

        assert invoices
        for invoice in invoices:
            limit = int(invoice.id.rsplit("-", 2)[1][1:])
            assert invoice.amount <= max(limit * 2, 1000), (
                f"{invoice.id} holds {invoice.amount}, impossible at limit {limit}"
            )

    def test_a_repeat_run_at_the_same_limit_still_reuses_its_invoices(
        self, client, admin_headers, db_engine
    ):
        """The original intent, preserved: identical inputs really are the same
        invoices being reprocessed, so the fix must not turn every re-run into a
        fresh pile of near-duplicate rows."""
        self._run(client, admin_headers, "good", count=40, seed=7)
        with Session(db_engine) as session:
            first = session.execute(
                select(func.count()).select_from(Invoice).where(Invoice.id.like("sim-%"))
            ).scalar_one()

        self._run(client, admin_headers, "good", count=40, seed=7)
        with Session(db_engine) as session:
            second = session.execute(
                select(func.count()).select_from(Invoice).where(Invoice.id.like("sim-%"))
            ).scalar_one()

        assert second == first, "a repeat at the same limit should reuse its invoices"
