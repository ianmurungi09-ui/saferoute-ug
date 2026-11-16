"""End-to-end through the real Starlette app: auth, all four modules, WebSocket fan-out."""
import asyncio
import time

from saferoute.app import create_app
from saferoute.config import Settings
from saferoute.notify import ConsoleNotifier
from saferoute.store import Store

from asgi import Client, WSClient

ADMIN = {"x-admin-key": "test-admin"}
GOOD = dict(brake_efficiency_pct=72, tyre_tread_mm_min=4.0, steering_play_ok=True, lights_ok=True)
LAT, LON = 0.37, 32.90
M = 1 / 111_320.0


def make():
    notifier = ConsoleNotifier()
    app = create_app(Settings(admin_key="test-admin", db_path=":memory:"), Store(":memory:"), notifier)
    return app, Client(app), notifier


def points(n=8, lon0=LON, speed=70.0, end=None):
    end = end or time.time() - 5
    out, lon = [], lon0
    for i in range(n):
        out.append({"ts": end - (n - 1 - i) * 2, "lat": LAT, "lon": lon, "speed_kmh": speed, "heading": 90})
        lon += speed / 3.6 * 2 * M
    return out


def profile(speeds, end=None, lon0=LON, dt=2.0):
    """Points along an east-bound road following a list of speeds (km/h), ending at `end`."""
    end = end or time.time() - 5
    out, lon = [], lon0
    for i, sp in enumerate(speeds):
        out.append({"ts": end - (len(speeds) - 1 - i) * dt, "lat": LAT, "lon": lon, "speed_kmh": sp, "heading": 90})
        lon += sp / 3.6 * dt * M
    return out


async def register(c, plate="UAX123A", device="dev-bus-1", vtype="bus"):
    st, _ = await c.call("POST", "/admin/vehicles", {"plate": plate, "vehicle_type": vtype, "sacco": "Link SACCO"}, ADMIN)
    assert st == 201
    st, body = await c.call("POST", "/admin/devices", {"device_id": device, "plate": plate}, ADMIN)
    assert st == 201 and len(body["api_key"]) > 20
    return {"x-device-key": body["api_key"]}


def test_auth_and_validation():
    async def go():
        app, c, _ = make()
        assert (await c.call("GET", "/health"))[0] == 200
        assert (await c.call("POST", "/admin/vehicles", {"plate": "UAX123A", "vehicle_type": "bus"}))[0] == 401
        assert (await c.call("POST", "/admin/vehicles", {"plate": "UAX123A", "vehicle_type": "bus"}, {"x-admin-key": "nope"}))[0] == 401
        dev = await register(c)
        body = {"device_id": "dev-bus-1", "points": points()}
        assert (await c.call("POST", "/telemetry", body))[0] == 401  # no device key
        assert (await c.call("POST", "/telemetry", body, {"x-device-key": "wrong"}))[0] == 401
        assert (await c.call("POST", "/telemetry", body, dev))[0] == 200
        bad = {"device_id": "dev-bus-1", "points": [{**points()[0], "lat": 95}]}
        st, err = await c.call("POST", "/telemetry", bad, dev)
        assert st == 422 and err["error"] == "validation failed"
        assert (await c.call("POST", "/telemetry", raw=b"{not json", headers=dev))[0] == 400
        future = {"device_id": "dev-bus-1", "points": [{**points()[0], "ts": time.time() + 99999}]}
        assert (await c.call("POST", "/telemetry", future, dev))[0] == 422
        assert (await c.call("GET", "/crashes"))[0] == 401
        assert (await c.call("POST", "/admin/contacts", {"plate": "UAX123A", "name": "X", "phone": "123", "consent": True}, ADMIN))[0] == 422
        assert (await c.call("POST", "/admin/devices", {"device_id": "d-x", "plate": "UNKNOWN1"}, ADMIN))[0] == 404
    asyncio.run(go())


def test_dmc_vehicle_triggers_checkpoint_alert_over_websocket():
    async def go():
        app, c, _ = make()
        dev = await register(c)
        st, cp = await c.call("POST", "/admin/checkpoints", {"name": "Lugazi Checkpoint", "lat": LAT, "lon": LON + 0.05}, ADMIN)
        cp_id = cp["id"]
        st, comp = await c.call("POST", "/inspections", {"plate": "UAX 123A", "inspector_id": "insp-7",
                                "items": {**GOOD, "brake_efficiency_pct": 25}}, ADMIN)
        assert st == 201 and comp["status"] == "DMC" and comp["failed_items"][0]["item"] == "brakes"

        async with WSClient(app, "/ws/ops", "key=test-admin") as ops, \
                   WSClient(app, f"/ws/checkpoint/{cp_id}", "key=test-admin") as cpws, \
                   WSClient(app, "/ws/ops", "key=bad") as rejected:
            assert ops.accepted and cpws.accepted and not rejected.accepted
            st, res = await c.call("POST", "/telemetry", {"device_id": "dev-bus-1", "points": points()}, dev)
            assert st == 200
            seen = [await ops.receive_json() for _ in range(2)]
            assert {m["type"] for m in seen} == {"position", "flagged_vehicle_ahead"}
            alert = await cpws.receive_json()
            assert alert["plate"] == "UAX123A" and alert["status"] == "DMC" and "brakes" in alert["failed_items"]
            assert 4 < alert["distance_km"] < 7
            # repeat batch inside the cooldown: no second checkpoint alert
            await c.call("POST", "/telemetry", {"device_id": "dev-bus-1", "points": points(end=time.time())}, dev)
            nxt = await ops.receive_json()
            assert nxt["type"] == "position"
            assert cpws.outbox.empty()

        # a vehicle heading AWAY from the checkpoint is not flagged
        app2, c2, _ = make()
        dev2 = await register(c2)
        await c2.call("POST", "/admin/checkpoints", {"name": "Behind", "lat": LAT, "lon": LON - 0.05}, ADMIN)
        st, comp2 = await c2.call("POST", "/inspections", {"plate": "UAX123A", "inspector_id": "insp-1", "items": {**GOOD, "brake_efficiency_pct": 25}}, ADMIN)
        assert st == 201 and comp2["status"] == "DMC"  # really flagged, so "no alert" below is meaningful
        await c2.call("POST", "/telemetry", {"device_id": "dev-bus-1", "points": points()}, dev2)
        st, recent = await c2.call("GET", "/ops/recent", headers=ADMIN)
        assert not [m for m in recent if m["type"] == "flagged_vehicle_ahead"]
    asyncio.run(go())


def test_compliant_vehicle_gets_no_alert_and_risk_is_lower():
    async def go():
        app, c, _ = make()
        dev = await register(c)
        await c.call("POST", "/admin/checkpoints", {"name": "CP", "lat": LAT, "lon": LON + 0.05}, ADMIN)
        st, comp = await c.call("POST", "/inspections", {"plate": "UAX123A", "inspector_id": "insp-1", "items": GOOD}, ADMIN)
        assert st == 201 and comp["status"] == "COMPLIANT"
        await c.call("POST", "/telemetry", {"device_id": "dev-bus-1", "points": points()}, dev)
        _, recent = await c.call("GET", "/ops/recent", headers=ADMIN)
        assert not [m for m in recent if m["type"] == "flagged_vehicle_ahead"]
        st, risk = await c.call("GET", "/vehicles/UAX123A/risk", headers=ADMIN)
        assert st == 200 and risk["compliance"]["status"] == "COMPLIANT" and risk["risk_tier"] in ("A", "B")
        st, sac = await c.call("GET", "/saccos/Link SACCO/scores", headers=ADMIN)
        assert sac["vehicles"][0]["plate"] == "UAX123A"
    asyncio.run(go())


def test_telemetry_events_score_and_hazard_alerts():
    async def go():
        app, c, _ = make()
        dev = await register(c)
        # 40 s at 100 km/h, then an ordinary slowdown to 80: exactly one over-speed event
        pts = profile([100] * 21 + [80], end=time.time() - 60)
        st, res = await c.call("POST", "/telemetry", {"device_id": "dev-bus-1", "points": pts}, dev)
        assert st == 200 and [e["kind"] for e in res["events"]] == ["overspeed"]
        st, sc = await c.call("GET", "/vehicles/UAX123A/score", headers=ADMIN)
        assert st == 200 and sc["events"] == {"overspeed": 1}

        # three different devices confirm a pothole ~300 m ahead of the next batch
        keys = [dev]
        for i in (2, 3):
            keys.append(await register(c, device=f"dev-bus-{i}"))
        haz_lon = LON + 0.02 + 300 * M
        for i, k in enumerate(keys):
            st, r = await c.call("POST", "/hazards/report", {"device_id": f"dev-bus-{i + 1}" if i else "dev-bus-1",
                                 "lat": LAT, "lon": haz_lon, "peak_g": 1.5, "speed_kmh": 60}, k)
            assert st == 201
        assert r["status"] == "confirmed"
        st, gj = await c.call("GET", "/hazards")  # public
        assert st == 200 and len(gj["features"]) == 1 and gj["features"][0]["properties"]["status"] == "confirmed"
        assert (await c.call("GET", "/hazards", query="bbox=1,2,3"))[0] == 400
        assert len((await c.call("GET", "/hazards", query="bbox=32,0,33,1"))[1]["features"]) == 1
        assert len((await c.call("GET", "/hazards", query="bbox=10,10,11,11"))[1]["features"]) == 0
        assert (await c.call("GET", "/hazards/ahead", query=f"lat={LAT}&lon={LON + 0.02}&heading=90"))[1][0]["distance_m"] in range(280, 320)

        st, res = await c.call("POST", "/telemetry", {"device_id": "dev-bus-1", "points": points(4, lon0=LON + 0.02, speed=40.0, end=time.time())}, dev)
        assert [a["type"] for a in res["alerts"]] == ["hazard_ahead"] and "Slow down" in res["alerts"][0]["say"]
        st, res = await c.call("POST", "/telemetry", {"device_id": "dev-bus-1", "points": points(4, lon0=LON + 0.02, speed=40.0, end=time.time() + 1)}, dev)
        assert res["alerts"] == []  # cooldown: not repeated on every batch

        # operator marks it repaired -> disappears from the default map
        st, _ = await c.call("POST", "/admin/hazards/1/status", {"status": "repaired"}, ADMIN)
        assert st == 200 and (await c.call("GET", "/hazards"))[1]["features"] == []
        assert (await c.call("POST", "/admin/hazards/99/status", {"status": "repaired"}, ADMIN))[0] == 404

        # low-speed report is rejected politely, not an error
        st, r = await c.call("POST", "/hazards/report", {"device_id": "dev-bus-1", "lat": LAT, "lon": LON, "peak_g": 1.0, "speed_kmh": 4}, dev)
        assert st == 200 and r["accepted"] is False
    asyncio.run(go())


def test_crash_dispatch_cancel_and_ownership():
    async def go():
        app, c, notifier = make()
        dev = await register(c)
        other = await register(c, plate="UBA456B", device="dev-boda-1", vtype="boda")
        await c.call("POST", "/admin/emergency-units", {"name": "Lugazi Police Post", "kind": "police", "lat": 0.3667, "lon": 32.9333, "phone": "0700 000 001"}, ADMIN)
        await c.call("POST", "/admin/emergency-units", {"name": "Jinja Hospital", "kind": "hospital", "lat": 0.4244, "lon": 33.2041, "phone": "+256700000003"}, ADMIN)
        st, k = await c.call("POST", "/admin/contacts", {"plate": "UAX123A", "name": "Sarah", "phone": "0700000010", "relation": "spouse", "consent": True}, ADMIN)
        assert k["phone"] == "+256700000010"

        async with WSClient(app, "/ws/ops", "key=test-admin") as ops:
            crash = {"device_id": "dev-bus-1", "event_id": "evt-000001", "lat": 0.3937, "lon": 32.9555,
                     "peak_g": 9.0, "delta_v_kmh": 45, "tilt_deg": 70, "speed_before_kmh": 80, "stationary_s": 12}
            assert (await c.call("POST", "/crash", crash))[0] == 401
            st, res = await c.call("POST", "/crash", crash, dev)
            assert st == 201 and res["crash"]["status"] == "DISPATCHED" and len(res["dispatches"]) == 3
            assert {p for p, _ in notifier.outbox} == {"+256700000001", "+256700000003", "+256700000010"}
            msg = await ops.receive_json()
            assert msg["type"] == "crash" and msg["plate"] == "UAX123A" and "Sarah" in msg["notified"]

            st, again = await c.call("POST", "/crash", crash, dev)  # device retry
            assert st == 200 and again["duplicate"] and len(notifier.outbox) == 3

            crash_id = res["crash"]["id"]
            # another device cannot cancel it; the owning device can
            assert (await c.call("POST", f"/crash/{crash_id}/cancel", headers={**other, "x-device-id": "dev-boda-1"}))[0] == 403
            st, done = await c.call("POST", f"/crash/{crash_id}/cancel", headers={**dev, "x-device-id": "dev-bus-1"})
            assert st == 200 and done["crash"]["status"] == "CANCELLED"
            assert len(notifier.outbox) == 6 and "FALSE ALARM" in notifier.outbox[-1][1]
        st, lst = await c.call("GET", "/crashes", headers=ADMIN)
        assert lst[0]["status"] == "CANCELLED"
        assert (await c.call("POST", "/crash/999/confirm", headers=ADMIN))[0] == 404
    asyncio.run(go())
