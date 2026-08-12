import json
import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest

from production_orchestrator.fixtures import rush_order_scenario
from production_orchestrator.persistence import SQLiteShopRepository
from production_orchestrator.workflow import ProductionPlanApprovalHook, ShopService


class FakeBeforeToolCallEvent:
    def __init__(self, proposal_hash: str, response: str) -> None:
        self.tool_use = {
            "name": "apply_production_plan",
            "input": {"proposal_hash": proposal_hash},
        }
        self.response = response
        self.cancel_tool: bool | str = False
        self.interrupt_reason = None

    def interrupt(self, name: str, reason=None):
        assert name == "production-orchestrator-apply-plan"
        self.interrupt_reason = reason
        return self.response


def _service(tmp_path):
    repository = SQLiteShopRepository(
        tmp_path / "shop.db",
        clock=lambda: "2026-08-11T18:00:00Z",
    )
    repository.initialize(rush_order_scenario())
    return repository, ShopService(repository)


def test_rejection_hook_binds_hash_and_preserves_domain_state(tmp_path) -> None:
    repository, service = _service(tmp_path)
    proposal = service.propose_schedule("RUSH-200")
    before = repository.domain_digest()
    event = FakeBeforeToolCallEvent(proposal["content_hash"], "n")

    ProductionPlanApprovalHook(service, actor="spike-operator").approve(event)

    assert event.interrupt_reason["proposal_hash"] == proposal["content_hash"]
    assert event.cancel_tool == "Human rejected the production plan"
    assert repository.domain_digest() == before
    assert repository.latest_decision(proposal["content_hash"]).approved is False


def test_approval_hook_allows_exact_registered_plan_to_apply(tmp_path) -> None:
    repository, service = _service(tmp_path)
    proposal = service.propose_schedule("RUSH-200")
    event = FakeBeforeToolCallEvent(proposal["content_hash"], "y")

    ProductionPlanApprovalHook(service, actor="spike-operator").approve(event)
    result = service.apply_plan(proposal["content_hash"])

    assert event.cancel_tool is False
    assert result == {
        "proposal_hash": proposal["content_hash"],
        "applied_revision": 2,
        "status": "applied",
    }
    assert repository.load_state().revision == 2


def test_service_records_complete_evidence_to_mutation_chain(tmp_path) -> None:
    repository, service = _service(tmp_path)

    service.list_active_orders()
    service.get_inventory()
    service.get_machine_capacity()
    service.analyze_shop_blockers("RUSH-200")
    proposal = service.propose_schedule("RUSH-200")
    service.draft_communications(proposal["content_hash"])
    event = FakeBeforeToolCallEvent(proposal["content_hash"], "y")
    ProductionPlanApprovalHook(service, actor="spike-operator").approve(event)
    service.apply_plan(proposal["content_hash"])

    events = repository.audit_events()
    assert [event.event_type for event in events] == [
        "scenario_initialized",
        "active_orders_read",
        "inventory_read",
        "machine_capacity_read",
        "blockers_analyzed",
        "proposal_created",
        "communications_drafted",
        "approval_granted",
        "plan_applied",
    ]
    assert events[4].details["blocker_kinds"] == [
        "inventory_shortage",
        "capacity_conflict",
    ]
    assert events[5].proposal_hash == proposal["content_hash"]
    assert events[6].proposal_hash == proposal["content_hash"]
    assert events[-1].proposal_hash == proposal["content_hash"]


def test_fresh_service_reconstructs_and_applies_persisted_proposal(tmp_path) -> None:
    repository, first_service = _service(tmp_path)
    proposal = first_service.propose_schedule("RUSH-200")

    fresh_repository = SQLiteShopRepository(
        repository.path,
        clock=lambda: "2026-08-11T18:00:01Z",
    )
    fresh_service = ShopService(fresh_repository)
    assert fresh_service.get_proposal(proposal["content_hash"]) == first_service.get_proposal(
        proposal["content_hash"]
    )
    event = FakeBeforeToolCallEvent(proposal["content_hash"], "y")

    ProductionPlanApprovalHook(fresh_service, actor="spike-operator").approve(event)
    result = fresh_service.apply_plan(proposal["content_hash"])

    assert event.cancel_tool is False
    assert result["proposal_hash"] == proposal["content_hash"]
    assert result["applied_revision"] == 2
    assert fresh_repository.load_state().revision == 2


def test_missing_and_malformed_persisted_proposals_fail_closed(tmp_path) -> None:
    repository, service = _service(tmp_path)
    missing_hash = "f" * 64

    with pytest.raises(ValueError, match="Unknown proposal hash"):
        service.get_proposal(missing_hash)

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            INSERT INTO production_proposals(content_hash, payload, created_at)
            VALUES (?, ?, ?)
            """,
            (missing_hash, "not-json", "2026-08-11T18:00:00Z"),
        )

    with pytest.raises(ValueError, match="integrity"):
        ShopService(repository).get_proposal(missing_hash)


def test_tampered_persisted_proposal_fails_closed(tmp_path) -> None:
    repository, first_service = _service(tmp_path)
    proposal = first_service.propose_schedule("RUSH-200")
    before = repository.domain_digest()
    with sqlite3.connect(repository.path) as connection:
        row = connection.execute(
            "SELECT payload FROM production_proposals WHERE content_hash = ?",
            (proposal["content_hash"],),
        ).fetchone()
        payload = json.loads(row[0])
        payload["target_order_id"] = "FORGED-ORDER"
        connection.execute(
            "UPDATE production_proposals SET payload = ? WHERE content_hash = ?",
            (json.dumps(payload), proposal["content_hash"]),
        )

    fresh_service = ShopService(repository)

    with pytest.raises(ValueError, match="integrity"):
        fresh_service.get_proposal(proposal["content_hash"])

    assert repository.domain_digest() == before


def test_conflicting_persisted_payload_cannot_be_overwritten(tmp_path) -> None:
    repository, service = _service(tmp_path)
    proposal = service.propose_schedule("RUSH-200")
    before = repository.domain_digest()
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE production_proposals SET payload = ? WHERE content_hash = ?",
            ("{}", proposal["content_hash"]),
        )

    with pytest.raises(ValueError, match="integrity conflict"):
        service.propose_schedule("RUSH-200")

    assert repository.domain_digest() == before


def test_forged_proposal_identity_is_rejected_before_persistence(tmp_path) -> None:
    repository, service = _service(tmp_path)
    proposal_hash = service.propose_schedule("RUSH-200")["content_hash"]
    proposal = service.get_proposal(proposal_hash)
    forged = replace(proposal, content_hash="0" * 64, proposal_id="plan-000000000000")

    with pytest.raises(ValueError, match="integrity"):
        repository.save_proposal(forged)

    assert repository.load_proposal(forged.content_hash) is None


def test_reconstructed_proposal_cannot_be_replayed_after_apply(tmp_path) -> None:
    repository, first_service = _service(tmp_path)
    proposal = first_service.propose_schedule("RUSH-200")
    repository.record_decision(
        proposal_hash=proposal["content_hash"],
        reviewed_hash=proposal["content_hash"],
        approved=True,
        actor="spike-operator",
        reason="Approved before process restart",
    )
    fresh_service = ShopService(repository)
    fresh_service.apply_plan(proposal["content_hash"])
    after_first_apply = repository.domain_digest()
    another_fresh_service = ShopService(repository)

    with pytest.raises(RuntimeError, match="revision"):
        another_fresh_service.apply_plan(proposal["content_hash"])

    assert repository.domain_digest() == after_first_apply
    assert [event.event_type for event in repository.audit_events()].count("plan_applied") == 1


def test_fresh_python_process_applies_persisted_exact_proposal(tmp_path) -> None:
    repository, service = _service(tmp_path)
    proposal = service.propose_schedule("RUSH-200")
    proposal_hash = proposal["content_hash"]
    repository.record_decision(
        proposal_hash=proposal_hash,
        reviewed_hash=proposal_hash,
        approved=True,
        actor="spike-operator",
        reason="Approved before child process starts",
    )
    script = """
import sys
from pathlib import Path
from production_orchestrator.persistence import SQLiteShopRepository
from production_orchestrator.workflow import ShopService

repository = SQLiteShopRepository(Path(sys.argv[1]), clock=lambda: "2026-08-11T18:00:02Z")
result = ShopService(repository).apply_plan(sys.argv[2])
print(f"{result['proposal_hash']}:{result['applied_revision']}")
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(repository.path), proposal_hash],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == f"{proposal_hash}:2"
    assert repository.load_state().revision == 2
