from saferoute.hazards import HazardService

LAT, LON = 0.40, 32.90
M = 1 / 111_320.0  # one metre in degrees of latitude


def test_merge_confirm_and_severity(store, cfg):
    h = HazardService(store, cfg)
    r1 = h.report("d1", LAT, LON, 1.2, 60, 100)
    assert r1["new"] and r1["status"] == "reported"
    r2 = h.report("d2", LAT + 6 * M, LON, 1.9, 55, 110)  # 6 m away -> same hazard
    assert r2["hazard_id"] == r1["hazard_id"] and r2["status"] == "reported"
    # same device again must not count as a third witness
    r2b = h.report("d2", LAT, LON, 1.0, 50, 115)
    assert r2b["status"] == "reported"
    r3 = h.report("d3", LAT, LON, 1.1, 40, 120)
    assert r3["status"] == "confirmed"
    haz = store.get_hazard(r1["hazard_id"])
    assert haz["n_reports"] == 4 and haz["severity"] == "high" and haz["max_g"] == 1.9


def test_far_reports_make_new_hazard(store, cfg):
    h = HazardService(store, cfg)
    a = h.report("d1", LAT, LON, 1.0, 60, 1)
    b = h.report("d1", LAT + 200 * M, LON, 1.0, 60, 2)
    assert a["hazard_id"] != b["hazard_id"]


def test_rejections(store, cfg):
    h = HazardService(store, cfg)
    assert h.report("d1", LAT, LON, 1.0, 5, 1)["reason"] == "speed_too_low"
    assert h.report("d1", LAT, LON, 12.0, 60, 1)["reason"] == "peak_too_high_for_road_hazard"
    assert store.list_hazards(("reported", "confirmed")) == []


def _confirm(h, t0=100):
    for i, d in enumerate(("d1", "d2", "d3")):
        res = h.report(d, LAT, LON, 1.2, 60, t0 + i)
    assert res["status"] == "confirmed"
    return res["hazard_id"]


def test_repair_and_reopen(store, cfg):
    h = HazardService(store, cfg)
    hid = _confirm(h)
    assert h.set_status(hid, "repaired", now=500)["status"] == "repaired"
    # old-style reports after repair from fewer than 3 devices keep it repaired
    h.report("d1", LAT, LON, 1.2, 60, 600)
    assert store.get_hazard(hid)["status"] == "repaired"
    h.report("d2", LAT, LON, 1.2, 60, 601)
    h.report("d3", LAT, LON, 1.2, 60, 602)
    assert store.get_hazard(hid)["status"] == "confirmed"
    assert store.get_hazard(hid)["repaired_at"] is None


def test_dismissed_location_ignored(store, cfg):
    h = HazardService(store, cfg)
    hid = _confirm(h)
    h.set_status(hid, "dismissed", now=500)
    res = h.report("d9", LAT, LON, 1.5, 60, 700)
    assert res["accepted"] is False and "dismissed" in res["reason"]


def test_ahead_uses_heading_and_confirmation(store, cfg):
    h = HazardService(store, cfg)
    # hazard ~300 m east of the vehicle
    east = LON + 300 / (111_320.0)
    for i, d in enumerate(("d1", "d2", "d3")):
        h.report(d, LAT, east, 1.3, 60, 10 + i)
    assert len(h.ahead(LAT, LON, 90)) == 1  # heading east: ahead
    assert h.ahead(LAT, LON, 270) == []  # heading west: behind
    assert h.ahead(LAT, LON, 90, radius_m=100) == []  # outside radius
    # an unconfirmed hazard elsewhere does not trigger alerts
    lat2 = LAT + 0.05  # ~5.5 km north, well away from the confirmed one
    h.report("d1", lat2, east, 1.3, 60, 20)
    assert h.ahead(lat2, LON, 90) == []


def test_geojson_has_no_device_ids(store, cfg):
    h = HazardService(store, cfg)
    _confirm(h)
    gj = h.geojson()
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) == 1
    flat = str(gj)
    assert "d1" not in flat and "device" not in flat
    assert gj["features"][0]["geometry"]["coordinates"][0] == round(LON, 6)
