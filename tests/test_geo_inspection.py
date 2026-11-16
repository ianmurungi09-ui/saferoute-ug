import time

from saferoute.geo import angle_diff, bearing_deg, haversine_m, is_ahead, nearest_landmark
from saferoute.inspection import compliance_status, evaluate
from saferoute.notify import normalize_ug_phone, parse_at_response
from saferoute.risk import risk_profile

GOOD = dict(brake_efficiency_pct=72, tyre_tread_mm_min=4.0, steering_play_ok=True, lights_ok=True)


def test_haversine_kampala_jinja():
    d = haversine_m(0.3476, 32.5825, 0.4244, 33.2041) / 1000
    assert 65 < d < 75  # straight line; the road is longer


def test_bearing_and_ahead():
    assert 85 < bearing_deg(0.35, 32.58, 0.35, 32.9) < 95  # due east
    assert angle_diff(350, 10) == 20
    assert is_ahead(0.35, 32.58, 90, 0.35, 32.9)
    assert not is_ahead(0.35, 32.58, 270, 0.35, 32.9)
    assert is_ahead(0.35, 32.58, None, 0.35, 32.9)  # unknown heading: alert rather than miss


def test_nearest_landmark_text():
    assert nearest_landmark(0.3476, 32.5825) == "at Kampala"
    txt = nearest_landmark(0.3937, 32.9555)
    assert "of Lugazi" in txt and "km" in txt


def test_phone_normalisation():
    assert normalize_ug_phone("0772 123 456") == "+256772123456"
    assert normalize_ug_phone("256772123456") == "+256772123456"
    assert normalize_ug_phone("+256-772-123-456") == "+256772123456"
    assert normalize_ug_phone("772123456") == "+256772123456"
    assert normalize_ug_phone("12345") is None


def test_parse_at_response():
    ok = '{"SMSMessageData":{"Recipients":[{"statusCode":101,"status":"Success","messageId":"ATXid_1"}]}}'
    bad = '{"SMSMessageData":{"Recipients":[{"statusCode":403,"status":"InvalidPhoneNumber"}]}}'
    assert parse_at_response(ok) == (True, "ATXid_1")
    assert parse_at_response(bad)[0] is False
    assert parse_at_response("<html>")[0] is False


def test_inspection_outcomes(cfg):
    now = 1_000_000.0
    assert evaluate(GOOD, "bus", now, cfg).status == "COMPLIANT"
    assert evaluate({**GOOD, "brake_efficiency_pct": 30}, "bus", now, cfg).status == "DMC"
    assert evaluate({**GOOD, "tyre_tread_mm_min": 0.8}, "bus", now, cfg).status == "DMC"
    assert evaluate({**GOOD, "tyre_damage": True}, "bus", now, cfg).status == "DMC"
    cond = evaluate({**GOOD, "lights_ok": False}, "bus", now, cfg)
    assert cond.status == "CONDITIONAL" and cond.valid_until == now + 14 * 86400
    minor = evaluate({**GOOD, "fire_extinguisher_ok": False}, "bus", now, cfg)
    assert minor.status == "COMPLIANT" and minor.failed[0]["severity"] == "minor"
    # a missing optional item is "not applicable", not a failure
    assert evaluate({**GOOD, "handbrake_holds": None}, "boda", now, cfg).status == "COMPLIANT"
    assert evaluate({**GOOD, "handbrake_holds": False}, "bus", now, cfg).status == "DMC"


def test_compliance_lifecycle(store, cfg):
    now = time.time()
    assert compliance_status(store, "UAX123A", now)["status"] == "UNVERIFIED"
    ev = evaluate(GOOD, "bus", now - 100 * 86400, cfg)  # 100 days ago, bus valid for 90
    store.add_inspection("UAX123A", "insp-1", now - 100 * 86400, ev.valid_until, ev.status, ev.failed, GOOD)
    assert compliance_status(store, "UAX123A", now)["status"] == "EXPIRED"
    bad = evaluate({**GOOD, "brake_efficiency_pct": 10}, "bus", now, cfg)
    store.add_inspection("UAX123A", "insp-2", now, bad.valid_until, bad.status, bad.failed, GOOD)
    assert compliance_status(store, "UAX123A", now + 400 * 86400)["status"] == "DMC"  # DMC never "expires"
    fixed = evaluate(GOOD, "bus", now + 1, cfg)
    store.add_inspection("UAX123A", "insp-3", now + 1, fixed.valid_until, fixed.status, fixed.failed, GOOD)
    assert compliance_status(store, "UAX123A", now + 2)["status"] == "COMPLIANT"


def test_risk_profile_ordering():
    assert risk_profile("COMPLIANT", 95)["indicative_premium_factor"] < risk_profile("COMPLIANT", 60)["indicative_premium_factor"]
    assert risk_profile("DMC", 95)["risk_tier"] == "D"
    assert risk_profile("COMPLIANT", None)["indicative_premium_factor"] == 1.0
