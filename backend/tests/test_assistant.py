from __future__ import annotations

import pytest
from governance.llm.errors import LLMAuthError, LLMTransportError

from app.services import assistant_llm


@pytest.fixture(autouse=True)
def stub_mode(monkeypatch):
    """Every test in this file runs `ASSISTANT_MODE=stub` unless it overrides the
    mode explicitly — no test here should ever make a network call.
    """
    monkeypatch.setenv("ASSISTANT_MODE", "stub")


def _ask(client, headers, question: str, agent_id: str | None = None) -> dict:
    resp = client.post(
        "/api/v1/assistant/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": question}], "agent_id": agent_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_general_scope_returns_a_reply_with_sources(client, admin_headers):
    body = _ask(client, admin_headers, "why the Wilson lower bound instead of accuracy")
    assert body["reply"]
    assert isinstance(body["sources"], list)
    assert len(body["sources"]) > 0
    for source in body["sources"]:
        assert source["doc"]
        assert source["section"]
    # Stub mode echoes the assembled context; a Wilson-bound question should have
    # pulled in the ADR that argues for it, or the glossary entry that names it.
    assert "wilson" in body["reply"].lower()


def test_general_scope_reply_carries_no_agent_evidence_heading(client, admin_headers):
    body = _ask(client, admin_headers, "what stops the LLM from raising a limit on its own")
    assert "Agent evidence" not in body["reply"]


def test_agent_scope_fetches_only_the_named_agent(client, admin_headers):
    body = _ask(client, admin_headers, "why is this agent not eligible for an increase", agent_id="agent-01")
    assert "Agent evidence — agent-01" in body["reply"]
    # agent-01's own numbers (seeded in app/seed.py) should be present...
    assert "agent-01" in body["reply"]
    # ...and no other agent's id should ever appear in an agent-01-scoped reply.
    assert "agent-02" not in body["reply"]
    assert "agent-03" not in body["reply"]


def test_cross_agent_isolation_by_name(client, admin_headers):
    """Ask agent-01's assistant about agent-03 by name. The reply must contain
    none of agent-03's figures — not because the model was told to refuse, but
    because agent-03's data was never fetched into agent-01's context in the
    first place (app/services/assistant.py:build_agent_context).
    """
    question = "how does agent-03 compare to this one, what is agent-03's trust score"
    body = _ask(client, admin_headers, question, agent_id="agent-01")
    reply = body["reply"]

    # The question itself names agent-03 — quoting the user's own question back
    # is not a leak, so strip it before checking for agent-03's actual data.
    reply_minus_question = reply.replace(question, "")
    assert "agent-03" not in reply_minus_question
    assert "Agent evidence — agent-03" not in reply
    # app/seed.py's agent-03 trust evaluation: trust_score=41.2, state=RESTRICTED,
    # current_limit=1000 (rung 1), 3 critical errors in the recent window.
    assert "41.2" not in reply
    assert "restricted" not in reply.lower()
    assert "Invoice Agent — Marketing" not in reply  # agent-03's name
    # And agent-01's own evidence *is* present — this is a scoping test, not a
    # test that the assistant refuses to say anything.
    assert "Agent evidence — agent-01" in reply


def test_agent_scope_unknown_agent_returns_404(client, admin_headers):
    resp = client.post(
        "/api/v1/assistant/chat",
        headers=admin_headers,
        json={"messages": [{"role": "user", "content": "hello"}], "agent_id": "agent-does-not-exist"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "agent_not_found"


def test_stub_mode_makes_no_llm_call(client, admin_headers, monkeypatch):
    def _fail_build_client(*args, **kwargs):
        raise AssertionError("stub mode must never construct an LLM client")

    monkeypatch.setattr(assistant_llm, "build_client", _fail_build_client)
    body = _ask(client, admin_headers, "why the Wilson lower bound instead of accuracy")
    assert body["reply"].startswith("[stub mode")


def test_live_failure_falls_back_to_cached(client, admin_headers, monkeypatch, tmp_path):
    """A live call that fails (auth error, say) serves the recording for the same
    prompt instead of erroring — mirrors governance's own live-mode fallback
    (governance/governance/agents/llm_backed.py:opine_with_provenance).
    """
    monkeypatch.setenv("ASSISTANT_MODE", "live")

    class _FailingClient:
        provider = "gemini"
        model = "gemini-3.6-flash"
        slug = "gemini-3-6-flash"

        @property
        def has_key(self) -> bool:
            return False

        def generate_text(self, prompt, *, timeout_s=None):
            raise LLMAuthError("no key configured")

    monkeypatch.setattr(assistant_llm, "build_client", lambda *a, **k: _FailingClient())

    # Pre-seed a recording for exactly the prompt this question will produce.
    store = assistant_llm.RecordingStore(directory=tmp_path)

    from app.services import assistant as assistant_ctx

    context, _ = assistant_ctx.build_general_context("why the Wilson lower bound instead of accuracy")
    user_prompt = f"{context}\n\n## Question\nwhy the Wilson lower bound instead of accuracy"
    prompt = assistant_llm._build_prompt("general", assistant_ctx.SYSTEM_PROMPT, user_prompt)
    recording = assistant_llm.build_recording(
        prompt, "A cached, pre-recorded answer about Wilson bounds.", "gemini-3.6-flash",
        provider="gemini", model_slug="gemini-3-6-flash",
    )
    store.save(recording)

    result = assistant_llm.generate_reply(
        "general", assistant_ctx.SYSTEM_PROMPT, user_prompt, store=store
    )
    assert result.mode == "live+cached"
    assert result.text == "A cached, pre-recorded answer about Wilson bounds."


def test_live_failure_with_no_recording_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("ASSISTANT_MODE", "live")

    class _FailingClient:
        provider = "gemini"
        model = "gemini-3.6-flash"
        slug = "gemini-3-6-flash"
        has_key = False

        def generate_text(self, prompt, *, timeout_s=None):
            raise LLMTransportError("network unreachable")

    monkeypatch.setattr(assistant_llm, "build_client", lambda *a, **k: _FailingClient())
    store = assistant_llm.RecordingStore(directory=tmp_path)

    from governance.llm.errors import RecordingMissError

    with pytest.raises(RecordingMissError):
        assistant_llm.generate_reply("general", "sys", "user question", store=store)
