import sqlite3
from dataclasses import replace

import pytest

from production_orchestrator.approval import (
    ApprovalRejected,
    ApprovalRequired,
    ProposalIntegrityError,
    StaleProposal,
    apply_production_plan,
)
from production_orchestrator.fixtures import rush_order_scenario
from production_orchestrator.persistence import SQLiteShopRepository
from production_orchestrator.planning import create_production_plan


def test_missing_or_rejected_approval_never_mutates_domain_state(tmp_path) -> None:
    repository = SQLiteShopRepository(
        tmp_path / "shop.db",
        clock=lambda: "2026-08-11T18:00:00Z",
    )
    repository.initialize(rush_order_scenario())
    proposal = create_production_plan(repository.load_state(), "RUSH-200")
    before = repository.domain_digest()

    with pytest.raises(ApprovalRequired):
        apply_production_plan(repository, proposal)
    assert repository.domain_digest() == before

    repository.record_decision(
        proposal_hash=proposal.content_hash,
        reviewed_hash=proposal.content_hash,
        approved=False,
        actor="operator@example.test",
        reason="Supplier cannot meet the rush date",
    )
    with pytest.raises(ApprovalRejected):
        apply_production_plan(repository, proposal)

    assert repository.domain_digest() == before
    assert [event.event_type for event in repository.audit_events()] == [
        "scenario_initialized",
        "approval_rejected",
    ]


def test_exact_approval_atomically_applies_schedule_procurement_and_audit(tmp_path) -> None:
    repository = SQLiteShopRepository(
        tmp_path / "shop.db",
        clock=lambda: "2026-08-11T18:00:00Z",
    )
    repository.initialize(rush_order_scenario())
    proposal = create_production_plan(repository.load_state(), "RUSH-200")
    repository.record_decision(
        proposal_hash=proposal.content_hash,
        reviewed_hash=proposal.content_hash,
        approved=True,
        actor="operator@example.test",
        reason="Material ETA and revised schedule accepted",
    )

    result = apply_production_plan(repository, proposal)

    state = repository.load_state()
    schedule = {entry.order_id: entry.day for entry in state.schedule}
    assert result.proposal_hash == proposal.content_hash
    assert result.applied_revision == 2
    assert state.revision == 2
    assert schedule == {
        "RUSH-200": "2026-08-12",
        "STANDARD-100": "2026-08-13",
    }
    assert len(state.procurement_tasks) == 1
    assert state.procurement_tasks[0].material_id == "THREAD-RED-40"
    assert state.procurement_tasks[0].quantity == 600
    assert state.procurement_tasks[0].proposal_hash == proposal.content_hash
    assert [event.event_type for event in repository.audit_events()] == [
        "scenario_initialized",
        "approval_granted",
        "plan_applied",
    ]


def test_altered_plan_is_rejected_even_when_original_hash_was_approved(tmp_path) -> None:
    repository = SQLiteShopRepository(
        tmp_path / "shop.db",
        clock=lambda: "2026-08-11T18:00:00Z",
    )
    repository.initialize(rush_order_scenario())
    proposal = create_production_plan(repository.load_state(), "RUSH-200")
    repository.record_decision(
        proposal_hash=proposal.content_hash,
        reviewed_hash=proposal.content_hash,
        approved=True,
        actor="operator@example.test",
        reason="Original proposal accepted",
    )
    changed_move = replace(proposal.schedule_changes[1], to_day="2026-08-14")
    altered = replace(
        proposal,
        schedule_changes=(proposal.schedule_changes[0], changed_move),
    )
    before = repository.domain_digest()

    with pytest.raises(ProposalIntegrityError):
        apply_production_plan(repository, altered)

    assert repository.domain_digest() == before


def test_approved_plan_cannot_be_replayed_after_revision_advances(tmp_path) -> None:
    repository = SQLiteShopRepository(
        tmp_path / "shop.db",
        clock=lambda: "2026-08-11T18:00:00Z",
    )
    repository.initialize(rush_order_scenario())
    proposal = create_production_plan(repository.load_state(), "RUSH-200")
    repository.record_decision(
        proposal_hash=proposal.content_hash,
        reviewed_hash=proposal.content_hash,
        approved=True,
        actor="operator@example.test",
        reason="Proposal accepted",
    )
    apply_production_plan(repository, proposal)
    after_first_apply = repository.domain_digest()

    with pytest.raises(StaleProposal):
        apply_production_plan(repository, proposal)

    assert repository.domain_digest() == after_first_apply
    assert [event.event_type for event in repository.audit_events()].count("plan_applied") == 1


def test_approval_for_a_different_reviewed_hash_is_denied(tmp_path) -> None:
    repository = SQLiteShopRepository(
        tmp_path / "shop.db",
        clock=lambda: "2026-08-11T18:00:00Z",
    )
    repository.initialize(rush_order_scenario())
    proposal = create_production_plan(repository.load_state(), "RUSH-200")
    repository.record_decision(
        proposal_hash=proposal.content_hash,
        reviewed_hash="0" * 64,
        approved=True,
        actor="operator@example.test",
        reason="Wrong document was reviewed",
    )
    before = repository.domain_digest()

    with pytest.raises(ProposalIntegrityError):
        apply_production_plan(repository, proposal)

    assert repository.domain_digest() == before


def test_state_update_rolls_back_if_audit_append_fails(tmp_path) -> None:
    repository = SQLiteShopRepository(
        tmp_path / "shop.db",
        clock=lambda: "2026-08-11T18:00:00Z",
    )
    repository.initialize(rush_order_scenario())
    proposal = create_production_plan(repository.load_state(), "RUSH-200")
    repository.record_decision(
        proposal_hash=proposal.content_hash,
        reviewed_hash=proposal.content_hash,
        approved=True,
        actor="operator@example.test",
        reason="Proposal accepted",
    )
    before = repository.domain_digest()
    with sqlite3.connect(repository.path) as connection:
        connection.executescript(
            """
            CREATE TRIGGER fail_plan_applied_audit
            BEFORE INSERT ON audit_events
            WHEN NEW.event_type = 'plan_applied'
            BEGIN
                SELECT RAISE(ABORT, 'simulated audit failure');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated audit failure"):
        apply_production_plan(repository, proposal)

    assert repository.domain_digest() == before
    assert [event.event_type for event in repository.audit_events()].count("plan_applied") == 0
