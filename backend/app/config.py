"""Deploy-facing environment variables, read in one place.

Every value here is something that differs between a laptop and a hosted
deployment, and every one of them has a default that makes local development
and the test suite behave exactly as they did before this module existed.
That is the point: a misconfigured deployment should fail loudly, but an
*unconfigured* local checkout should still just run.

Each function reads `os.environ` when it is called rather than at import
time, so the normal "set the variable, then start the process" model works
and tests can monkeypatch without reimporting anything.
"""

from __future__ import annotations

import os

from app.schemas.user import Role

DEFAULT_DATABASE_URL = "postgresql://aagp:aagp_dev_password@localhost:5432/aagp"

# The Next.js dev server (frontend/package.json: "dev": "next dev"). Kept as
# the default so a local checkout and the panel demo need no configuration.
DEFAULT_CORS_ORIGINS = ("http://localhost:3000",)


def database_url() -> str:
    """The SQLAlchemy connection string, with one hosted-provider quirk fixed.

    Several managed Postgres providers hand out URLs beginning `postgres://`.
    SQLAlchemy 2.0 removed support for that alias and raises
    `NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgres`,
    which names a plugin nobody asked for and does not mention the scheme —
    so the cause is genuinely hard to see from the error. Normalising it is
    one line and turns a confusing startup crash into a non-event.
    """
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def cors_allow_origins() -> list[str]:
    """Browser origins allowed to call this API, comma-separated.

    This has to be configurable rather than hardcoded because the browser
    enforces it and the failure is invisible from the server side: the API
    answers every request correctly, and the dashboard renders empty. A
    deployed frontend on an origin missing from this list looks identical to
    a backend with no data.

    An empty or unset value falls back to the local dev origin rather than to
    `*`. Widening to `*` would silently disable the check for everyone, and
    with `allow_credentials=True` the browser rejects the wildcard anyway.
    """
    raw = os.environ.get("CORS_ALLOW_ORIGINS", "")
    origins = [origin.strip().rstrip("/") for origin in raw.split(",")]
    return [origin for origin in origins if origin] or list(DEFAULT_CORS_ORIGINS)


def default_role() -> Role:
    """The role assumed for a request that sends no `X-User-Role` header.

    Defaults to ADMIN, which is the dev convenience `current_user` has always
    provided: `/docs` and a bare `curl` work with no setup.

    On anything publicly reachable, set `AUTH_DEFAULT_ROLE=auditor`. AUDITOR
    is read-only everywhere, so an unauthenticated caller can look but not
    move a limit, start a run, or rule on a decision. Read
    `docs/DEPLOYMENT.md` before assuming that is authentication — it is not,
    and it is not meant to be. Identity here is a header a caller chooses for
    itself, so this narrows the blast radius of a request that arrives with
    *no* opinion about who it is; it does nothing whatsoever about one that
    claims to be an admin. Only real auth fixes that.
    """
    raw = os.environ.get("AUTH_DEFAULT_ROLE", "").strip().lower()
    if not raw:
        return Role.ADMIN
    try:
        return Role(raw)
    except ValueError as exc:
        # Fail at startup rather than serving traffic under a role nobody
        # chose. A typo here is exactly the "everyone got admin" outcome the
        # header parsing in `current_user` already refuses to allow.
        raise RuntimeError(
            f"AUTH_DEFAULT_ROLE={raw!r} is not a recognized role; "
            f"expected one of {[r.value for r in Role]}."
        ) from exc
