"""The environment variables that differ between a laptop and a deployment.

Three of these were genuine blockers to deploying at all, and the reason they
need tests is that each one fails *quietly*:

  - A CORS origin that does not match is enforced by the browser, so the API
    answers every request correctly and the dashboard renders empty. A
    misconfigured deployment and a backend with no data look identical.
  - A header-less request defaulting to ADMIN means a bare curl against a
    public URL can move a limit. Nothing in the response says so.
  - `postgres://` raises `NoSuchModuleError` naming a plugin nobody asked
    for, without mentioning the scheme that caused it.

Every default here must leave local development and the existing suite
behaving exactly as they did before `app.config` existed.
"""

from __future__ import annotations

import pytest

from app import config
from app.schemas.user import Role


class TestDatabaseUrl:
    def test_unset_falls_back_to_the_local_compose_database(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert config.database_url() == config.DEFAULT_DATABASE_URL

    def test_a_postgres_scheme_is_normalised(self, monkeypatch):
        """The whole reason this function exists. SQLAlchemy 2.0 removed the
        `postgres://` alias, and several managed providers still hand it out."""
        monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host:5432/db")
        assert config.database_url() == "postgresql://u:p@host:5432/db"

    def test_an_already_correct_url_is_untouched(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host:5432/db")
        assert config.database_url() == "postgresql://u:p@host:5432/db"

    def test_normalisation_replaces_the_scheme_not_every_occurrence(self, monkeypatch):
        """A password could contain the string, so this must not be a blanket
        replace."""
        monkeypatch.setenv("DATABASE_URL", "postgres://u:postgres_pw@host/db")
        assert config.database_url() == "postgresql://u:postgres_pw@host/db"

    def test_a_non_postgres_url_is_left_alone(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///./local.db")
        assert config.database_url() == "sqlite:///./local.db"


class TestCorsOrigins:
    def test_unset_allows_the_next_dev_server(self, monkeypatch):
        """Local work and the panel demo must need no configuration."""
        monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
        assert config.cors_allow_origins() == ["http://localhost:3000"]

    def test_blank_is_treated_as_unset(self, monkeypatch):
        """Render creates the variable with an empty value before the Vercel
        URL is known, and an empty allow-list would block everything."""
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "")
        assert config.cors_allow_origins() == ["http://localhost:3000"]

    def test_a_comma_separated_list_is_split(self, monkeypatch):
        monkeypatch.setenv(
            "CORS_ALLOW_ORIGINS", "https://a.vercel.app,http://localhost:3000"
        )
        assert config.cors_allow_origins() == [
            "https://a.vercel.app",
            "http://localhost:3000",
        ]

    def test_whitespace_and_trailing_slashes_are_tolerated(self, monkeypatch):
        """A trailing slash makes the origin not match, and the failure gives no
        hint — the browser just blocks it. Copy-pasting a URL out of a hosting
        dashboard is exactly how a slash gets in."""
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", " https://a.vercel.app/ , https://b.dev ")
        assert config.cors_allow_origins() == ["https://a.vercel.app", "https://b.dev"]

    def test_empty_entries_from_a_trailing_comma_are_dropped(self, monkeypatch):
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://a.vercel.app,")
        assert config.cors_allow_origins() == ["https://a.vercel.app"]

    def test_the_wildcard_is_never_a_fallback(self, monkeypatch):
        """Falling back to a wildcard would disable the check for everyone while
        looking like it worked. With allow_credentials=True the browser rejects
        the wildcard anyway."""
        monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
        assert "*" not in config.cors_allow_origins()


class TestDefaultRole:
    def test_unset_is_admin(self, monkeypatch):
        """The dev convenience `current_user` has always provided. Changing this
        default would break /docs, a bare curl, and the existing suite."""
        monkeypatch.delenv("AUTH_DEFAULT_ROLE", raising=False)
        assert config.default_role() is Role.ADMIN

    def test_auditor_can_be_made_the_anonymous_default(self, monkeypatch):
        monkeypatch.setenv("AUTH_DEFAULT_ROLE", "auditor")
        assert config.default_role() is Role.AUDITOR

    def test_the_value_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("AUTH_DEFAULT_ROLE", "  AUDITOR  ")
        assert config.default_role() is Role.AUDITOR

    def test_a_typo_raises_rather_than_serving_traffic(self, monkeypatch):
        """The failure a silent fallback would produce here is "everyone got
        admin", which is the one outcome that must never happen quietly — the
        same reasoning the X-User-Role parsing already uses."""
        monkeypatch.setenv("AUTH_DEFAULT_ROLE", "adminn")
        with pytest.raises(RuntimeError, match="not a recognized role"):
            config.default_role()


_RUN = {
    "agent_id": "agent-01",
    "phase": "good",
    "invoice_count": 1,
    "seed": 1,
    "reason": "default role wiring check",
}


class TestTheDefaultRoleIsActuallyWired:
    """`config.default_role()` being right is not the same as the API using it.
    These go through a real request."""

    def test_an_anonymous_request_is_admin_by_default(self, client, monkeypatch):
        monkeypatch.delenv("AUTH_DEFAULT_ROLE", raising=False)
        resp = client.post("/api/v1/simulation/runs", json=_RUN)
        assert resp.status_code != 403, "the local ADMIN default must be unchanged"

    def test_an_anonymous_mutation_is_refused_when_the_default_is_auditor(
        self, client, monkeypatch
    ):
        """The deployment posture: no header, no writes."""
        monkeypatch.setenv("AUTH_DEFAULT_ROLE", "auditor")
        resp = client.post("/api/v1/simulation/runs", json=_RUN)
        assert resp.status_code == 403, resp.text

    def test_an_anonymous_read_still_works_when_the_default_is_auditor(
        self, client, monkeypatch
    ):
        """AUDITOR is read-only, not locked out — the dashboard's read paths must
        still answer so a deployed demo is viewable."""
        monkeypatch.setenv("AUTH_DEFAULT_ROLE", "auditor")
        assert client.get("/api/v1/agents").status_code == 200

    def test_an_explicit_admin_header_still_wins(self, client, monkeypatch):
        """The honest limit of the mitigation, pinned as a test so nobody
        mistakes it for authentication: a caller that *claims* to be an admin is
        still an admin."""
        monkeypatch.setenv("AUTH_DEFAULT_ROLE", "auditor")
        resp = client.post(
            "/api/v1/simulation/runs",
            headers={"X-User-Role": "admin"},
            json=_RUN,
        )
        assert resp.status_code != 403


def test_the_app_serves_the_configured_cors_origin(monkeypatch):
    """End to end through the middleware: a preflight from a configured origin
    is allowed and one from an unknown origin is not. The app reads origins at
    import time, so this rebuilds the module."""
    import importlib

    from fastapi.testclient import TestClient

    import app.main

    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://panel.vercel.app")
    fresh = importlib.reload(app.main)
    try:
        with TestClient(fresh.app) as c:
            allowed = c.options(
                "/api/v1/agents",
                headers={
                    "Origin": "https://panel.vercel.app",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert (
                allowed.headers.get("access-control-allow-origin")
                == "https://panel.vercel.app"
            )

            blocked = c.options(
                "/api/v1/agents",
                headers={
                    "Origin": "https://not-ours.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert "access-control-allow-origin" not in blocked.headers
    finally:
        # Leave the module as the rest of the suite expects to find it.
        monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
        importlib.reload(app.main)
