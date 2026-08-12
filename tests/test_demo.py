import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from production_orchestrator.demo import DemoController, build_server, render_app
from production_orchestrator.restart_spike import agent_id_for


def _request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object] | str]:
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            **({"Content-Type": "application/json"} if body is not None else {}),
            **(headers or {}),
        },
    )
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    content_type = response.headers.get_content_type()
    data = response.read().decode()
    return response.status, json.loads(data) if content_type == "application/json" else data


@pytest.mark.parametrize(
    ("decision", "expected_phase", "expected_revision", "expected_applied", "expected_event"),
    [
        ("reject", "rejected", 1, 0, "approval_rejected"),
        ("approve", "approved", 2, 1, "plan_applied"),
    ],
)
def test_controller_runs_real_before_interrupt_after_processes(
    tmp_path: Path,
    decision: str,
    expected_phase: str,
    expected_revision: int,
    expected_applied: int,
    expected_event: str,
) -> None:
    controller = DemoController(tmp_path)

    pending = controller.create_scenario()

    assert pending["phase"] == "pending"
    assert pending["state"]["revision"] == 1
    assert pending["proposal"]["base_revision"] == 1
    assert pending["proposal"]["content_hash"] == pending["checkpoint"]["proposal_hash"]
    assert {item["kind"] for item in pending["proposal"]["evidence"]} == {
        "inventory_shortage",
        "capacity_conflict",
    }
    assert len(pending["proposal"]["communication_drafts"]) == 3
    assert pending["checkpoint"]["first_stop_reason"] == "interrupt"

    completed = controller.decide(str(pending["scenario_id"]), decision)

    assert completed["phase"] == expected_phase
    assert completed["state"]["revision"] == expected_revision
    assert completed["report"]["plan_applied_count"] == expected_applied
    assert completed["report"]["workflow_passed"] is True
    assert completed["report"]["start_process_id"] != completed["report"]["resume_process_id"]
    expected_events = (
        ["scenario_initialized", "proposal_created", "approval_granted", "plan_applied"]
        if decision == "approve"
        else ["scenario_initialized", "proposal_created", "approval_rejected"]
    )
    completed_audit = completed["audit"]
    assert isinstance(completed_audit, list)
    assert [event["event_type"] for event in completed_audit] == expected_events
    assert expected_event in expected_events


def test_controller_refuses_missing_invalid_and_replayed_decisions(tmp_path: Path) -> None:
    controller = DemoController(tmp_path)
    pending = controller.create_scenario()
    scenario_id = str(pending["scenario_id"])

    with pytest.raises(ValueError, match="decision"):
        controller.decide(scenario_id, "maybe")
    with pytest.raises(ValueError, match="scenario"):
        controller.get_scenario("../escape")
    with pytest.raises(FileNotFoundError, match="scenario"):
        controller.get_scenario("0" * 32)

    controller.decide(scenario_id, "reject")
    with pytest.raises(RuntimeError, match="already decided"):
        controller.decide(scenario_id, "approve")


def test_controller_retains_decision_lock_after_uncertain_resume_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = DemoController(tmp_path)
    pending = controller.create_scenario()
    scenario_id = str(pending["scenario_id"])

    def fail_resume(*arguments: str) -> None:
        raise RuntimeError("uncertain process-B outcome")

    monkeypatch.setattr(controller, "_run_phase", fail_resume)

    with pytest.raises(RuntimeError, match="uncertain"):
        controller.decide(scenario_id, "approve")
    with pytest.raises(RuntimeError, match="already decided"):
        controller.decide(scenario_id, "reject")


def test_controller_rejects_unverified_or_tampered_completion_report(tmp_path: Path) -> None:
    controller = DemoController(tmp_path)
    pending = controller.create_scenario()
    scenario_id = str(pending["scenario_id"])
    controller.decide(scenario_id, "approve")
    report_path = tmp_path / scenario_id / "report.json"
    report = json.loads(report_path.read_text())
    checkpoint = pending["checkpoint"]
    assert isinstance(checkpoint, dict)

    report["workflow_passed"] = False
    report_path.write_text(json.dumps(report))
    with pytest.raises(RuntimeError, match="report"):
        controller.get_scenario(scenario_id)

    report["workflow_passed"] = True
    report["proposal_hash"] = "0" * 64
    report_path.write_text(json.dumps(report))
    with pytest.raises(RuntimeError, match="report"):
        controller.get_scenario(scenario_id)

    report["proposal_hash"] = str(checkpoint["proposal_hash"])
    report["plan_applied_count"] = 0
    report_path.write_text(json.dumps(report))
    with pytest.raises(RuntimeError, match="report"):
        controller.get_scenario(scenario_id)


def test_controller_rejects_tampered_pending_checkpoint_before_render(tmp_path: Path) -> None:
    controller = DemoController(tmp_path)
    pending = controller.create_scenario()
    scenario_id = str(pending["scenario_id"])
    checkpoint_path = tmp_path / scenario_id / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text())

    checkpoint["model_id"] = "forged-model"
    checkpoint["agent_id"] = agent_id_for(
        provider="deterministic",
        model_id="forged-model",
        proposal_hash=str(checkpoint["proposal_hash"]),
        aws_profile=None,
        aws_region=None,
    )
    checkpoint_path.write_text(json.dumps(checkpoint))

    with pytest.raises(RuntimeError, match="checkpoint"):
        controller.get_scenario(scenario_id)


def test_http_api_is_local_demo_boundary_and_fails_closed(tmp_path: Path) -> None:
    controller = DemoController(tmp_path)
    server = build_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        status, page = _request(base_url + "/")
        assert status == 200
        assert isinstance(page, str)
        assert 'id="app"' in page
        assert 'aria-live="polite"' in page
        assert "Approve coordinated plan" in page
        assert "Keep current schedule" in page

        status, cross_site = _request(
            base_url + "/api/scenarios",
            method="POST",
            payload={},
            headers={"Origin": "https://attacker.invalid", "Sec-Fetch-Site": "cross-site"},
        )
        assert status == 403
        assert isinstance(cross_site, dict)
        assert cross_site["error"] == "forbidden"

        status, foreign_local = _request(
            base_url + "/api/scenarios",
            method="POST",
            payload={},
            headers={"Origin": "http://127.0.0.1:65535", "Sec-Fetch-Site": "same-site"},
        )
        assert status == 403
        assert isinstance(foreign_local, dict)
        assert foreign_local["error"] == "forbidden"

        status, pending = _request(
            base_url + "/api/scenarios",
            method="POST",
            payload={},
            headers={"Origin": base_url, "Sec-Fetch-Site": "same-origin"},
        )
        assert status == 201
        assert isinstance(pending, dict)
        scenario_id = str(pending["scenario_id"])

        status, fetched = _request(base_url + f"/api/scenarios/{scenario_id}")
        assert status == 200
        assert isinstance(fetched, dict)
        assert fetched["phase"] == "pending"

        status, invalid = _request(
            base_url + f"/api/scenarios/{scenario_id}/decision",
            method="POST",
            payload={"decision": "force"},
        )
        assert status == 400
        assert isinstance(invalid, dict)
        assert invalid["error"] == "invalid_request"

        status, completed = _request(
            base_url + f"/api/scenarios/{scenario_id}/decision",
            method="POST",
            payload={"decision": "approve"},
        )
        assert status == 200
        assert isinstance(completed, dict)
        assert completed["phase"] == "approved"

        status, replay = _request(
            base_url + f"/api/scenarios/{scenario_id}/decision",
            method="POST",
            payload={"decision": "reject"},
        )
        assert status == 409
        assert isinstance(replay, dict)
        assert replay["error"] == "conflict"

        status, traversal = _request(base_url + "/api/scenarios/..%2Fescape")
        assert status == 400
        assert isinstance(traversal, dict)
        assert traversal["error"] == "invalid_request"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_refuses_non_loopback_bind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="localhost"):
        build_server("0.0.0.0", 0, DemoController(tmp_path))


def test_demo_runtime_directory_is_gitignored() -> None:
    repository_root = Path(__file__).parents[1]
    result = subprocess.run(
        ["git", "check-ignore", "data/demo-runtime/checkpoint.json"],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0


def test_rendered_app_has_accessible_structure_and_no_external_runtime_assets() -> None:
    page = render_app()

    assert "<main" in page
    assert "<h1" in page
    assert 'aria-live="polite"' in page
    assert 'type="button"' in page
    assert ":focus-visible" in page
    assert "@media" in page
    assert "max-width:720px" in page
    assert "Proposal decision recorded" in page
    assert "Final deterministic state" in page
    assert "The decision outcome is uncertain" in page
    assert "Further decisions are locked" in page
    assert "Can we fit the rush order into today’s schedule?" in page
    assert "2 hours over today’s machine capacity" in page
    assert "600 units of red thread short" in page
    assert "What the agent recommends" in page
    assert "Why this is useful" in page
    assert "Keep current schedule" in page
    assert "Approve coordinated plan" in page
    assert "Technical proof" in page
    assert "<details" in page
    assert "Rush order scheduled; follow-up work created" in page
    assert "Current shop plan kept unchanged" in page
    assert "Nothing changes unless that exact plan is approved" in page
    assert "Today scheduled" in page
    assert "https://" not in page
    assert "http://" not in page
