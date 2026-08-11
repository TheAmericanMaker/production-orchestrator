import hashlib
import json
from dataclasses import asdict

from production_orchestrator.models import (
    Blocker,
    CommunicationDraft,
    ProcurementAction,
    ProductionPlan,
    ScheduleChange,
    ShopState,
)


def analyze_blockers(state: ShopState, target_order_id: str) -> tuple[Blocker, ...]:
    order = state.orders[target_order_id]
    blockers: list[Blocker] = []

    for material_id, required in sorted(order.materials.items()):
        available = state.inventory.get(material_id, 0)
        if available < required:
            blockers.append(
                Blocker(
                    kind="inventory_shortage",
                    resource_id=material_id,
                    required=required,
                    available=available,
                    shortage=required - available,
                )
            )

    compatible_machines = sorted(
        (
            machine
            for machine in state.machines.values()
            if machine.machine_type == order.machine_type
        ),
        key=lambda machine: machine.machine_id,
    )
    for machine in compatible_machines:
        day_entries = tuple(
            entry
            for entry in state.schedule
            if entry.machine_id == machine.machine_id and entry.day == order.requested_day
        )
        scheduled_hours = sum(entry.duration_hours for entry in day_entries)
        required_hours = scheduled_hours + order.duration_hours
        capacity = machine.daily_capacity.get(order.requested_day, 0)
        if required_hours <= capacity:
            continue

        lower_priority_orders = tuple(
            sorted(
                entry.order_id
                for entry in day_entries
                if state.orders[entry.order_id].priority < order.priority
            )
        )
        blockers.append(
            Blocker(
                kind="capacity_conflict",
                resource_id=machine.machine_id,
                required=required_hours,
                available=capacity,
                shortage=required_hours - capacity,
                related_order_ids=lower_priority_orders,
            )
        )

    return tuple(blockers)


def create_production_plan(state: ShopState, target_order_id: str) -> ProductionPlan:
    target_order = state.orders[target_order_id]
    evidence = analyze_blockers(state, target_order_id)
    schedule_changes: list[ScheduleChange] = []

    for blocker in evidence:
        if blocker.kind != "capacity_conflict":
            continue
        machine = state.machines[blocker.resource_id]
        hours_freed = 0
        for displaced_order_id in blocker.related_order_ids:
            displaced_entry = next(
                entry
                for entry in state.schedule
                if entry.order_id == displaced_order_id
                and entry.machine_id == machine.machine_id
                and entry.day == target_order.requested_day
            )
            destination = _first_available_future_day(state, machine.machine_id, displaced_entry)
            schedule_changes.append(
                ScheduleChange(
                    order_id=displaced_order_id,
                    machine_id=machine.machine_id,
                    from_day=displaced_entry.day,
                    to_day=destination,
                    reason=f"Make capacity for higher-priority {target_order_id}",
                )
            )
            hours_freed += displaced_entry.duration_hours
            if hours_freed >= blocker.shortage:
                break

        if hours_freed < blocker.shortage:
            raise ValueError(f"No feasible displacement for {target_order_id}")

        schedule_changes.append(
            ScheduleChange(
                order_id=target_order_id,
                machine_id=machine.machine_id,
                from_day=None,
                to_day=target_order.requested_day,
                reason="Schedule approved rush order",
            )
        )

    schedule_changes.sort(key=lambda change: change.order_id)
    procurement_actions = tuple(
        ProcurementAction(
            material_id=blocker.resource_id,
            quantity=blocker.shortage,
            reason=f"Cover shortage for {target_order_id}",
        )
        for blocker in evidence
        if blocker.kind == "inventory_shortage"
    )
    communication_drafts = _draft_communications(
        target_order_id,
        tuple(schedule_changes),
        procurement_actions,
    )
    content_hash = _hash_plan_parts(
        base_revision=state.revision,
        target_order_id=target_order_id,
        evidence=evidence,
        schedule_changes=tuple(schedule_changes),
        procurement_actions=procurement_actions,
        communication_drafts=communication_drafts,
    )

    return ProductionPlan(
        proposal_id=f"plan-{content_hash[:12]}",
        content_hash=content_hash,
        base_revision=state.revision,
        target_order_id=target_order_id,
        evidence=evidence,
        schedule_changes=tuple(schedule_changes),
        procurement_actions=procurement_actions,
        communication_drafts=communication_drafts,
    )


def calculate_production_plan_hash(proposal: ProductionPlan) -> str:
    return _hash_plan_parts(
        base_revision=proposal.base_revision,
        target_order_id=proposal.target_order_id,
        evidence=proposal.evidence,
        schedule_changes=proposal.schedule_changes,
        procurement_actions=proposal.procurement_actions,
        communication_drafts=proposal.communication_drafts,
    )


def _hash_plan_parts(
    *,
    base_revision: int,
    target_order_id: str,
    evidence: tuple[Blocker, ...],
    schedule_changes: tuple[ScheduleChange, ...],
    procurement_actions: tuple[ProcurementAction, ...],
    communication_drafts: tuple[CommunicationDraft, ...],
) -> str:
    payload = {
        "base_revision": base_revision,
        "target_order_id": target_order_id,
        "evidence": [asdict(blocker) for blocker in evidence],
        "schedule_changes": [asdict(change) for change in schedule_changes],
        "procurement_actions": [asdict(action) for action in procurement_actions],
        "communication_drafts": [asdict(draft) for draft in communication_drafts],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _first_available_future_day(state: ShopState, machine_id: str, entry) -> str:
    machine = state.machines[machine_id]
    for day, capacity in sorted(machine.daily_capacity.items()):
        if day <= entry.day:
            continue
        scheduled = sum(
            scheduled_entry.duration_hours
            for scheduled_entry in state.schedule
            if scheduled_entry.machine_id == machine_id and scheduled_entry.day == day
        )
        if scheduled + entry.duration_hours <= capacity:
            return day
    raise ValueError(f"No future capacity for {entry.order_id}")


def _draft_communications(
    target_order_id: str,
    changes: tuple[ScheduleChange, ...],
    procurement_actions: tuple[ProcurementAction, ...],
) -> tuple[CommunicationDraft, ...]:
    moved = ", ".join(
        f"{change.order_id} to {change.to_day}"
        for change in changes
        if change.from_day is not None
    )
    requested = ", ".join(
        f"{action.quantity} units of {action.material_id}" for action in procurement_actions
    )
    return (
        CommunicationDraft(
            audience="customer",
            subject=f"Production update for {target_order_id}",
            body="Your rush order has a proposed production slot pending material procurement.",
        ),
        CommunicationDraft(
            audience="operator",
            subject="Proposed production schedule change",
            body=f"Schedule {target_order_id}; move {moved}.",
        ),
        CommunicationDraft(
            audience="supplier",
            subject=f"Material request for {target_order_id}",
            body=f"Please confirm availability for {requested}.",
        ),
    )
