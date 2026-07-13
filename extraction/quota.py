"""Shared handling for Gemini's per-day free-tier quota (429
RESOURCE_EXHAUSTED), used by both Gemini-calling modules in this app —
extraction.llm_fallback (plain listing extraction, gemini-3.1-flash-lite)
and extraction.address_lookup (Google Search-grounded address lookup,
gemini-2.5-flash). Each has its own separate model and its own separate
daily quota (see each module's own docstring for why), but a
quota-exhausted error looks the same from the API's own error shape
either way, and should read the same way to whoever's running a batch.
"""
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — stdlib since Python 3.9
    ZoneInfo = None

PACIFIC = "America/Los_Angeles"
# This app's own audience is UK-based (team@spacepoint.co.uk) — the reset
# time is converted to UK local time rather than assuming whoever reads
# the message already knows what "midnight Pacific" is in their own
# clock, or leaving it in Pacific time only.
LOCAL_DISPLAY_TZ = "Europe/London"


def is_quota_exceeded(exc):
    """True if `exc` (any exception raised by a google-genai call) looks
    like a 429/RESOURCE_EXHAUSTED daily-quota error specifically — never
    a network hiccup, an auth failure, or anything else that should be
    reported/retried differently."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc)
    return code in (429, "429") or "RESOURCE_EXHAUSTED" in text


def reset_message(what_hit_it):
    """A user-facing sentence: `what_hit_it` (e.g. "Gemini's daily
    AI-extraction limit", "Gemini's daily address-search limit") plus
    this app's own best-effort note on when it resets. Google resets
    Gemini's free-tier daily quotas at midnight Pacific Time; this
    computes the next one from the real current time (not a hardcoded
    offset — Pacific and UK clocks each observe their own DST on
    different dates, so the gap between them isn't always exactly the
    same number of hours) and converts it to a UK clock-time plus an
    approximate hours-remaining figure. Falls back to a
    timezone-agnostic version of the message if the deployment's Python
    has no IANA tzdata available (see the `tzdata` package in
    requirements.txt for why this shouldn't normally happen on Render) —
    never allowed to itself raise and break the error path it's called
    from."""
    reset_clause = "This resets at midnight Pacific Time"
    if ZoneInfo is not None:
        try:
            pacific_now = datetime.now(ZoneInfo(PACIFIC))
            next_midnight_pacific = (pacific_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            hours_remaining = (next_midnight_pacific - pacific_now).total_seconds() / 3600
            reset_local = next_midnight_pacific.astimezone(ZoneInfo(LOCAL_DISPLAY_TZ))
            hours_display = max(1, round(hours_remaining))
            reset_clause = (
                f"This resets at midnight Pacific Time — "
                f"{reset_local.hour:02d}:{reset_local.minute:02d} UK time, "
                f"in about {hours_display} hour{'s' if hours_display != 1 else ''}"
            )
        except Exception:
            pass
    return f"{what_hit_it} has been reached for today. {reset_clause}."
