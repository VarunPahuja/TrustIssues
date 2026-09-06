"""
simulator/cli.py
-----------------
Typer CLI — the user-facing entry point for the simulator.

COMMANDS:
  generate   Build fixture JSON files (no LLM, no API needed)
  run        Run the agent against a fixture or fresh invoices
  validate   Validate a fixture file against the Pydantic schema
  smoke-test Quick validation: generate 40 invoices (20 good + 20 degraded)
             and report error rates to verify the 5-15 % / >20 % targets

USAGE EXAMPLES:
  # Generate all three fixture files
  python -m simulator generate --phase good      --count 200 --seed 42
  python -m simulator generate --phase degraded  --count 200 --seed 42
  python -m simulator generate --phase recovery  --count 200 --seed 42

  # Run the scripted agent against the good fixture
  python -m simulator run --phase good --agent scripted

  # Validate a fixture
  python -m simulator validate fixtures/good.json

  # Smoke test — verify error rates
  python -m simulator smoke-test
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint

from shared.constants import AUTONOMY_FLOOR
from simulator.constants import DEFAULT_API_BASE_URL, DEFAULT_SEED, PHASE_ERROR_RATES
from simulator.models import Invoice, SimulationPhase, SimulationRunConfig
from simulator.distributions import get_params
from simulator.generator import InvoiceGenerator
from simulator.runner import SimulationRunner

app = typer.Typer(
    name="simulator",
    help="Earned Autonomy Engine — invoice simulator CLI",
    add_completion=False,
)
console = Console()

# Default fixture output directory
DEFAULT_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

@app.command()
def generate(
    phase: SimulationPhase = typer.Option(SimulationPhase.GOOD, help="Distribution phase"),
    count: int = typer.Option(200, help="Number of invoices to generate"),
    seed: int = typer.Option(DEFAULT_SEED, help="Random seed for reproducibility"),
    out_dir: Path = typer.Option(DEFAULT_FIXTURE_DIR, "--out", help="Output directory"),
) -> None:
    """Generate a fixture JSON file for the given phase."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{phase.value}.json"

    console.print(f"[bold green]Generating[/] {count} [cyan]{phase.value}[/] invoices (seed={seed})...")

    params = get_params(phase.value)
    gen = InvoiceGenerator(seed=seed, params=params, phase=phase)
    invoices = gen.generate(count)

    # Count decision distribution
    decisions: dict[str, int] = {"approve": 0, "reject": 0, "escalate": 0}
    for inv in invoices:
        decisions[inv.ground_truth_decision.value] += 1

    # Write fixture
    data = [inv.model_dump(mode="json") for inv in invoices]
    out_file.write_text(
        json.dumps({"phase": phase.value, "seed": seed, "count": count, "invoices": data},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    console.print(f"[bold]✓[/] Written to [underline]{out_file}[/]")

    # Summary table
    t = Table(title=f"Ground Truth Distribution — {phase.value}")
    t.add_column("Decision", style="cyan")
    t.add_column("Count", justify="right")
    t.add_column("Pct", justify="right")
    for d, n in decisions.items():
        t.add_row(d, str(n), f"{100*n/count:.1f}%")
    console.print(t)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

@app.command()
def run(
    phase: SimulationPhase = typer.Option(SimulationPhase.GOOD, help="Distribution phase"),
    agent_type: str = typer.Option("scripted", "--agent", help="Agent type: scripted"),
    count: int = typer.Option(100, help="Number of invoices to process"),
    seed: int = typer.Option(DEFAULT_SEED, help="Random seed"),
    agent_id: str = typer.Option(
        "scripted-agent-001", "--agent-id",
        help="Agent id to run as / submit decisions for. Must be a real agent on "
             "the backend when --submit is used (e.g. agent-01).",
    ),
    limit: int = typer.Option(
        AUTONOMY_FLOOR, "--limit",
        help="The agent's current autonomy limit (rupees). It approves within this "
             "and escalates above it. Set this to the agent's real backend limit so "
             "escalation volume drops as the agent earns higher rungs.",
    ),
    error_rate: Optional[float] = typer.Option(
        None, "--error-rate",
        help="Fraction of decisions the agent gets deliberately wrong (0.0-1.0). "
             "Defaults to the phase's rate from PHASE_ERROR_RATES: "
             "good=0.05, degraded=0.30, recovery=0.10.",
    ),
    api_url: str = typer.Option(DEFAULT_API_BASE_URL, help="Backend API base URL"),
    submit: bool = typer.Option(False, "--submit/--no-submit", help="Submit invoices to backend API"),
    fixture: Optional[Path] = typer.Option(None, help="Load invoices from fixture file instead of generating"),
) -> None:
    """Run an agent over invoices and report accuracy metrics."""

    # A phase's error rate is the default; --error-rate overrides it explicitly.
    if error_rate is None:
        error_rate = PHASE_ERROR_RATES.get(phase.value, 0.05)

    # Build agent
    agent = _build_agent(agent_type, agent_id, limit, error_rate)
    console.print(
        f"[bold]Agent:[/] {agent.name}  "
        f"[dim](limit=INR {limit}, error_rate={error_rate:.0%})[/]"
    )

    # Load or generate invoices
    if fixture and fixture.exists():
        console.print(f"[bold]Loading fixture:[/] {fixture}")
        raw = json.loads(fixture.read_text(encoding="utf-8"))
        invoices = [Invoice(**inv) for inv in raw["invoices"]][:count]
    else:
        console.print(f"[bold]Generating[/] {count} [cyan]{phase.value}[/] invoices...")
        params = get_params(phase.value)
        gen = InvoiceGenerator(seed=seed, params=params, phase=phase)
        invoices = gen.generate(count)

    # Build runner config
    config = SimulationRunConfig(
        phase=phase,
        invoice_count=len(invoices),
        seed=seed,
        agent_type=agent_type,
        agent_id=agent_id,
        api_base_url=api_url,
    )

    # Optional API client
    api_client = None
    if submit:
        from simulator.api_client import APIClient
        api_client = APIClient(base_url=api_url)
        if not api_client.health_check():
            console.print(f"[red]Warning: backend at {api_url} is not reachable. Skipping API submission.[/]")
            api_client = None

    # Run
    runner = SimulationRunner(config=config, agent=agent, api_client=api_client)
    result = runner.run(invoices)

    # Report
    _print_result(result)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

@app.command()
def validate(
    fixture_file: Path = typer.Argument(..., help="Path to fixture JSON file"),
) -> None:
    """Validate a fixture file against the Invoice Pydantic schema."""
    if not fixture_file.exists():
        console.print(f"[red]File not found: {fixture_file}[/]")
        raise typer.Exit(1)

    raw = json.loads(fixture_file.read_text(encoding="utf-8"))
    invoices_data = raw.get("invoices", raw) if isinstance(raw, dict) else raw

    errors = []
    for i, inv_data in enumerate(invoices_data):
        try:
            Invoice(**inv_data)
        except Exception as exc:
            errors.append(f"Invoice {i}: {exc}")

    if errors:
        console.print(f"[red]Validation FAILED — {len(errors)} errors:[/]")
        for e in errors[:10]:
            console.print(f"  • {e}")
        raise typer.Exit(1)

    console.print(f"[bold green]✓ Valid[/] — {len(invoices_data)} invoices all pass schema validation")


# ---------------------------------------------------------------------------
# smoke-test
# ---------------------------------------------------------------------------

@app.command(name="smoke-test")
def smoke_test(
    n_per_phase: int = typer.Option(20, help="Invoices per phase (good + degraded)"),
    agent_type: str = typer.Option("scripted", "--agent", help="Agent: scripted"),
) -> None:
    """
    Quick validation: run agent over good + degraded invoices and report error rates.
    Target: good error rate 5-15 %, degraded clearly higher (>20 %).
    """
    agent = _build_agent(agent_type)
    console.print(f"[bold]Smoke test[/] with {agent.name}, {n_per_phase} invoices per phase\n")

    results = {}
    for phase_name in ("good", "degraded"):
        phase = SimulationPhase(phase_name)
        params = get_params(phase_name)
        gen = InvoiceGenerator(seed=DEFAULT_SEED, params=params, phase=phase)
        invoices = gen.generate(n_per_phase)

        config = SimulationRunConfig(
            phase=phase,
            invoice_count=n_per_phase,
            seed=DEFAULT_SEED,
            agent_type=agent_type,
            agent_id=agent.agent_id,
            api_base_url=DEFAULT_API_BASE_URL,
        )
        runner = SimulationRunner(config=config, agent=agent)
        result = runner.run(invoices)
        results[phase_name] = result

    # Print comparison table
    t = Table(title="Smoke Test Results")
    t.add_column("Phase", style="cyan")
    t.add_column("Total", justify="right")
    t.add_column("Correct", justify="right")
    t.add_column("Accuracy", justify="right")
    t.add_column("Wilson LB", justify="right")
    t.add_column("Status", justify="center")

    for phase_name, r in results.items():
        acc = r.accuracy or 0.0
        wlb = r.wilson_lower_bound or 0.0
        if phase_name == "good":
            ok = 0.85 <= acc <= 0.95
        else:
            ok = acc < 0.85  # Should be clearly worse
        status = "[green]✓[/]" if ok else "[red]✗ ADJUST KNOBS[/]"
        t.add_row(
            phase_name,
            str(r.total_invoices),
            str(r.correct_decisions),
            f"{acc:.1%}",
            f"{wlb:.1%}",
            status,
        )

    console.print(t)

    good_acc = results["good"].accuracy or 0
    if good_acc > 0.95:
        console.print("\n[yellow]⚠ Good-phase accuracy is too high (>95 %). "
                      "Increase degraded knobs or reduce good-phase amount margins.[/]")
    elif good_acc < 0.80:
        console.print("\n[yellow]⚠ Good-phase accuracy is too low (<80 %). "
                      "Simplify good-phase invoices.[/]")
    else:
        console.print("\n[bold green]✓ Error rates look good for a convincing demo.[/]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_agent(agent_type: str, agent_id: str = "scripted-agent-001",
                 current_limit: int = AUTONOMY_FLOOR,
                 error_rate: float = 0.05):
    if agent_type == "scripted":
        from simulator.agents.scripted import ScriptedAgent
        return ScriptedAgent(
            agent_id=agent_id, current_limit=current_limit, error_rate=error_rate
        )
    else:
        console.print(f"[red]Unknown agent type: {agent_type!r}. Choose 'scripted'.[/]")
        raise typer.Exit(1)


def _print_result(result) -> None:
    t = Table(title=f"Simulation Result — {result.config.phase.value}")
    t.add_column("Metric", style="cyan")
    t.add_column("Value", justify="right")
    t.add_row("Total invoices", str(result.total_invoices))
    t.add_row("Approved",       str(result.approved_count))
    t.add_row("Rejected",       str(result.rejected_count))
    t.add_row("Escalated",      str(result.escalated_count))
    t.add_row("Correct decisions", str(result.correct_decisions))
    t.add_row("Accuracy",          f"{result.accuracy:.1%}" if result.accuracy else "—")
    t.add_row("Wilson LB (95%)",   f"{result.wilson_lower_bound:.1%}" if result.wilson_lower_bound else "—")
    t.add_row("LLM calls",         str(result.llm_calls))
    t.add_row("Cache hits",        str(result.cache_hits))
    t.add_row("Errors",            str(len(result.errors)))
    console.print(t)

    if result.errors:
        console.print("[red]Errors:[/]")
        for e in result.errors[:5]:
            console.print(f"  • {e}")


if __name__ == "__main__":
    app()
