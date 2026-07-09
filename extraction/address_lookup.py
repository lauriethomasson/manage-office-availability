"""Web-search-grounded address lookup for buildings Nominatim can't find
by name/postcode alone. Uses Gemini's Google Search grounding tool to
actually search the web for a named building and extract a real address
from genuine search results — this is not a Nominatim query variant, it's
a distinct fallback that finds new address information Nominatim never
had to begin with.
"""
import json
import os
from pathlib import Path

# Confirmed empirically (2026-07) that Google Search grounding returns
# 429 RESOURCE_EXHAUSTED on this project's free tier for both
# gemini-3.1-flash-lite and gemini-2.0-flash (used elsewhere in this repo
# for plain, non-grounded extraction) — only gemini-2.5-flash actually
# has free grounding quota available. Worth re-checking if this ever
# starts failing; Google's model/quota lineup for grounding shifts over time.
MODEL = "gemini-2.5-flash"
NOT_FOUND = "NOT_FOUND"

# A web search is not perfectly deterministic — the same building name can
# come back with a subtly different address (e.g. a neighboring postcode)
# on different calls. Caching by building name pins whichever address was
# found first, so repeated runs for the same building stay consistent
# instead of drifting between calls.
CACHE_PATH = Path(__file__).resolve().parent.parent / ".address_lookup_cache.json"
_cache = None


def find_address(building_name, context_hint="a commercial office building in London, UK"):
    """Best-effort: returns a plain address string found via a real web
    search, or None if unconfigured, the model couldn't confidently find
    a real address for this specific building, or any error occurred.
    Never raises — this is always an optional last-resort fallback, never
    something that should fail the batch."""
    building_name = (building_name or "").strip()
    if not building_name:
        return None

    cache = _load_cache()
    key = building_name.lower()
    if key in cache:
        return cache[key]

    address = _search(building_name, context_hint)
    cache[key] = address
    _save_cache(cache)
    return address


def _search(building_name, context_hint):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None

    prompt = (
        f'Search the web to find the real, current full street address (including '
        f'postcode) of a specific building called "{building_name}", which is {context_hint}. '
        f"Return ONLY the address as plain text on a single line "
        f'(e.g. "12 Example Street, London EC1A 1BB") — no commentary, no markdown, no quotes. '
        f"If a web search does not confidently identify a real address for this specific "
        f'building, return exactly {NOT_FOUND} and nothing else.'
    )

    try:
        client = genai.Client(api_key=api_key, vertexai=False)
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            # No thinking_config here — gemini-2.5-flash rejects
            # thinking_level (that param is for the newer 3.x models used
            # elsewhere in this repo); this model's defaults are fine for
            # a short lookup like this.
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
        )
    except Exception as e:
        print(f"[address_lookup] web search failed for '{building_name}': {type(e).__name__}: {e}")
        return None

    text = (response.text or "").strip()
    if not text or text.upper().startswith(NOT_FOUND):
        return None
    # Guard against the model adding anything beyond the single line asked
    # for despite instructions.
    return text.splitlines()[0].strip().strip('"')


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
