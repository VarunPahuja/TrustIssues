"""Contract-drift tests: every `app/schemas/` response model that mirrors a
`shared/contracts.py` frozen dataclass must actually still mirror it.

docs/lanes/vp.md: "add a test asserting the field names and types match so
drift is caught in CI." This is that test, generalized once and applied to
all nine mirrored types rather than hand-written nine times.

What "type match" means here: every dataclass field's name exists on the
Pydantic model with a structurally equivalent type — primitives and enum
classes compared directly (enums are literally the same class, imported
from `shared.enums`, not redefined), `X | None` compared modulo optionality,
and `tuple[Y, ...]` accepted as equivalent to `list[Y]` (idiomatic JSON
representation, not drift). A nested dataclass type (e.g. `ScoreComponent`
inside `TrustEvaluation.components`) is mapped to its corresponding `*Out`
schema via an explicit `rename` table per test — never inferred by name
alone, so a genuine mismatch can't hide behind a naming coincidence.
"""

from __future__ import annotations

import dataclasses
import functools
import operator
import types
import typing

from shared.contracts import (
    AgentContext,
    AgentOpinion,
    AuditSample,
    DecisionRecord,
    DriftResult,
    ProportionResult,
    Recommendation,
    ScoreComponent,
    TrustEvaluation,
)

from app.schemas.agent import AgentContextOut
from app.schemas.audit import AuditSampleOut
from app.schemas.decision import DecisionRecordOut
from app.schemas.governance import AgentOpinionOut, RecommendationOut
from app.schemas.trust import (
    DriftResultOut,
    ProportionResultOut,
    ScoreComponentOut,
    TrustEvaluationOut,
)

_UNION_ORIGINS = {typing.Union, types.UnionType}


def _strip_optional(tp: object):
    origin = typing.get_origin(tp)
    if origin in _UNION_ORIGINS:
        args = typing.get_args(tp)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
        return functools.reduce(operator.or_, non_none)
    return tp


def _shape(tp: object):
    """Reduce a type to a comparable shape: a bare type, or ('seq', inner-shape)."""
    tp = _strip_optional(tp)
    origin = typing.get_origin(tp)
    if origin in (tuple, list):
        args = typing.get_args(tp)
        inner = args[0] if args else object
        return ("seq", _shape(inner))
    return tp


def assert_mirrors_dataclass(
    dataclass_type: type,
    pydantic_type: type,
    *,
    extra_fields: frozenset[str] = frozenset(),
    rename: dict[type, type] | None = None,
) -> None:
    """Assert `pydantic_type` mirrors `dataclass_type` field-for-field.

    `extra_fields` names fields the Pydantic model is allowed to add beyond
    the dataclass (e.g. `TrustEvaluationOut.id` — see app/schemas/trust.py).
    Every other field on both sides must correspond exactly.
    """
    dc_hints = typing.get_type_hints(dataclass_type)
    dc_fields = {f.name: dc_hints[f.name] for f in dataclasses.fields(dataclass_type)}
    pyd_fields = pydantic_type.model_fields

    missing = set(dc_fields) - set(pyd_fields)
    assert not missing, (
        f"{pydantic_type.__name__} is missing fields present on "
        f"{dataclass_type.__name__}: {sorted(missing)}"
    )

    unexpected = set(pyd_fields) - set(dc_fields) - extra_fields
    assert not unexpected, (
        f"{pydantic_type.__name__} has fields absent from {dataclass_type.__name__} "
        f"and not declared in extra_fields: {sorted(unexpected)}"
    )

    rename = rename or {}

    def resolve(shape: object) -> object:
        if isinstance(shape, tuple) and shape[0] == "seq":
            return ("seq", resolve(shape[1]))
        return rename.get(shape, shape)

    for name, dc_type in dc_fields.items():
        expected = resolve(_shape(dc_type))
        actual = _shape(pyd_fields[name].annotation)
        assert expected == actual, (
            f"{pydantic_type.__name__}.{name}: expected shape {expected} (mirroring "
            f"{dataclass_type.__name__}.{name}: {dc_type}), got {actual} "
            f"({pyd_fields[name].annotation})"
        )


def test_decision_record_out_mirrors_decision_record():
    assert_mirrors_dataclass(DecisionRecord, DecisionRecordOut)


def test_proportion_result_out_mirrors_proportion_result():
    assert_mirrors_dataclass(ProportionResult, ProportionResultOut)


def test_score_component_out_mirrors_score_component():
    assert_mirrors_dataclass(ScoreComponent, ScoreComponentOut)


def test_drift_result_out_mirrors_drift_result():
    assert_mirrors_dataclass(DriftResult, DriftResultOut)


def test_agent_context_out_mirrors_agent_context():
    assert_mirrors_dataclass(AgentContext, AgentContextOut)


def test_trust_evaluation_out_mirrors_trust_evaluation():
    assert_mirrors_dataclass(
        TrustEvaluation,
        TrustEvaluationOut,
        extra_fields=frozenset({"id"}),
        rename={
            ProportionResult: ProportionResultOut,
            ScoreComponent: ScoreComponentOut,
            DriftResult: DriftResultOut,
        },
    )


def test_agent_opinion_out_mirrors_agent_opinion():
    assert_mirrors_dataclass(AgentOpinion, AgentOpinionOut)


def test_recommendation_out_mirrors_recommendation():
    assert_mirrors_dataclass(
        Recommendation,
        RecommendationOut,
        rename={AgentOpinion: AgentOpinionOut},
        # `reason_codes`: shared.contracts.Recommendation has no such field
        # to mirror (shared/ is frozen) — see app/schemas/governance.py's
        # docstring for why it's a backend-local addition, derived from
        # `clamped` rather than stored.
        extra_fields=frozenset({"reason_codes"}),
    )


def test_audit_sample_out_mirrors_audit_sample():
    assert_mirrors_dataclass(AuditSample, AuditSampleOut)
