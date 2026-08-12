from collections.abc import Callable
from dataclasses import dataclass

from production_orchestrator.models import Machine, Order, ScheduleEntry, ShopState

_TODAY = "2026-08-12"
_TOMORROW = "2026-08-13"


def rush_order_scenario() -> ShopState:
    return ShopState(
        revision=1,
        inventory={
            "THREAD-RED-40": 600,
            "THREAD-BLUE-40": 2_000,
        },
        orders={
            "STANDARD-100": Order(
                order_id="STANDARD-100",
                priority=20,
                requested_day=_TODAY,
                machine_type="embroidery",
                duration_hours=6,
                materials={"THREAD-BLUE-40": 200},
            ),
            "RUSH-200": Order(
                order_id="RUSH-200",
                priority=100,
                requested_day=_TODAY,
                machine_type="embroidery",
                duration_hours=4,
                materials={"THREAD-RED-40": 1_200},
            ),
        },
        machines={
            "EMB-01": Machine(
                machine_id="EMB-01",
                machine_type="embroidery",
                daily_capacity={_TODAY: 8, _TOMORROW: 8},
            )
        },
        schedule=(
            ScheduleEntry(
                order_id="STANDARD-100",
                machine_id="EMB-01",
                day=_TODAY,
                duration_hours=6,
            ),
        ),
    )


def team_jerseys_scenario() -> ShopState:
    return ShopState(
        revision=1,
        inventory={
            "THREAD-NAVY-40": 2_000,
            "THREAD-WHITE-40": 900,
        },
        orders={
            "CAPS-110": Order(
                order_id="CAPS-110",
                priority=10,
                requested_day=_TODAY,
                machine_type="embroidery",
                duration_hours=2,
                materials={"THREAD-WHITE-40": 150},
            ),
            "TOTES-120": Order(
                order_id="TOTES-120",
                priority=15,
                requested_day=_TODAY,
                machine_type="embroidery",
                duration_hours=3,
                materials={"THREAD-NAVY-40": 250},
            ),
            "JERSEY-310": Order(
                order_id="JERSEY-310",
                priority=90,
                requested_day=_TODAY,
                machine_type="embroidery",
                duration_hours=7,
                materials={"THREAD-NAVY-40": 800},
            ),
        },
        machines={
            "EMB-01": Machine(
                machine_id="EMB-01",
                machine_type="embroidery",
                daily_capacity={_TODAY: 8, _TOMORROW: 8},
            )
        },
        schedule=(
            ScheduleEntry(
                order_id="CAPS-110",
                machine_id="EMB-01",
                day=_TODAY,
                duration_hours=2,
            ),
            ScheduleEntry(
                order_id="TOTES-120",
                machine_id="EMB-01",
                day=_TODAY,
                duration_hours=3,
            ),
        ),
    )


def metallic_monogram_scenario() -> ShopState:
    return ShopState(
        revision=1,
        inventory={
            "THREAD-GOLD-60": 400,
            "THREAD-BLUE-40": 500,
        },
        orders={
            "BATCH-220": Order(
                order_id="BATCH-220",
                priority=30,
                requested_day=_TODAY,
                machine_type="embroidery",
                duration_hours=6,
                materials={"THREAD-BLUE-40": 100},
            ),
            "GOLD-500": Order(
                order_id="GOLD-500",
                priority=80,
                requested_day=_TODAY,
                machine_type="embroidery",
                duration_hours=5,
                materials={"THREAD-GOLD-60": 1_500},
            ),
        },
        machines={
            "EMB-02": Machine(
                machine_id="EMB-02",
                machine_type="embroidery",
                daily_capacity={_TODAY: 8, _TOMORROW: 8},
            )
        },
        schedule=(
            ScheduleEntry(
                order_id="BATCH-220",
                machine_id="EMB-02",
                day=_TODAY,
                duration_hours=6,
            ),
        ),
    )


@dataclass(frozen=True)
class ScenarioSpec:
    """A named synthetic shop scenario the demo can run end to end."""

    name: str
    title: str
    question: str
    summary: str
    target_order_id: str
    build: Callable[[], ShopState]


SCENARIOS: dict[str, ScenarioSpec] = {
    spec.name: spec
    for spec in (
        ScenarioSpec(
            name="rush-order",
            title="Rush order, full machine",
            question="Can we fit the rush order into today’s schedule?",
            summary=(
                "A high-priority rush order arrives with today’s machine already "
                "booked and a key thread color running low."
            ),
            target_order_id="RUSH-200",
            build=rush_order_scenario,
        ),
        ScenarioSpec(
            name="team-jerseys",
            title="Team jerseys need the day",
            question="Can we clear today for the team-jersey rush?",
            summary=(
                "A large team-jersey order outranks the two small jobs already "
                "holding today’s machine."
            ),
            target_order_id="JERSEY-310",
            build=team_jerseys_scenario,
        ),
        ScenarioSpec(
            name="metallic-monogram",
            title="Metallic monogram batch",
            question="Can the monogram batch jump the queue?",
            summary=(
                "A corporate monogram batch needs metallic thread the shop is short "
                "on, and the machine is already committed."
            ),
            target_order_id="GOLD-500",
            build=metallic_monogram_scenario,
        ),
    )
}
