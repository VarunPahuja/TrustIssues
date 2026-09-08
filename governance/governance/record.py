"""Record real model responses for the scenarios the demo replays.

    python -m governance.record --dry-run     # print what would be called, call nothing
    python -m governance.record               # record every missing scenario
    python -m governance.record --force       # re-record, including what already exists

Lives in `governance/` rather than `scripts/` because the lane brief says not to write
code outside this directory, and because it imports the prompt layer it is exercising.

**Why a script rather than recording on first use.** Recording lazily would mean the
first run of a demo makes twenty live calls, at roughly ten requests per minute on
Gemini's free tier, in front of whoever is watching. Recording is a deliberate act with
a rate limit attached; replay is the thing that has to be instant.

**Each agent may be on a different provider** (ADR-0012), so the plan is printed before
anything is sent — a panel that is accidentally four Gemini agents costs nothing to fix
beforehand and is invisible afterwards.

**It validates every response before saving it.** A recording that cannot be parsed is
worse than no recording — it turns a clean "nothing recorded" error into a parse failure
at demo time, and the fix requires a network connection you may not have.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator
from pathlib import Path

from governance.agents.base import AGENT_NAMES
from governance.llm.base import LLMClient
from governance.llm.errors import GovernanceLLMError
from governance.llm.recording import RecordingStore, build_recording, cache_key_for
from governance.llm.registry import build_client, describe_panel
from governance.prompts.loader import build_prompt
from governance.prompts.schema import OpinionParseError, parse_opinion
from governance.scenarios import SCENARIOS, Scenario


def load_dotenv(path: Path | None = None) -> list[str]:
    """Read `.env` at the repo root into the environment. Returns the names it set.

    The library never does this — `gemini.py` reads `os.environ` and nothing else, so
    importing this lane can never pick up an API key as a side effect. Only this
    dev-time CLI reads the file, because it is the only thing whose help text promises
    that `.env` works.

    Hand-rolled rather than depending on python-dotenv: this lane justifies every
    runtime dependency it takes, and the format we need is `NAME=value` lines.

    A name already present in the real environment wins. An explicit
    `GEMINI_API_KEY=... python -m governance.record` must not be silently overridden by
    a stale file — the whole point of this run is knowing which key answered.
    """
    path = path or Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return []

    applied: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip("'\"")
        # An empty value is not a key. Leaving the name unset makes the "no API key"
        # message fire, which is the accurate diagnosis; setting it to "" would send a
        # keyless request and report whatever Gemini says about it instead.
        if not name or not value or name in os.environ:
            continue
        os.environ[name] = value
        applied.append(name)
    return applied


def select_scenarios(names: list[str] | None) -> tuple[Scenario, ...]:
    """The scenarios to record, defaulting to all five.

    An unknown name is an error rather than an empty run: a typo'd `--scenario` that
    quietly recorded nothing looks exactly like "everything was already recorded".
    """
    if not names:
        return tuple(SCENARIOS)
    by_name = {scenario.name: scenario for scenario in SCENARIOS}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise ValueError(
            f"unknown scenario(s) {', '.join(sorted(unknown))}; "
            f"expected one of {', '.join(sorted(by_name))}"
        )
    # Deduplicated, but in SCENARIOS order rather than the order they were typed, so a
    # partial run is a subset of the full run and not a differently-ordered one.
    chosen = set(names)
    return tuple(s for s in SCENARIOS if s.name in chosen)


def iter_work(
    store: RecordingStore,
    clients: dict[str, LLMClient],
    *,
    force: bool,
    scenarios: tuple[Scenario, ...] = (),
) -> Iterator[tuple[Scenario, str, bool]]:
    """Yield (scenario, agent_name, already_recorded) for every prompt in the matrix."""
    for scenario in scenarios or tuple(SCENARIOS):
        evaluation = scenario.build()
        for agent_name in AGENT_NAMES:
            prompt = build_prompt(agent_name, evaluation)
            key = cache_key_for(prompt, clients[agent_name].slug)
            yield scenario, agent_name, store.has(key) and not force


def _pacing_interval_of(client: LLMClient) -> float:
    """The gap a client paces with, whichever way its config spells it.

    Gemini derives the gap from the model's requests-per-minute limit, because those
    differ threefold across models on the same free tier; the other providers carry a
    flat number. Read through whichever attribute exists rather than asking the client
    what it is — the estimate is a courtesy, and a provider added later must not be able
    to break `--dry-run` by naming the field differently.
    """
    config = client.config
    interval = getattr(config, "pacing_interval_s", None)
    if interval is None:
        interval = getattr(config, "min_interval_s", None)
    return float(interval) if interval is not None else 0.0


def _first_agent_on(clients: dict[str, LLMClient], provider: str) -> str:
    """Any agent using this provider — they share one client, so any of them will do."""
    return next(name for name, client in clients.items() if client.provider == provider)


def record_all(
    *, force: bool = False, dry_run: bool = False, scenarios: list[str] | None = None
) -> int:
    """Record every missing scenario/agent pair. Returns a process exit code."""
    store = RecordingStore()
    clients: dict[str, LLMClient] = {name: build_client(name) for name in AGENT_NAMES}
    panel = describe_panel(AGENT_NAMES)
    chosen = select_scenarios(scenarios)

    pending = [
        (scenario, agent)
        for scenario, agent, already in iter_work(
            store, clients, force=force, scenarios=chosen
        )
        if not already
    ]
    skipped = sum(
        1
        for _, _, already in iter_work(store, clients, force=force, scenarios=chosen)
        if already
    )

    total_calls = len(pending)
    # Agents on one provider share a client and therefore a pacer, because a rate limit
    # belongs to the key rather than to the agent. So the run is bounded by whichever
    # *provider* has the most calls to make, not by the total and not by any one agent.
    by_provider: dict[str, int] = {}
    for _, agent in pending:
        by_provider[clients[agent].provider] = by_provider.get(clients[agent].provider, 0) + 1
    seconds = max(
        (count * _pacing_interval_of(clients[_first_agent_on(clients, p)])
         for p, count in by_provider.items()),
        default=0.0,
    )
    minutes = seconds / 60

    print(f"recordings dir   : {store.directory}")
    print("panel            : " + ", ".join(f"{a}={p}" for a, p in panel.items()))
    for name in AGENT_NAMES:
        print(f"  {name:<12} {clients[name].model}  ->  {clients[name].slug}")
    if len(set(panel.values())) == 1:
        print(
            f"  note: all four agents use {next(iter(panel.values()))}. Set "
            f"GOVERNANCE_PROVIDER_<AGENT> to mix providers — a single-model panel shares "
            f"one model's biases (ADR-0012)."
        )
    print(f"already recorded : {skipped}")
    print(f"to record        : {total_calls}")
    print(f"estimated time   : {minutes:.1f} min")
    print()

    if dry_run:
        for scenario, agent in pending:
            print(f"  would record  {agent:<12} {clients[agent].slug:<22} {scenario.name}")
        return 0

    if not total_calls:
        print("Nothing to do. Use --force to re-record.")
        return 0

    missing_keys = sorted({clients[a].provider for _, a in pending if not clients[a].has_key})
    if missing_keys:
        print(
            f"No API key for: {', '.join(missing_keys)}. Set the matching "
            f"<PROVIDER>_API_KEY in .env (gitignored), or point those agents at a "
            f"provider you do have a key for. Stub and cached modes need no key.",
            file=sys.stderr,
        )
        return 2

    failures = 0
    for index, (scenario, agent_name) in enumerate(pending, start=1):
        client = clients[agent_name]
        prompt = build_prompt(agent_name, scenario.build())
        label = f"[{index}/{total_calls}] {agent_name:<12} {client.slug:<22} {scenario.name}"
        try:
            text = client.generate(prompt)
        except GovernanceLLMError as exc:
            failures += 1
            print(f"{label}  FAILED  {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        # Parse before saving. An unparseable recording only reveals itself at demo
        # time, when the network that could fix it may not be there.
        try:
            parse_opinion(text, agent_name)
        except OpinionParseError as exc:
            failures += 1
            print(f"{label}  INVALID  {exc}", file=sys.stderr)
            continue

        path = store.save(
            build_recording(
                prompt,
                text,
                client.model,
                provider=client.provider,
                model_slug=client.slug,
            )
        )
        print(f"{label}  saved  {path.name}")

    print()
    print(f"done: {total_calls - failures} recorded, {failures} failed")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m governance.record",
        description="Record real model responses for the cached-mode demo.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-record scenarios that already have a recording",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the call plan and exit without touching the network",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        metavar="NAME",
        help=(
            "record only this scenario; repeatable. Defaults to all five. "
            f"One of: {', '.join(s.name for s in SCENARIOS)}"
        ),
    )
    args = parser.parse_args(argv)
    load_dotenv()
    try:
        return record_all(force=args.force, dry_run=args.dry_run, scenarios=args.scenario)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
