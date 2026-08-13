"""Contract tests for the Bedrock AgentCore Runtime adapter.

AgentCore Runtime requires a container listening on 0.0.0.0:8080 that
answers `GET /ping` and `POST /invocations`, with session continuity
carried by the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header.
These tests drive the real HTTP handler with the deterministic workflow
provider, so they need no AWS account. They prove the contract and the
approval mapping; they do not prove a deployment.
"""

import json
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path

import pytest

from production_orchestrator import agentcore_app

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
SESSION = "session-0123456789abcdef"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[int]:
    httpd = agentcore_app.build_server("127.0.0.1", 0, root=tmp_path / "sessions")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(
    port: int,
    method: str,
    path: str,
    payload: dict | None = None,
    session: str | None = SESSION,
) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", port, timeout=180)
    headers = {"Content-Type": "application/json"}
    if session is not None:
        headers[SESSION_HEADER] = session
    body = json.dumps(payload).encode() if payload is not None else None
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, (json.loads(raw) if raw else {})


def test_ping_reports_healthy(server: int) -> None:
    status, body = _request(server, "GET", "/ping", session=None)

    assert status == 200
    assert body["status"] == "Healthy"


def test_start_invocation_returns_a_proposal_awaiting_approval(server: int) -> None:
    status, body = _request(server, "POST", "/invocations", {"action": "start"})

    assert status == 200
    assert body["phase"] == "awaiting_approval"
    assert len(body["proposal_hash"]) == 64
    assert body["state_revision"] == 1
    assert body["plan_applied_count"] == 0
    assert body["audit_event_types"][:2] == ["scenario_initialized", "request_intake"]
    assert body["blockers"]
    assert body["session_id"] == SESSION


def test_rejection_invocation_leaves_the_shop_unchanged(server: int) -> None:
    _, started = _request(server, "POST", "/invocations", {"action": "start"})

    status, body = _request(
        server,
        "POST",
        "/invocations",
        {"action": "decide", "decision": "reject", "proposal_hash": started["proposal_hash"]},
    )

    assert status == 200
    assert body["phase"] == "rejected"
    assert body["state_revision"] == 1
    assert body["plan_applied_count"] == 0
    assert body["process_boundary_proven"] is True
    assert body["start_process_id"] != body["resume_process_id"]


def test_approval_invocation_applies_the_reviewed_plan_once(server: int) -> None:
    _, started = _request(server, "POST", "/invocations", {"action": "start"})

    status, body = _request(
        server,
        "POST",
        "/invocations",
        {"action": "decide", "decision": "approve", "proposal_hash": started["proposal_hash"]},
    )

    assert status == 200
    assert body["phase"] == "approved"
    assert body["state_revision"] == 2
    assert body["plan_applied_count"] == 1
    assert body["proposal_hash"] == started["proposal_hash"]


def test_decision_bound_to_a_different_hash_fails_closed(server: int) -> None:
    _request(server, "POST", "/invocations", {"action": "start"})

    status, body = _request(
        server,
        "POST",
        "/invocations",
        {"action": "decide", "decision": "approve", "proposal_hash": "f" * 64},
    )

    assert status == 409
    assert "hash" in body["error"].lower()

    _, state = _request(server, "POST", "/invocations", {"action": "status"})
    assert state["phase"] == "awaiting_approval"
    assert state["plan_applied_count"] == 0


def test_second_decision_on_the_same_session_fails_closed(server: int) -> None:
    _, started = _request(server, "POST", "/invocations", {"action": "start"})
    payload = {
        "action": "decide",
        "decision": "approve",
        "proposal_hash": started["proposal_hash"],
    }
    _request(server, "POST", "/invocations", payload)

    status, body = _request(server, "POST", "/invocations", payload)

    assert status == 409
    assert "decided" in body["error"].lower()


def test_deciding_in_one_session_does_not_touch_another(server: int) -> None:
    """Same scenario, so both sessions hold the same canonical proposal hash —
    the shop state behind them must still move independently."""

    other = "session-fedcba9876543210"
    _, first = _request(server, "POST", "/invocations", {"action": "start"}, session=SESSION)
    _, second = _request(server, "POST", "/invocations", {"action": "start"}, session=other)
    assert first["proposal_hash"] == second["proposal_hash"]

    status, decided = _request(
        server,
        "POST",
        "/invocations",
        {"action": "decide", "decision": "approve", "proposal_hash": second["proposal_hash"]},
        session=other,
    )

    assert status == 200
    assert decided["state_revision"] == 2

    _, first_state = _request(server, "POST", "/invocations", {"action": "status"}, session=SESSION)
    assert first_state["phase"] == "awaiting_approval"
    assert first_state["state_revision"] == 1
    assert first_state["plan_applied_count"] == 0


def test_a_hash_from_another_scenario_is_refused(server: int) -> None:
    other = "session-fedcba9876543210"
    _, first = _request(server, "POST", "/invocations", {"action": "start"}, session=SESSION)
    _, second = _request(
        server,
        "POST",
        "/invocations",
        {"action": "start", "scenario": "team-jerseys"},
        session=other,
    )
    assert first["proposal_hash"] != second["proposal_hash"]

    status, body = _request(
        server,
        "POST",
        "/invocations",
        {"action": "decide", "decision": "approve", "proposal_hash": second["proposal_hash"]},
        session=SESSION,
    )

    assert status == 409
    assert "hash" in body["error"].lower()


def test_status_before_start_is_not_found(server: int) -> None:
    status, body = _request(server, "POST", "/invocations", {"action": "status"})

    assert status == 404
    assert "session" in body["error"].lower()


def test_unknown_action_is_rejected(server: int) -> None:
    status, body = _request(server, "POST", "/invocations", {"action": "apply_everything"})

    assert status == 400
    assert "action" in body["error"].lower()


def test_missing_session_header_is_rejected(server: int) -> None:
    status, body = _request(server, "POST", "/invocations", {"action": "start"}, session=None)

    assert status == 400
    assert "session" in body["error"].lower()


@pytest.mark.parametrize(
    "session",
    ["../escape", "with/slash", "with space", "a" * 300, "", "sémicolon"],
)
def test_hostile_session_ids_are_rejected(server: int, session: str) -> None:
    status, body = _request(server, "POST", "/invocations", {"action": "start"}, session=session)

    assert status == 400
    assert "session" in body["error"].lower()


def test_unknown_path_is_not_found(server: int) -> None:
    status, _ = _request(server, "POST", "/admin", {"action": "start"})

    assert status == 404


def test_start_twice_in_one_session_fails_closed(server: int) -> None:
    _request(server, "POST", "/invocations", {"action": "start"})

    status, body = _request(server, "POST", "/invocations", {"action": "start"})

    assert status == 409
    assert "already" in body["error"].lower()


def test_oversized_body_is_rejected(server: int) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=30)
    connection.request(
        "POST",
        "/invocations",
        body=json.dumps({"action": "start", "padding": "x" * 8192}).encode(),
        headers={"Content-Type": "application/json", SESSION_HEADER: SESSION},
    )
    response = connection.getresponse()
    response.read()
    connection.close()

    assert response.status == 413
