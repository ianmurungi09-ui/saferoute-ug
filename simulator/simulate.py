"""Drive a running SafeRoute server end to end with a simulated Kampala -> Jinja trip.

    SAFEROUTE_ADMIN_KEY=demo python -m saferoute &
    python simulator/simulate.py --admin-key demo

Uses only the standard library. Straight-line route; it exercises all four modules:
DMC checkpoint alert, over-speed + harsh braking scoring, a pothole confirmed by three
devices, a hazard alert on the next batch, and a crash with SMS dispatch (to the
server's console notifier unless you configured a real provider).
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

KAMPALA = (0.3476, 32.5825)
JINJA = (0.4244, 33.2041)
M = 1 / 111_320.0


def call(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, method=method, headers={"Content-Type": "application/json", **(headers or {})},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def route_points(speed_for, t_end, dt=2.0, hard_stop=(0.44, 0.4415)):
    """Integrate position along the straight Kampala->Jinja line. speed_for(fraction) is the target
    speed in km/h; the vehicle ramps toward it at a comfortable 12 km/h per step, except inside
    `hard_stop`, where it may brake as hard as it likes (the deliberate harsh-braking event)."""
    (la0, lo0), (la1, lo1) = KAMPALA, JINJA
    total_km = ((la1 - la0) ** 2 + (lo1 - lo0) ** 2) ** 0.5 * 111.32
    pts, km, sp = [], 0.0, 0.0
    while km < total_km:
        f = km / total_km
        target = speed_for(f)
        down = 80.0 if hard_stop[0] <= f < hard_stop[1] else 12.0
        sp += max(-down, min(12.0, target - sp))
        pts.append({"lat": la0 + f * (la1 - la0), "lon": lo0 + f * (lo1 - lo0), "speed_kmh": round(sp, 1), "heading": 80.0})
        km += sp / 3600.0 * dt
    for j, p in enumerate(pts):  # timestamps end at t_end
        p["ts"] = t_end - (len(pts) - 1 - j) * dt
    return pts


def profile(f):
    if f < 0.10 or f > 0.95:
        return 45.0          # Kampala (6 km) / Jinja (3 km) built-up zones, limit 50
    if 0.40 <= f < 0.44:
        return 105.0         # sustained speeding on the highway (limit 80)
    if 0.44 <= f < 0.4415:
        return 40.0          # a hard stop right after it
    return 75.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--admin-key", required=True)
    a = ap.parse_args()
    base, ADMIN = a.base_url, {"X-Admin-Key": a.admin_key}

    print("== setup ==")
    keys = {}
    for plate, vtype, dev in (("UAX 123A", "bus", "bus-001"), ("UBA 456B", "boda", "boda-001"), ("UAZ 789C", "truck", "truck-001")):
        call(base, "POST", "/admin/vehicles", {"plate": plate, "vehicle_type": vtype, "sacco": "Demo SACCO"}, ADMIN)
        st, r = call(base, "POST", "/admin/devices", {"device_id": dev, "plate": plate}, ADMIN)
        keys[dev] = {"X-Device-Key": r["api_key"]}
    call(base, "POST", "/admin/contacts", {"plate": "UAX123A", "name": "Demo Next-of-kin", "phone": "0700000010", "relation": "spouse", "consent": True}, ADMIN)
    call(base, "POST", "/admin/emergency-units", {"name": "Lugazi Police Post (demo)", "kind": "police", "lat": 0.3667, "lon": 32.9333, "phone": "0700000001"}, ADMIN)
    call(base, "POST", "/admin/emergency-units", {"name": "Jinja Hospital (demo)", "kind": "hospital", "lat": 0.4244, "lon": 33.2041, "phone": "0700000002"}, ADMIN)
    call(base, "POST", "/admin/checkpoints", {"name": "Mukono Checkpoint (demo)", "lat": 0.3533, "lon": 32.7553}, ADMIN)

    print("== module 1: bus fails inspection (worn brakes) ==")
    st, comp = call(base, "POST", "/inspections", {"plate": "UAX123A", "inspector_id": "insp-demo",
        "items": {"brake_efficiency_pct": 28, "tyre_tread_mm_min": 3.1, "steering_play_ok": True, "lights_ok": True}}, ADMIN)
    print("  status:", comp["status"], "failed:", [f["item"] for f in comp["failed_items"]])
    call(base, "POST", "/inspections", {"plate": "UAZ789C", "inspector_id": "insp-demo",
        "items": {"brake_efficiency_pct": 70, "tyre_tread_mm_min": 5, "steering_play_ok": True, "lights_ok": True}}, ADMIN)

    print("== module 2: bus drives Kampala -> Jinja ==")
    pts = route_points(profile, time.time() - 5)
    totals = {}
    for i in range(0, len(pts), 200):
        batch = pts[i:i + 200]
        st, r = call(base, "POST", "/telemetry", {"device_id": "bus-001", "points": batch}, keys["bus-001"])
        for e in r.get("events", []):
            totals[e["kind"]] = totals.get(e["kind"], 0) + 1
    print("  events:", totals)
    st, sc = call(base, "GET", "/vehicles/UAX123A/score", headers=ADMIN)
    print(f"  score: {sc['score']} grade {sc['grade']} over {sc['distance_km']} km")
    st, risk = call(base, "GET", "/vehicles/UAX123A/risk", headers=ADMIN)
    print("  risk tier:", risk["risk_tier"], "indicative factor:", risk["indicative_premium_factor"])

    st, recent = call(base, "GET", "/ops/recent", headers=ADMIN)
    flagged = [m for m in recent if m["type"] == "flagged_vehicle_ahead"]
    print("  checkpoint alerts:", [(m["checkpoint"], m["status"], m["failed_items"]) for m in flagged])

    print("== module 3: three vehicles hit the same pothole ==")
    mid = pts[int(len(pts) * 0.3)]
    for dev, off in (("bus-001", 0), ("boda-001", 4), ("truck-001", -3)):
        st, r = call(base, "POST", "/hazards/report", {"device_id": dev, "lat": mid["lat"] + off * M, "lon": mid["lon"],
                     "peak_g": 1.6, "speed_kmh": 55}, keys[dev])
        print(f"  {dev}: {r.get('status')}")
    st, gj = call(base, "GET", "/hazards")
    print("  public map features:", len(gj["features"]))
    # next telemetry batch from a vehicle 250 m before the pothole gets a spoken alert
    approach = {"ts": time.time(), "lat": mid["lat"], "lon": mid["lon"] - 250 * M, "speed_kmh": 60, "heading": 90.0}
    st, r = call(base, "POST", "/telemetry", {"device_id": "truck-001", "points": [approach]}, keys["truck-001"])
    print("  driver alert:", [x["say"] for x in r["alerts"]])

    print("== module 4: crash near Lugazi ==")
    near = pts[int(len(pts) * 0.6)]
    st, res = call(base, "POST", "/crash", {"device_id": "bus-001", "event_id": f"sim-{int(time.time())}", "lat": near["lat"], "lon": near["lon"],
                   "peak_g": 9.2, "delta_v_kmh": 62, "tilt_deg": 75, "speed_before_kmh": 78, "stationary_s": 14}, keys["bus-001"])
    print("  status:", res["crash"]["status"], "| confidence:", res["crash"]["confidence"], "|", res["crash"]["landmark"])
    for d in res["dispatches"]:
        print(f"  -> {d['recipient_kind']:12} {d['recipient_name']:28} {d['status']}")
    print("  sample SMS:", res["dispatches"][0]["message"])
    print("done.")


if __name__ == "__main__":
    main()
