"""Runtime settings.

Every threshold below is a tunable default chosen for a first field trial.
None of them is a regulatory fact: confirm speed limits, inspection intervals
and brake standards against current Ugandan regulations before relying on them,
and calibrate the sensor thresholds on real vehicles and real roads.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- platform ---
    db_path: str = "saferoute.db"
    admin_key: str = ""
    seed_speed_zones: bool = True

    # --- SMS ---
    sms_provider: str = "console"  # console | africastalking
    at_username: str = "sandbox"
    at_api_key: str = ""
    at_sender_id: str | None = None
    at_sandbox: bool = True

    # --- Module 1: inspection ---
    inspection_validity_days: dict = field(
        default_factory=lambda: {"default": 180, "bus": 90, "taxi": 90, "truck": 90, "boda": 180}
    )
    conditional_fix_days: int = 14
    min_brake_efficiency_pct: float = 50.0
    min_tyre_tread_mm: float = 1.6
    checkpoint_alert_radius_km: float = 10.0
    checkpoint_alert_cooldown_s: float = 1800.0

    # --- Module 2: telematics ---
    default_speed_limit_kmh: float = 80.0
    overspeed_tolerance_kmh: float = 5.0
    min_overspeed_s: float = 10.0
    harsh_brake_ms2: float = 3.5
    harsh_accel_ms2: float = 3.0
    swerve_ms2: float = 3.0
    max_gap_s: float = 60.0
    max_plausible_kmh: float = 220.0
    score_window_days: int = 7

    # --- Module 3: hazards ---
    hazard_merge_radius_m: float = 25.0
    hazard_min_speed_kmh: float = 15.0
    hazard_max_peak_g: float = 8.0  # above this it is a crash, not a pothole
    hazard_confirm_devices: int = 3
    hazard_severity_medium_g: float = 1.0
    hazard_severity_high_g: float = 1.8
    hazard_alert_radius_m: float = 400.0
    hazard_alert_cooldown_s: float = 300.0

    # --- Module 4: crash ---
    crash_full_dispatch_conf: float = 0.6
    crash_nok_conf: float = 0.3
    crash_min_speed_kmh: float = 15.0
    crash_merge_window_s: float = 600.0
    sms_attempts: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.db_path = os.environ.get("SAFEROUTE_DB", s.db_path)
        s.admin_key = os.environ.get("SAFEROUTE_ADMIN_KEY", "")
        s.sms_provider = os.environ.get("SAFEROUTE_SMS_PROVIDER", s.sms_provider)
        s.at_username = os.environ.get("AT_USERNAME", s.at_username)
        s.at_api_key = os.environ.get("AT_API_KEY", "")
        s.at_sender_id = os.environ.get("AT_SENDER_ID") or None
        s.at_sandbox = _env_bool("AT_SANDBOX", True)
        return s
