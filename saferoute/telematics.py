"""Module 2: behaviour scoring from GPS speed (and optional lateral acceleration).

Points arrive in batches (devices store-and-forward when the network drops).
An over-speed run is reported when it ENDS (driver slows down, or a signal gap
is detected), carrying its full duration and peak excess. Per-device state lives
in memory, so a run still open when the process restarts is lost.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .geo import haversine_m


@dataclass
class Point:
    ts: float
    lat: float
    lon: float
    speed_kmh: float
    heading: float | None = None
    ay: float | None = None  # lateral acceleration, m/s^2


@dataclass
class Event:
    kind: str  # overspeed | harsh_brake | harsh_accel | swerve
    ts: float
    lat: float
    lon: float
    value: float  # overspeed: max excess km/h; brake/accel/swerve: m/s^2
    duration_s: float = 0.0

    def as_dict(self) -> dict:
        return {"kind": self.kind, "ts": self.ts, "lat": self.lat, "lon": self.lon,
                "value": self.value, "duration_s": self.duration_s}


class SpeedZones:
    def __init__(self, default_kmh: float) -> None:
        self.default_kmh = default_kmh
        self._zones: list[dict] = []

    def set_zones(self, zones: list[dict]) -> None:
        self._zones = list(zones)

    def limit_at(self, lat: float, lon: float) -> float:
        limits = [z["limit_kmh"] for z in self._zones
                  if haversine_m(lat, lon, z["lat"], z["lon"]) <= z["radius_m"]]
        return min(limits) if limits else self.default_kmh


@dataclass
class _State:
    last: Point | None = None
    run: dict | None = None
    last_event_ts: dict = field(default_factory=dict)


class TripAnalyzer:
    EVENT_DEBOUNCE_S = 5.0

    def __init__(self, cfg, zones: SpeedZones) -> None:
        self.cfg = cfg
        self.zones = zones
        self._st: dict[str, _State] = {}

    def process(self, device_id: str, points: list[Point]) -> tuple[list[Event], float]:
        """Return (events, km travelled) for a batch of points from one device."""
        cfg = self.cfg
        st = self._st.setdefault(device_id, _State())
        events: list[Event] = []
        km = 0.0
        for p in sorted(points, key=lambda x: x.ts):
            prev = st.last
            if prev is not None and p.ts <= prev.ts:
                continue  # duplicate or out-of-order retransmission
            if prev is None or p.ts - prev.ts > cfg.max_gap_s:
                self._close_run(st, prev, events)  # signal gap: don't join two separate runs
                st.last = p
                self._speed_check(st, p, events)
                continue
            dt = p.ts - prev.ts
            d = haversine_m(prev.lat, prev.lon, p.lat, p.lon)
            if d / dt * 3.6 > cfg.max_plausible_kmh:
                continue  # GPS glitch: drop the point, keep the previous good one
            km += d / 1000.0
            if dt <= 5.0:  # longer intervals smear acceleration; skip the check
                accel = (p.speed_kmh - prev.speed_kmh) / 3.6 / dt
                if accel <= -cfg.harsh_brake_ms2:
                    self._emit(st, events, "harsh_brake", p, accel)
                elif accel >= cfg.harsh_accel_ms2:
                    self._emit(st, events, "harsh_accel", p, accel)
            if p.ay is not None and abs(p.ay) >= cfg.swerve_ms2 and p.speed_kmh >= 30:
                self._emit(st, events, "swerve", p, abs(p.ay))
            self._speed_check(st, p, events)
            st.last = p
        return events, km

    # ---- internals ----
    def _emit(self, st: _State, events: list[Event], kind: str, p: Point, value: float) -> None:
        last = st.last_event_ts.get(kind)
        if last is not None and p.ts - last < self.EVENT_DEBOUNCE_S:
            return
        st.last_event_ts[kind] = p.ts
        events.append(Event(kind, p.ts, p.lat, p.lon, round(value, 2)))

    def _speed_check(self, st: _State, p: Point, events: list[Event]) -> None:
        limit = self.zones.limit_at(p.lat, p.lon)
        excess = p.speed_kmh - limit
        if excess > self.cfg.overspeed_tolerance_kmh:
            if st.run is None:
                st.run = {"start": p, "max_excess": excess}
            else:
                st.run["max_excess"] = max(st.run["max_excess"], excess)
        else:
            self._close_run(st, p, events)

    def _close_run(self, st: _State, end: Point | None, events: list[Event]) -> None:
        run, st.run = st.run, None
        if not run or end is None:
            return
        start: Point = run["start"]
        dur = end.ts - start.ts
        if dur >= self.cfg.min_overspeed_s:  # sustained speeding only, not a brief overtake
            events.append(Event("overspeed", start.ts, start.lat, start.lon,
                                round(run["max_excess"], 1), round(dur, 1)))


def event_points(kind: str, value: float, duration_s: float) -> float:
    if kind == "overspeed":
        return min(10.0, 2.0 + duration_s / 30.0 + value / 10.0)
    return {"harsh_brake": 2.0, "harsh_accel": 1.0, "swerve": 2.0}.get(kind, 0.0)


def compute_score(events: list[dict], km: float) -> dict:
    """Heuristic 0-100 score: penalty grows with weighted events per 100 km.
    Tune the 1.5 multiplier and weights against real fleet data."""
    counts: dict[str, int] = {}
    pts = 0.0
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        pts += event_points(e["kind"], e["value"], e["duration_s"])
    if km < 5.0:
        return {"score": None, "grade": None, "insufficient_data": True,
                "distance_km": round(km, 1), "events": counts}
    per100 = pts / max(km, 10.0) * 100.0
    score = max(0.0, min(100.0, 100.0 - 1.5 * per100))
    grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 50 else "E"
    return {"score": round(score, 1), "grade": grade, "insufficient_data": False,
            "distance_km": round(km, 1), "events": counts, "weighted_points": round(pts, 1),
            "points_per_100km": round(per100, 1)}


def vehicle_score(store, plate: str, days: int, now: float) -> dict:
    since = now - days * 86400.0
    out = compute_score(store.events_since(plate, since), store.distance_since(plate, since))
    out.update({"plate": plate, "window_days": days})
    return out
