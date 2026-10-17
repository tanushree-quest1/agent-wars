"""Google Routes API (traffic-aware) with a haversine fallback so the app works without a key."""
import functools, math
import httpx
from .config import MAPS_KEY


def haversine(a, b):
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def decode_polyline(s):
    pts, i, lat, lng = [], 0, 0, 0
    while i < len(s):
        for k in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if k == 0:
                lat += d
            else:
                lng += d
        pts.append([lat / 1e5, lng / 1e5])
    return pts


_HTTP = httpx.Client(timeout=8.0)


@functools.lru_cache(maxsize=4096)
def _route(o, d):
    if haversine(o, d) < 0.05:
        return {"minutes": 0.0, "km": 0.0, "points": [list(o), list(d)], "source": "same_point"}
    if MAPS_KEY:
        try:
            body = {"origin": {"location": {"latLng": {"latitude": o[0], "longitude": o[1]}}},
                    "destination": {"location": {"latLng": {"latitude": d[0], "longitude": d[1]}}},
                    "travelMode": "DRIVE", "routingPreference": "TRAFFIC_AWARE", "units": "METRIC"}
            r = _HTTP.post("https://routes.googleapis.com/directions/v2:computeRoutes", json=body,
                           headers={"X-Goog-Api-Key": MAPS_KEY,
                                    "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline"})
            r.raise_for_status()
            rt = r.json()["routes"][0]
            pts = decode_polyline(rt["polyline"]["encodedPolyline"])
            step = max(1, len(pts) // 80)
            return {"minutes": int(rt["duration"].rstrip("s")) / 60, "km": rt["distanceMeters"] / 1000,
                    "points": pts[::step] + [pts[-1]], "source": "google_routes"}
        except Exception:
            pass
    km = haversine(o, d) * 1.28
    return {"minutes": km / 48 * 60, "km": km, "points": [list(o), list(d)], "source": "haversine_fallback"}


def route(o, d):
    return _route((round(o[0], 4), round(o[1], 4)), (round(d[0], 4), round(d[1], 4)))
