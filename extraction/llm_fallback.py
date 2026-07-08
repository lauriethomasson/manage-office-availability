"""LLM-based extraction fallback, used when no rule-based parser recognizes
a file's layout. Calls the Gemini API with the raw extracted text and
asks for JSON matching the target schema; validates the response before it
ever reaches the spreadsheet.
"""
import json
import os
import re

from .schema import LLM_FIELDS

MODEL = "gemini-3.5-flash"
MAX_TEXT_CHARS = 15000  # keep prompts bounded; most sources are far shorter


class LLMExtractionError(Exception):
    pass


def extract_with_llm(text, source_hint=""):
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
                max_output_tokens=4000,
                response_mime_type="application/json",
                # Keep the token budget for the actual JSON response, not
                # hidden reasoning — max_output_tokens caps both combined.
                thinking_config=types.ThinkingConfig(thinking_level="low"),
            ),
        )
    except Exception as e:
        raise LLMExtractionError(f"Gemini API call failed: {e}")

    raw = response.text or ""
    records = _parse_and_validate(raw)
    if not records:
        raise LLMExtractionError("LLM returned no usable records for this file")
    return records


def _build_prompt(text, source_hint):
    fields = ", ".join(f'"{f}"' for f in LLM_FIELDS)
    return (
        "You extract commercial office-space listings from arbitrary documents "
        "(broker emails, PDFs, spreadsheets) into a fixed JSON schema.\n\n"
        f"Return ONLY a JSON array (no markdown, no commentary). Each element is one "
        f"listing (one floor/unit = one element) with exactly these string fields: "
        f"[{fields}].\n\n"
        "Rules:\n"
        '- If a field isn\'t present in the source, use "" (empty string) — never omit a field or invent data.\n'
        "- \"Size (sq ft)\", \"Desks (max)\", the two \"Marketing Price...\" fields should be plain numbers "
        "as strings (no currency symbols, no commas, no units) — e.g. \"4284\" not \"4,284 sqft\".\n"
        "- If only a monthly price OR only a price-per-sqft is given, still only fill in the one you can "
        "read directly from the source — do not calculate the other yourself.\n"
        "- \"Area\" is the neighbourhood/district name if given (e.g. \"Mayfair\", \"Fitzrovia\").\n"
        "- \"Special Features\" can combine any descriptive text that doesn't fit another field.\n"
        "- If the document has no office-listing data at all, return an empty JSON array: [].\n\n"
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

    if not isinstance(data, list):
        raise LLMExtractionError("LLM response was valid JSON but not a list of records")

    records = []
    for item in data:
        if not isinstance(item, dict):
            continue
        record = {}
        for field in LLM_FIELDS:
            v = item.get(field, "")
            record[field] = "" if v is None else str(v)
        records.append(record)
    return records
