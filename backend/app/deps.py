"""Shared FastAPI dependencies: identity, role checks, and the DB session.

No real auth yet — a header-based stub identity today, a real JWT later
behind the same `current_user` signature. Persistence is real as of this
branch (docs/lanes/vp.md, decision-ingest): `get_session` opens one
`Session` per request, committing on a clean return and rolling back on any
exception, so every route that writes gets an all-or-nothing transaction for
free just by depending on it.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import config
from app.schemas.envelope import ErrorBody
from app.schemas.user import CurrentUser, Role

# Stub users, one per role, keyed by the header value a caller sends. Real JWT
# claims replace this table; the CurrentUser shape it produces does not change.
_STUB_USERS: dict[Role, CurrentUser] = {
    Role.ADMIN: CurrentUser(user_id="user-admin-01", email="admin@aagp.dev", role=Role.ADMIN),
    Role.REVIEWER: CurrentUser(
        user_id="user-reviewer-01", email="reviewer@aagp.dev", role=Role.REVIEWER
    ),
    Role.AUDITOR: CurrentUser(user_id="user-auditor-01", email="auditor@aagp.dev", role=Role.AUDITOR),
}


def current_user(
    x_user_role: Annotated[str | None, Header(alias="X-User-Role")] = None,
) -> CurrentUser:
    """Read `X-User-Role` and return the stubbed user for that role.

    Defaults to `config.default_role()` when the header is absent — ADMIN
    unless `AUTH_DEFAULT_ROLE` says otherwise, so `/docs` and a bare `curl`
    still work with no setup locally while a public deployment can default
    an anonymous caller to read-only AUDITOR instead. Any recognized role
    string works case-insensitively; an unrecognized one is a 401, not a
    silent fallback (the same "fail loud on a typo" reasoning
    `governance/governance/agents/base.py`'s `require_stub_mode` already
    uses for `GOVERNANCE_MODE` — an unnoticed typo here would mean
    "everyone got admin" instead of "the demo obviously isn't working").
    """
    if x_user_role is None:
        return _STUB_USERS[config.default_role()]
    try:
        role = Role(x_user_role.strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=401,
            detail=ErrorBody(
                code="invalid_role_header",
                message=f"X-User-Role {x_user_role!r} is not a recognized role.",
                detail={"expected_one_of": [r.value for r in Role]},
            ).model_dump(),
        ) from exc
    return _STUB_USERS[role]


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]


def require_role(*allowed: Role):
    """FastAPI's idiom for a role-check decorator: a dependency factory.

    `Depends(require_role(Role.ADMIN))` on a route raises 403 for anyone
    whose stubbed role isn't in `allowed`. Applied now, on every
    state-changing endpoint, even though the identity behind it is a stub —
    docs/lanes/vp.md: "the role-check decorator exists and is applied from
    the start" so the shape is already right when real auth lands.
    """

    def _check(user: CurrentUserDep) -> CurrentUser:
        if user.role not in allowed:
            raise HTTPException(
                status_code=403,
                detail=ErrorBody(
                    code="forbidden",
                    message=f"Role {user.role.value!r} may not perform this action.",
                    detail={"required_one_of": [r.value for r in allowed]},
                ).model_dump(),
            )
        return user

    return _check


# Re-exported from `app.config`, which is the single place the connection
# string is read and normalised. Kept as a module-level name here because
# callers already import it from this module.
DEFAULT_DATABASE_URL = config.DEFAULT_DATABASE_URL

_engine = None
_session_maker: sessionmaker | None = None


def _default_session_maker() -> sessionmaker:
    """The process-wide engine/sessionmaker, built lazily on first real use —
    reading `DATABASE_URL` at that point, not at import time, so the normal
    "set the env var, then start the process" deployment model just works.
    Tests never reach this: `client` (`backend/tests/conftest.py`) replaces
    `get_session` entirely via `app.dependency_overrides` before any request
    is made, so there's no staleness risk from `DATABASE_URL` changing
    between tests in the same run.
    """
    global _engine, _session_maker
    if _session_maker is None:
        _engine = create_engine(config.database_url())
        _session_maker = sessionmaker(bind=_engine)
    return _session_maker


def session_dependency_factory(
    session_maker: Callable[[], Session],
) -> Callable[[], Generator[Session, None, None]]:
    """Build a FastAPI dependency that yields one `Session` per request,
    committing on a clean return and rolling back on any exception raised
    while handling it — the one place every write endpoint gets its
    "everything in this request, or nothing" guarantee from
    (docs/lanes/vp.md: decision ingest is one transaction).

    Shared between `get_session` below and `backend/tests/conftest.py`'s
    per-test override (pointed at a throwaway test database instead of
    `DATABASE_URL`) so both get identical commit/rollback semantics rather
    than two implementations that could quietly drift apart.
    """

    def _get_session() -> Generator[Session, None, None]:
        session = session_maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _get_session


get_session = session_dependency_factory(lambda: _default_session_maker()())

DbSessionDep = Annotated[Session, Depends(get_session)]
