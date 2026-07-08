"""Orchestrates extraction for a batch of uploaded files: read -> try rules
-> fall back to LLM -> normalize. Never raises for a single bad file — each
file gets its own result entry so one failure doesn't sink the batch.
"""
from .file_readers import read_file
from .llm_fallback import LLMExtractionError, extract_with_llm
from .rules import try_rules
from .schema import normalize_record


def process_files(paths):
    """Returns (all_records, file_results). file_results is a list of dicts:
    {filename, status: "ok"|"error", method: "rule:<Name>"|"llm"|None,
     record_count, error}
    """
    all_records = []
    file_results = []

    for path in paths:
        filename = path.name if hasattr(path, "name") else str(path)
        result = {"filename": filename, "status": "ok", "method": None, "record_count": 0, "error": None}
        try:
            content = read_file(path)
        except ValueError as e:
            result["status"] = "error"
            result["error"] = str(e)
            file_results.append(result)
            continue
        except Exception as e:
            result["status"] = "error"
            result["error"] = f"Unexpected error reading file: {e}"
            file_results.append(result)
            continue

        rule_name, raw_records = try_rules(content)
        if raw_records:
            result["method"] = f"rule:{rule_name}"
        else:
            try:
                raw_records = extract_with_llm(content["text"], source_hint=filename)
                result["method"] = "llm"
            except LLMExtractionError as e:
                result["status"] = "error"
                result["error"] = str(e)
                file_results.append(result)
                continue
            except Exception as e:
                result["status"] = "error"
                result["error"] = f"Unexpected error during LLM extraction: {e}"
                file_results.append(result)
                continue

        normalized = [normalize_record(r) for r in raw_records]
        normalized = [r for r in normalized if r.get("Building") or r.get("Area")]
        if not normalized:
            result["status"] = "error"
            result["error"] = "No usable records found in this file"
            file_results.append(result)
            continue

        result["record_count"] = len(normalized)
        all_records.extend(normalized)
        file_results.append(result)

    return all_records, file_results
