"""Free geocoding via OpenStreetMap Nominatim, with an on-disk cache and
rate limiting per Nominatim's usage policy (max 1 request/sec, and a
descriptive User-Agent identifying the app instead of a default library
one): https://operations.osmfoundation.org/policies/nominatim/
"""
import json
import time
from pathlib import Path

import requests

from .address import extract_postcode

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "manage-office-availability/1.0 (contact: team@spacepoint.co.uk)"
MIN_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT = 10

CACHE_PATH = Path(__file__).resolve().parent.parent / ".geocode_cache.json"

_cache = None
_last_request_at = 0.0


def geocode(address):
    """Look up (lat, lng, postcode) for a free-text address via Nominatim.

    Returns (lat, lng, postcode, error): lat/lng are floats, or None if no
    confident match was found; postcode is whatever Nominatim's address
    breakdown reports for the match (normalized the same way as
    extraction.address.extract_postcode), or "" if it didn't include one —
    a useful fallback for sources whose own address text has no postcode
    at all. error is a short human-readable reason (never raises — a
    network failure is reported the same way as "no match", so callers can
    always just leave Lat/Lng/postcode blank on error rather than guessing).

    Results are cached on disk keyed by the normalized address string, so
    the same building is never re-geocoded across runs.
    """
    address = (address or "").strip()
    if not address:
        return None, None, "", "No address to geocode"

    cache = _load_cache()
    key = _cache_key(address)
    if key in cache:
        hit = cache[key]
        return hit.get("lat"), hit.get("lng"), hit.get("postcode", ""), hit.get("error")

    lat, lng, postcode, error = _fetch(address)
    cache[key] = {"lat": lat, "lng": lng, "postcode": postcode, "error": error}
    _save_cache(cache)
    return lat, lng, postcode, error


def _fetch(address):
    _throttle()
    try:
        resp = requests.get(
            NOMINATIM_URL,
            # addressdetails=1 gets a structured breakdown (road, postcode,
            # city, ...) alongside the match, so we can fall back to
            # Nominatim's postcode when the source text didn't have one.
            params={"q": address, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        return None, None, "", f"Geocoding request failed: {e}"

    if not results:
        return None, None, "", "No geocoding match found for this address"

    try:
        lat = float(results[0]["lat"])
        lng = float(results[0]["lon"])
    except (KeyError, ValueError, TypeError) as e:
        return None, None, "", f"Unexpected geocoding response shape: {e}"

    postcode = extract_postcode((results[0].get("address") or {}).get("postcode", ""))
    return lat, lng, postcode, None


def _throttle():
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_INTERVAL_SECONDS:
        time.sleep(MIN_INTERVAL_SECONDS - elapsed)
    _last_request_at = time.monotonic()


def _cache_key(address):
    return " ".join(address.strip().lower().split())


STORAGE_KEY = "cache/geocode_cache.json"


def _load_cache():
    global _cache
    if _cache is not None:
        return _cache
    if CACHE_PATH.exists():
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            return _cache
        except (json.JSONDecodeError, OSError):
            pass
    # Same ephemeral-disk risk as extraction.address_lookup's cache — Render's
    # free-tier disk is wiped on cold start/redeploy, so fall back to the
    # same S3-compatible object storage used for batch outputs (top-level
    # storage.py) before giving up and starting from an empty cache.
    import storage

    data = storage.fetch(STORAGE_KEY)
    if data is not None:
        try:
            _cache = json.loads(data)
            return _cache
        except json.JSONDecodeError:
            pass
    _cache = {}
    return _cache


def _save_cache(cache):
    """Local disk only — see extraction.address_lookup._save_cache for why
    mirroring to S3 here too (once per record, called after every new
    address geocoded) was confirmed to add enough latency to risk
    exceeding gunicorn's default worker timeout on a multi-building batch.
    flush_to_storage below does the mirror once per batch instead."""
    global _cache
    _cache = cache
    try:
        CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def flush_to_storage():
    """Best-effort, one-shot mirror of the current on-disk cache to the
    same S3-compatible storage used for batch outputs — call this once
    after a whole batch finishes (extraction.pipeline.process_files),
    not per-record. No-op if unconfigured or nothing has been cached yet
    this process."""
    if not CACHE_PATH.exists():
        return
    import storage

    storage.upload(STORAGE_KEY, CACHE_PATH)
