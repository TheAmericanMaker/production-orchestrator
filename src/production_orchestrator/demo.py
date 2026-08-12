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
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from production_orchestrator.persistence import SQLiteShopRepository
from production_orchestrator.restart_spike import _load_checkpoint
from production_orchestrator.spike import utc_now

_SCENARIO_ID = re.compile(r"[0-9a-f]{32}")
_MAX_REQUEST_BYTES = 4_096


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

    def create_scenario(self) -> dict[str, object]:
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
        if (
            checkpoint["provider"] != "deterministic"
            or checkpoint["model_id"] != "deterministic-apply-model"
            or checkpoint["aws_profile"] is not None
            or checkpoint["aws_region"] is not None
            or (
                not report_path.is_file()
                and (
                    checkpoint["initial_domain_digest"] != current_digest
                    or checkpoint["digest_at_interrupt"] != current_digest
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
            "state": asdict(state),
            "proposal": asdict(proposal),
            "checkpoint": {
                "first_stop_reason": checkpoint["first_stop_reason"],
                "interrupt_id": checkpoint["interrupt_id"],
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
            event_types
            == ["scenario_initialized", "proposal_created", "approval_granted", "plan_applied"],
        )
        rejection_checks = (
            state_revision == 1,
            repository.domain_digest() == checkpoint.get("initial_domain_digest"),
            not applied,
            event_types == ["scenario_initialized", "proposal_created", "approval_rejected"],
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
        )
        return self.get_scenario(scenario_id)


def render_app() -> str:
    return _APP_HTML


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
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
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
                    self._send_json(HTTPStatus.CREATED, controller.create_scenario())
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


_APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Production Orchestrator — Approval Console</title>
<style>
:root{color-scheme:dark;--bg:#08090a;--panel:#0f1011;--surface:#191a1b;--line:rgba(255,255,255,.08);--text:#f7f8f8;--sub:#d0d6e0;--muted:#8a8f98;--dim:#62666d;--accent:#7170ff;--accent2:#828fff;--ok:#10b981;--danger:#f87171;--warn:#fbbf24}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 50% -20%,rgba(113,112,255,.15),transparent 38%),var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;font-feature-settings:"cv01","ss03"}button{font:inherit}.shell{max-width:1200px;margin:auto;padding:0 28px 64px}.topbar{height:68px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line)}.brand{display:flex;gap:11px;align-items:center;font-size:14px;font-weight:600}.mark{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,var(--accent),#4b4abb);display:grid;place-items:center;box-shadow:0 0 24px rgba(113,112,255,.25)}.mark:after{content:"P";font-size:13px}.local{color:var(--muted);font:12px ui-monospace,monospace;border:1px solid var(--line);border-radius:999px;padding:5px 9px}.hero{padding:68px 0 38px;max-width:820px}.eyebrow{color:var(--accent2);text-transform:uppercase;font:600 11px ui-monospace,monospace;letter-spacing:.12em}.hero h1{font-size:clamp(38px,6vw,64px);line-height:1;letter-spacing:-1.4px;font-weight:510;margin:17px 0}.hero p{font-size:17px;line-height:1.65;color:var(--muted);max-width:690px}.phasebar{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;border:1px solid var(--line);background:var(--line);border-radius:10px;overflow:hidden;margin:12px 0 24px}.step{background:var(--panel);padding:13px 15px;color:var(--dim);font-size:13px}.step b{display:block;color:var(--sub);font:500 11px ui-monospace,monospace;margin-bottom:5px}.step.active{background:rgba(113,112,255,.11);color:var(--text)}.step.active b{color:var(--accent2)}.grid{display:grid;grid-template-columns:1.05fr .95fr;gap:16px}.card{background:rgba(255,255,255,.025);border:1px solid var(--line);border-radius:12px;padding:20px;min-width:0}.card.wide{grid-column:1/-1}.cardhead{display:flex;justify-content:space-between;gap:16px;align-items:start;margin-bottom:18px}.card h2{font-size:16px;margin:0;font-weight:590}.caption{color:var(--muted);font-size:12px;margin-top:5px}.pill{border:1px solid var(--line);border-radius:999px;padding:5px 9px;font:500 11px ui-monospace,monospace;color:var(--sub)}.pill.ok{color:var(--ok);border-color:rgba(16,185,129,.28)}.pill.warn{color:var(--warn);border-color:rgba(251,191,36,.25)}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.stat{background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.05);padding:13px;border-radius:8px}.stat span{display:block;color:var(--dim);font-size:11px;margin-bottom:7px}.stat strong{font:500 18px ui-monospace,monospace}.list{display:grid;gap:8px}.row{display:flex;justify-content:space-between;gap:14px;padding:11px 12px;border:1px solid rgba(255,255,255,.05);border-radius:7px;background:rgba(255,255,255,.015);font-size:13px}.row span:last-child{text-align:right;color:var(--muted)}.blocker{border-left:2px solid var(--warn)}.hash{font:12px ui-monospace,monospace;color:var(--accent2);overflow-wrap:anywhere}.actions{display:flex;gap:10px;margin-top:18px}.btn{min-height:44px;padding:0 17px;border-radius:7px;border:1px solid var(--line);background:rgba(255,255,255,.035);color:var(--text);cursor:pointer;font-weight:510}.btn:hover{background:rgba(255,255,255,.07)}.btn.primary{background:#5e6ad2;border-color:#7170ff}.btn.primary:hover{background:#7170ff}.btn:disabled{opacity:.45;cursor:not-allowed}.btn:focus-visible{outline:3px solid rgba(130,143,255,.55);outline-offset:2px}.notice{padding:13px 14px;border:1px solid var(--line);border-radius:8px;color:var(--sub);font-size:13px;line-height:1.5}.notice.ok{border-color:rgba(16,185,129,.3);background:rgba(16,185,129,.06)}.notice.danger{border-color:rgba(248,113,113,.3);background:rgba(248,113,113,.06)}.timeline{display:grid;gap:0}.event{display:grid;grid-template-columns:32px 1fr;gap:10px;min-height:49px}.event i{width:9px;height:9px;background:var(--accent);border-radius:50%;margin:5px auto;box-shadow:0 0 0 5px rgba(113,112,255,.1)}.event div{border-left:1px solid var(--line);padding:0 0 18px 17px;color:var(--sub);font-size:13px}.event:last-child div{border-color:transparent}.event small{display:block;color:var(--dim);font:11px ui-monospace,monospace;margin-top:4px}.empty{padding:42px;text-align:center;color:var(--muted)}.spinner{width:18px;height:18px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;display:inline-block;vertical-align:middle;margin-right:9px}@keyframes spin{to{transform:rotate(360deg)}}@media (max-width:720px){.shell{padding:0 16px 42px}.hero{padding-top:46px}.grid{grid-template-columns:1fr}.card.wide{grid-column:auto}.phasebar{grid-template-columns:1fr}.stats{grid-template-columns:1fr}.actions{flex-direction:column}.btn{width:100%}.local{display:none}}
</style>
</head>
<body><div class="shell"><header class="topbar"><div class="brand"><span class="mark" aria-hidden="true"></span>Production Orchestrator</div><span class="local">LOCAL · SYNTHETIC DATA</span></header><main><section class="hero"><div class="eyebrow">Human-gated production agent</div><h1>One plan. Exact approval. Auditable outcome.</h1><p>Inspect the shop before mutation, review the hash-bound proposal at a real Strands interrupt, then approve or reject across a fresh process boundary.</p></section><div id="app" aria-live="polite"><div class="empty"><span class="spinner"></span>Starting process A and preparing the exact proposal…</div></div></main></div>
<script>
const app=document.getElementById('app');let scenario=null;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const rows=items=>items.map(x=>`<div class="row"><span>${esc(x[0])}</span><span>${esc(x[1])}</span></div>`).join('');
function render(data){scenario=data;const p=data.proposal,s=data.state,r=data.report,done=data.phase!=='pending';const approved=data.phase==='approved';app.innerHTML=`
<div class="phasebar"><div class="step active"><b>01 · BEFORE</b>Revision ${esc(p.base_revision)} inspected</div><div class="step active"><b>02 · INTERRUPT</b>${done?'Proposal decision recorded':'Exact proposal pending'}</div><div class="step ${done?'active':''}"><b>03 · AFTER</b>${done?esc(data.phase):'Awaiting human decision'}</div></div>
<div class="grid"><section class="card"><div class="cardhead"><div><h2>${done?'Shop outcome':'Shop snapshot'}</h2><div class="caption">${done?'Final deterministic state':'Deterministic facts before approval'}</div></div><span class="pill">REV ${esc(s.revision)}</span></div><div class="stats"><div class="stat"><span>Active orders</span><strong>${Object.keys(s.orders).length}</strong></div><div class="stat"><span>Red thread</span><strong>${esc(s.inventory['THREAD-RED-40'])}</strong></div><div class="stat"><span>Machine hours</span><strong>8h</strong></div></div><div class="list" style="margin-top:9px">${rows(Object.values(s.orders).map(o=>[o.order_id,`Priority ${o.priority} · ${o.duration_hours}h`]))}</div></section>
<section class="card"><div class="cardhead"><div><h2>Detected blockers</h2><div class="caption">Calculated by deterministic tools</div></div><span class="pill warn">${p.evidence.length} BLOCKERS</span></div><div class="list">${p.evidence.map(b=>`<div class="row blocker"><span>${esc(b.kind.replaceAll('_',' '))}</span><span>Need ${esc(b.required)} · have ${esc(b.available)}</span></div>`).join('')}</div></section>
<section class="card wide"><div class="cardhead"><div><h2>Exact proposal at interrupt</h2><div class="caption">Base revision ${esc(p.base_revision)} · proposal ${esc(p.proposal_id)}</div></div><span class="pill ${done?'ok':'warn'}">${done?'DECIDED':'HUMAN REQUIRED'}</span></div><div class="hash">${esc(p.content_hash)}</div><div class="grid" style="margin-top:16px"><div><div class="caption">Schedule changes</div><div class="list">${rows(p.schedule_changes.map(c=>[c.order_id,`${c.from_day||'unscheduled'} → ${c.to_day}`]))}</div></div><div><div class="caption">Procurement</div><div class="list">${rows(p.procurement_actions.map(a=>[a.material_id,`${a.quantity} units`]))}</div></div></div>${done?outcome(data,approved):`<div class="actions"><button type="button" class="btn" onclick="decide('reject')">Reject plan</button><button type="button" class="btn primary" onclick="decide('approve')">Approve exact plan</button></div>`}</section>
<section class="card"><div class="cardhead"><div><h2>Communication drafts</h2><div class="caption">Prepared, not sent</div></div><span class="pill">${p.communication_drafts.length} DRAFTS</span></div><div class="list">${rows(p.communication_drafts.map(d=>[d.audience,d.subject]))}</div></section>
<section class="card"><div class="cardhead"><div><h2>Audit chain</h2><div class="caption">Immutable application events</div></div><span class="pill">${data.audit.length} EVENTS</span></div><div class="timeline">${data.audit.map((e,i)=>`<div class="event"><i></i><div>${esc(e.event_type.replaceAll('_',' '))}<small>#${esc(e.sequence)} · ${esc(e.proposal_hash?.slice(0,12)||'domain')}</small></div></div>`).join('')}</div></section></div>`}
function outcome(d,approved){const r=d.report;return `<div class="notice ${approved?'ok':'danger'}" style="margin-top:18px"><strong>${approved?'Approved and applied exactly once':'Rejected with no domain mutation'}</strong><br>Process ${esc(r.start_process_id)} → ${esc(r.resume_process_id)} · final revision ${esc(r.final_state_revision)} · applications ${esc(r.plan_applied_count)}</div><div class="actions"><button type="button" class="btn" onclick="newScenario()">Run another scenario</button></div>`}
async function request(path,options){const response=await fetch(path,options);const body=await response.json();if(!response.ok)throw new Error(body.error||'request_failed');return body}
async function newScenario(){app.innerHTML='<div class="empty"><span class="spinner"></span>Starting process A and preparing the exact proposal…</div>';try{render(await request('/api/scenarios',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}))}catch(e){app.innerHTML='<div class="notice danger">The scenario failed closed. Restart the local demo and try again.</div>'}}
async function decide(decision){document.querySelectorAll('button').forEach(b=>b.disabled=true);try{render(await request(`/api/scenarios/${scenario.scenario_id}/decision`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision})}))}catch(e){app.innerHTML='<div class="notice danger"><strong>The decision outcome is uncertain.</strong><br>Further decisions are locked. Inspect the local audit state before taking any other action.</div>'}}
newScenario();
</script></body></html>"""


if __name__ == "__main__":
    main()
