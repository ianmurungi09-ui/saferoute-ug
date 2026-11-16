"""Module 3: crowdsourced pothole / hazard map.

A device reports a vertical-acceleration spike with a GPS fix. Reports within
~25 m merge into one hazard. A hazard only becomes `confirmed` (and only then
triggers driver alerts) once several DISTINCT devices have hit it, so one noisy
sensor can't paint the map. Severity thresholds depend heavily on vehicle,
mounting and speed: calibrate them on real roads before trusting them."""
from __future__ import annotations

from .geo import haversine_m, is_ahead

PUBLIC_STATUSES = ("reported", "confirmed")


class HazardService:
    def __init__(self, store, cfg) -> None:
        self.store, self.cfg = store, cfg

    def severity_for(self, peak_g: float) -> str:
        c = self.cfg
        return "high" if peak_g >= c.hazard_severity_high_g else "medium" if peak_g >= c.hazard_severity_medium_g else "low"

    def report(self, device_id: str, lat: float, lon: float, peak_g: float, speed_kmh: float, ts: float) -> dict:
        c = self.cfg
        if peak_g > c.hazard_max_peak_g:
            return {"accepted": False, "reason": "peak_too_high_for_road_hazard"}
        if speed_kmh < c.hazard_min_speed_kmh:
            return {"accepted": False, "reason": "speed_too_low"}  # humps and handling noise at crawl speed

        haz = self.store.find_hazard_near(lat, lon, c.hazard_merge_radius_m)
        if haz is None:
            hid = self.store.create_hazard(lat, lon, peak_g, self.severity_for(peak_g), ts)
            self.store.add_hazard_report(hid, device_id, ts, lat, lon, peak_g, speed_kmh)
            return {"accepted": True, "hazard_id": hid, "status": "reported", "new": True}

        if haz["status"] == "dismissed":
            return {"accepted": False, "reason": "location_dismissed_by_operator", "hazard_id": haz["id"]}

        hid, n = haz["id"], haz["n_reports"]
        self.store.add_hazard_report(hid, device_id, ts, lat, lon, peak_g, speed_kmh)
        max_g = max(haz["max_g"], peak_g)
        status = haz["status"]
        fields = {
            "lat": (haz["lat"] * n + lat) / (n + 1),  # running centroid
            "lon": (haz["lon"] * n + lon) / (n + 1),
            "n_reports": n + 1,
            "max_g": max_g,
            "severity": self.severity_for(max_g),
            "last_seen": ts,
        }
        if status == "reported" and self.store.hazard_device_count(hid) >= c.hazard_confirm_devices:
            status = "confirmed"
        elif status == "repaired":
            # Re-open only if several different devices hit it again after the repair.
            if self.store.hazard_device_count(hid, since=haz["repaired_at"]) >= c.hazard_confirm_devices:
                status = "confirmed"
                fields["repaired_at"] = None
        fields["status"] = status
        self.store.update_hazard(hid, **fields)
        return {"accepted": True, "hazard_id": hid, "status": status, "new": False}

    def set_status(self, hazard_id: int, status: str, now: float) -> dict | None:
        if self.store.get_hazard(hazard_id) is None:
            return None
        fields: dict = {"status": status}
        if status == "repaired":
            fields["repaired_at"] = now
        self.store.update_hazard(hazard_id, **fields)
        return self.store.get_hazard(hazard_id)

    def ahead(self, lat: float, lon: float, heading: float | None, radius_m: float | None = None) -> list[dict]:
        radius = radius_m or self.cfg.hazard_alert_radius_m
        box = self.store.list_hazards(("confirmed",), self._bbox(lat, lon, radius))
        out = []
        for h in box:
            d = haversine_m(lat, lon, h["lat"], h["lon"])
            if d <= radius and is_ahead(lat, lon, heading, h["lat"], h["lon"], 50.0):
                out.append({"id": h["id"], "distance_m": round(d), "severity": h["severity"],
                            "lat": h["lat"], "lon": h["lon"]})
        return sorted(out, key=lambda x: x["distance_m"])

    @staticmethod
    def _bbox(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
        import math
        dlat = radius_m / 111_320.0 * 1.2
        dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
        return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)

    def geojson(self, statuses=PUBLIC_STATUSES, bbox=None) -> dict:
        """Hazard heatmap feed for UNRA / municipal GIS. Contains no device or vehicle identifiers."""
        feats = []
        for h in self.store.list_hazards(statuses, bbox):
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(h["lon"], 6), round(h["lat"], 6)]},
                "properties": {"id": h["id"], "status": h["status"], "severity": h["severity"],
                             "reports": h["n_reports"], "max_g": round(h["max_g"], 2),
                             "first_seen": h["first_seen"], "last_seen": h["last_seen"]},
            })
        return {"type": "FeatureCollection", "features": feats}
