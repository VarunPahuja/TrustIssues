"""
simulator/api_client.py
------------------------
HTTP client that talks to the backend API.

THE CRITICAL RULE:
  The simulator NEVER writes directly to the database.
  Every invoice passes through this client → the real API → the policy engine.
  This ensures the charts on the dashboard are built from data that actually
  passed through the governance layer, which is the whole demo.

DESIGN:
  - Uses httpx.Client (synchronous — no async complexity for a CLI tool)
  - JWT auth injected from environment or constructor argument
  - Retry with exponential backoff for rate-limit errors (429)
  - All money amounts travel as strings (matching the shared contract)
"""

from __future__ import annotations

import os
import sys
import time
from decimal import Decimal
from typing import Self

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import httpx
from shared.enums import Action

from simulator.constants import DEFAULT_API_BASE_URL, DEFAULT_API_VERSION
from simulator.models import AgentOutcome, Invoice


class APIClient:
    """
    Typed HTTP client for the Earned Autonomy Engine backend API.

    Args:
        base_url:   Backend base URL, e.g. "http://localhost:8000"
        jwt_token:  Bearer token (falls back to API_JWT_TOKEN env var)
        timeout:    Per-request timeout in seconds
        max_retries: Number of retry attempts on 429/503 responses
    """

    def __init__(
        self,
        base_url: str = DEFAULT_API_BASE_URL,
        jwt_token: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_prefix = f"/api/{DEFAULT_API_VERSION}"
        self._max_retries = max_retries

        resolved_token = jwt_token or os.environ.get("API_JWT_TOKEN")
        headers = {"Content-Type": "application/json"}
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"

        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_decision(
        self,
        invoice: Invoice,
        outcome: AgentOutcome,
        agent_id: str,
        reason: str,
        recommended_action: Action | None = None,
    ) -> dict:
        """
        POST /api/v1/decisions
        Submit a decision (agent action + ground truth) to the backend.

        Args:
            invoice:            The Invoice object (source of ground_truth_decision and amount)
            outcome:            The AgentOutcome with the agent's decision
            agent_id:           Agent identifier
            reason:             Why this decision is being submitted (e.g., simulation run ID)
            recommended_action: What the agent would have done had its limit not
                                stopped it. Only meaningful when the action is
                                ESCALATE; the backend rejects ESCALATE here, and
                                `human_agreement` needs it alongside a later
                                ruling before the pair counts.

        Returns:
            Backend's DecisionRecordOut response.
        """
        body = {
            "invoice_id": invoice.invoice_id,
            "amount": int(Decimal(invoice.amount)),
            "action": outcome.action.value,
            "ground_truth": invoice.ground_truth_decision.value,
            "agent_id": agent_id,
            "reason": reason,
        }
        if recommended_action is not None:
            body["recommended_action"] = recommended_action.value
        data = self._post(
            f"{self.api_prefix}/decisions",
            body,
        )
        return data

    def submit_ruling(self, decision_id: str, ruling: Action, reason: str) -> dict:
        """POST /api/v1/decisions/{decision_id}/ruling

        Records a human's verdict on an escalated decision. Together with the
        `recommended_action` sent at ingest, this is what gives
        `human_agreement` live data instead of the trust engine dropping the
        component and renormalising the other three weights.
        """
        return self._post(
            f"{self.api_prefix}/decisions/{decision_id}/ruling",
            {"ruling": ruling.value, "reason": reason},
        )

    def get_agent_status(self, agent_id: str) -> dict:
        """GET /api/v1/agents/{agent_id}"""
        data = self._get(f"{self.api_prefix}/agents/{agent_id}")
        return data

    def health_check(self) -> bool:
        """GET /health — returns True if the backend is reachable."""
        try:
            resp = self._client.get("/health", timeout=5.0)
            return resp.status_code < 500
        except Exception:  # noqa: BLE001 - unreachable backend of any kind means unhealthy
            return False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def _post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, json=body)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._client.request(method, path, **kwargs)
                if resp.status_code == 429:
                    # Rate limited — back off
                    wait = 2 ** attempt
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"API error {exc.response.status_code} on {method} {path}: "
                    f"{exc.response.text}"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - any transport error is worth a retry
                last_exc = exc
                if attempt < self._max_retries:
                    time.sleep(2 ** attempt)
        raise RuntimeError(
            f"API request failed after {self._max_retries} attempts: {last_exc}"
        )
