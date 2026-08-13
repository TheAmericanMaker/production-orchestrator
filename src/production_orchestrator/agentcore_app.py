"""Bedrock AgentCore Runtime adapter for the governed production workflow.

AgentCore Runtime requires a container listening on `0.0.0.0:8080` that
answers `GET /ping` for health and `POST /invocations` for work, with
session continuity carried by the
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header.

## How the approval round-trip maps onto invocations — read this before
## claiming anything about it

Our approval boundary is a real Strands interrupt held between two
processes. AgentCore invocations are request/response, so the interrupt
cannot be held *inside* one invocation while a human thinks. It is held
in persisted state *between* two invocations of the same session:

1. `{"action": "start"}` runs the workflow to the interrupt, persists the
   immutable proposal and the checkpoint, and returns the proposal for
   review. Nothing beyond the sanctioned intake has mutated.
2. `{"action": "decide", ...}` reconstructs the session in a fresh
   process, re-verifies the checkpoint, submits the official
   `interruptResponse`, and either applies exactly the reviewed proposal
   once or refuses.

Each phase runs as a subprocess, so the process boundary the evidence
claims is real inside the container, not a figure of speech.

## What is ephemeral and what is durable

Session state (SQLite shop database, Strands session files, checkpoint,
report) lives under a per-session directory on the container filesystem.
AgentCore gives each session an isolated microVM, so that state survives
across invocations *within a session* and is destroyed with it. It is
**not** durable across sessions or beyond session expiry, and nothing here
should be described as durable cloud persistence. Making it survive that
boundary needs an external store, which this slice deliberately does not
add.

Session identifiers arrive in a header, i.e. from outside, so they are
validated against a strict allowlist before ever becoming a path segment.
"""

import argparse
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from production_orchestrator.demo import (
    DemoController,
    ProviderConfiguration,
    ReportVerificationError,
)
from production_orchestrator.fixtures import SCENARIOS
from production_orchestrator.spike import CONTAINER_ROLE_CREDENTIALS

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
DEFAULT_PORT = 8080
_MAX_REQUEST_BYTES = 4_096
# AgentCore session ids are opaque; accept only what is safe as one path
# segment and reject everything else rather than sanitising it.
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_ACTIONS = ("start", "decide", "status")


class InvocationError(Exception):
    """A rejected invocation, carrying the status the caller should see."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class AgentCoreService:
    """Map AgentCore invocations onto the verified two-phase controller."""

    def __init__(self, root: Path, configuration: ProviderConfiguration | None = None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.configuration = configuration or ProviderConfiguration()

    def _controller(self, session_id: str) -> DemoController:
        return DemoController(self.root / session_id, self.configuration)

    @staticmethod
    def _session_directory_name(session_id: str) -> str:
        if _SESSION_ID.fullmatch(session_id) is None:
            raise InvocationError(HTTPStatus.BAD_REQUEST, "Invalid or missing session identifier")
        return session_id

    def _scenario_pointer(self, session_id: str) -> Path:
        return self.root / session_id / "scenario_id"

    def invoke(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one invocation for one session."""

        directory = self._session_directory_name(session_id)
        action = payload.get("action")
        if action not in _ACTIONS:
            raise InvocationError(
                HTTPStatus.BAD_REQUEST,
                f"Unsupported action; expected one of {', '.join(_ACTIONS)}",
            )
        if action == "start":
            return self._start(directory, payload)
        if action == "status":
            return self._status(directory)
        return self._decide(directory, payload)

    def _start(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        pointer = self._scenario_pointer(session_id)
        if pointer.exists():
            raise InvocationError(
                HTTPStatus.CONFLICT,
                "This session has already started a workflow; use action 'status'",
            )
        scenario = payload.get("scenario", "rush-order")
        if scenario not in SCENARIOS:
            raise InvocationError(HTTPStatus.BAD_REQUEST, "Unknown scenario")
        state = self._controller(session_id).create_scenario(scenario)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(str(state["scenario_id"]))
        return self._public(session_id, state)

    def _status(self, session_id: str) -> dict[str, Any]:
        return self._public(session_id, self._load_state(session_id))

    def _decide(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        decision = payload.get("decision")
        if decision not in {"approve", "reject"}:
            raise InvocationError(HTTPStatus.BAD_REQUEST, "Decision must be 'approve' or 'reject'")
        reviewed_hash = payload.get("proposal_hash")
        if not isinstance(reviewed_hash, str) or not reviewed_hash:
            raise InvocationError(
                HTTPStatus.BAD_REQUEST, "Decision must name the reviewed proposal hash"
            )
        state = self._load_state(session_id)
        if state["phase"] != "pending":
            raise InvocationError(
                HTTPStatus.CONFLICT, "This proposal was already decided; decisions apply once"
            )
        # The decision must bind to the exact proposal the caller reviewed.
        # This is the outer half of the guarantee; the resume phase re-checks
        # the same binding against persisted evidence before any write.
        if reviewed_hash != state["checkpoint"]["proposal_hash"]:
            raise InvocationError(
                HTTPStatus.CONFLICT,
                "Decision hash does not match the pending proposal hash",
            )
        scenario_id = str(self._scenario_pointer(session_id).read_text().strip())
        try:
            decided = self._controller(session_id).decide(scenario_id, decision)
        except ReportVerificationError as error:
            raise InvocationError(HTTPStatus.CONFLICT, str(error)) from error
        except RuntimeError as error:
            raise InvocationError(HTTPStatus.CONFLICT, str(error)) from error
        return self._public(session_id, decided)

    def _load_state(self, session_id: str) -> dict[str, Any]:
        pointer = self._scenario_pointer(session_id)
        if not pointer.exists():
            raise InvocationError(
                HTTPStatus.NOT_FOUND, "No workflow for this session; invoke action 'start' first"
            )
        scenario_id = pointer.read_text().strip()
        try:
            return self._controller(session_id).get_scenario(scenario_id)
        except FileNotFoundError as error:
            raise InvocationError(HTTPStatus.NOT_FOUND, "Session state is missing") from error

    def _public(self, session_id: str, state: dict[str, Any]) -> dict[str, Any]:
        report = state.get("report") or {}
        audit = state.get("audit") or []
        domain = state.get("state") or {}
        checkpoint = state.get("checkpoint") or {}
        proposal = state.get("proposal") or {}
        applied = [event for event in audit if event.get("event_type") == "plan_applied"]
        # The controller's internal "pending" reads as ambiguous over an API;
        # name the state for what it is: a write held for a human.
        phase = {"pending": "awaiting_approval"}.get(str(state["phase"]), str(state["phase"]))
        return {
            "session_id": session_id,
            "phase": phase,
            "scenario": state.get("scenario"),
            "proposal_hash": checkpoint.get("proposal_hash"),
            "blockers": [blocker.get("kind") for blocker in (proposal.get("evidence") or [])],
            "interrupt_id": checkpoint.get("interrupt_id"),
            "interrupt_name": checkpoint.get("interrupt_name"),
            "provider": checkpoint.get("provider"),
            "model_id": checkpoint.get("model_id"),
            "state_revision": domain.get("revision"),
            "plan_applied_count": len(applied),
            "audit_event_types": [event.get("event_type") for event in audit],
            "start_process_id": checkpoint.get("start_process_id"),
            "resume_process_id": report.get("resume_process_id"),
            "process_boundary_proven": report.get("process_boundary_proven"),
            "workflow_passed": report.get("workflow_passed"),
            "checkpoint": checkpoint,
        }


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _handler_for(service: AgentCoreService) -> type[BaseHTTPRequestHandler]:
    class AgentCoreHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "production-orchestrator-agentcore"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            # Session identifiers and payloads are request data; keep them out
            # of container logs rather than shipping them to CloudWatch.
            return

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/ping":
                self._respond(HTTPStatus.NOT_FOUND, {"error": "Unknown path"})
                return
            self._respond(HTTPStatus.OK, {"status": "Healthy"})

        def do_POST(self) -> None:
            if self.path != "/invocations":
                self._respond(HTTPStatus.NOT_FOUND, {"error": "Unknown path"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._respond(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
                return
            if length > _MAX_REQUEST_BYTES:
                self._respond(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request body too large"}
                )
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._respond(HTTPStatus.BAD_REQUEST, {"error": "Body must be JSON"})
                return
            if not isinstance(payload, dict):
                self._respond(HTTPStatus.BAD_REQUEST, {"error": "Body must be a JSON object"})
                return
            session_id = self.headers.get(SESSION_HEADER) or ""
            try:
                result = service.invoke(session_id, payload)
            except InvocationError as error:
                self._respond(error.status, {"error": error.message})
                return
            except Exception:  # noqa: BLE001 - never leak internals to the caller
                self._respond(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "The workflow failed closed; no plan was applied"},
                )
                return
            self._respond(HTTPStatus.OK, result)

    return AgentCoreHandler


def build_server(
    host: str,
    port: int,
    root: Path,
    configuration: ProviderConfiguration | None = None,
) -> ThreadingHTTPServer:
    """Build the AgentCore-contract HTTP server."""

    service = AgentCoreService(root, configuration)
    return ThreadingHTTPServer((host, port), _handler_for(service))


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the AgentCore Runtime contract")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--root", type=Path, default=Path("/tmp/production-orchestrator-sessions"))
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--aws-region", default=None)
    parser.add_argument("--aws-credential-source", default=None)
    args = parser.parse_args()

    defaults = ProviderConfiguration()
    configuration = ProviderConfiguration(
        provider=args.provider or defaults.provider,
        model_id=args.model or defaults.model_id,
        aws_region=args.aws_region,
        credential_source=args.aws_credential_source or defaults.credential_source,
    )
    server = build_server(args.host, args.port, args.root, configuration)
    print(f"AgentCore contract listening on {args.host}:{args.port}")
    print(f"provider={configuration.provider} model={configuration.model_id}")
    if configuration.credential_source == CONTAINER_ROLE_CREDENTIALS:
        print("credentials=container-role")
    server.serve_forever()


if __name__ == "__main__":
    main()
