"""The LangGraph workflow: four agents in parallel, one aggregated Recommendation out.

LangGraph concept, stated plainly because this design has to survive a panel question:
a `StateGraph` is a directed graph whose nodes are functions over a shared state. Edges
from `START` to all four agents mean LangGraph runs them in the same superstep — they
do not see each other's opinions, which is the point. Independent opinions can
disagree; sequential ones anchor on whatever the first agent said.

All four then edge into `aggregate`, which LangGraph runs once, after every incoming
node has finished.

    START ──┬──> risk ────────┬──> aggregate ──> END
            ├──> performance ─┤
            ├──> compliance ──┤
            └──> audit ───────┘

What this graph will never contain: an edge to anything that writes. Governance
recommends, the Policy Engine enforces, a human authorizes (ADR-0001, ADR-0004).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from langgraph.graph import END, START, StateGraph
from shared.constants import rung_of
from shared.contracts import AgentOpinion, Recommendation, TrustEvaluation
from shared.enums import Direction, OpinionVerdict, RecommendationStatus

from governance.agents import AGENT_MODULES, AGENT_NAMES
from governance.agents.llm_backed import opine_with_provenance
from governance.llm.errors import RecordingMissError
from governance.modes import CACHED, LIVE, STUB, resolve_mode, stub_fallback_allowed
from governance.state import GovernanceState


def _agent_node(agent_name: str):
    """Wrap one agent module as a LangGraph node.

    The node returns a *partial* state update — just its own opinion, as a
    single-element list. The `operator.add` reducer on `GovernanceState.opinions`
    concatenates the four instead of letting the last one win (see state.py).

    Mode routing happens here rather than inside each agent. Four copies of the same
    `if mode == CACHED` branch would be four chances for one agent to drift out of step
    with the others — and an agent still reasoning from hand-written stub text while the
    other three read model responses is not a difference anyone would spot in the output.
    """

    module = AGENT_MODULES[agent_name]

    def node(state: GovernanceState) -> dict:
        mode = state["mode"]
        if mode in (CACHED, LIVE):
            try:
                result = opine_with_provenance(agent_name, state["evaluation"], mode)
            except RecordingMissError:
                # A recording answers exactly one question: its key is a hash of
                # the whole prompt, so it replays only for the evidence it was
                # made from. Any evaluation the recordings were not built
                # against therefore misses, which makes cached mode unusable for
                # anything except the recorded scenarios.
                #
                # Raising is the default and stays the default: a silent stub
                # substitution would hide that a cached call had no recording,
                # which is exactly the failure this lane chose to make loud.
                #
                # An operator can opt into falling back — for a demo that must
                # not stop on a miss — and the recommendation then says
                # "cached+stub" in `governance_mode` and in its rationale, the
                # same way live already labels a fallback to cached. The
                # decision itself is unaffected either way: the trust engine
                # sets the direction, the panel only writes it up.
                if not stub_fallback_allowed():
                    raise
                return {
                    "opinions": [module.opine(state["evaluation"], STUB)],
                    "fell_back": [agent_name],
                }
            return {
                "opinions": [result.opinion],
                "fell_back": [agent_name] if result.fell_back else [],
            }
        return {"opinions": [module.opine(state["evaluation"], mode)], "fell_back": []}

    node.__name__ = f"{agent_name}_node"
    return node


def _aggregate(state: GovernanceState) -> dict:
    """Combine four independent opinions into one Recommendation.

    Two rules govern this step, and both are deliberately one-directional:

    - **Dissent can only make the proposal more conservative.** An OBJECT downgrades an
      INCREASE to HOLD. Nothing here can turn a HOLD into an INCREASE or soften a
      CLAWBACK — no combination of agreeable agents can talk the system into more
      authority than the evidence already supports.
    - **The evidence sets the ceiling.** `proposed_limit` is never above the trust
      engine's `recommended_limit`. The backend enforces a hard ceiling of its own
      regardless (that is what `Recommendation.clamped` records), but this lane does not
      rely on being caught — it does not make the ask in the first place.
    """
    evaluation = state["evaluation"]

    # LangGraph does not promise an ordering across parallel nodes; impose one so the
    # same evaluation always produces the same Recommendation.
    by_name = {opinion.agent_name: opinion for opinion in state["opinions"]}
    opinions = tuple(by_name[name] for name in AGENT_NAMES if name in by_name)

    dissenters = tuple(o for o in opinions if o.verdict is OpinionVerdict.OBJECT)
    has_dissent = bool(dissenters)

    direction = evaluation.direction
    proposed_limit = evaluation.recommended_limit

    if direction is Direction.INCREASE and has_dissent:
        direction = Direction.HOLD
        proposed_limit = evaluation.current_limit

    # Belt and braces on the ceiling rule above. If this ever trips, the bug is in this
    # function, not in the caller — better to fail here than to hand the Policy Engine
    # an ask it has to clamp.
    if proposed_limit > max(evaluation.recommended_limit, evaluation.current_limit):
        raise AssertionError(
            f"governance proposed {proposed_limit}, above both the evidence-supported "
            f"{evaluation.recommended_limit} and the current {evaluation.current_limit}"
        )

    confidence = sum(o.confidence for o in opinions) / len(opinions) if opinions else 0.0

    # Say which mode actually answered, not which one was asked for. If any agent's live
    # call failed and its recording served instead, this recommendation is not `live` and
    # must not claim to be — a recording is a real response to the same evidence, but it
    # was made earlier, and a reviewer deciding whether to trust this deserves to know.
    fell_back = tuple(n for n in AGENT_NAMES if n in set(state.get("fell_back", [])))
    fallback_target = CACHED if state["mode"] == LIVE else STUB
    effective_mode = f"{state['mode']}+{fallback_target}" if fell_back else state["mode"]

    recommendation = Recommendation(
        recommendation_id=uuid.uuid4().hex,
        agent_id=evaluation.agent_id,
        direction=direction,
        proposed_limit=proposed_limit,
        proposed_rung=rung_of(proposed_limit),
        rationale=_rationale(
            evaluation, opinions, dissenters, direction, fell_back, fallback_target
        ),
        opinions=opinions,
        has_dissent=has_dissent,
        confidence=round(confidence, 4),
        governance_mode=effective_mode,
        # PENDING, always. Governance cannot approve its own recommendation; an increase
        # needs a human (ADR-0004). Nothing in this lane may set APPROVED.
        status=RecommendationStatus.PENDING,
        trust_evaluation_ref=state.get("trust_evaluation_ref"),
        generated_at=datetime.now(UTC),
        # The backend owns clamping. Governance never reports itself as clamped.
        clamped=False,
        clamped_from=None,
    )
    return {"recommendation": recommendation}


def _rationale(
    evaluation: TrustEvaluation,
    opinions: tuple[AgentOpinion, ...],
    dissenters: tuple[AgentOpinion, ...],
    direction: Direction,
    fell_back: tuple[str, ...] = (),
    fallback_target: str = CACHED,
) -> str:
    """One paragraph a human reviewer reads before deciding.

    Disagreement is surfaced, not averaged away. If the risk agent objected and the
    performance agent concurred, that conflict is the most useful thing on the reviewer's
    screen — burying it behind a mean confidence score would defeat the point of running
    four agents instead of one prompt.
    """
    tally = {v: sum(1 for o in opinions if o.verdict is v) for v in OpinionVerdict}
    parts = [
        (
            f"Trust score {evaluation.trust_score:.1f}. "
            f"The trust engine proposes {evaluation.direction.value}; "
            f"governance forwards {direction.value}."
        ),
        (
            f"Panel: {tally[OpinionVerdict.CONCUR]} concur, "
            f"{tally[OpinionVerdict.OBJECT]} object, "
            f"{tally[OpinionVerdict.ABSTAIN]} abstain."
        ),
    ]

    if dissenters:
        names = ", ".join(o.agent_name for o in dissenters)
        parts.append(f"Dissent from {names}, which holds the proposal at its current limit.")
        for o in dissenters:
            parts.append(f"[{o.agent_name}] {o.reasoning}")
    else:
        parts.append("No agent objected.")

    if fell_back:
        # In the rationale, not only in `governance_mode`, because this is the line a
        # reviewer reads. A live run that quietly served recordings would look
        # identical to one that did not.
        names = ", ".join(fell_back)
        if fallback_target == CACHED:
            parts.append(
                f"Live call failed for {names}; served from recorded responses to the same "
                f"evidence."
            )
        else:
            # Named explicitly. A reviewer needs to know this paragraph was
            # written from a template rather than by a model, because that is
            # the difference between reasoning about this evidence and
            # describing it.
            parts.append(
                f"No recording matched this evidence for {names}; served from stub "
                f"reasoning instead."
            )

    if direction is not Direction.CLAWBACK:
        parts.append("Requires human authorization before any limit changes.")

    return " ".join(parts)


def build_graph():
    """Assemble and compile the coordinator graph."""
    graph = StateGraph(GovernanceState)

    for name in AGENT_NAMES:
        graph.add_node(name, _agent_node(name))

    graph.add_node("aggregate", _aggregate)

    for name in AGENT_NAMES:
        graph.add_edge(START, name)
        graph.add_edge(name, "aggregate")

    graph.add_edge("aggregate", END)
    return graph.compile()


# Compiling walks and validates the graph, so do it once at import rather than per call.
_COMPILED = build_graph()


def recommend(
    evaluation: TrustEvaluation,
    mode: str | None = None,
    trust_evaluation_ref: str | None = None,
) -> Recommendation:
    """Run the four agents over one evaluation and return their combined recommendation.

    This is the whole public surface of the lane — the backend calls this and nothing
    else.

    `trust_evaluation_ref` is supplied by the caller because `TrustEvaluation` carries no
    identity field of its own. The backend persists both sides and is the only component
    positioned to link them; inventing an id here would produce a reference that points
    at nothing.
    """
    resolved = resolve_mode(mode)
    result = _COMPILED.invoke(
        {
            "evaluation": evaluation,
            "mode": resolved,
            "opinions": [],
            "fell_back": [],
            "trust_evaluation_ref": trust_evaluation_ref,
        }
    )
    return result["recommendation"]
