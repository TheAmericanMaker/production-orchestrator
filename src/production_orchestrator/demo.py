import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from production_orchestrator.fixtures import SCENARIOS
from production_orchestrator.persistence import SQLiteShopRepository
from production_orchestrator.restart_spike import (
    WORKFLOW_MODEL_ID,
    WORKFLOW_PROVIDER,
    _load_checkpoint,
)
from production_orchestrator.spike import utc_now

_SCENARIO_ID = re.compile(r"[0-9a-f]{32}")
_MAX_REQUEST_BYTES = 4_096

_STATIC = resources.files("production_orchestrator").joinpath("static")
_STATIC_ASSETS = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}

_PENDING_EVENTS = (
    "scenario_initialized",
    "active_orders_read",
    "inventory_read",
    "machine_capacity_read",
    "blockers_analyzed",
    "proposal_created",
    "communications_drafted",
)
_APPROVED_EVENTS = (*_PENDING_EVENTS, "approval_granted", "plan_applied")
_REJECTED_EVENTS = (*_PENDING_EVENTS, "approval_rejected")


def _scenario_public(name: str) -> dict[str, str]:
    spec = SCENARIOS[name]
    return {
        "name": spec.name,
        "title": spec.title,
        "question": spec.question,
        "summary": spec.summary,
        "target_order_id": spec.target_order_id,
    }


def demo_meta() -> dict[str, object]:
    return {
        "scenarios": [_scenario_public(name) for name in SCENARIOS],
        "provider": {
            "provider": WORKFLOW_PROVIDER,
            "model_id": WORKFLOW_MODEL_ID,
            "strands_agents_version": version("strands-agents"),
        },
    }


class ReportVerificationError(RuntimeError):
    """Persisted completion evidence does not prove a valid outcome."""


class DemoController:
    """Run the validated restart proof and expose judge-readable state."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _scenario_dir(self, scenario_id: str) -> Path:
        if _SCENARIO_ID.fullmatch(scenario_id) is None:
            raise ValueError("Invalid scenario ID")
        return self.root / scenario_id

    @staticmethod
    def _run_phase(*arguments: str) -> None:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "production_orchestrator.restart_spike", *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("The local agent process timed out and failed closed") from error
        if result.returncode != 0:
            raise RuntimeError("The local agent process failed closed")

    def create_scenario(self, scenario: str = "rush-order") -> dict[str, object]:
        if scenario not in SCENARIOS:
            raise ValueError("Invalid scenario name")
        scenario_id = uuid4().hex
        scenario_dir = self._scenario_dir(scenario_id)
        scenario_dir.mkdir(mode=0o700)
        runtime_dir = scenario_dir / "runtime"
        checkpoint_path = scenario_dir / "checkpoint.json"
        self._run_phase(
            "start",
            "--runtime-dir",
            str(runtime_dir),
            "--checkpoint",
            str(checkpoint_path),
            "--provider",
            WORKFLOW_PROVIDER,
            "--model",
            WORKFLOW_MODEL_ID,
            "--scenario",
            scenario,
        )
        return self.get_scenario(scenario_id)

    def get_scenario(self, scenario_id: str) -> dict[str, object]:
        scenario_dir = self._scenario_dir(scenario_id)
        checkpoint_path = scenario_dir / "checkpoint.json"
        if not checkpoint_path.is_file():
            raise FileNotFoundError("Unknown scenario")
        checkpoint = _load_checkpoint(checkpoint_path)
        repository = SQLiteShopRepository(scenario_dir / "runtime" / "shop.db", clock=utc_now)
        proposal_hash = str(checkpoint["proposal_hash"])
        proposal = repository.load_proposal(proposal_hash)
        if proposal is None:
            raise RuntimeError("Persisted scenario proposal is missing")
        state = repository.load_state()
        audit = repository.audit_events()
        current_digest = repository.domain_digest()
        report_path = scenario_dir / "report.json"
        event_types = [event.event_type for event in audit]
        if (
            checkpoint["provider"] != WORKFLOW_PROVIDER
            or checkpoint["model_id"] != WORKFLOW_MODEL_ID
            or checkpoint["aws_profile"] is not None
            or checkpoint["aws_region"] is not None
            or checkpoint["scenario"] not in SCENARIOS
            or (
                not report_path.is_file()
                and (
                    checkpoint["initial_domain_digest"] != current_digest
                    or checkpoint["digest_at_interrupt"] != current_digest
                    or event_types != list(_PENDING_EVENTS)
                )
            )
        ):
            raise RuntimeError("Pending checkpoint failed demo verification")
        report = json.loads(report_path.read_text()) if report_path.is_file() else None
        if report is not None:
            self._verify_report(
                report, checkpoint, repository, proposal_hash, state.revision, audit
            )
        decision_value = report.get("decision") if isinstance(report, dict) else None
        decision = decision_value if isinstance(decision_value, str) else None
        phase = (
            "approved"
            if decision == "approve"
            else "rejected"
            if decision == "reject"
            else "pending"
        )
        return {
            "scenario_id": scenario_id,
            "phase": phase,
            "scenario": _scenario_public(str(checkpoint["scenario"])),
            "state": asdict(state),
            "proposal": asdict(proposal),
            "checkpoint": {
                "first_stop_reason": checkpoint["first_stop_reason"],
                "interrupt_id": checkpoint["interrupt_id"],
                "interrupt_name": checkpoint["interrupt_name"],
                "proposal_hash": proposal_hash,
                "provider": checkpoint["provider"],
                "model_id": checkpoint["model_id"],
                "start_process_id": checkpoint["start_process_id"],
            },
            "report": report,
            "audit": [asdict(event) for event in audit],
        }

    @staticmethod
    def _verify_report(
        report: object,
        checkpoint: dict[str, object],
        repository: SQLiteShopRepository,
        proposal_hash: str,
        state_revision: int,
        audit: Sequence[object],
    ) -> None:
        if not isinstance(report, dict):
            raise ReportVerificationError("Completion report failed verification")
        decision = report.get("decision")
        event_types = [getattr(event, "event_type", None) for event in audit]
        applied = [event for event in audit if getattr(event, "event_type", None) == "plan_applied"]
        common_checks = (
            decision in {"approve", "reject"},
            report.get("workflow_passed") is True,
            report.get("official_interrupt_response_used") is True,
            report.get("process_boundary_proven") is True,
            report.get("session_interrupt_restored") is True,
            report.get("final_stop_reason") == "end_turn",
            report.get("proposal_hash") == proposal_hash,
            report.get("interrupt_id") == checkpoint.get("interrupt_id"),
            report.get("provider") == checkpoint.get("provider"),
            report.get("model_id") == checkpoint.get("model_id"),
            report.get("scenario") == checkpoint.get("scenario"),
            report.get("aws_region") == checkpoint.get("aws_region"),
            report.get("start_process_id") == checkpoint.get("start_process_id"),
            isinstance(report.get("resume_process_id"), int),
            report.get("resume_process_id") != checkpoint.get("start_process_id"),
            report.get("final_state_revision") == state_revision,
            report.get("final_domain_digest") == repository.domain_digest(),
            report.get("plan_applied_count") == len(applied),
            report.get("audit_event_types") == event_types,
        )
        approval_checks = (
            state_revision == 2,
            len(applied) == 1,
            getattr(applied[0], "proposal_hash", None) == proposal_hash if applied else False,
            event_types == list(_APPROVED_EVENTS),
        )
        rejection_checks = (
            state_revision == 1,
            repository.domain_digest() == checkpoint.get("initial_domain_digest"),
            not applied,
            event_types == list(_REJECTED_EVENTS),
        )
        decision_checks = approval_checks if decision == "approve" else rejection_checks
        if not all((*common_checks, *decision_checks)):
            raise ReportVerificationError("Completion report failed verification")

    def decide(self, scenario_id: str, decision: str) -> dict[str, object]:
        if decision not in {"approve", "reject"}:
            raise ValueError("Invalid decision")
        scenario_dir = self._scenario_dir(scenario_id)
        checkpoint_path = scenario_dir / "checkpoint.json"
        if not checkpoint_path.is_file():
            raise FileNotFoundError("Unknown scenario")
        report_path = scenario_dir / "report.json"
        lock_path = scenario_dir / "decision.lock"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise RuntimeError("Scenario is already decided") from error
        with os.fdopen(descriptor, "w") as lock:
            lock.write(decision + "\n")
        self._run_phase(
            "resume",
            "--runtime-dir",
            str(scenario_dir / "runtime"),
            "--checkpoint",
            str(checkpoint_path),
            "--decision",
            decision,
            "--report",
            str(report_path),
            "--provider",
            WORKFLOW_PROVIDER,
            "--model",
            WORKFLOW_MODEL_ID,
        )
        return self.get_scenario(scenario_id)


def render_app() -> str:
    return _STATIC.joinpath("index.html").read_text(encoding="utf-8")


def _json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _handler_for(controller: DemoController) -> type[BaseHTTPRequestHandler]:
    class DemoHandler(BaseHTTPRequestHandler):
        server_version = "ProductionOrchestratorDemo/1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self) -> None:
            body = render_app().encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; object-src 'none'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, asset: str) -> None:
            content_type = _STATIC_ASSETS.get(asset)
            if content_type is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            body = _STATIC.joinpath(asset).read_text(encoding="utf-8").encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _read_payload(self) -> dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("Invalid request length") from error
            if length < 0 or length > _MAX_REQUEST_BYTES:
                raise ValueError("Invalid request length")
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as error:
                raise ValueError("Invalid JSON body") from error
            if not isinstance(payload, dict):
                raise TypeError("JSON body must be an object")
            return payload

        def _path_parts(self) -> list[str]:
            path = unquote(urlsplit(self.path).path)
            return [part for part in path.split("/") if part]

        def _require_local_browser_origin(self) -> None:
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                raise PermissionError("Cross-site requests are forbidden")
            origin = self.headers.get("Origin")
            if origin is None:
                return
            parsed = urlsplit(origin)
            request_host = self.headers.get("Host", "")
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.netloc.lower() != request_host.lower()
            ):
                raise PermissionError("Foreign origins are forbidden")

        def do_GET(self) -> None:
            try:
                parts = self._path_parts()
                if not parts:
                    self._send_html()
                elif len(parts) == 2 and parts[0] == "static":
                    self._send_static(parts[1])
                elif parts == ["api", "meta"]:
                    self._send_json(HTTPStatus.OK, demo_meta())
                elif len(parts) == 3 and parts[:2] == ["api", "scenarios"]:
                    self._send_json(HTTPStatus.OK, controller.get_scenario(parts[2]))
                elif parts[:2] == ["api", "scenarios"]:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            except (TypeError, ValueError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            except FileNotFoundError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            except (json.JSONDecodeError, KeyError, OSError, RuntimeError):
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

        def do_POST(self) -> None:
            try:
                self._require_local_browser_origin()
                parts = self._path_parts()
                payload = self._read_payload()
                if parts == ["api", "scenarios"]:
                    scenario = payload.get("scenario", "rush-order")
                    if not isinstance(scenario, str):
                        raise ValueError("Invalid scenario name")
                    self._send_json(HTTPStatus.CREATED, controller.create_scenario(scenario))
                elif (
                    len(parts) == 4 and parts[:2] == ["api", "scenarios"] and parts[3] == "decision"
                ):
                    decision = payload.get("decision")
                    if not isinstance(decision, str):
                        raise ValueError("Invalid decision")
                    self._send_json(HTTPStatus.OK, controller.decide(parts[2], decision))
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            except PermissionError:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            except (TypeError, ValueError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            except FileNotFoundError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            except RuntimeError as error:
                status = (
                    HTTPStatus.CONFLICT
                    if "already decided" in str(error)
                    else HTTPStatus.INTERNAL_SERVER_ERROR
                )
                self._send_json(
                    status, {"error": "conflict" if status == 409 else "internal_error"}
                )
            except (json.JSONDecodeError, KeyError, OSError):
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

    return DemoHandler


def build_server(host: str, port: int, controller: DemoController) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The demo server must bind to localhost")
    return ThreadingHTTPServer((host, port), _handler_for(controller))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Production Orchestrator demo")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost", "::1"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runtime-dir", type=Path, default=Path("data/demo-runtime"))
    args = parser.parse_args()
    server = build_server(args.host, args.port, DemoController(args.runtime_dir))
    print(f"Production Orchestrator demo listening on {args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
