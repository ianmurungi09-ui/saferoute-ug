import time

from saferoute.crash import CrashReport, CrashService, assess

LAT, LON = 0.3937, 32.9555  # near Lugazi


def report(event_id="evt-0001", **kw):
    base = dict(device_id="dev1", plate="UAX123A", event_id=event_id, ts=time.time(), lat=LAT, lon=LON,
                peak_g=9.0, delta_v_kmh=45, tilt_deg=70, speed_before_kmh=80, stationary_s=12)
    base.update(kw)
    return CrashReport(**base)


def setup(store, notifier, cfg):
    store.add_emergency_unit("Lugazi Police Post", "police", 0.3667, 32.9333, "+256700000001")
    store.add_emergency_unit("Far Hospital", "hospital", 2.77, 32.3, "+256700000002")
    store.add_emergency_unit("Jinja Hospital", "hospital", 0.4244, 33.2041, "+256700000003")
    store.add_contact("UAX123A", "Sarah", "+256700000010", "spouse", True)
    store.add_contact("UAX123A", "No Consent", "+256700000011", "friend", False)
    return CrashService(store, notifier, cfg)


def test_assess_ranges():
    assert assess(report()) == 1.0
    dropped = report(peak_g=6, delta_v_kmh=0, tilt_deg=90, speed_before_kmh=0, stationary_s=20)
    assert 0.3 <= assess(dropped) < 0.9
    assert assess(report(peak_g=1.2, delta_v_kmh=2, tilt_deg=3, stationary_s=0)) < 0.3


def test_full_dispatch_goes_to_nearest_units_and_consenting_contacts(store, notifier, cfg):
    svc = setup(store, notifier, cfg)
    res = svc.handle(report())
    assert res["crash"]["status"] == "DISPATCHED"
    phones = {p for p, _ in notifier.outbox}
    assert phones == {"+256700000001", "+256700000003", "+256700000010"}  # nearest police + nearest hospital + consenting NOK
    assert "+256700000011" not in phones  # contact without consent is never messaged
    police_msg = dict(notifier.outbox)["+256700000001"]
    assert "UAX123A" in police_msg and "0.39370,32.95550" in police_msg and "not yet verified" in police_msg
    assert "of Lugazi" in police_msg and police_msg.isascii()
    assert len(res["dispatches"]) == 3


def test_low_speed_fall_only_notifies_family(store, notifier, cfg):
    svc = setup(store, notifier, cfg)
    res = svc.handle(report(speed_before_kmh=8, delta_v_kmh=8, peak_g=6, tilt_deg=90))
    assert res["crash"]["status"] == "NOK_NOTIFIED"
    assert {p for p, _ in notifier.outbox} == {"+256700000010"}
    assert "call the driver" in notifier.outbox[0][1]


def test_low_confidence_is_logged_only(store, notifier, cfg):
    svc = setup(store, notifier, cfg)
    res = svc.handle(report(peak_g=1.5, delta_v_kmh=3, tilt_deg=5, stationary_s=0, speed_before_kmh=40))
    assert res["crash"]["status"] == "LOGGED_ONLY" and notifier.outbox == []


def test_idempotent_retry_and_merge(store, notifier, cfg):
    svc = setup(store, notifier, cfg)
    first = svc.handle(report("evt-A001"))
    n = len(notifier.outbox)
    again = svc.handle(report("evt-A001"))
    assert again["duplicate"] and again["crash"]["id"] == first["crash"]["id"]
    merged = svc.handle(report("evt-B002"))  # same incident, new event id
    assert merged["merged"] and merged["crash"]["id"] == first["crash"]["id"]
    assert len(notifier.outbox) == n  # no second round of SMS


def test_cancel_sends_false_alarm_to_everyone_notified(store, notifier, cfg):
    svc = setup(store, notifier, cfg)
    crash_id = svc.handle(report())["crash"]["id"]
    before = len(notifier.outbox)
    res = svc.cancel(crash_id, "the driver")
    assert res["crash"]["status"] == "CANCELLED"
    notices = notifier.outbox[before:]
    assert len(notices) == 3 and all("FALSE ALARM" in m for _, m in notices)
    assert len(svc.cancel(crash_id, "the driver")["dispatches"]) == len(res["dispatches"])  # idempotent
    assert len(notifier.outbox) == before + 3


def test_no_one_reachable_is_flagged_for_the_ops_desk(store, notifier, cfg):
    svc = setup(store, notifier, cfg)
    notifier.fail_for = {"+256700000001", "+256700000003", "+256700000010"}
    res = svc.handle(report())
    assert res["crash"]["status"] == "DISPATCH_FAILED"
    assert all(d["status"] == "failed" for d in res["dispatches"])


def test_unit_failure_but_family_reached(store, notifier, cfg):
    svc = setup(store, notifier, cfg)
    notifier.fail_for = {"+256700000001", "+256700000003"}
    assert svc.handle(report())["crash"]["status"] == "NOK_NOTIFIED"


def test_no_units_registered_still_reaches_family(store, notifier, cfg):
    store.add_contact("UAX123A", "Sarah", "+256700000010", None, True)
    svc = CrashService(store, notifier, cfg)
    assert svc.handle(report())["crash"]["status"] == "NOK_NOTIFIED"
