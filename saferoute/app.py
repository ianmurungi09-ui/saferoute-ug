"""HTTP + WebSocket API (Starlette).

Auth model, deliberately simple for a first deployment:
  * admin endpoints      -> X-Admin-Key (env SAFEROUTE_ADMIN_KEY)
  * device endpoints     -> X-Device-Key issued by POST /admin/devices (stored hashed)
  * hazard map GeoJSON   -> public (no personal data in it)
Split the admin key into per-role keys (inspector, police, insurer, SACCO) before real use.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .config import Settings
from .crash import CrashReport, CrashService
from .geo import bearing_deg, haversine_m, is_ahead
from .hazards import HazardService
from .hub import Hub
from .inspection import FLAGGED, compliance_status, evaluate
from .notify import build_notifier, normalize_ug_phone
from .risk import risk_profile
from .store import Store, norm_plate
from .telematics import Point, SpeedZones, TripAnalyzer, vehicle_score

log = logging.getLogger("saferoute")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


# --------------------------------------------------------------------------- plumbing
class ApiError(Exception):
    def __init__(self, status: int, message: str, detail=None) -> None:
        self.status, self.message, self.detail = status, message, detail


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    body = {"error": exc.message}
    if exc.detail is not None:
        body["detail"] = exc.detail
    return JSONResponse(body, status_code=exc.status)


class Services:
    def __init__(self, cfg: Settings, store: Store | None = None, notifier=None) -> None:
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.notifier = notifier or build_notifier(cfg)
        self.hub = Hub()
        if cfg.seed_speed_zones:
            self.store.seed_default_zones()
        self.zones = SpeedZones(cfg.default_speed_limit_kmh)
        self.reload_zones()
        self.analyzer = TripAnalyzer(cfg, self.zones)
        self.hazards = HazardService(self.store, cfg)
        self.crashes = CrashService(self.store, self.notifier, cfg)
        self._seen: dict[tuple, float] = {}

    def reload_zones(self) -> None:
        self.zones.set_zones(self.store.list_speed_zones())

    def first_time(self, key: tuple, cooldown_s: float) -> bool:
        """Cooldown gate so the same alert isn't repeated on every telemetry batch."""
        now = time.time()
        last = self._seen.get(key)
        if last is not None and now - last < cooldown_s:
            return False
        self._seen[key] = now
        return True


def svc_of(request: Request) -> Services:
    return request.app.state.svc


def need_admin(request: Request) -> None:
    key = request.headers.get("x-admin-key", "")
    expected = svc_of(request).cfg.admin_key
    if not key or not hmac.compare_digest(key.encode(), expected.encode()):
        raise ApiError(401, "admin key required")


def need_device(request: Request, device_id: str) -> dict:
    dev = svc_of(request).store.verify_device(device_id, request.headers.get("x-device-key", ""))
    if not dev:
        raise ApiError(401, "invalid device credentials")
    return dev


async def parse(request: Request, model):
    try:
        raw = await request.json()
    except Exception:
        raise ApiError(400, "body must be valid JSON")
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise ApiError(422, "validation failed", json.loads(e.json(include_url=False)))


# --------------------------------------------------------------------------- request models
Lat = Field(ge=-90, le=90)
Lon = Field(ge=-180, le=180)


class VehicleIn(BaseModel):
    plate: str = Field(min_length=3, max_length=12)
    vehicle_type: Literal["bus", "taxi", "truck", "boda", "private", "other"]
    sacco: str | None = None


class DeviceIn(BaseModel):
    device_id: str = Field(min_length=3, max_length=40)
    plate: str


class ContactIn(BaseModel):
    plate: str
    name: str
    phone: str
    relation: str | None = None
    consent: bool


class UnitIn(BaseModel):
    name: str
    kind: Literal["police", "hospital", "ambulance"]
    lat: float = Lat
    lon: float = Lon
    phone: str


class CheckpointIn(BaseModel):
    name: str
    lat: float = Lat
    lon: float = Lon
    phone: str | None = None


class ZoneIn(BaseModel):
    name: str
    lat: float = Lat
    lon: float = Lon
    radius_m: float = Field(gt=0, le=100_000)
    limit_kmh: float = Field(gt=0, le=200)


class HazardStatusIn(BaseModel):
    status: Literal["confirmed", "repaired", "dismissed"]


class InspectionItems(BaseModel):
    brake_efficiency_pct: float = Field(ge=0, le=100)
    handbrake_holds: bool | None = None
    tyre_tread_mm_min: float = Field(ge=0, le=30)
    tyre_damage: bool = False
    steering_play_ok: bool
    lights_ok: bool
    suspension_ok: bool = True
    windscreen_wipers_ok: bool | None = None
    seatbelts_ok: bool | None = None
    speed_limiter_ok: bool | None = None
    fire_extinguisher_ok: bool | None = None


class InspectionIn(BaseModel):
    plate: str
    inspector_id: str = Field(min_length=2)
    items: InspectionItems
    inspected_at: float | None = None


class PointIn(BaseModel):
    ts: float
    lat: float = Lat
    lon: float = Lon
    speed_kmh: float = Field(ge=0, le=300)
    heading: float | None = Field(None, ge=0, le=360)
    ay: float | None = None


class TelemetryIn(BaseModel):
    device_id: str
    points: list[PointIn] = Field(min_length=1, max_length=500)


class HazardReportIn(BaseModel):
    device_id: str
    ts: float | None = None
    lat: float = Lat
    lon: float = Lon
    peak_g: float = Field(gt=0)
    speed_kmh: float = Field(ge=0, le=300)


class CrashIn(BaseModel):
    device_id: str
    event_id: str = Field(min_length=6, max_length=64)
    ts: float | None = None
    lat: float = Lat
    lon: float = Lon
    peak_g: float = Field(0, ge=0)
    delta_v_kmh: float = Field(0, ge=0)
    tilt_deg: float = Field(0, ge=0, le=180)
    speed_before_kmh: float = Field(0, ge=0, le=300)
    stationary_s: float = Field(0, ge=0)


# --------------------------------------------------------------------------- admin
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def admin_vehicle(request: Request) -> JSONResponse:
    need_admin(request)
    b = await parse(request, VehicleIn)
    return JSONResponse(svc_of(request).store.upsert_vehicle(b.plate, b.vehicle_type, b.sacco), status_code=201)


async def admin_device(request: Request) -> JSONResponse:
    need_admin(request)
    b = await parse(request, DeviceIn)
    s = svc_of(request)
    if not s.store.get_vehicle(b.plate):
        raise ApiError(404, "unknown plate; register the vehicle first")
    key = s.store.register_device(b.device_id, b.plate)
    return JSONResponse({"device_id": b.device_id, "plate": norm_plate(b.plate), "api_key": key,
                         "note": "store this key on the device now; it cannot be shown again"}, status_code=201)


async def admin_contact(request: Request) -> JSONResponse:
    need_admin(request)
    b = await parse(request, ContactIn)
    s = svc_of(request)
    phone = normalize_ug_phone(b.phone)
    if not phone:
        raise ApiError(422, "phone is not a valid Ugandan number")
    if not s.store.get_vehicle(b.plate):
        raise ApiError(404, "unknown plate")
    cid = s.store.add_contact(b.plate, b.name, phone, b.relation, b.consent)
    return JSONResponse({"id": cid, "phone": phone, "consent": b.consent}, status_code=201)


async def admin_unit(request: Request) -> JSONResponse:
    need_admin(request)
    b = await parse(request, UnitIn)
    phone = normalize_ug_phone(b.phone)
    if not phone:
        raise ApiError(422, "phone is not a valid Ugandan number")
    uid = svc_of(request).store.add_emergency_unit(b.name, b.kind, b.lat, b.lon, phone)
    return JSONResponse({"id": uid}, status_code=201)


async def admin_checkpoint(request: Request) -> JSONResponse:
    need_admin(request)
    b = await parse(request, CheckpointIn)
    phone = normalize_ug_phone(b.phone) if b.phone else None
    cid = svc_of(request).store.add_checkpoint(b.name, b.lat, b.lon, phone)
    return JSONResponse({"id": cid}, status_code=201)


async def admin_zone(request: Request) -> JSONResponse:
    need_admin(request)
    b = await parse(request, ZoneIn)
    s = svc_of(request)
    zid = s.store.add_speed_zone(b.name, b.lat, b.lon, b.radius_m, b.limit_kmh)
    s.reload_zones()
    return JSONResponse({"id": zid}, status_code=201)


async def admin_hazard_status(request: Request) -> JSONResponse:
    need_admin(request)
    b = await parse(request, HazardStatusIn)
    h = svc_of(request).hazards.set_status(int(request.path_params["hazard_id"]), b.status, time.time())
    if h is None:
        raise ApiError(404, "unknown hazard")
    return JSONResponse(h)


async def ops_recent(request: Request) -> JSONResponse:
    need_admin(request)
    return JSONResponse(svc_of(request).hub.recent("ops"))


# --------------------------------------------------------------------------- module 1
async def post_inspection(request: Request) -> JSONResponse:
    need_admin(request)  # TODO: restrict to accredited inspector keys
    b = await parse(request, InspectionIn)
    s = svc_of(request)
    veh = s.store.get_vehicle(b.plate)
    if not veh:
        raise ApiError(404, "unknown plate")
    at = b.inspected_at or time.time()
    items = b.items.model_dump()
    ev = evaluate(items, veh["vehicle_type"], at, s.cfg)
    s.store.add_inspection(b.plate, b.inspector_id, at, ev.valid_until, ev.status, ev.failed, items)
    await s.hub.publish("ops", {"type": "inspection", "plate": veh["plate"], "status": ev.status,
                                "failed": [f["item"] for f in ev.failed]})
    return JSONResponse(compliance_status(s.store, veh["plate"], time.time()), status_code=201)


async def get_compliance(request: Request) -> JSONResponse:
    need_admin(request)
    s = svc_of(request)
    plate = norm_plate(request.path_params["plate"])
    if not s.store.get_vehicle(plate):
        raise ApiError(404, "unknown plate")
    return JSONResponse(compliance_status(s.store, plate, time.time()))


# --------------------------------------------------------------------------- module 2
async def post_telemetry(request: Request) -> JSONResponse:
    b = await parse(request, TelemetryIn)
    dev = need_device(request, b.device_id)
    s = svc_of(request)
    plate = dev["plate"]
    now = time.time()

    pts = [Point(p.ts, p.lat, p.lon, p.speed_kmh, p.heading, p.ay) for p in b.points if p.ts <= now + 300]
    rejected = len(b.points) - len(pts)  # points stamped in the future: bad device clock
    if not pts:
        raise ApiError(422, "all points have timestamps in the future; check the device clock")
    pts.sort(key=lambda p: p.ts)

    events, km = s.analyzer.process(b.device_id, pts)
    s.store.add_events(b.device_id, plate, events)
    s.store.add_distance(b.device_id, plate, pts[-1].ts, km)

    last = pts[-1]
    heading = last.heading
    if heading is None and len(pts) >= 2 and haversine_m(pts[-2].lat, pts[-2].lon, last.lat, last.lon) > 10:
        heading = bearing_deg(pts[-2].lat, pts[-2].lon, last.lat, last.lon)

    alerts = []
    for h in s.hazards.ahead(last.lat, last.lon, heading):
        if s.first_time(("hazard", b.device_id, h["id"]), s.cfg.hazard_alert_cooldown_s):
            alerts.append({"type": "hazard_ahead", "hazard_id": h["id"], "distance_m": h["distance_m"],
                           "severity": h["severity"],
                           "say": f"{h['severity'].capitalize()} road hazard ahead in {round(h['distance_m'], -1):.0f} metres. Slow down."})

    await s.hub.publish("ops", {"type": "position", "plate": plate, "lat": last.lat, "lon": last.lon,
                                "speed_kmh": last.speed_kmh, "ts": last.ts})
    for e in events:
        await s.hub.publish("ops", {"type": "driving_event", "plate": plate, **e.as_dict()})
    for a in alerts:
        await s.hub.publish(f"device:{b.device_id}", a)

    comp = compliance_status(s.store, plate, now)
    if comp["status"] in FLAGGED:
        for cp in s.store.list_checkpoints():
            d_km = haversine_m(last.lat, last.lon, cp["lat"], cp["lon"]) / 1000.0
            if d_km <= s.cfg.checkpoint_alert_radius_km and is_ahead(last.lat, last.lon, heading, cp["lat"], cp["lon"]):
                if s.first_time(("checkpoint", plate, cp["id"]), s.cfg.checkpoint_alert_cooldown_s):
                    msg = {"type": "flagged_vehicle_ahead", "plate": plate, "status": comp["status"],
                           "failed_items": [f["item"] for f in comp["failed_items"]],
                           "checkpoint": cp["name"], "distance_km": round(d_km, 1),
                           "eta_min": round(d_km / max(last.speed_kmh, 20.0) * 60.0)}
                    await s.hub.publish(f"checkpoint:{cp['id']}", msg)
                    await s.hub.publish("ops", msg)

    return JSONResponse({"accepted": len(pts), "rejected_future": rejected, "distance_km": round(km, 2),
                         "events": [e.as_dict() for e in events], "alerts": alerts})


async def get_score(request: Request) -> JSONResponse:
    need_admin(request)
    s = svc_of(request)
    plate = norm_plate(request.path_params["plate"])
    if not s.store.get_vehicle(plate):
        raise ApiError(404, "unknown plate")
    days = int(request.query_params.get("days", s.cfg.score_window_days))
    return JSONResponse(vehicle_score(s.store, plate, days, time.time()))


async def get_risk(request: Request) -> JSONResponse:
    need_admin(request)
    s = svc_of(request)
    plate = norm_plate(request.path_params["plate"])
    if not s.store.get_vehicle(plate):
        raise ApiError(404, "unknown plate")
    comp = compliance_status(s.store, plate, time.time())
    score = vehicle_score(s.store, plate, s.cfg.score_window_days, time.time())
    return JSONResponse({"plate": plate, "compliance": comp, "driving": score,
                         **risk_profile(comp["status"], score["score"])})


async def sacco_scores(request: Request) -> JSONResponse:
    need_admin(request)
    s = svc_of(request)
    sacco = request.path_params["sacco"]
    now = time.time()
    rows = []
    for v in s.store.vehicles_in_sacco(sacco):
        sc = vehicle_score(s.store, v["plate"], s.cfg.score_window_days, now)
        rows.append({"plate": v["plate"], "vehicle_type": v["vehicle_type"], "score": sc["score"],
                     "grade": sc["grade"], "compliance": compliance_status(s.store, v["plate"], now)["status"]})
    scored = [r["score"] for r in rows if r["score"] is not None]
    return JSONResponse({"sacco": sacco, "vehicles": rows,
                         "fleet_average": round(sum(scored) / len(scored), 1) if scored else None})


# --------------------------------------------------------------------------- module 3
async def post_hazard_report(request: Request) -> JSONResponse:
    b = await parse(request, HazardReportIn)
    need_device(request, b.device_id)
    s = svc_of(request)
    res = s.hazards.report(b.device_id, b.lat, b.lon, b.peak_g, b.speed_kmh, b.ts or time.time())
    if res.get("accepted") and res["status"] == "confirmed":
        await s.hub.publish("ops", {"type": "hazard_confirmed", "hazard_id": res["hazard_id"]})
    return JSONResponse(res, status_code=201 if res.get("accepted") else 200)


async def get_hazards(request: Request) -> JSONResponse:
    s = svc_of(request)
    q = request.query_params
    statuses = tuple(x for x in q.get("status", "reported,confirmed").split(",") if x in
                     ("reported", "confirmed", "repaired", "dismissed"))
    bbox = None
    if q.get("bbox"):
        try:
            vals = tuple(float(x) for x in q["bbox"].split(","))
            if len(vals) != 4:
                raise ValueError
            bbox = vals
        except ValueError:
            raise ApiError(400, "bbox must be min_lon,min_lat,max_lon,max_lat")
    if not statuses:
        raise ApiError(400, "no valid status values")
    return JSONResponse(s.hazards.geojson(statuses, bbox))  # type: ignore[arg-type]


async def get_hazards_ahead(request: Request) -> JSONResponse:
    s = svc_of(request)
    q = request.query_params
    try:
        lat, lon = float(q["lat"]), float(q["lon"])
        heading = float(q["heading"]) if "heading" in q else None
        radius = float(q["radius_m"]) if "radius_m" in q else None
    except (KeyError, ValueError):
        raise ApiError(400, "lat and lon are required numbers; heading and radius_m are optional numbers")
    return JSONResponse(s.hazards.ahead(lat, lon, heading, radius))


# --------------------------------------------------------------------------- module 4
async def post_crash(request: Request) -> JSONResponse:
    b = await parse(request, CrashIn)
    dev = need_device(request, b.device_id)
    s = svc_of(request)
    report = CrashReport(b.device_id, dev["plate"], b.event_id, b.ts or time.time(), b.lat, b.lon,
                         b.peak_g, b.delta_v_kmh, b.tilt_deg, b.speed_before_kmh, b.stationary_s)
    res = await run_in_threadpool(s.crashes.handle, report)  # SMS sending blocks
    if not (res.get("duplicate") or res.get("merged")):
        c = res["crash"]
        await s.hub.publish("ops", {"type": "crash", "crash_id": c["id"], "plate": c["plate"],
                                    "status": c["status"], "confidence": c["confidence"],
                                    "lat": c["lat"], "lon": c["lon"], "landmark": c["landmark"],
                                    "notified": [d["recipient_name"] for d in res["dispatches"] if d["status"] == "sent"]})
    return JSONResponse(res, status_code=200 if res.get("duplicate") or res.get("merged") else 201)


def _crash_actor(request: Request, crash_id: int) -> str:
    """Admin may act on any crash; a device only on its own."""
    s = svc_of(request)
    crash = s.store.get_crash(crash_id)
    if crash is None:
        raise ApiError(404, "unknown crash")
    if request.headers.get("x-admin-key"):
        need_admin(request)
        return "operations desk"
    need_device(request, request.headers.get("x-device-id", ""))
    if request.headers.get("x-device-id") != crash["device_id"]:
        raise ApiError(403, "device does not own this crash event")
    return "the driver"


async def crash_cancel(request: Request) -> JSONResponse:
    cid = int(request.path_params["crash_id"])
    by = _crash_actor(request, cid)
    s = svc_of(request)
    res = await run_in_threadpool(s.crashes.cancel, cid, by)
    await s.hub.publish("ops", {"type": "crash_cancelled", "crash_id": cid, "by": by})
    return JSONResponse(res)


async def crash_confirm(request: Request) -> JSONResponse:
    need_admin(request)
    cid = int(request.path_params["crash_id"])
    s = svc_of(request)
    res = s.crashes.confirm(cid)
    if res is None:
        raise ApiError(404, "unknown crash")
    await s.hub.publish("ops", {"type": "crash_confirmed", "crash_id": cid})
    return JSONResponse(res)


async def list_crashes(request: Request) -> JSONResponse:
    need_admin(request)
    return JSONResponse(svc_of(request).store.list_crashes())


# --------------------------------------------------------------------------- websockets + dashboard
async def _serve_ws(ws: WebSocket, topic: str, allowed: bool) -> None:
    if not allowed:
        await ws.close(code=4401)
        return
    hub: Hub = ws.app.state.svc.hub
    await ws.accept()
    hub.subscribe(topic, ws)
    try:
        while True:
            await ws.receive_text()  # clients may send pings; content is ignored
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(topic, ws)


async def ws_ops(ws: WebSocket) -> None:
    key = ws.query_params.get("key", "")
    expected = ws.app.state.svc.cfg.admin_key
    await _serve_ws(ws, "ops", bool(key) and hmac.compare_digest(key.encode(), expected.encode()))


async def ws_checkpoint(ws: WebSocket) -> None:
    key = ws.query_params.get("key", "")
    expected = ws.app.state.svc.cfg.admin_key
    ok = bool(key) and hmac.compare_digest(key.encode(), expected.encode())
    await _serve_ws(ws, f"checkpoint:{ws.path_params['checkpoint_id']}", ok)


async def ws_device(ws: WebSocket) -> None:
    device_id = ws.path_params["device_id"]
    dev = ws.app.state.svc.store.verify_device(device_id, ws.query_params.get("key", ""))
    await _serve_ws(ws, f"device:{device_id}", dev is not None)


async def ops_page(request: Request):
    return FileResponse(WEB_DIR / "ops.html")


def create_app(settings: Settings | None = None, store: Store | None = None, notifier=None) -> Starlette:
    cfg = settings or Settings.from_env()
    if not cfg.admin_key:
        cfg.admin_key = secrets.token_urlsafe(18)
        log.warning("SAFEROUTE_ADMIN_KEY not set; using a random key for this run: %s", cfg.admin_key)
    routes = [
        Route("/health", health),
        Route("/admin/vehicles", admin_vehicle, methods=["POST"]),
        Route("/admin/devices", admin_device, methods=["POST"]),
        Route("/admin/contacts", admin_contact, methods=["POST"]),
        Route("/admin/emergency-units", admin_unit, methods=["POST"]),
        Route("/admin/checkpoints", admin_checkpoint, methods=["POST"]),
        Route("/admin/speed-zones", admin_zone, methods=["POST"]),
        Route("/admin/hazards/{hazard_id:int}/status", admin_hazard_status, methods=["POST"]),
        Route("/ops/recent", ops_recent),
        Route("/ops", ops_page),
        Route("/inspections", post_inspection, methods=["POST"]),
        Route("/vehicles/{plate}/compliance", get_compliance),
        Route("/vehicles/{plate}/score", get_score),
        Route("/vehicles/{plate}/risk", get_risk),
        Route("/saccos/{sacco}/scores", sacco_scores),
        Route("/telemetry", post_telemetry, methods=["POST"]),
        Route("/hazards/report", post_hazard_report, methods=["POST"]),
        Route("/hazards/ahead", get_hazards_ahead),
        Route("/hazards", get_hazards),
        Route("/crash", post_crash, methods=["POST"]),
        Route("/crash/{crash_id:int}/cancel", crash_cancel, methods=["POST"]),
        Route("/crash/{crash_id:int}/confirm", crash_confirm, methods=["POST"]),
        Route("/crashes", list_crashes),
        WebSocketRoute("/ws/ops", ws_ops),
        WebSocketRoute("/ws/checkpoint/{checkpoint_id}", ws_checkpoint),
        WebSocketRoute("/ws/device/{device_id}", ws_device),
    ]
    app = Starlette(routes=routes, exception_handlers={ApiError: api_error_handler})
    app.state.svc = Services(cfg, store, notifier)
    return app
