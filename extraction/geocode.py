"""Free geocoding via OpenStreetMap Nominatim, with an on-disk cache and
rate limiting per Nominatim's usage policy (max 1 request/sec, and a
descriptive User-Agent identifying the app instead of a default library
one): https://operations.osmfoundation.org/policies/nominatim/
"""
import json
import time
from pathlib import Path

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "manage-office-availability/1.0 (contact: team@spacepoint.co.uk)"
MIN_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT = 10

CACHE_PATH = Path(__file__).resolve().parent.parent / ".geocode_cache.json"

_cache = None
_last_request_at = 0.0


def geocode(address):
    """Look up (lat, lng) for a free-text address via Nominatim.

    Returns (lat, lng, error): lat/lng are floats, or None if no confident
    match was found; error is a short human-readable reason (never raises —
    a network failure is reported the same way as "no match", so callers
    can always just leave Lat/Lng blank on error rather than guessing).

    Results are cached on disk keyed by the normalized address string, so
    the same building is never re-geocoded across runs.
    """
    address = (address or "").strip()
    if not address:
        return None, None, "No address to geocode"

    cache = _load_cache()
    key = _cache_key(address)
    if key in cache:
        hit = cache[key]
        return hit.get("lat"), hit.get("lng"), hit.get("error")

    lat, lng, error = _fetch(address)
    cache[key] = {"lat": lat, "lng": lng, "error": error}
    _save_cache(cache)
    return lat, lng, error


def _fetch(address):
    _throttle()
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        return None, None, f"Geocoding request failed: {e}"

    if not results:
        return None, None, "No geocoding match found for this address"

    try:
        return float(results[0]["lat"]), float(results[0]["lon"]), None
    except (KeyError, ValueError, TypeError) as e:
        return None, None, f"Unexpected geocoding response shape: {e}"


def _throttle():
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_INTERVAL_SECONDS:
        time.sleep(MIN_INTERVAL_SECONDS - elapsed)
    _last_request_at = time.monotonic()


def _cache_key(address):
    return " ".join(address.strip().lower().split())


def _load_cache():
    global _cache
    if _cache is not None:
        return _cache
    if CACHE_PATH.exists():
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _cache = {}
    else:
        _cache = {}
    return _cache


def _save_cache(cache):
    global _cache
    _cache = cache
    try:
        CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
