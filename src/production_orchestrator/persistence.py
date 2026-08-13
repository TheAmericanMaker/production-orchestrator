import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from production_orchestrator.models import (
    Blocker,
    CommunicationDraft,
    Machine,
    Order,
    ProcurementAction,
    ProcurementTask,
    ProductionPlan,
    ScheduleChange,
    ScheduleEntry,
    ShopState,
)
from production_orchestrator.planning import calculate_production_plan_hash


@dataclass(frozen=True)
class ApprovalDecision:
    proposal_hash: str
    reviewed_hash: str
    approved: bool
    actor: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    event_type: str
    proposal_hash: str | None
    details: dict[str, object]
    created_at: str


class StaleStateError(RuntimeError):
    """Raised when a proposal's base revision no longer matches shop state."""


class SQLiteShopRepository:
    def __init__(self, path: Path, clock: Callable[[], str]) -> None:
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS shop_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_decisions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_hash TEXT NOT NULL,
                    reviewed_hash TEXT NOT NULL,
                    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    proposal_hash TEXT,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS production_proposals (
                    content_hash TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def initialize(self, state: ShopState) -> None:
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM shop_state WHERE singleton = 1").fetchone():
                raise RuntimeError("Shop state is already initialized")
            connection.execute(
                "INSERT INTO shop_state(singleton, payload) VALUES (1, ?)",
                (_encode_state(state),),
            )
            self._append_audit(
                connection,
                event_type="scenario_initialized",
                proposal_hash=None,
                details={"revision": state.revision},
            )

    def load_state(self) -> ShopState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM shop_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("Shop state is not initialized")
        return _decode_state(row["payload"])

    def domain_digest(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM shop_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("Shop state is not initialized")
        return hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()

    def add_order(self, order: Order) -> None:
        """Insert a validated intake order and its audit event atomically.

        Intake is the one sanctioned pre-proposal mutation: it only adds a new
        order to the queue. Revision is unchanged; consequential mutations
        (schedule, procurement) remain approval-gated.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM shop_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("Shop state is not initialized")
            state = _decode_state(row["payload"])
            if order.order_id in state.orders:
                raise ValueError(f"Order {order.order_id} already exists")
            new_state = replace(state, orders={**state.orders, order.order_id: order})
            payload = _encode_state(new_state)
            connection.execute(
                "UPDATE shop_state SET payload = ? WHERE singleton = 1",
                (payload,),
            )
            self._append_audit(
                connection,
                event_type="request_intake",
                proposal_hash=None,
                details={
                    "order_id": order.order_id,
                    "requested_day": order.requested_day,
                    "duration_hours": order.duration_hours,
                    "materials": dict(order.materials),
                    "priority": order.priority,
                    "domain_digest_after": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                },
            )

    def record_decision(
        self,
        *,
        proposal_hash: str,
        reviewed_hash: str,
        approved: bool,
        actor: str,
        reason: str,
    ) -> None:
        created_at = self.clock()
        event_type = "approval_granted" if approved else "approval_rejected"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approval_decisions(
                    proposal_hash, reviewed_hash, approved, actor, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (proposal_hash, reviewed_hash, int(approved), actor, reason, created_at),
            )
            self._append_audit(
                connection,
                event_type=event_type,
                proposal_hash=proposal_hash,
                details={
                    "actor": actor,
                    "reason": reason,
                    "reviewed_hash": reviewed_hash,
                },
            )

    def record_audit(
        self,
        *,
        event_type: str,
        proposal_hash: str | None,
        details: dict[str, object],
    ) -> None:
        with self._connect() as connection:
            self._append_audit(
                connection,
                event_type=event_type,
                proposal_hash=proposal_hash,
                details=details,
            )

    def latest_decision(self, proposal_hash: str) -> ApprovalDecision | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT proposal_hash, reviewed_hash, approved, actor, reason, created_at
                FROM approval_decisions
                WHERE proposal_hash = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (proposal_hash,),
            ).fetchone()
        if row is None:
            return None
        return ApprovalDecision(
            proposal_hash=row["proposal_hash"],
            reviewed_hash=row["reviewed_hash"],
            approved=bool(row["approved"]),
            actor=row["actor"],
            reason=row["reason"],
            created_at=row["created_at"],
        )

    def save_proposal(self, proposal: ProductionPlan) -> None:
        calculated_hash = calculate_production_plan_hash(proposal)
        expected_id = f"plan-{calculated_hash[:12]}"
        if proposal.content_hash != calculated_hash or proposal.proposal_id != expected_id:
            raise ValueError("Proposal integrity validation failed before persistence")
        payload = _encode_proposal(proposal)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO production_proposals(content_hash, payload, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(content_hash) DO NOTHING
                """,
                (proposal.content_hash, payload, self.clock()),
            )
            row = connection.execute(
                "SELECT payload FROM production_proposals WHERE content_hash = ?",
                (proposal.content_hash,),
            ).fetchone()
        if row is None or row["payload"] != payload:
            raise ValueError("Persisted proposal integrity conflict")

    def load_proposal(self, proposal_hash: str) -> ProductionPlan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM production_proposals WHERE content_hash = ?",
                (proposal_hash,),
            ).fetchone()
        if row is None:
            return None
        try:
            proposal = _decode_proposal(row["payload"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Persisted proposal integrity validation failed") from error
        calculated_hash = calculate_production_plan_hash(proposal)
        expected_id = f"plan-{calculated_hash[:12]}"
        if (
            proposal.content_hash != proposal_hash
            or calculated_hash != proposal_hash
            or proposal.proposal_id != expected_id
        ):
            raise ValueError("Persisted proposal integrity validation failed")
        return proposal

    def apply_approved_plan(self, proposal: ProductionPlan) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM shop_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("Shop state is not initialized")
            state = _decode_state(row["payload"])
            if state.revision != proposal.base_revision:
                raise StaleStateError(
                    f"State revision {state.revision} does not match {proposal.base_revision}"
                )

            schedule = list(state.schedule)
            for change in proposal.schedule_changes:
                if change.from_day is not None:
                    matches = [
                        entry
                        for entry in schedule
                        if entry.order_id == change.order_id
                        and entry.machine_id == change.machine_id
                        and entry.day == change.from_day
                    ]
                    if len(matches) != 1:
                        raise RuntimeError(f"Expected one scheduled entry for {change.order_id}")
                    schedule.remove(matches[0])
                schedule.append(
                    ScheduleEntry(
                        order_id=change.order_id,
                        machine_id=change.machine_id,
                        day=change.to_day,
                        duration_hours=state.orders[change.order_id].duration_hours,
                    )
                )

            procurement_tasks = state.procurement_tasks + tuple(
                ProcurementTask(
                    material_id=action.material_id,
                    quantity=action.quantity,
                    proposal_hash=proposal.content_hash,
                    status="proposed",
                )
                for action in proposal.procurement_actions
            )
            applied_state = replace(
                state,
                revision=state.revision + 1,
                schedule=tuple(sorted(schedule, key=lambda entry: entry.order_id)),
                procurement_tasks=procurement_tasks,
            )
            connection.execute(
                "UPDATE shop_state SET payload = ? WHERE singleton = 1",
                (_encode_state(applied_state),),
            )
            self._append_audit(
                connection,
                event_type="plan_applied",
                proposal_hash=proposal.content_hash,
                details={
                    "base_revision": proposal.base_revision,
                    "applied_revision": applied_state.revision,
                    "schedule_changes": [asdict(change) for change in proposal.schedule_changes],
                    "procurement_actions": [
                        asdict(action) for action in proposal.procurement_actions
                    ],
                },
            )
        return applied_state.revision

    def audit_events(self) -> tuple[AuditEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, event_type, proposal_hash, details, created_at
                FROM audit_events
                ORDER BY sequence
                """
            ).fetchall()
        return tuple(
            AuditEvent(
                sequence=row["sequence"],
                event_type=row["event_type"],
                proposal_hash=row["proposal_hash"],
                details=json.loads(row["details"]),
                created_at=row["created_at"],
            )
            for row in rows
        )

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        proposal_hash: str | None,
        details: dict[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events(event_type, proposal_hash, details, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                event_type,
                proposal_hash,
                json.dumps(details, sort_keys=True, separators=(",", ":")),
                self.clock(),
            ),
        )


def _encode_state(state: ShopState) -> str:
    payload = {
        "revision": state.revision,
        "inventory": dict(state.inventory),
        "orders": {order_id: asdict(order) for order_id, order in state.orders.items()},
        "machines": {machine_id: asdict(machine) for machine_id, machine in state.machines.items()},
        "schedule": [asdict(entry) for entry in state.schedule],
        "procurement_tasks": [asdict(task) for task in state.procurement_tasks],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode_state(payload: str) -> ShopState:
    data = json.loads(payload)
    return ShopState(
        revision=data["revision"],
        inventory=data["inventory"],
        orders={order_id: Order(**order) for order_id, order in data["orders"].items()},
        machines={
            machine_id: Machine(**machine) for machine_id, machine in data["machines"].items()
        },
        schedule=tuple(ScheduleEntry(**entry) for entry in data["schedule"]),
        procurement_tasks=tuple(
            ProcurementTask(**task) for task in data.get("procurement_tasks", [])
        ),
    )


def _encode_proposal(proposal: ProductionPlan) -> str:
    return json.dumps(asdict(proposal), sort_keys=True, separators=(",", ":"))


def _decode_proposal(payload: str) -> ProductionPlan:
    data = json.loads(payload)
    return ProductionPlan(
        proposal_id=data["proposal_id"],
        content_hash=data["content_hash"],
        base_revision=data["base_revision"],
        target_order_id=data["target_order_id"],
        evidence=tuple(
            Blocker(
                **{
                    **item,
                    "related_order_ids": tuple(item.get("related_order_ids", ())),
                }
            )
            for item in data["evidence"]
        ),
        schedule_changes=tuple(ScheduleChange(**item) for item in data["schedule_changes"]),
        procurement_actions=tuple(
            ProcurementAction(**item) for item in data["procurement_actions"]
        ),
        communication_drafts=tuple(
            CommunicationDraft(**item) for item in data["communication_drafts"]
        ),
    )
