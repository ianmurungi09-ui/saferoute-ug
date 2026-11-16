from saferoute.config import Settings
from saferoute.telematics import Point, SpeedZones, TripAnalyzer, compute_score

# ~0.0009 degrees longitude is about 100 m at the equator
LAT, LON0 = 0.5, 32.7


def run(points, zones=None):
    cfg = Settings()
    z = SpeedZones(cfg.default_speed_limit_kmh)
    z.set_zones(zones or [])
    a = TripAnalyzer(cfg, z)
    return a.process("dev1", points), a


def trip(speeds, dt=2.0, start=1000.0):
    """Points along a west-east road, moving at each given speed (km/h)."""
    pts, lon, ts = [], LON0, start
    for sp in speeds:
        pts.append(Point(ts, LAT, lon, sp))
        lon += (sp / 3.6 * dt) / 111_320.0
        ts += dt
    return pts


def test_sustained_overspeed_is_one_event():
    (events, km), _ = run(trip([70] * 3 + [105] * 15 + [70] * 3))
    over = [e for e in events if e.kind == "overspeed"]
    assert len(over) == 1
    assert over[0].duration_s >= 28 and 24 <= over[0].value <= 26
    assert km > 1


def test_brief_overtake_ignored():
    (events, _), _ = run(trip([70, 70, 95, 95, 70, 70]))  # 4 s above limit
    assert not [e for e in events if e.kind == "overspeed"]


def test_tolerance_and_urban_zone():
    (events, _), _ = run(trip([84] * 15))  # within 5 km/h tolerance of the 80 limit
    assert not events
    zone = [{"lat": LAT, "lon": LON0, "radius_m": 20_000, "limit_kmh": 50}]
    # fine on the highway, speeding in town; a run is reported when it ends, so slow down at the end
    (events, _), _ = run(trip([70] * 15 + [55]), zone)  # 55 is within tolerance of 50, and not a harsh slowdown
    assert [e.kind for e in events] == ["overspeed"]


def test_harsh_brake_and_debounce():
    (events, _), _ = run(trip([90, 90, 40, 40, 40]))
    brakes = [e for e in events if e.kind == "harsh_brake"]
    assert len(brakes) == 1 and brakes[0].value < -3.5
    (events, _), _ = run(trip([70, 70, 75, 75]))  # gentle change
    assert not events


def test_harsh_accel_and_swerve():
    pts = trip([20, 20, 60, 60])
    pts[3].ay = 4.5
    (events, _), _ = run(pts)
    kinds = {e.kind for e in events}
    assert {"harsh_accel", "swerve"} <= kinds


def test_gps_jump_and_duplicates_ignored():
    pts = trip([60] * 6)
    jump = Point(pts[2].ts + 0.5, LAT + 1.0, LON0, 60)  # ~111 km in half a second
    dup = Point(pts[3].ts, pts[3].lat, pts[3].lon, 60)
    (events, km), _ = run(pts + [jump, dup])
    assert not events
    assert km < 1  # the jump did not add 111 km


def test_gap_breaks_overspeed_run():
    a = trip([110] * 8)  # 14 s of speeding
    b = trip([110] * 8 + [60], start=5000.0)  # after a long signal gap, then slows (ending the run)
    (events, _), _ = run(a + b)
    assert len([e for e in events if e.kind == "overspeed"]) == 2


def test_overspeed_run_continues_across_batches():
    cfg = Settings()
    z = SpeedZones(cfg.default_speed_limit_kmh)
    a = TripAnalyzer(cfg, z)
    pts = trip([110] * 12 + [70] * 2)
    e1, _ = a.process("d", pts[:6])
    e2, _ = a.process("d", pts[6:])
    assert not e1 and len([e for e in e2 if e.kind == "overspeed"]) == 1


def test_score():
    assert compute_score([], 2.0)["insufficient_data"] is True
    clean = compute_score([], 100.0)
    assert clean["score"] == 100.0 and clean["grade"] == "A"
    bad = compute_score([{"kind": "harsh_brake", "value": -5, "duration_s": 0}] * 12
                        + [{"kind": "overspeed", "value": 30, "duration_s": 120}] * 4, 100.0)
    assert bad["score"] < 50 and bad["grade"] == "E"
    mid = compute_score([{"kind": "harsh_brake", "value": -4, "duration_s": 0}] * 5, 100.0)
    assert 80 <= mid["score"] <= 95
