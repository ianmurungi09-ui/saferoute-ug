"""Small geodesy helpers plus a seed gazetteer of Ugandan towns."""
from __future__ import annotations

import math

EARTH_R = 6_371_000.0

# Approximate town-centre coordinates, good enough for "3.2 km NE of Lugazi" in an SMS.
# Replace with a proper gazetteer (OpenStreetMap / GeoNames) for production.
LANDMARKS: list[tuple[str, float, float]] = [
    ("Kampala", 0.3476, 32.5825),
    ("Entebbe", 0.0512, 32.4637),
    ("Mpigi", 0.2250, 32.3139),
    ("Lukaya", 0.1850, 31.8870),
    ("Masaka", -0.3136, 31.7413),
    ("Mbarara", -0.6072, 30.6545),
    ("Kabale", -1.2490, 29.9899),
    ("Mukono", 0.3533, 32.7553),
    ("Lugazi", 0.3667, 32.9333),
    ("Jinja", 0.4244, 33.2041),
    ("Iganga", 0.6092, 33.4686),
    ("Tororo", 0.6928, 34.1810),
    ("Mbale", 1.0821, 34.1751),
    ("Soroti", 1.7147, 33.6111),
    ("Lira", 2.2499, 32.8998),
    ("Gulu", 2.7746, 32.2990),
    ("Luweero", 0.8333, 32.4833),
    ("Masindi", 1.6744, 31.7150),
    ("Hoima", 1.4331, 31.3524),
    ("Mityana", 0.4167, 32.0333),
    ("Mubende", 0.5833, 31.3667),
    ("Fort Portal", 0.6710, 30.2748),
    ("Kasese", 0.1833, 30.0833),
]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_diff(a: float, b: float) -> float:
    """Smallest absolute difference between two compass angles, 0..180."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def compass(deg: float) -> str:
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int((deg % 360) / 45.0 + 0.5) % 8]


def is_ahead(lat: float, lon: float, heading: float | None,
             tlat: float, tlon: float, half_angle: float = 60.0) -> bool:
    """True if the target lies within +/-half_angle of the direction of travel.
    With no known heading we cannot tell, so we say yes (alert rather than miss)."""
    if heading is None:
        return True
    return angle_diff(heading, bearing_deg(lat, lon, tlat, tlon)) <= half_angle


def nearest_landmark(lat: float, lon: float) -> str:
    name, llat, llon = min(LANDMARKS, key=lambda t: haversine_m(lat, lon, t[1], t[2]))
    d_km = haversine_m(lat, lon, llat, llon) / 1000.0
    if d_km < 1.0:
        return f"at {name}"
    return f"{d_km:.1f} km {compass(bearing_deg(llat, llon, lat, lon))} of {name}"
