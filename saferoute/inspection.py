"""Module 1: digital inspection and compliance ledger (Dangerous Mechanical Condition).

A critical failure (brakes, steering, tyres) marks the vehicle DMC immediately.
Major failures give a short repair window. Minor failures are recorded only.
"""
from __future__ import annotations

from dataclasses import dataclass

DAY = 86400.0
FLAGGED = ("DMC", "EXPIRED")  # statuses that trigger checkpoint alerts


@dataclass
class Evaluation:
    status: str
    failed: list[dict]
    valid_until: float


def _checks(i: dict, cfg) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = [
        ("brakes", i["brake_efficiency_pct"] >= cfg.min_brake_efficiency_pct, "critical"),
        ("tyre_tread", i["tyre_tread_mm_min"] >= cfg.min_tyre_tread_mm, "critical"),
        ("tyre_damage", not i.get("tyre_damage", False), "critical"),
        ("steering", bool(i["steering_play_ok"]), "critical"),
        ("lights", bool(i["lights_ok"]), "major"),
        ("suspension", bool(i.get("suspension_ok", True)), "major"),
    ]
    # Optional items: None means "not applicable to this vehicle type".
    for key, name, sev in (
        ("handbrake_holds", "handbrake", "critical"),
        ("seatbelts_ok", "seatbelts", "major"),
        ("speed_limiter_ok", "speed_limiter", "major"),
        ("windscreen_wipers_ok", "wipers", "minor"),
        ("fire_extinguisher_ok", "fire_extinguisher", "minor"),
    ):
        if i.get(key) is not None:
            out.append((name, bool(i[key]), sev))
    return out


def evaluate(items: dict, vehicle_type: str, inspected_at: float, cfg) -> Evaluation:
    failed = [{"item": n, "severity": s} for n, ok, s in _checks(items, cfg) if not ok]
    severities = {f["severity"] for f in failed}
    if "critical" in severities:
        # Not roadworthy until re-inspected: validity ends the moment it is recorded.
        return Evaluation("DMC", failed, inspected_at)
    if "major" in severities:
        return Evaluation("CONDITIONAL", failed, inspected_at + cfg.conditional_fix_days * DAY)
    days = cfg.inspection_validity_days.get(vehicle_type, cfg.inspection_validity_days["default"])
    return Evaluation("COMPLIANT", failed, inspected_at + days * DAY)


def compliance_status(store, plate: str, now: float) -> dict:
    """Current compliance of a vehicle: COMPLIANT | CONDITIONAL | EXPIRED | DMC | UNVERIFIED."""
    latest = store.latest_inspection(plate)
    if latest is None:
        return {"plate": plate, "status": "UNVERIFIED", "inspected_at": None, "valid_until": None,
                "days_remaining": None, "failed_items": []}
    status = latest["status"]
    if status != "DMC" and now > latest["valid_until"]:
        status = "EXPIRED"  # a DMC vehicle stays DMC until a passing inspection replaces the record
    days = None if status == "DMC" else round((latest["valid_until"] - now) / DAY, 1)
    return {"plate": plate, "status": status, "inspected_at": latest["inspected_at"],
            "valid_until": latest["valid_until"], "days_remaining": days,
            "failed_items": latest["failed_items"], "inspector_id": latest["inspector_id"]}
