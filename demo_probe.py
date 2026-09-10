"""Live demo probe — feed the system a batch of decisions, watch trust move.

Built for the panel question "what if the agent were 90% accurate?" — you type
the number, the system answers with its own arithmetic. Nothing here is
special-cased: it POSTs ordinary decisions through the same
`POST /api/v1/decisions` the simulator uses, then reads the trust evaluation
the backend computes and asks governance for a recommendation.

Usage
-----
  python demo_probe.py --agent agent-02 --count 40 --accuracy 0.95
  python demo_probe.py --agent agent-02 --count 40 --accuracy 0.60
  python demo_probe.py --agent agent-02 --count 20 --accuracy 0.95 --critical 1
  python demo_probe.py --agent agent-02 --reset-view      (read only, sends nothing)

Notes
-----
* Decisions accumulate. The trust engine scores an agent's whole history, so a
  second run adds to the first rather than replacing it. Use --reset-view to
  read the current state without sending anything.
* --critical N injects N critical errors (APPROVE where the truth is REJECT).
  One is enough to trigger an immediate clawback: the drift detector treats a
  critical error in the recent window as decisive without waiting for
  statistics. That is the most striking thing you can show a panel in one
  command.
* --recommend asks governance for a recommendation afterwards. Leave it off
  unless you mean it: on a branch with auto-clawback merged, a CLAWBACK
  recommendation applies itself immediately.
"""

from __future__ import annotations

import argparse
import random
import sys
import time

import httpx

HEADERS = {"X-User-Role": "admin"}


def fetch_trust(client: httpx.Client, agent: str) -> dict:
    return client.get(f"/api/v1/agents/{agent}/trust").json()


def show(label: str, trust: dict, agent_row: dict) -> None:
    print(f"\n{label}")
    print(f"  limit          INR {agent_row['current_limit']}  (rung {agent_row['current_rung']})")
    print(f"  trust score    {trust['trust_score']:.1f}")
    print(f"  direction      {trust['direction']}")
    drift = trust.get("drift") or {}
    print(f"  drift          {drift.get('severity', 'n/a')}")
    for c in trust.get("components", []):
        # A component with no evidence has value None and is dropped; the trust
        # engine renormalises the remaining weights so they still sum to 1.
        avail = "" if c["available"] else "   <- no data, weight redistributed"
        value = "    n/a" if c["value"] is None else f"{c['value']:7.4f}"
        print(f"    {c['name']:24s} {value}   w={c['effective_weight']:.3f}{avail}")
    codes = ", ".join(trust.get("reason_codes", []))
    print(f"  reason codes   {codes}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--agent", default="agent-02")
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--accuracy", type=float, default=0.95,
                    help="fraction of decisions the agent gets right (0.0-1.0)")
    ap.add_argument("--critical", type=int, default=0,
                    help="how many of the errors are critical (APPROVE where truth is REJECT)")
    ap.add_argument("--amount", type=int, default=300,
                    help="invoice amount; keep it under the agent's limit so it acts rather than escalates")
    ap.add_argument("--seed", type=int, default=None, help="repeatable batch")
    ap.add_argument("--recommend", action="store_true",
                    help="ask governance for a recommendation afterwards")
    ap.add_argument("--reset-view", action="store_true", help="read current state, send nothing")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    tag = f"probe-{int(time.time())}"

    with httpx.Client(base_url=args.api, headers=HEADERS, timeout=60.0) as client:
        try:
            agent_row = client.get(f"/api/v1/agents/{args.agent}").json()
        except Exception as exc:
            print(f"cannot reach {args.api}: {exc}")
            return 1
        if "id" not in agent_row:
            print(f"no such agent {args.agent!r}")
            return 1

        show("BEFORE", fetch_trust(client, args.agent), agent_row)
        if args.reset_view:
            return 0

        # Build the batch: correct decisions, then errors, then criticals.
        n_wrong = round(args.count * (1.0 - args.accuracy))
        n_critical = min(args.critical, n_wrong) if args.critical else 0
        n_noncritical = n_wrong - n_critical
        plan = (
            ["correct"] * (args.count - n_wrong)
            + ["critical"] * n_critical
            + ["noncritical"] * n_noncritical
        )
        rng.shuffle(plan)

        print(f"\nsending {args.count} decisions to {args.agent}: "
              f"{args.count - n_wrong} correct, {n_noncritical} wrong, {n_critical} CRITICAL")

        sent = failed = 0
        for i, kind in enumerate(plan):
            if kind == "correct":
                action, truth = "APPROVE", "APPROVE"
            elif kind == "critical":
                # Approving something that should have been rejected: money leaves.
                action, truth = "APPROVE", "REJECT"
            else:
                # Wrongly rejecting a good invoice: cautious, not dangerous.
                action, truth = "REJECT", "APPROVE"
            r = client.post("/api/v1/decisions", json={
                "invoice_id": f"inv-{tag}-{i:04d}",
                "amount": args.amount,
                "action": action,
                "ground_truth": truth,
                "agent_id": args.agent,
                "reason": f"{tag} demo probe",
            })
            if r.status_code == 201:
                sent += 1
            else:
                failed += 1
        print(f"  accepted {sent}/{args.count}" + (f", failed {failed}" if failed else ""))

        agent_row = client.get(f"/api/v1/agents/{args.agent}").json()
        show("AFTER", fetch_trust(client, args.agent), agent_row)

        if args.recommend:
            r = client.post(f"/api/v1/agents/{args.agent}/recommendations")
            if r.status_code >= 300:
                print(f"\nrecommendation -> HTTP {r.status_code}: {r.text[:200]}")
            else:
                rec = r.json()
                print(f"\nRECOMMENDATION  {rec['direction']} -> INR {rec['proposed_limit']}  "
                      f"[{rec['status']}]")
                print(f"  {rec['rationale'][:300]}")
                for o in rec.get("agent_opinions", []):
                    print(f"    {o['agent_name']:12s} {o['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
