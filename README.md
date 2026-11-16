# SafeRoute UG (Safiri)

[![Tests](https://github.com/ianmurungi09-ui/saferoute-ug/actions/workflows/tests.yml/badge.svg)](https://github.com/ianmurungi09-ui/saferoute-ug/actions/workflows/tests.yml)
[![License](https://img.shields.io/github/license/ianmurungi09-ui/saferoute-ug)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/ianmurungi09-ui/saferoute-ug)](https://github.com/ianmurungi09-ui/saferoute-ug/commits/main)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Status](https://img.shields.io/badge/status-prototype-orange)

One backend for four road-safety problems in Uganda: dangerous vehicles, speeding, potholes, and slow crash response.

| Module | What it does | Code |
|---|---|---|
| 1. Inspection & compliance ledger | Digital inspections per plate. Failed brakes, tyres or steering mark the vehicle **DMC** at once. Flagged vehicles approaching a checkpoint trigger an alert to that checkpoint. Compliance feeds an insurer/SACCO risk tier. | `inspection.py`, `risk.py` |
| 2. Telematics & behaviour scoring | Over-speed (sustained, zone-aware), harsh braking/acceleration, swerving from batched GPS points. 0-100 score per vehicle, SACCO roll-up. | `telematics.py` |
| 3. Hazard map | Accelerometer spikes merge into hazards. A hazard is `confirmed` only after 3 distinct devices hit it. Public GeoJSON feed for UNRA/municipalities. Drivers get "hazard ahead" alerts in the telemetry response. | `hazards.py` |
| 4. Crash detection & dispatch | Device-side detection with a **20 s cancel window**, then tiered SMS dispatch with an audit trail and false-alarm retraction. | `crash.py`, firmware |

## Run it

```bash
pip install -r requirements.txt
SAFEROUTE_ADMIN_KEY=change-me python -m saferoute          # http://127.0.0.1:8000, dashboard at /ops
python simulator/simulate.py --admin-key change-me          # drives a Kampala->Jinja trip through all four modules
python -m pytest                                            # 38 tests
```

SMS goes to the console log by default. For real delivery set `SAFEROUTE_SMS_PROVIDER=africastalking`, `AT_USERNAME`, `AT_API_KEY` (and `AT_SANDBOX=false` for production).

## What has and hasn't been verified

Verified in the build session: 38 automated tests (logic modules plus the full HTTP/WebSocket API driven in-process), and a live run of the simulator against a real uvicorn server.

**Not verified. Treat as drafts:**
- **FastAPI / PostgreSQL / PostGIS.** The sandbox had neither FastAPI nor a database server and no package installs, so the app is built on **Starlette** (what FastAPI is built on) with a **sqlite** dev store. `db/schema.sql` is the PostGIS design, never executed. A `PostgresStore` with the same methods as `store.py` is the next step.
- **WebSockets on a real server.** Tested in-process only; uvicorn here lacked the `websockets` package. `requirements.txt` includes `uvicorn[standard]`.
- **Africa's Talking adapter.** Written from the public API; never called.
- **Firmware** (`firmware/saferoute_tracker`): not compiled, no hardware. Expect compile fixes; thresholds must be bench-tested.
- **Dashboard** (`/ops`): opened in a headless browser against live demo data. The alert feed works, including a polling fallback when WebSockets are blocked. The **map itself has never been seen rendering**: the test sandbox could not reach the Leaflet CDN or map tiles.

## Things to decide before a pilot

1. **Thresholds are guesses.** Speed limits, inspection intervals, brake/tread limits (`config.py`), pothole g-levels, and crash thresholds all need checking against Ugandan regulations and calibrating on real vehicles. A boda frame, bus floor and truck cab vibrate very differently.
2. **Placeholder reference data.** Town coordinates, speed zones (circles), and *no* emergency units or checkpoints are seeded on purpose, so nothing can text a wrong number. Load real police posts, hospitals and ambulance contacts via `/admin/emergency-units`, and agree the SMS alerts with them first. An unverified sensor alert is only useful if someone is staffed to act on it.
3. **Vehicle is not driver.** Boda bodas and buses are shared, so scores attach to the vehicle/device. Per-driver scoring needs a driver check-in (RFID or phone).
4. **Trust and tampering.** One admin key does everything today. Before real use: separate roles (accredited inspector, police, insurer, SACCO), inspection photo evidence, and tamper detection (device unplugged, GPS blocked).
5. **Privacy.** Location data is personal data, and next-of-kin and insurer sharing need consent. The design helps: raw GPS tracks are not stored (only events and distance), contacts without `consent` are never messaged, and the public hazard feed has no identifiers. Get legal advice on the Data Protection and Privacy Act, 2019 (consent text, retention, who may see scores).
6. **False dispatches cost real resources.** Mitigations built in: local cancel window, server-side confidence check, family-only tier for low-speed falls, idempotent retries, merged duplicates, and a "false alarm" SMS to everyone already notified. Run it in shadow mode (log only, no SMS to emergency units) for a few weeks first.
7. **Scale-out.** The WebSocket hub and per-device analyzer state are in-process. For more than one worker, put Redis pub/sub behind `Hub.publish` and persist analyzer state.

## Suggested next steps

1. Bench-test the tracker on a bench rig, then on one boda and one bus; record real accelerometer traces and tune thresholds.
2. Build `PostgresStore` against `db/schema.sql` and run the tests against it.
3. Replace the circle speed zones with OpenStreetMap `maxspeed` data or surveyed limits.
4. Pilot with one SACCO (about 10 vehicles) in shadow mode.

## API at a glance

Admin (`X-Admin-Key`): `POST /admin/{vehicles,devices,contacts,emergency-units,checkpoints,speed-zones}`, `POST /inspections`, `GET /vehicles/{plate}/{compliance,score,risk}`, `GET /saccos/{name}/scores`, `GET /crashes`, `POST /crash/{id}/{cancel,confirm}`, `POST /admin/hazards/{id}/status`, `GET /ops/recent`.
Device (`X-Device-Key`): `POST /telemetry` (batched points, returns events and hazard alerts), `POST /hazards/report`, `POST /crash`.
Public: `GET /hazards` (GeoJSON, `?status=&bbox=`), `GET /hazards/ahead`, `GET /health`.
WebSocket: `/ws/ops?key=`, `/ws/checkpoint/{id}?key=`, `/ws/device/{id}?key=`.
