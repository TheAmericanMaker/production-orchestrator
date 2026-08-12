import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from production_orchestrator.demo import (
    _STATIC,
    DemoController,
    build_server,
    demo_meta,
    render_app,
)
from production_orchestrator.restart_spike import WORKFLOW_MODEL_ID, WORKFLOW_PROVIDER, agent_id_for

PENDING_EVENTS = [
    "scenario_initialized",
    "active_orders_read",
    "inventory_read",
    "machine_capacity_read",
    "blockers_analyzed",
    "proposal_created",
    "communications_drafted",
]


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
def test_controller_runs_full_workflow_before_interrupt_after_processes(
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
    assert pending["scenario"]["name"] == "rush-order"
    assert pending["scenario"]["target_order_id"] == "RUSH-200"
    assert pending["state"]["revision"] == 1
    assert pending["proposal"]["base_revision"] == 1
    assert pending["proposal"]["content_hash"] == pending["checkpoint"]["proposal_hash"]
    assert {item["kind"] for item in pending["proposal"]["evidence"]} == {
        "inventory_shortage",
        "capacity_conflict",
    }
    assert len(pending["proposal"]["communication_drafts"]) == 3
    assert pending["checkpoint"]["first_stop_reason"] == "interrupt"
    assert pending["checkpoint"]["provider"] == WORKFLOW_PROVIDER
    assert pending["checkpoint"]["model_id"] == WORKFLOW_MODEL_ID
    pending_audit = pending["audit"]
    assert isinstance(pending_audit, list)
    assert [event["event_type"] for event in pending_audit] == PENDING_EVENTS

    completed = controller.decide(str(pending["scenario_id"]), decision)

    assert completed["phase"] == expected_phase
    assert completed["state"]["revision"] == expected_revision
    assert completed["report"]["plan_applied_count"] == expected_applied
    assert completed["report"]["workflow_passed"] is True
    assert completed["report"]["start_process_id"] != completed["report"]["resume_process_id"]
    expected_events = PENDING_EVENTS + (
        ["approval_granted", "plan_applied"] if decision == "approve" else ["approval_rejected"]
    )
    completed_audit = completed["audit"]
    assert isinstance(completed_audit, list)
    assert [event["event_type"] for event in completed_audit] == expected_events
    assert expected_event in expected_events


def test_controller_runs_scenario_variations(tmp_path: Path) -> None:
    controller = DemoController(tmp_path)

    pending = controller.create_scenario("team-jerseys")

    assert pending["scenario"]["name"] == "team-jerseys"
    assert pending["scenario"]["target_order_id"] == "JERSEY-310"
    assert {item["kind"] for item in pending["proposal"]["evidence"]} == {"capacity_conflict"}
    assert not pending["proposal"]["procurement_actions"]
    assert [draft["audience"] for draft in pending["proposal"]["communication_drafts"]] == [
        "customer",
        "operator",
    ]
    moved = {
        change["order_id"]
        for change in pending["proposal"]["schedule_changes"]
        if change["from_day"]
    }
    assert moved == {"CAPS-110", "TOTES-120"}

    with pytest.raises(ValueError, match="scenario"):
        controller.create_scenario("not-a-scenario")


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

    report["plan_applied_count"] = 1
    report["scenario"] = "team-jerseys"
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
        provider=WORKFLOW_PROVIDER,
        model_id="forged-model",
        proposal_hash=None,
        aws_profile=None,
        aws_region=None,
        scenario=str(checkpoint["scenario"]),
    )
    checkpoint_path.write_text(json.dumps(checkpoint))

    with pytest.raises(RuntimeError, match="checkpoint"):
        controller.get_scenario(scenario_id)


def test_demo_meta_lists_scenarios_and_provider() -> None:
    meta = demo_meta()

    scenarios = meta["scenarios"]
    assert isinstance(scenarios, list)
    assert [spec["name"] for spec in scenarios] == [
        "rush-order",
        "team-jerseys",
        "metallic-monogram",
    ]
    for spec in scenarios:
        assert spec["title"] and spec["question"] and spec["summary"]
    provider = meta["provider"]
    assert isinstance(provider, dict)
    assert provider["provider"] == WORKFLOW_PROVIDER
    assert provider["model_id"] == WORKFLOW_MODEL_ID
    assert provider["strands_agents_version"]


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
        assert '<link rel="stylesheet" href="/static/app.css">' in page
        assert '<script src="/static/app.js" defer>' in page

        status, script = _request(base_url + "/static/app.js")
        assert status == 200
        assert isinstance(script, str)
        assert "Approve coordinated plan" in script

        status, missing_asset = _request(base_url + "/static/secrets.txt")
        assert status == 404
        assert isinstance(missing_asset, dict)
        assert missing_asset["error"] == "not_found"

        status, meta = _request(base_url + "/api/meta")
        assert status == 200
        assert isinstance(meta, dict)
        assert next(spec["name"] for spec in meta["scenarios"]) == "rush-order"

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

        status, unknown_scenario = _request(
            base_url + "/api/scenarios",
            method="POST",
            payload={"scenario": "not-a-scenario"},
        )
        assert status == 400
        assert isinstance(unknown_scenario, dict)
        assert unknown_scenario["error"] == "invalid_request"

        status, pending = _request(
            base_url + "/api/scenarios",
            method="POST",
            payload={"scenario": "metallic-monogram"},
            headers={"Origin": base_url, "Sec-Fetch-Site": "same-origin"},
        )
        assert status == 201
        assert isinstance(pending, dict)
        assert pending["scenario"]["name"] == "metallic-monogram"
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


def test_static_app_has_accessible_structure_and_no_external_runtime_assets() -> None:
    page = render_app()
    stylesheet = _STATIC.joinpath("app.css").read_text(encoding="utf-8")
    script = _STATIC.joinpath("app.js").read_text(encoding="utf-8")

    assert "<main" in page
    assert "<h1" in page
    assert 'aria-live="polite"' in page
    assert "<noscript>" in page
    assert 'lang="en"' in page

    assert ":focus-visible" in stylesheet
    assert "@media" in stylesheet
    assert "max-width: 720px" in stylesheet
    assert "prefers-reduced-motion" in stylesheet

    assert 'type="button"' in script
    assert "Keep current schedule" in script
    assert "Approve coordinated plan" in script
    assert "What the agent did" in script
    assert "Production board" in script
    assert "real Strands interrupt" in script
    assert "DRAFT · NOT SENT" in script
    assert "Amazon Bedrock" in script
    assert "fail-closed" in script

    for asset in (page, stylesheet, script):
        assert "https://" not in asset
        assert "http://" not in asset
