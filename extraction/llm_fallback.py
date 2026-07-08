"""LLM-based extraction fallback, used when no rule-based parser recognizes
a file's layout. Calls the Gemini API with the raw extracted text and
asks for JSON matching the target schema; validates the response before it
ever reaches the spreadsheet.
"""
import json
import os
import re

from .schema import LLM_FIELDS

# gemini-3.5-flash's free tier is capped at just 20 requests/day (Google's
# newest flash-tier model). gemini-3.1-flash-lite is explicitly positioned
# for high-volume, cost-sensitive traffic and gets a much more generous
# free-tier daily quota, while still supporting JSON mode and thinking_config.
MODEL = "gemini-3.1-flash-lite"
MAX_TEXT_CHARS = 40000  # keep prompts bounded; most sources are far shorter,
# but a multi-page brochure with 40+ listings can get close to this.
MAX_OUTPUT_TOKENS = 24000  # a 17-field JSON schema repeated per listing adds
# up fast — a ~40-listing document needs well over the old 4000-token budget.
# The model's ceiling is 65536; this leaves comfortable headroom for larger ones.


class LLMExtractionError(Exception):
    pass


def extract_with_llm(text, source_hint=""):
    """Returns (records, source_name). source_name is the LLM's best guess at
    the sender/broker/company this document is from (used to name the output
    spreadsheet), or "" if it couldn't confidently tell."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMExtractionError(
            "No GEMINI_API_KEY set — cannot use the LLM fallback for this file. "
            "Add it to your .env file (see README)."
        )

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise LLMExtractionError("The 'google-genai' package is not installed — run: pip install -r requirements.txt")

    truncated = text[:MAX_TEXT_CHARS]
    prompt = _build_prompt(truncated, source_hint)

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=MAX_OUTPUT_TOKENS,
                response_mime_type="application/json",
                # Keep the token budget for the actual JSON response, not
                # hidden reasoning — max_output_tokens caps both combined.
                thinking_config=types.ThinkingConfig(thinking_level="low"),
            ),
        )
    except Exception as e:
        raise LLMExtractionError(f"Gemini API call failed: {e}")

    raw = response.text or ""
    records, source_name = _parse_and_validate(raw)
    if not records:
        raise LLMExtractionError("LLM returned no usable records for this file")
    return records, source_name


def _build_prompt(text, source_hint):
    fields = ", ".join(f'"{f}"' for f in LLM_FIELDS)
    return (
        "You extract commercial office-space listings from arbitrary documents "
        "(broker emails, PDFs, spreadsheets) into a fixed JSON schema.\n\n"
        "Return ONLY a JSON object (no markdown, no commentary) shaped like:\n"
        '{"source_name": "...", "listings": [...]}\n\n'
        '"source_name" is the sender/broker/company this document is from, if evident '
        "(e.g. from a letterhead, email signature, or branding) — a short name suitable "
        'for use as a filename (e.g. "MetSpace", "Acme Realty"). If you cannot confidently '
        'identify it, use "".\n\n'
        f'"listings" is a JSON array. Each element is one listing (one floor/unit = one '
        f"element) with exactly these string fields: [{fields}].\n\n"
        "Rules:\n"
        '- If a field isn\'t present in the source, use "" (empty string) — never omit a field or invent data.\n'
        "- \"Size (sq ft)\", \"Desks (max)\", the two \"Marketing Price...\" fields should be plain numbers "
        "as strings (no currency symbols, no commas, no units) — e.g. \"4284\" not \"4,284 sqft\".\n"
        "- If only a monthly price OR only a price-per-sqft is given, still only fill in the one you can "
        "read directly from the source — do not calculate the other yourself.\n"
        "- \"Area\" is the neighbourhood/district name if given (e.g. \"Mayfair\", \"Fitzrovia\").\n"
        "- \"Contacts\" should list every named contact/broker/agent for that listing, comma-separated "
        "(e.g. \"Kieran Christie, Sophie Haugh, Nicki Mayle\") — however many are actually given, not "
        "just one or two.\n"
        "- \"Special Features\" can combine any descriptive text that doesn't fit another field.\n"
        '- If the document has no office-listing data at all, return {"source_name": "", "listings": []}.\n\n'
        f"Source file hint: {source_hint or 'unknown'}\n\n"
        "Document text:\n"
        "-----\n"
        f"{text}\n"
        "-----"
    )


def _parse_and_validate(raw):
    cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise LLMExtractionError(f"LLM response was not valid JSON: {e}")

    if not isinstance(data, dict) or "listings" not in data:
        raise LLMExtractionError('LLM response was valid JSON but not shaped like {"source_name", "listings"}')

    listings = data.get("listings")
    if not isinstance(listings, list):
        raise LLMExtractionError("LLM response's \"listings\" field was not a list")

    source_name = data.get("source_name")
    source_name = source_name.strip() if isinstance(source_name, str) else ""

    records = []
    for item in listings:
        if not isinstance(item, dict):
            continue
        record = {}
        for field in LLM_FIELDS:
            v = item.get(field, "")
            record[field] = "" if v is None else str(v)
        records.append(record)
    return records, source_name
