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

from . import quota

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

# Confirmed (2026-07) that grounding_metadata.grounding_chunks can come back
# completely empty on one call and populated on the next, for the exact same
# building/provider query — see the flakiness note in _search below. That
# case is retried on a future run rather than cached as a permanent
# rejection, but retrying forever on a building that's *consistently* flaky
# would quietly burn through the 20/day free-tier quota one call at a time,
# every time it's reprocessed. This caps it: after this many empty-metadata
# misses (across separate runs, not retried within a single run), give up
# and cache a final rejection like any other confident "not found" — same
# worst-case cost as the old behavior, just delayed enough to give a normal
# flaky miss a couple of chances to resolve itself first.
MAX_EMPTY_METADATA_MISSES = 3


def find_address(building_name, provider_name=None, context_hint="a commercial office building in London, UK"):
    """Best-effort: returns (address_or_none, sources, quota_exhausted).
    address is a plain address string found via a real web search, or
    None if unconfigured, fewer than MIN_INDEPENDENT_SOURCES independent
    sources backed it, the model couldn't confidently find one, or any
    error occurred. sources is the list of distinct source sites (domain,
    or page title when a real domain isn't resolvable from the grounding
    response) the answer was based on — [] whenever address is None.
    quota_exhausted is True specifically when this attempt hit Gemini's
    429 RESOURCE_EXHAUSTED (never for any other kind of failure, and
    never True on a cache hit — only a genuine, just-attempted API call
    can hit a live quota) — extraction.pipeline surfaces this as a
    per-file note so a batch that falls back further than usual (bare
    Nominatim instead of a web-search-found address) is explained rather
    than silently degraded. Never raises — this is always an optional
    last-resort fallback, never something that should fail the batch.

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
        return None, [], False

    cache = _load_cache()
    key = f"{building_name.lower()}|{(provider_name or '').strip().lower()}"
    pending_misses = 0
    if key in cache:
        entry = cache[key]
        if isinstance(entry, dict):
            status = entry.get("status")
            if status == "flaky":
                # A prior run got an answer with no grounding chunks at all
                # (API flakiness, not a real rejection — see _search) and
                # hadn't yet hit MAX_EMPTY_METADATA_MISSES. Retry now
                # rather than trusting that non-final result forever.
                pending_misses = entry.get("misses", 0)
            elif status == "final" or entry.get("address") is not None:
                return entry.get("address"), entry.get("sources") or [], False
            else:
                # A pre-fix cache entry: {"address": None, "sources": []}
                # with no "status" at all. The old code cached this
                # shape for BOTH a genuine rejection and the flaky
                # empty-metadata false negative identically — there's no
                # way to tell which one produced any given entry already
                # on disk. Treat it as an unfinished flaky attempt (retry,
                # still bounded by MAX_EMPTY_METADATA_MISSES) rather than
                # trusting a rejection that might just be stale API
                # flakiness from before this fix existed.
                pending_misses = entry.get("misses", 0)
        else:
            # Tolerate the oldest cache format (a bare string or null, from
            # before `sources`/`status` were tracked) rather than crashing.
            return entry, [], False

    address, sources, cacheable, flaky, quota_exhausted = _search(building_name, provider_name, context_hint)
    if cacheable:
        # A genuine, confident answer from the model (found, or a
        # deliberate "not enough sources"/"no address" rejection) — never a
        # transient failure (e.g. a 429 quota error, a network hiccup, no
        # API key configured). Caching those would permanently poison this
        # building/provider pair: it'd keep returning None on every future
        # run even after the quota resets or a key gets added, since the
        # cache is checked before ever calling the API again. Confirmed
        # this was actually happening — a quota error while re-testing GPE
        # got cached as null for Elsley/Kent House/City Tower/Elm Yard,
        # silently blocking retries.
        cache[key] = {"address": address, "sources": sources, "status": "final"}
        _save_cache(cache)
    elif flaky:
        misses = pending_misses + 1
        if misses >= MAX_EMPTY_METADATA_MISSES:
            print(f"[address_lookup] '{building_name}' hit {misses} empty-metadata misses — giving up for good")
            cache[key] = {"address": None, "sources": [], "status": "final"}
        else:
            cache[key] = {"status": "flaky", "misses": misses}
        _save_cache(cache)
    return address, sources, quota_exhausted


def _search(building_name, provider_name, context_hint):
    """Returns (address_or_none, sources, cacheable, flaky, quota_exhausted)
    — cacheable is False whenever the reason for a None result is
    transient (should be retried on a future run), True when the model
    gave a confident, final answer (an address backed by enough sources,
    or a deliberate rejection) worth remembering. quota_exhausted is True
    only for the specific 429/RESOURCE_EXHAUSTED case, never for any
    other kind of failure (network, no key, no chunks) — see
    extraction.quota.is_quota_exceeded."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, [], False, False, False

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None, [], False, False, False

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
        return None, [], False, False, quota.is_quota_exceeded(e)

    text = (response.text or "").strip()
    if not text or text.upper().startswith(NOT_FOUND):
        return None, [], True, False, False

    # Guard against the model adding anything beyond the single line asked
    # for despite instructions.
    address = text.splitlines()[0].strip().strip('"')
    sources, grounded = _extract_sources(response)

    if len(sources) < MIN_INDEPENDENT_SOURCES:
        if not grounded:
            # Confirmed empirically (2026-07): the same query, called
            # again moments later, can come back with an identical
            # confident address in response.text but with
            # grounding_metadata.grounding_chunks entirely empty/None —
            # even though an earlier call for the exact same building
            # returned real, distinct cited chunks. This is API-side
            # flakiness in whether grounding metadata is populated, not
            # evidence the answer is actually unsupported. Treating it as
            # a deliberate "not enough sources" rejection previously
            # marked this cacheable — which permanently poisoned the
            # cache for this building/provider pair (find_address()
            # checks the cache before ever calling the API again), so one
            # unlucky call blocked it forever, even past a quota reset.
            # Not cacheable here: let a future run retry instead.
            print(
                f"[address_lookup] '{building_name}' -> '{address}' but grounding "
                f"metadata had no chunks at all this call (likely API flakiness, not "
                f"a real rejection) — not caching, will retry on a future run"
            )
            return None, [], False, True, False
        # At least one real chunk came back, just fewer than required — a
        # single thin source is exactly the failure mode that produced
        # three confirmed wrong addresses (Porters Place, Elsley, Kent
        # House) before this check existed — don't accept it just because
        # the model sounded confident. Still a real, cacheable outcome
        # (not a transient error) — the caller falls back to the bare-name
        # Nominatim tier from here.
        print(
            f"[address_lookup] rejecting '{address}' for '{building_name}' — only "
            f"{len(sources)} independent source(s) cited (need >= {MIN_INDEPENDENT_SOURCES}): {sources}"
        )
        return None, sources, True, False, False

    return address, sources, True, False, False


def _extract_sources(response):
    """Returns (sources, grounded). sources is the best-effort list of
    distinct sources the grounding response actually cited, for the
    minimum-sources check above and so a wrong answer is traceable in the
    log/spreadsheet rather than an opaque coordinate. grounded is True
    whenever the response carried at least one raw grounding chunk at
    all — even if none of them resolved to a usable identity below — so
    the caller can tell "grounding ran and cited real sources, just too
    few of them" apart from "grounding metadata was empty this call"
    (see the flakiness note in _search above); those need different
    cacheability treatment. Gemini's grounding chunks give a redirect URL
    through Google's own domain, not the real source site, most of the
    time — so this prefers the chunk's page title (which usually names
    the real site) whenever the URL's own hostname isn't an independent
    domain."""
    try:
        metadata = response.candidates[0].grounding_metadata
        chunks = metadata.grounding_chunks if metadata else None
    except (AttributeError, IndexError):
        return [], False
    if not chunks:
        return [], False

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
    return sources, True


STORAGE_KEY = "cache/address_lookup_cache.json"


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
    # Local disk is gone (Render's free-tier disk is ephemeral and wiped on
    # every cold start/redeploy — confirmed 2026-07 this was causing
    # buildings to get needlessly re-queried against the 20/day free-tier
    # grounding quota after every restart). Fall back to the same
    # S3-compatible object storage already used for batch outputs
    # (top-level storage.py, not a package under extraction/) — a no-op
    # returning None if unconfigured, so local-only dev behaves exactly
    # as before.
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
    """Local disk only — fast, synchronous, called after every single new
    entry. Mirroring to S3 here too would add a real network round-trip on
    every distinct building/address processed (confirmed empirically this
    was slow enough, stacked on top of Nominatim's 1-req/sec throttle and
    Gemini's multi-second grounding calls, to blow past gunicorn's default
    30s worker timeout on a multi-building batch) — see flush_to_storage,
    called once per batch instead, not once per record."""
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


def invalidate(substring):
    """Removes every cached entry whose "building|provider" key contains
    `substring` (case-insensitive) — from the in-memory cache this process
    is currently using, local disk, and the S3 mirror, in that order. See
    extraction.geocode.invalidate for why mutating the already-loaded
    `_cache` global matters, not just rewriting disk/S3: a long-running
    worker process only reads this cache from disk/S3 once, on first use,
    so a fix here is what actually stops the *current* process from
    continuing to serve a stale answer, without waiting for a
    redeploy/restart.
    Returns the list of keys actually removed (empty if nothing matched)."""
    cache = _load_cache()
    needle = (substring or "").strip().lower()
    if not needle:
        return []
    removed = [k for k in cache if needle in k]
    for k in removed:
        del cache[k]
    if removed:
        _save_cache(cache)
        flush_to_storage()
    return removed
