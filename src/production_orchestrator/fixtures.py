from production_orchestrator.models import Machine, Order, ScheduleEntry, ShopState


def rush_order_scenario() -> ShopState:
    day = "2026-08-12"
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
                requested_day=day,
                machine_type="embroidery",
                duration_hours=6,
                materials={"THREAD-BLUE-40": 200},
            ),
            "RUSH-200": Order(
                order_id="RUSH-200",
                priority=100,
                requested_day=day,
                machine_type="embroidery",
                duration_hours=4,
                materials={"THREAD-RED-40": 1_200},
            ),
        },
        machines={
            "EMB-01": Machine(
                machine_id="EMB-01",
                machine_type="embroidery",
                daily_capacity={day: 8, "2026-08-13": 8},
            )
        },
        schedule=(
            ScheduleEntry(
                order_id="STANDARD-100",
                machine_id="EMB-01",
                day=day,
                duration_hours=6,
            ),
        ),
    )
