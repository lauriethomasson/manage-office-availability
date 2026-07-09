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
from urllib.parse import urlparse

# Confirmed empirically (2026-07) that Google Search grounding returns
# 429 RESOURCE_EXHAUSTED on this project's free tier for both
# gemini-3.1-flash-lite and gemini-2.0-flash (used elsewhere in this repo
# for plain, non-grounded extraction) — only gemini-2.5-flash actually
# has free grounding quota available. Worth re-checking if this ever
# starts failing; Google's model/quota lineup for grounding shifts over time.
MODEL = "gemini-2.5-flash"
NOT_FOUND = "NOT_FOUND"

# Confirmed (2026-07) that a single-source grounded answer can still be
# wrong — three real cases (Porters Place, Elsley, Kent House) each
# resolved to a plausible-looking but wrong address before this
# safeguard existed. Requiring at least this many independent sources to
# agree doesn't eliminate the risk (see MIN_INDEPENDENT_SOURCES test
# notes), but it does reject a lot of thin, single-page answers outright.
MIN_INDEPENDENT_SOURCES = 2

# A web search is not perfectly deterministic — the same building name can
# come back with a subtly different address (e.g. a neighboring postcode)
# on different calls. Caching by building name pins whichever address was
# found first, so repeated runs for the same building stay consistent
# instead of drifting between calls.
CACHE_PATH = Path(__file__).resolve().parent.parent / ".address_lookup_cache.json"
_cache = None


def find_address(building_name, provider_name=None, context_hint="a commercial office building in London, UK"):
    """Best-effort: returns (address_or_none, sources) — address is a
    plain address string found via a real web search, or None if
    unconfigured, fewer than MIN_INDEPENDENT_SOURCES independent sources
    backed it, the model couldn't confidently find one, or any error
    occurred. sources is the list of distinct source sites (domain, or
    page title when a real domain isn't resolvable from the grounding
    response) the answer was based on — [] whenever address is None.
    Never raises — this is always an optional last-resort fallback, never
    something that should fail the batch.

    provider_name (e.g. "GPE", "MetSpace", "Knotel", "Kitts", "BC") is the
    source/broker this listing came from — included in the search so a
    generic building name (e.g. "Elsley") isn't confused with an unrelated
    building of the same name elsewhere. Confirmed empirically this
    matters: a bare-name search for "Elsley" alone found a building in
    Battersea (SW11 5LL), when the real GPE-managed "Elsley" is in
    Fitzrovia (W1W 8BF) — adding "GPE" to the search fixed it.
    """
    building_name = (building_name or "").strip()
    if not building_name:
        return None, []

    cache = _load_cache()
    key = f"{building_name.lower()}|{(provider_name or '').strip().lower()}"
    if key in cache:
        entry = cache[key]
        # Tolerate the older cache format (a bare string or null, from
        # before `sources` was tracked) rather than crashing on it.
        if isinstance(entry, dict):
            return entry.get("address"), entry.get("sources") or []
        return entry, []

    address, sources, cacheable = _search(building_name, provider_name, context_hint)
    # Only cache a genuine, confident answer from the model (found, or a
    # deliberate "not enough sources"/"no address" rejection) — never a
    # transient failure (e.g. a 429 quota error, a network hiccup, no API
    # key configured). Caching those would permanently poison this
    # building/provider pair: it'd keep returning None on every future run
    # even after the quota resets or a key gets added, since the cache is
    # checked before ever calling the API again. Confirmed this was
    # actually happening — a quota error while re-testing GPE got cached
    # as null for Elsley/Kent House/City Tower/Elm Yard, silently blocking
    # retries.
    if cacheable:
        cache[key] = {"address": address, "sources": sources}
        _save_cache(cache)
    return address, sources


def _search(building_name, provider_name, context_hint):
    """Returns (address_or_none, sources, cacheable) — cacheable is False
    whenever the reason for a None result is transient (should be retried
    on a future run), True when the model gave a confident, final answer
    (an address backed by enough sources, or a deliberate rejection)
    worth remembering."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, [], False

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None, [], False

    subject = f'a building called "{building_name}"'
    if provider_name:
        subject += f', operated by or listed under "{provider_name}"'

    prompt = (
        f"Search the web to find the real, current full street address (including "
        f"postcode) of {subject}, which is {context_hint}. Base your answer only on "
        f"actual web search results — if search results don't confirm a specific "
        f"address for this specific building, don't guess from prior knowledge alone.\n\n"
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
        # A quota error (429), a network failure, etc. — not a real
        # answer, so not cacheable; the caller should be free to retry
        # this exact building/provider pair again next time.
        print(f"[address_lookup] web search failed for '{building_name}': {type(e).__name__}: {e}")
        return None, [], False

    text = (response.text or "").strip()
    if not text or text.upper().startswith(NOT_FOUND):
        return None, [], True

    # Guard against the model adding anything beyond the single line asked
    # for despite instructions.
    address = text.splitlines()[0].strip().strip('"')
    sources = _extract_sources(response)

    if len(sources) < MIN_INDEPENDENT_SOURCES:
        # A single thin source is exactly the failure mode that produced
        # three confirmed wrong addresses (Porters Place, Elsley, Kent
        # House) before this check existed — don't accept it just because
        # the model sounded confident. Still a real, cacheable outcome
        # (not a transient error) — the caller falls back to the bare-name
        # Nominatim tier from here.
        print(
            f"[address_lookup] rejecting '{address}' for '{building_name}' — only "
            f"{len(sources)} independent source(s) cited (need >= {MIN_INDEPENDENT_SOURCES}): {sources}"
        )
        return None, sources, True

    return address, sources, True


def _extract_sources(response):
    """Best-effort list of distinct sources the grounding response
    actually cited, for the minimum-sources check above and so a wrong
    answer is traceable in the log/spreadsheet rather than an opaque
    coordinate. Gemini's grounding chunks give a redirect URL through
    Google's own domain, not the real source site, most of the time — so
    this prefers the chunk's page title (which usually names the real
    site) whenever the URL's own hostname isn't an independent domain."""
    try:
        metadata = response.candidates[0].grounding_metadata
        chunks = metadata.grounding_chunks if metadata else None
    except (AttributeError, IndexError):
        return []
    if not chunks:
        return []

    sources = []
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        if not web:
            continue
        uri = getattr(web, "uri", "") or ""
        title = getattr(web, "title", "") or ""
        host = urlparse(uri).netloc.lower()
        is_real_domain = host and "google" not in host and "vertexaisearch" not in host
        identity = host if is_real_domain else (title.strip() or uri)
        if identity and identity not in sources:
            sources.append(identity)
    return sources


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
