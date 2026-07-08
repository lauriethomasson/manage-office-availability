"""Orchestrates extraction for a batch of uploaded files: read -> try rules
-> fall back to LLM -> normalize. Never raises for a single bad file — each
file gets its own result entry so one failure doesn't sink the batch.

Unlike an earlier version of this module, each file's records stay
separate (one output spreadsheet per source file, not one combined master).
"""
from .file_readers import read_file
from .llm_fallback import LLMExtractionError, extract_with_llm
from .naming import extract_date, resolve_provider_name
from .rules import try_rules
from .schema import normalize_record


def process_files(paths):
    """Returns a list of per-file dicts:
    {filename, status: "ok"|"error", method: "rule:<Name>"|"llm"|None,
     records, record_count, error, provider_name, date}
    provider_name/date/records are only meaningful when status == "ok".
    """
    results = []

    for path in paths:
        filename = path.name if hasattr(path, "name") else str(path)
        result = {
            "filename": filename,
            "status": "ok",
            "method": None,
            "records": [],
            "record_count": 0,
            "error": None,
            "provider_name": None,
            "date": None,
        }
        try:
            content = read_file(path)
        except ValueError as e:
            result["status"] = "error"
            result["error"] = str(e)
            results.append(result)
            continue
        except Exception as e:
            result["status"] = "error"
            result["error"] = f"Unexpected error reading file: {e}"
            results.append(result)
            continue

        rule_name, raw_records = try_rules(content)
        llm_source_name = None
        if raw_records:
            result["method"] = f"rule:{rule_name}"
        else:
            try:
                raw_records, llm_source_name = extract_with_llm(content["text"], source_hint=filename)
                result["method"] = "llm"
            except LLMExtractionError as e:
                result["status"] = "error"
                result["error"] = str(e)
                results.append(result)
                continue
            except Exception as e:
                result["status"] = "error"
                result["error"] = f"Unexpected error during LLM extraction: {e}"
                results.append(result)
                continue

        normalized = [normalize_record(r) for r in raw_records]
        normalized = [r for r in normalized if r.get("Building") or r.get("Area")]
        if not normalized:
            result["status"] = "error"
            result["error"] = "No usable records found in this file"
            results.append(result)
            continue

        result["records"] = normalized
        result["record_count"] = len(normalized)
        result["provider_name"] = resolve_provider_name(rule_name, filename, llm_source_name)
        result["date"] = extract_date(content)
        results.append(result)

    return results
