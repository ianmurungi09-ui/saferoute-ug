"""Module 4: crash detection hand-off and emergency dispatch.

The device decides locally (impact + speed drop / rollover) and gives the rider a
cancel window before it ever calls this API; a GSM SMS fallback on the device
covers the case where data is down. The server adds three things: a second
opinion on confidence, idempotent handling of retries, and tiered dispatch.

  confidence >= full threshold and moving fast before impact -> emergency units + next of kin
  confidence >= nok threshold                                -> next of kin only (ask them to call)
  otherwise                                                  -> logged for the operations desk

Sensor alerts are UNVERIFIED and every message says so."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .geo import haversine_m, nearest_landmark

EAT = timezone(timedelta(hours=3))  # East Africa Time, no DST
EMERGENCY_KINDS = ("police", "hospital", "ambulance")


@dataclass
class CrashReport:
    device_id: str
    plate: str
    event_id: str
    ts: float
    lat: float
    lon: float
    peak_g: float = 0.0
    delta_v_kmh: float = 0.0
    tilt_deg: float = 0.0
    speed_before_kmh: float = 0.0
    stationary_s: float = 0.0


def assess(r: CrashReport) -> float:
    """Heuristic 0..1 confidence from impact, speed change, rollover and post-impact stillness."""
    s = 0.0
    s += 0.5 if r.peak_g >= 8 else 0.4 if r.peak_g >= 5 else 0.2 if r.peak_g >= 3 else 0.0
    s += 0.3 if r.delta_v_kmh >= 40 else 0.2 if r.delta_v_kmh >= 25 else 0.1 if r.delta_v_kmh >= 15 else 0.0
    s += 0.25 if r.tilt_deg >= 60 else 0.1 if r.tilt_deg >= 35 else 0.0
    s += 0.15 if r.stationary_s >= 10 else 0.0
    return round(min(1.0, s), 2)


def _maps(lat: float, lon: float) -> str:
    return f"https://maps.google.com/?q={lat:.5f},{lon:.5f}"


class CrashService:
    def __init__(self, store, notifier, cfg) -> None:
        self.store, self.notifier, self.cfg = store, notifier, cfg

    # ---- public API ----
    def handle(self, r: CrashReport) -> dict:
        existing = self.store.get_crash_by_event(r.device_id, r.event_id)
        if existing:  # device retried the same event
            return self._view(existing, duplicate=True)
        recent = self.store.recent_active_crash(r.device_id, time.time() - self.cfg.crash_merge_window_s)
        if recent:  # same incident reported again under a new event id
            return self._view(recent, merged=True)

        conf = assess(r)
        c = self.cfg
        if conf >= c.crash_full_dispatch_conf and r.speed_before_kmh >= c.crash_min_speed_kmh:
            tier = "full"
        elif conf >= c.crash_nok_conf:
            tier = "nok"
        else:
            tier = "log"

        landmark = nearest_landmark(r.lat, r.lon)
        crash_id = self.store.add_crash(
            device_id=r.device_id, event_id=r.event_id, plate=r.plate, ts=r.ts, lat=r.lat, lon=r.lon,
            peak_g=r.peak_g, delta_v_kmh=r.delta_v_kmh, tilt_deg=r.tilt_deg,
            speed_before_kmh=r.speed_before_kmh, stationary_s=r.stationary_s,
            confidence=conf, status="PENDING", landmark=landmark, created_at=time.time(),
        )
        crash = self.store.get_crash(crash_id)
        sent = self._dispatch(crash, tier) if tier != "log" else []

        units_ok = any(s["recipient_kind"] in EMERGENCY_KINDS and s["status"] == "sent" for s in sent)
        nok_ok = any(s["recipient_kind"] == "next_of_kin" and s["status"] == "sent" for s in sent)
        if tier == "log":
            status = "LOGGED_ONLY"
        elif units_ok:
            status = "DISPATCHED"
        elif nok_ok:
            status = "NOK_NOTIFIED"
        else:
            status = "DISPATCH_FAILED"  # nobody was reached: the operations desk must act
        self.store.set_crash_status(crash_id, status)
        return self._view(self.store.get_crash(crash_id))

    def cancel(self, crash_id: int, by: str) -> dict | None:
        crash = self.store.get_crash(crash_id)
        if crash is None:
            return None
        if crash["status"] == "CANCELLED":
            return self._view(crash)
        notified = [d for d in self.store.dispatches_for(crash_id) if d["status"] == "sent"
                    and d["recipient_kind"] != "cancel_notice"]
        self.store.set_crash_status(crash_id, "CANCELLED")
        ref = f"SR{crash_id:05d}"
        msg = f"SAFEROUTE ref {ref}: FALSE ALARM, cancelled by {by}. No response needed. Thank you."
        for d in notified:
            ok, detail = self._send(d["phone"], msg)
            self.store.add_dispatch(crash_id, "cancel_notice", d["recipient_name"], d["phone"], msg,
                                    "sent" if ok else "failed", detail)
        return self._view(self.store.get_crash(crash_id))

    def confirm(self, crash_id: int) -> dict | None:
        crash = self.store.get_crash(crash_id)
        if crash is None:
            return None
        self.store.set_crash_status(crash_id, "CONFIRMED")
        return self._view(self.store.get_crash(crash_id))

    # ---- internals ----
    def _view(self, crash: dict, **flags) -> dict:
        return {**flags, "crash": crash, "dispatches": [
            {k: d[k] for k in ("recipient_kind", "recipient_name", "status", "message")}
            for d in self.store.dispatches_for(crash["id"])]}

    def _send(self, phone: str, message: str) -> tuple[bool, str]:
        detail = ""
        for _ in range(max(1, self.cfg.sms_attempts)):
            ok, detail = self.notifier.send(phone, message)
            if ok:
                return True, detail
        return False, detail

    def _nearest_units(self, lat: float, lon: float) -> list[dict]:
        picks = []
        for kind in EMERGENCY_KINDS:
            units = self.store.list_emergency_units(kind)
            if units:
                picks.append(min(units, key=lambda u: haversine_m(lat, lon, u["lat"], u["lon"])))
        return picks

    def _dispatch(self, crash: dict, tier: str) -> list[dict]:
        veh = self.store.get_vehicle(crash["plate"]) or {}
        when = datetime.fromtimestamp(crash["ts"], EAT).strftime("%H:%M")
        ref = f"SR{crash['id']:05d}"
        lat, lon, where = crash["lat"], crash["lon"], crash["landmark"]
        who = f"{veh.get('vehicle_type', 'vehicle')} {crash['plate']}" + (f" ({veh['sacco']})" if veh.get("sacco") else "")

        recipients: list[tuple[str, str, str, str]] = []  # kind, name, phone, message
        if tier == "full":
            for u in self._nearest_units(lat, lon):
                km = haversine_m(lat, lon, u["lat"], u["lon"]) / 1000.0
                recipients.append((u["kind"], u["name"], u["phone"],
                    f"SAFEROUTE CRASH ALERT {ref}: {who} possible serious crash at {when}, {where}. "
                    f"GPS {lat:.5f},{lon:.5f} ({km:.0f} km from {u['name']}). Map {_maps(lat, lon)}. "
                    f"Automatic sensor alert, not yet verified."))
        for k in self.store.contacts_for(crash["plate"], consent_only=True):
            recipients.append(("next_of_kin", k["name"], k["phone"],
                f"SAFEROUTE {ref}: {crash['plate']} may have been in a crash at {when}, {where}. "
                f"Please call the driver now. If no answer, call 999 or 112. Map {_maps(lat, lon)}"))

        sent = []
        for kind, name, phone, msg in recipients:
            ok, detail = self._send(phone, msg)
            self.store.add_dispatch(crash["id"], kind, name, phone, msg, "sent" if ok else "failed", detail)
            sent.append({"recipient_kind": kind, "status": "sent" if ok else "failed"})
        return sent
