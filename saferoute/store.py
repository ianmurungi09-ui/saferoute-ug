"""Persistence layer.

`Store` is a stdlib-sqlite implementation so the whole platform runs with no
database server. The production schema (PostgreSQL + PostGIS) lives in
db/schema.sql; a PostGIS-backed class with the same method names is the next
step. Raw GPS tracks are deliberately NOT stored: only derived driving events
and distance are kept, which limits how much personal location data exists.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Iterable

from .geo import haversine_m


def norm_plate(plate: str) -> str:
    return re.sub(r"[\s-]+", "", plate).upper()


SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles(
  plate TEXT PRIMARY KEY, vehicle_type TEXT NOT NULL, sacco TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS devices(
  device_id TEXT PRIMARY KEY, plate TEXT NOT NULL REFERENCES vehicles(plate),
  key_hash TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, plate TEXT NOT NULL REFERENCES vehicles(plate),
  name TEXT NOT NULL, phone TEXT NOT NULL, relation TEXT, consent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS inspections(
  id INTEGER PRIMARY KEY AUTOINCREMENT, plate TEXT NOT NULL, inspector_id TEXT NOT NULL,
  inspected_at REAL NOT NULL, valid_until REAL NOT NULL, status TEXT NOT NULL,
  failed_items TEXT NOT NULL, items TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_insp_plate ON inspections(plate, inspected_at);
CREATE TABLE IF NOT EXISTS checkpoints(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, phone TEXT);
CREATE TABLE IF NOT EXISTS emergency_units(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL,
  lat REAL NOT NULL, lon REAL NOT NULL, phone TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS speed_zones(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  radius_m REAL NOT NULL, limit_kmh REAL NOT NULL);
CREATE TABLE IF NOT EXISTS driving_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, plate TEXT NOT NULL, ts REAL NOT NULL,
  kind TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, value REAL NOT NULL, duration_s REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ev_plate_ts ON driving_events(plate, ts);
CREATE TABLE IF NOT EXISTS distance_log(
  device_id TEXT NOT NULL, plate TEXT NOT NULL, ts REAL NOT NULL, km REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_dist_plate_ts ON distance_log(plate, ts);
CREATE TABLE IF NOT EXISTS hazards(
  id INTEGER PRIMARY KEY AUTOINCREMENT, lat REAL NOT NULL, lon REAL NOT NULL,
  n_reports INTEGER NOT NULL, max_g REAL NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, repaired_at REAL);
CREATE TABLE IF NOT EXISTS hazard_reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT, hazard_id INTEGER NOT NULL REFERENCES hazards(id),
  device_id TEXT NOT NULL, ts REAL NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  peak_g REAL NOT NULL, speed_kmh REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_hr_hazard ON hazard_reports(hazard_id, ts);
CREATE TABLE IF NOT EXISTS crashes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, event_id TEXT NOT NULL, plate TEXT NOT NULL,
  ts REAL NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, peak_g REAL, delta_v_kmh REAL, tilt_deg REAL,
  speed_before_kmh REAL, stationary_s REAL, confidence REAL NOT NULL, status TEXT NOT NULL,
  landmark TEXT, created_at REAL NOT NULL, UNIQUE(device_id, event_id));
CREATE TABLE IF NOT EXISTS dispatches(
  id INTEGER PRIMARY KEY AUTOINCREMENT, crash_id INTEGER NOT NULL REFERENCES crashes(id),
  recipient_kind TEXT NOT NULL, recipient_name TEXT NOT NULL, phone TEXT NOT NULL, message TEXT NOT NULL,
  status TEXT NOT NULL, detail TEXT, sent_at REAL NOT NULL);
"""

DEFAULT_ZONES = [  # placeholder built-up-area zones; replace with surveyed limits
    ("Kampala built-up", 0.3476, 32.5825, 6000, 50),
    ("Entebbe built-up", 0.0512, 32.4637, 3000, 50),
    ("Jinja built-up", 0.4244, 33.2041, 3000, 50),
    ("Mbarara built-up", -0.6072, 30.6545, 3000, 50),
    ("Gulu built-up", 2.7746, 32.2990, 3000, 50),
    ("Mbale built-up", 1.0821, 34.1751, 3000, 50),
]


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._c.executescript(SCHEMA)

    # ---- low-level helpers ----
    def _run(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock:
            return self._c.execute(sql, tuple(args)).lastrowid

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(sql, tuple(args)).fetchall()]

    def _one(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # ---- vehicles, devices, contacts ----
    def upsert_vehicle(self, plate: str, vehicle_type: str, sacco: str | None) -> dict:
        plate = norm_plate(plate)
        self._run(
            "INSERT INTO vehicles(plate,vehicle_type,sacco,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(plate) DO UPDATE SET vehicle_type=excluded.vehicle_type, sacco=excluded.sacco",
            (plate, vehicle_type, sacco, time.time()),
        )
        return self.get_vehicle(plate)  # type: ignore[return-value]

    def get_vehicle(self, plate: str) -> dict | None:
        return self._one("SELECT * FROM vehicles WHERE plate=?", (norm_plate(plate),))

    def vehicles_in_sacco(self, sacco: str) -> list[dict]:
        return self._all("SELECT * FROM vehicles WHERE sacco=? ORDER BY plate", (sacco,))

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def register_device(self, device_id: str, plate: str) -> str:
        """Create or rotate a device. The plaintext key is returned once and never stored."""
        key = secrets.token_urlsafe(24)
        self._run(
            "INSERT INTO devices(device_id,plate,key_hash,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET plate=excluded.plate, key_hash=excluded.key_hash",
            (device_id, norm_plate(plate), self._hash(key), time.time()),
        )
        return key

    def verify_device(self, device_id: str, key: str) -> dict | None:
        if not key:
            return None
        row = self._one("SELECT device_id, plate, key_hash FROM devices WHERE device_id=?", (device_id,))
        if row and secrets.compare_digest(row["key_hash"], self._hash(key)):
            return {"device_id": row["device_id"], "plate": row["plate"]}
        return None

    def add_contact(self, plate: str, name: str, phone: str, relation: str | None, consent: bool) -> int:
        return self._run(
            "INSERT INTO contacts(plate,name,phone,relation,consent) VALUES(?,?,?,?,?)",
            (norm_plate(plate), name, phone, relation, 1 if consent else 0),
        )

    def contacts_for(self, plate: str, consent_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM contacts WHERE plate=?" + (" AND consent=1" if consent_only else "")
        return self._all(sql, (norm_plate(plate),))

    # ---- inspections ----
    def add_inspection(self, plate: str, inspector_id: str, inspected_at: float, valid_until: float,
                       status: str, failed_items: list, items: dict) -> int:
        return self._run(
            "INSERT INTO inspections(plate,inspector_id,inspected_at,valid_until,status,failed_items,items) "
            "VALUES(?,?,?,?,?,?,?)",
            (norm_plate(plate), inspector_id, inspected_at, valid_until, status,
             json.dumps(failed_items), json.dumps(items)),
        )

    def latest_inspection(self, plate: str) -> dict | None:
        row = self._one(
            "SELECT * FROM inspections WHERE plate=? ORDER BY inspected_at DESC, id DESC LIMIT 1",
            (norm_plate(plate),),
        )
        if row:
            row["failed_items"] = json.loads(row["failed_items"])
            row["items"] = json.loads(row["items"])
        return row

    # ---- reference data: checkpoints, emergency units, speed zones ----
    def add_checkpoint(self, name: str, lat: float, lon: float, phone: str | None) -> int:
        return self._run("INSERT INTO checkpoints(name,lat,lon,phone) VALUES(?,?,?,?)", (name, lat, lon, phone))

    def list_checkpoints(self) -> list[dict]:
        return self._all("SELECT * FROM checkpoints")

    def add_emergency_unit(self, name: str, kind: str, lat: float, lon: float, phone: str) -> int:
        return self._run(
            "INSERT INTO emergency_units(name,kind,lat,lon,phone) VALUES(?,?,?,?,?)",
            (name, kind, lat, lon, phone),
        )

    def list_emergency_units(self, kind: str | None = None) -> list[dict]:
        if kind:
            return self._all("SELECT * FROM emergency_units WHERE kind=?", (kind,))
        return self._all("SELECT * FROM emergency_units")

    def add_speed_zone(self, name: str, lat: float, lon: float, radius_m: float, limit_kmh: float) -> int:
        return self._run(
            "INSERT INTO speed_zones(name,lat,lon,radius_m,limit_kmh) VALUES(?,?,?,?,?)",
            (name, lat, lon, radius_m, limit_kmh),
        )

    def list_speed_zones(self) -> list[dict]:
        return self._all("SELECT * FROM speed_zones")

    def seed_default_zones(self) -> None:
        if not self.list_speed_zones():
            for z in DEFAULT_ZONES:
                self.add_speed_zone(*z)

    # ---- telematics ----
    def add_events(self, device_id: str, plate: str, events: list) -> None:
        with self._lock:
            self._c.executemany(
                "INSERT INTO driving_events(device_id,plate,ts,kind,lat,lon,value,duration_s) VALUES(?,?,?,?,?,?,?,?)",
                [(device_id, plate, e.ts, e.kind, e.lat, e.lon, e.value, e.duration_s) for e in events],
            )

    def add_distance(self, device_id: str, plate: str, ts: float, km: float) -> None:
        if km > 0:
            self._run("INSERT INTO distance_log(device_id,plate,ts,km) VALUES(?,?,?,?)", (device_id, plate, ts, km))

    def events_since(self, plate: str, since: float) -> list[dict]:
        return self._all("SELECT * FROM driving_events WHERE plate=? AND ts>=? ORDER BY ts", (norm_plate(plate), since))

    def distance_since(self, plate: str, since: float) -> float:
        row = self._one("SELECT COALESCE(SUM(km),0) AS km FROM distance_log WHERE plate=? AND ts>=?",
                        (norm_plate(plate), since))
        return float(row["km"]) if row else 0.0

    # ---- hazards ----
    def find_hazard_near(self, lat: float, lon: float, radius_m: float) -> dict | None:
        dlat = radius_m / 111_320.0 * 1.5
        dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
        rows = self._all(
            "SELECT * FROM hazards WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        )
        best: tuple[float, dict] | None = None
        for r in rows:
            d = haversine_m(lat, lon, r["lat"], r["lon"])
            if d <= radius_m and (best is None or d < best[0]):
                best = (d, r)
        return best[1] if best else None

    def create_hazard(self, lat: float, lon: float, max_g: float, severity: str, ts: float) -> int:
        return self._run(
            "INSERT INTO hazards(lat,lon,n_reports,max_g,severity,status,first_seen,last_seen) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (lat, lon, 1, max_g, severity, "reported", ts, ts),
        )

    _HAZARD_FIELDS = {"lat", "lon", "n_reports", "max_g", "severity", "status", "last_seen", "repaired_at"}

    def update_hazard(self, hazard_id: int, **fields: Any) -> None:
        bad = set(fields) - self._HAZARD_FIELDS
        if bad:
            raise ValueError(f"unknown hazard fields: {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE hazards SET {sets} WHERE id=?", (*fields.values(), hazard_id))

    def get_hazard(self, hazard_id: int) -> dict | None:
        return self._one("SELECT * FROM hazards WHERE id=?", (hazard_id,))

    def add_hazard_report(self, hazard_id: int, device_id: str, ts: float, lat: float, lon: float,
                          peak_g: float, speed_kmh: float) -> None:
        self._run(
            "INSERT INTO hazard_reports(hazard_id,device_id,ts,lat,lon,peak_g,speed_kmh) VALUES(?,?,?,?,?,?,?)",
            (hazard_id, device_id, ts, lat, lon, peak_g, speed_kmh),
        )

    def hazard_device_count(self, hazard_id: int, since: float | None = None) -> int:
        if since is None:
            row = self._one("SELECT COUNT(DISTINCT device_id) AS n FROM hazard_reports WHERE hazard_id=?", (hazard_id,))
        else:
            row = self._one(
                "SELECT COUNT(DISTINCT device_id) AS n FROM hazard_reports WHERE hazard_id=? AND ts>=?",
                (hazard_id, since),
            )
        return int(row["n"]) if row else 0

    def list_hazards(self, statuses: Iterable[str], bbox: tuple[float, float, float, float] | None = None) -> list[dict]:
        statuses = list(statuses)
        q = f"SELECT * FROM hazards WHERE status IN ({','.join('?' * len(statuses))})"
        args: list[Any] = list(statuses)
        if bbox:
            min_lon, min_lat, max_lon, max_lat = bbox
            q += " AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
            args += [min_lon, max_lon, min_lat, max_lat]
        return self._all(q + " ORDER BY id", args)

    # ---- crashes ----
    def add_crash(self, **f: Any) -> int:
        cols = ("device_id", "event_id", "plate", "ts", "lat", "lon", "peak_g", "delta_v_kmh", "tilt_deg",
                "speed_before_kmh", "stationary_s", "confidence", "status", "landmark", "created_at")
        return self._run(
            f"INSERT INTO crashes({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            [f.get(c) for c in cols],
        )

    def get_crash(self, crash_id: int) -> dict | None:
        return self._one("SELECT * FROM crashes WHERE id=?", (crash_id,))

    def get_crash_by_event(self, device_id: str, event_id: str) -> dict | None:
        return self._one("SELECT * FROM crashes WHERE device_id=? AND event_id=?", (device_id, event_id))

    def recent_active_crash(self, device_id: str, since: float) -> dict | None:
        return self._one(
            "SELECT * FROM crashes WHERE device_id=? AND created_at>=? "
            "AND status IN ('DISPATCHED','NOK_NOTIFIED') ORDER BY id DESC LIMIT 1",
            (device_id, since),
        )

    def set_crash_status(self, crash_id: int, status: str) -> None:
        self._run("UPDATE crashes SET status=? WHERE id=?", (status, crash_id))

    def list_crashes(self, limit: int = 50) -> list[dict]:
        return self._all("SELECT * FROM crashes ORDER BY id DESC LIMIT ?", (limit,))

    def add_dispatch(self, crash_id: int, kind: str, name: str, phone: str, message: str,
                     status: str, detail: str) -> int:
        return self._run(
            "INSERT INTO dispatches(crash_id,recipient_kind,recipient_name,phone,message,status,detail,sent_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (crash_id, kind, name, phone, message, status, detail, time.time()),
        )

    def dispatches_for(self, crash_id: int) -> list[dict]:
        return self._all("SELECT * FROM dispatches WHERE crash_id=? ORDER BY id", (crash_id,))
