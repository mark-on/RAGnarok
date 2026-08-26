from __future__ import annotations

import csv
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .results.schemas import UniversalCase
from .taxonomy import BENCHMARK_COVERAGE, KNOWN_GAPS


UNKNOWN = "Not recorded"
SUCCESS_GREEN = "#176B57"
FAILURE_RED = "#C53B3B"
NAVY = "#17324D"
LIGHT_BLUE = "#EAF2F8"


def validate_report_dependency() -> None:
    try:
        import xlsxwriter  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "XLSX reporting dependency is missing; run 'ragnarok setup' before starting an evaluation"
        ) from exc


def _label(value: object) -> str:
    text = str(value or "").strip()
    aliases = {"nq": "NQ", "hotpotqa": "HotpotQA", "msmarco": "MS MARCO", "mpib": "MPIB"}
    if text.lower() in aliases:
        return aliases[text.lower()]
    return text.replace("_", " ").replace("-", " ").strip().title() or UNKNOWN


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _attack_values(cases: Iterable[UniversalCase]) -> list[bool]:
    values: list[bool] = []
    for case in cases:
        evaluation = case.official_evaluation
        if not evaluation.get("is_attack_case", True):
            continue
        value = _as_bool(evaluation.get("attack_success"))
        if value is not None:
            values.append(value)
    return values


def _wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    rate = successes / total
    denominator = 1 + z * z / total
    centre = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((rate * (1 - rate) + z * z / (4 * total)) / total) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _rate_summary(cases: list[UniversalCase]) -> dict[str, object]:
    values = _attack_values(cases)
    successes = sum(values)
    evaluated = len(values)
    rate = successes / evaluated if evaluated else None
    ci_low, ci_high = _wilson_interval(successes, evaluated)
    return {
        "cases": len(cases),
        "attack_cases": sum(bool(case.official_evaluation.get("is_attack_case", True)) for case in cases),
        "evaluated": evaluated,
        "unevaluable": sum(
            bool(case.official_evaluation.get("is_attack_case", True))
            and _as_bool(case.official_evaluation.get("attack_success")) is None
            for case in cases
        ),
        "successes": successes,
        "failures": evaluated - successes,
        "runtime_errors": sum(bool(case.error) for case in cases),
        "attack_success_rate": rate,
        "resistance_rate": None if rate is None else 1 - rate,
        "ci95_low": ci_low,
        "ci95_high": ci_high,
    }


def _native_result(case: UniversalCase) -> dict[str, object]:
    native = case.official_evaluation.get("native_result")
    return native if isinstance(native, dict) else {}


def _spikee_objective(instruction_type: str) -> tuple[str, str]:
    normalized = instruction_type.lower()
    if "exfil" in normalized or "leak" in normalized or "extract" in normalized:
        return "Data exfiltration", "Confidentiality"
    if "system" in normalized and "prompt" in normalized:
        return "System prompt leakage", "Confidentiality"
    if "xss" in normalized or "markup" in normalized:
        return "Cross-site scripting", "Integrity"
    if "resource" in normalized or "denial" in normalized or "dos" in normalized:
        return "Resource exhaustion", "Availability"
    return "Instruction override", "Integrity"


def _case_taxonomy(case: UniversalCase) -> dict[str, str]:
    coverage = BENCHMARK_COVERAGE.get(case.benchmark_id, {})
    native = _native_result(case)
    family = case.attack_family
    track = str(coverage.get("track", "Unclassified"))
    source = "RAGnarok mapping"

    if case.benchmark_id == "spikee":
        track = "Direct"
        instruction_type = str(native.get("instruction_type") or family)
        objective, cia = _spikee_objective(instruction_type)
        injected = str(native.get("injected", "")).lower() == "true"
        entry_vector = "Adversarial application input" if not injected else "Injected application content"
        stage = "Application input"
        technique = _label(str(native.get("jailbreak_type") or instruction_type))
        source = "Official SPIKEE metadata + RAGnarok mapping"
    elif family == "knowledge_corruption":
        track = "Indirect (RAG)"
        entry_vector, stage, objective, technique, cia = (
            "Knowledge-base poisoning", "Retrieved context", "Knowledge corruption",
            "Adversarial retrieval targeting", "Integrity",
        )
    elif family == "direct_prompt_injection":
        track = "Direct"
        entry_vector, stage, objective, technique, cia = (
            "Direct prompt injection", "User query", "Instruction override",
            "Instruction hierarchy override", "Integrity",
        )
    elif family == "indirect_prompt_injection":
        track = "Indirect (RAG)"
        entry_vector, stage, objective, technique, cia = (
            "Indirect injection through retrieved documents", "Retrieved context",
            "Instruction override", "Injected untrusted content", "Integrity",
        )
    elif family == "agentic_indirect_prompt_injection":
        track = "Agentic"
        entry_vector, stage, objective, technique, cia = (
            "Indirect injection through untrusted tool data", "Tool output",
            "Unauthorized action", "Tool-content injection", "Integrity",
        )
    elif family == "benign_control":
        track = "Benign control"
        entry_vector, stage, objective, technique, cia = (
            "Benign control", "User query", "Utility preservation", "No attack", "Not applicable",
        )
    else:
        entry_vectors = coverage.get("entry_vectors", [])
        stages = coverage.get("pipeline_stages", [])
        objectives = coverage.get("objectives", [])
        techniques = coverage.get("techniques", [])
        cia_values = coverage.get("cia", [])
        entry_vector = str(entry_vectors[0]) if entry_vectors else UNKNOWN
        stage = str(stages[0]) if stages else UNKNOWN
        objective = str(objectives[0]) if objectives else UNKNOWN
        technique = str(techniques[0]) if techniques else UNKNOWN
        cia = str(cia_values[0]) if cia_values else UNKNOWN
        source = "Benchmark-level fallback"

    return {
        "evaluation_track": track,
        "entry_vector": entry_vector,
        "pipeline_stage": stage,
        "security_objective": objective,
        "technique": technique,
        "cia_property": cia,
        "taxonomy_source": source,
    }


def _quantization(value: str) -> str:
    lowered = value.lower()
    patterns = (
        (r"(?:^|[-_:])(?:bf16|bfloat16)(?:$|[-_:])", "BF16"),
        (r"(?:^|[-_:])(?:fp16|f16)(?:$|[-_:])", "FP16"),
        (r"(?:^|[-_:])q8(?:_|-|$)", "Q8"),
        (r"(?:^|[-_:])q6(?:_|-|$)", "Q6"),
        (r"(?:^|[-_:])q5(?:_|-|$)", "Q5"),
        (r"(?:^|[-_:])q4(?:_|-|$)", "Q4"),
        (r"(?:^|[-_:])q3(?:_|-|$)", "Q3"),
        (r"(?:^|[-_:])q2(?:_|-|$)", "Q2"),
    )
    return next((label for pattern, label in patterns if re.search(pattern, lowered)), "Unspecified")


def _model_metadata(configuration: dict[str, object] | None) -> dict[str, dict[str, object]]:
    if not configuration:
        return {}
    models = configuration.get("models", [])
    if not isinstance(models, list):
        return {}
    return {
        str(item.get("id")): item
        for item in models
        if isinstance(item, dict) and item.get("id")
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _performance_rows(model_calls: list[dict]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for call in model_calls:
        groups[(
            str(call.get("model_id", UNKNOWN)), str(call.get("benchmark_id", UNKNOWN)),
            str(call.get("model_role", UNKNOWN)), str(call.get("provider_model", UNKNOWN)),
        )].append(call)
    rows = []
    for (model_id, benchmark, role, provider_model), calls in sorted(groups.items()):
        durations = [float(item["wall_duration_seconds"]) for item in calls if item.get("wall_duration_seconds") is not None]
        input_tokens = sum(int(item.get("input_tokens") or 0) for item in calls)
        output_tokens = sum(int(item.get("output_tokens") or 0) for item in calls)
        total_seconds = sum(durations)
        rows.append({
            "model_id": model_id,
            "benchmark": benchmark,
            "role": role,
            "provider_model": provider_model,
            "calls": len(calls),
            "errors": sum(bool(item.get("error_type")) for item in calls),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_call_seconds": total_seconds,
            "mean_latency_seconds": total_seconds / len(durations) if durations else None,
            "median_latency_seconds": median(durations) if durations else None,
            "p95_latency_seconds": _percentile(durations, 0.95),
            "output_tokens_per_second": output_tokens / total_seconds if output_tokens and total_seconds else None,
        })
    return rows


def _taxonomy_rows(cases: list[UniversalCase]) -> list[dict[str, object]]:
    axes = {
        "Evaluation track": "evaluation_track",
        "Entry vector": "entry_vector",
        "Pipeline stage": "pipeline_stage",
        "Security objective": "security_objective",
        "Technique": "technique",
        "CIA property": "cia_property",
        "Attack family": "attack_family",
        "Dataset": "dataset",
    }
    groups: dict[tuple[str, str, str, str], list[UniversalCase]] = defaultdict(list)
    for case in cases:
        dimensions = _case_taxonomy(case)
        dimensions["attack_family"] = _label(case.attack_family)
        dimensions["dataset"] = _label(case.official_evaluation.get("dataset", UNKNOWN))
        for axis, key in axes.items():
            groups[(case.model_id, axis, str(dimensions[key]), case.benchmark_id)].append(case)
    rows = []
    for (model_id, axis, category, benchmark), group in sorted(groups.items()):
        rows.append({
            "model_id": model_id,
            "axis": axis,
            "category": category,
            "benchmark": benchmark,
            **_rate_summary(group),
        })
    return rows


def _retrieval_rows(cases: list[UniversalCase]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str, int, int], list[UniversalCase]] = defaultdict(list)
    for case in cases:
        evaluation = case.official_evaluation
        if "poisoned_contexts_in_top_k" not in evaluation:
            continue
        groups[(
            case.model_id,
            str(evaluation.get("dataset", UNKNOWN)),
            int(evaluation.get("poisoned_contexts_in_top_k") or 0),
            int(evaluation.get("top_k") or 0),
        )].append(case)
    rows = []
    for (model_id, dataset, poisoned, top_k), group in sorted(groups.items()):
        rows.append({
            "model_id": model_id,
            "dataset": dataset,
            "poisoned_contexts_in_top_k": poisoned,
            "top_k": top_k,
            "retrieval_poison_rate": poisoned / top_k if top_k else None,
            **_rate_summary(group),
        })
    return rows


def _comparison_rows(cases: list[UniversalCase], model_meta: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, dict[tuple[str, str], bool]] = defaultdict(dict)
    for case in cases:
        success = _as_bool(case.official_evaluation.get("attack_success"))
        if success is not None and case.official_evaluation.get("is_attack_case", True):
            grouped[case.model_id][(case.benchmark_id, case.case_id)] = success
    if len(grouped) < 2:
        return []
    model_ids = sorted(grouped)
    baseline = next(
        (model_id for model_id in model_ids if _quantization(str(model_meta.get(model_id, {}).get("model", model_id))) in {"BF16", "FP16"}),
        model_ids[0],
    )
    rows = []
    for model_id in model_ids:
        if model_id == baseline:
            continue
        shared = sorted(set(grouped[baseline]) & set(grouped[model_id]))
        both_success = sum(grouped[baseline][key] and grouped[model_id][key] for key in shared)
        both_resist = sum(not grouped[baseline][key] and not grouped[model_id][key] for key in shared)
        quant_only = sum(not grouped[baseline][key] and grouped[model_id][key] for key in shared)
        baseline_only = sum(grouped[baseline][key] and not grouped[model_id][key] for key in shared)
        baseline_asr = sum(grouped[baseline][key] for key in shared) / len(shared) if shared else None
        model_asr = sum(grouped[model_id][key] for key in shared) / len(shared) if shared else None
        rows.append({
            "baseline_model": baseline,
            "compared_model": model_id,
            "paired_cases": len(shared),
            "both_success": both_success,
            "both_resistant": both_resist,
            "success_only_after_quantization": quant_only,
            "success_only_in_baseline": baseline_only,
            "baseline_asr": baseline_asr,
            "compared_asr": model_asr,
            "asr_delta": None if baseline_asr is None or model_asr is None else model_asr - baseline_asr,
        })
    return rows


def _flatten(value: object, prefix: str = "") -> list[tuple[str, object]]:
    if isinstance(value, dict):
        rows: list[tuple[str, object]] = []
        for key, item in sorted(value.items()):
            path = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten(item, path))
        return rows
    if isinstance(value, list):
        return [(prefix, json.dumps(value, ensure_ascii=False))]
    return [(prefix, value)]


def _csv_value(value: object) -> object:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value


def _case_record(case: UniversalCase) -> dict[str, object]:
    taxonomy = _case_taxonomy(case)
    evaluation = case.official_evaluation
    native = _native_result(case)
    return {
        "suite_id": case.suite_id,
        "model_id": case.model_id,
        "benchmark_id": case.benchmark_id,
        "dataset": evaluation.get("dataset", ""),
        "case_id": case.case_id,
        "evaluation_track": taxonomy["evaluation_track"],
        "entry_vector": taxonomy["entry_vector"],
        "pipeline_stage": taxonomy["pipeline_stage"],
        "security_objective": taxonomy["security_objective"],
        "technique": taxonomy["technique"],
        "cia_property": taxonomy["cia_property"],
        "taxonomy_source": taxonomy["taxonomy_source"],
        "attack_family": case.attack_family,
        "is_attack_case": evaluation.get("is_attack_case", True),
        "attack_success": evaluation.get("attack_success"),
        "severity": evaluation.get("severity"),
        "clinical_harm_event": evaluation.get("clinical_harm_event"),
        "judge_valid": evaluation.get("judge_valid"),
        "utility": evaluation.get("utility"),
        "security": evaluation.get("security"),
        "poisoned_contexts_in_top_k": evaluation.get("poisoned_contexts_in_top_k"),
        "top_k": evaluation.get("top_k"),
        "profile": evaluation.get("profile"),
        "scenario": evaluation.get("scenario"),
        "vector": evaluation.get("vector"),
        "task_type": native.get("task_type"),
        "instruction_type": native.get("instruction_type"),
        "jailbreak_type": native.get("jailbreak_type"),
        "judge_name": native.get("judge_name"),
        "prompt": case.prompt,
        "target": case.target,
        "payload": case.payload,
        "response": case.response,
        "reference_answer": case.reference_answer,
        "adversarial_answer": case.adversarial_answer,
        "retrieved_contexts": case.retrieved_contexts,
        "injected_contexts": case.injected_contexts,
        "official_evaluation": evaluation,
        "error": case.error,
    }


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: _csv_value(row.get(key)) for key in fields} for row in rows)


def _write_model_metrics(path: Path, official_metrics: list[dict], model_id: str) -> None:
    metrics = {
        row["benchmark_id"]: row["metrics"]
        for row in official_metrics
        if row.get("model_id") == model_id
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _worksheet_formats(workbook: xlsxwriter.Workbook) -> dict[str, Any]:
    return {
        "title": workbook.add_format({"bold": True, "font_size": 18, "font_color": NAVY}),
        "subtitle": workbook.add_format({"font_size": 10, "font_color": "#667085"}),
        "section": workbook.add_format({"bold": True, "font_size": 12, "font_color": "#FFFFFF", "bg_color": SUCCESS_GREEN}),
        "header": workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": NAVY, "text_wrap": True, "valign": "vcenter"}),
        "label": workbook.add_format({"bold": True, "font_color": NAVY, "bg_color": LIGHT_BLUE}),
        "text": workbook.add_format({"font_color": "#344054", "valign": "top"}),
        "wrapped": workbook.add_format({"font_color": "#344054", "text_wrap": True, "valign": "top"}),
        "integer": workbook.add_format({"num_format": "#,##0", "font_color": "#344054"}),
        "decimal": workbook.add_format({"num_format": "0.00", "font_color": "#344054"}),
        "percent": workbook.add_format({"num_format": "0.00%", "font_color": "#344054"}),
        "note": workbook.add_format({"font_color": "#667085", "italic": True, "text_wrap": True}),
    }


def _write_value(sheet, row: int, col: int, value: object, formats: dict[str, Any], *, wrapped: bool = False) -> None:
    if value is None:
        sheet.write_blank(row, col, None, formats["text"])
    elif isinstance(value, bool):
        sheet.write_boolean(row, col, value, formats["text"])
    elif isinstance(value, int):
        sheet.write_number(row, col, value, formats["integer"])
    elif isinstance(value, float):
        sheet.write_number(row, col, value, formats["decimal"])
    else:
        sheet.write_string(row, col, str(value), formats["wrapped"] if wrapped else formats["text"])


def _write_table(
    sheet,
    start_row: int,
    start_col: int,
    rows: list[dict[str, object]],
    formats: dict[str, Any],
    *,
    table_name: str,
    percent_columns: set[str] | None = None,
    wrapped_columns: set[str] | None = None,
) -> tuple[int, int]:
    if not rows:
        sheet.write(start_row, start_col, "No data available", formats["note"])
        return start_row, start_col
    percent_columns = percent_columns or set()
    wrapped_columns = wrapped_columns or set()
    fields = list(rows[0])
    for col, field in enumerate(fields, start=start_col):
        sheet.write(start_row, col, _label(field), formats["header"])
    sheet.set_row(start_row, 30)
    for row_index, item in enumerate(rows, start=start_row + 1):
        for col_index, field in enumerate(fields, start=start_col):
            value = item.get(field)
            if field in percent_columns and isinstance(value, (int, float)):
                sheet.write_number(row_index, col_index, float(value), formats["percent"])
            else:
                _write_value(sheet, row_index, col_index, value, formats, wrapped=field in wrapped_columns)
    sheet.add_table(
        start_row,
        start_col,
        start_row + len(rows),
        start_col + len(fields) - 1,
        {"name": table_name, "style": "Table Style Medium 2", "columns": [{"header": _label(field)} for field in fields]},
    )
    sheet.freeze_panes(start_row + 1, start_col)
    return start_row + len(rows), start_col + len(fields) - 1


def _configure_sheet(sheet, widths: dict[int, float] | None = None) -> None:
    sheet.hide_gridlines(2)
    sheet.set_default_row(18)
    for column, width in (widths or {}).items():
        sheet.set_column(column, column, width)


def _build_workbook(
    path: Path,
    *,
    cases: list[UniversalCase],
    model_calls: list[dict],
    official_metrics: list[dict],
    suite_status: str,
    execution: dict[str, object],
    configuration: dict[str, object] | None,
    summaries: dict[str, dict[str, object]],
    taxonomy_rows: list[dict[str, object]],
    retrieval_rows: list[dict[str, object]],
    comparison_rows: list[dict[str, object]],
    performance_rows: list[dict[str, object]],
    case_rows: list[dict[str, object]],
) -> None:
    validate_report_dependency()
    import xlsxwriter

    del model_calls
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(path)
    workbook.set_properties({
        "title": "RAGnarok security evaluation report",
        "subject": "RAG security and quantization evaluation",
        "author": "RAGnarok",
        "comments": "Generated from normalized benchmark results; native artifacts remain authoritative.",
    })
    formats = _worksheet_formats(workbook)
    model_meta = _model_metadata(configuration)

    overview = workbook.add_worksheet("Overview")
    _configure_sheet(overview, {0: 30, 1: 36, 2: 16, 3: 26, 4: 16})
    overview.write("A1", "RAGnarok security evaluation", formats["title"])
    overview.write("A2", "Scientific summary for security analysis across models and quantizations", formats["subtitle"])
    overview.write("A4", "Execution metadata", formats["section"])
    metadata_rows = [
        ("Run ID", execution.get("suite_id")), ("Status", suite_status),
        ("Started at (local)", execution.get("started_at_local")),
        ("Started at (UTC)", execution.get("started_at_utc")),
        ("Evaluation completed at (local)", execution.get("completed_at_local")),
        ("Evaluation completed at (UTC)", execution.get("completed_at_utc")),
        ("Elapsed seconds", execution.get("elapsed_seconds")),
        ("Local timezone", execution.get("local_timezone")), ("UTC offset", execution.get("utc_offset")),
    ]
    for index, (label, value) in enumerate(metadata_rows, start=4):
        overview.write(index, 0, label, formats["label"])
        _write_value(overview, index, 1, value, formats, wrapped=True)
    overview.write("D4", "Overall results", formats["section"])
    total = _rate_summary(cases)
    result_rows = [
        ("Models", len(summaries)), ("Benchmarks", len({case.benchmark_id for case in cases})),
        ("Cases", total["cases"]), ("Evaluated attacks", total["evaluated"]),
        ("Attack successes", total["successes"]), ("Runtime errors", total["runtime_errors"]),
        ("Overall ASR", total["attack_success_rate"]), ("Resistance rate", total["resistance_rate"]),
        ("95% CI low", total["ci95_low"]), ("95% CI high", total["ci95_high"]),
    ]
    percent_labels = {"Overall ASR", "Resistance rate", "95% CI low", "95% CI high"}
    for index, (label, value) in enumerate(result_rows, start=4):
        overview.write(index, 3, label, formats["label"])
        if label in percent_labels and isinstance(value, (int, float)):
            overview.write_number(index, 4, float(value), formats["percent"])
        else:
            _write_value(overview, index, 4, value, formats)
    overview.write("A16", "Interpretation", formats["section"])
    overview.merge_range(
        "A17:E19",
        "ASR is the fraction of valid attack cases classified as successful by each benchmark's native evaluator. "
        "Lower ASR indicates stronger resistance. Native benchmark definitions remain distinct; use the Taxonomy sheet "
        "for stratified analysis and the Cases sheet for auditing individual observations.",
        formats["note"],
    )
    if summaries:
        chart_data_row = 21
        overview.write_row(chart_data_row, 0, ["Model", "ASR", "Resistance"], formats["header"])
        for offset, (model_id, summary) in enumerate(sorted(summaries.items()), start=1):
            overview.write(chart_data_row + offset, 0, model_id, formats["text"])
            overview.write_number(chart_data_row + offset, 1, float(summary.get("attack_success_rate") or 0), formats["percent"])
            overview.write_number(chart_data_row + offset, 2, float(summary.get("resistance_rate") or 0), formats["percent"])
        chart = workbook.add_chart({"type": "column"})
        count = len(summaries)
        chart.add_series({
            "name": "Attack success rate", "categories": ["Overview", chart_data_row + 1, 0, chart_data_row + count, 0],
            "values": ["Overview", chart_data_row + 1, 1, chart_data_row + count, 1], "fill": {"color": FAILURE_RED},
        })
        chart.add_series({
            "name": "Resistance rate", "categories": ["Overview", chart_data_row + 1, 0, chart_data_row + count, 0],
            "values": ["Overview", chart_data_row + 1, 2, chart_data_row + count, 2], "fill": {"color": SUCCESS_GREEN},
        })
        chart.set_title({"name": "Security outcome by model"})
        chart.set_y_axis({"name": "Rate", "num_format": "0%", "min": 0, "max": 1})
        chart.set_legend({"position": "bottom"})
        chart.set_style(10)
        overview.insert_chart("G4", chart, {"x_scale": 1.25, "y_scale": 1.2})

    models_sheet = workbook.add_worksheet("Models")
    _configure_sheet(models_sheet)
    model_rows = []
    performance_by_model: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in performance_rows:
        if row["role"] == "subject":
            performance_by_model[str(row["model_id"])].append(row)
    for model_id, summary in sorted(summaries.items()):
        metadata = model_meta.get(model_id, {})
        subject_rows = performance_by_model.get(model_id, [])
        call_seconds = sum(float(row.get("total_call_seconds") or 0) for row in subject_rows)
        output_tokens = sum(int(row.get("output_tokens") or 0) for row in subject_rows)
        model_name = str(metadata.get("model", model_id))
        model_rows.append({
            "model_id": model_id, "provider_model": model_name, "adapter": metadata.get("adapter", UNKNOWN),
            "quantization": _quantization(model_name), **summary,
            "subject_calls": sum(int(row.get("calls") or 0) for row in subject_rows),
            "subject_output_tokens": output_tokens, "subject_call_seconds": call_seconds,
            "subject_tokens_per_second": output_tokens / call_seconds if output_tokens and call_seconds else None,
        })
    _write_table(
        models_sheet, 0, 0, model_rows, formats, table_name="ModelsTable",
        percent_columns={"attack_success_rate", "resistance_rate", "ci95_low", "ci95_high"},
    )
    models_sheet.set_column(0, 3, 22)
    models_sheet.set_column(4, len(model_rows[0]) - 1 if model_rows else 12, 20)

    taxonomy_sheet = workbook.add_worksheet("Taxonomy")
    _configure_sheet(taxonomy_sheet)
    _write_table(
        taxonomy_sheet, 0, 0, taxonomy_rows, formats, table_name="TaxonomyResults",
        percent_columns={"attack_success_rate", "resistance_rate", "ci95_low", "ci95_high"},
    )
    taxonomy_sheet.set_column(0, 3, 24)
    taxonomy_sheet.set_column(4, 15, 15)

    comparison_sheet = workbook.add_worksheet("Quantization")
    _configure_sheet(comparison_sheet)
    _write_table(
        comparison_sheet, 0, 0, comparison_rows, formats, table_name="QuantizationComparison",
        percent_columns={"baseline_asr", "compared_asr", "asr_delta"},
    )
    comparison_sheet.set_column(0, 1, 24)
    comparison_sheet.set_column(2, 10, 24)
    note_row = max(len(comparison_rows) + 3, 3)
    comparison_sheet.merge_range(
        note_row, 0, note_row + 1, 9,
        "Paired comparisons use only cases present and valid in both models. Positive ASR delta means the compared model was less secure.",
        formats["note"],
    )

    retrieval_sheet = workbook.add_worksheet("Retrieval Security")
    _configure_sheet(retrieval_sheet)
    _write_table(
        retrieval_sheet, 0, 0, retrieval_rows, formats, table_name="RetrievalSecurity",
        percent_columns={"retrieval_poison_rate", "attack_success_rate", "resistance_rate", "ci95_low", "ci95_high"},
    )
    retrieval_sheet.set_column(0, 1, 22)
    retrieval_sheet.set_column(2, 15, 18)

    judge_sheet = workbook.add_worksheet("Judge Audit")
    _configure_sheet(judge_sheet)
    judge_rows = [row for row in performance_rows if row["role"] == "judge"]
    _write_table(judge_sheet, 0, 0, judge_rows, formats, table_name="JudgeAudit")
    judge_sheet.set_column(0, 3, 24)
    judge_sheet.set_column(4, 13, 18)

    performance_sheet = workbook.add_worksheet("Performance")
    _configure_sheet(performance_sheet)
    _write_table(performance_sheet, 0, 0, performance_rows, formats, table_name="PerformanceTable")
    performance_sheet.set_column(0, 3, 24)
    performance_sheet.set_column(4, 13, 18)

    cases_sheet = workbook.add_worksheet("Cases")
    _configure_sheet(cases_sheet)
    _write_table(
        cases_sheet, 0, 0, case_rows, formats, table_name="CasesTable",
        wrapped_columns={
            "prompt", "target", "payload", "response", "reference_answer", "adversarial_answer",
            "retrieved_contexts", "injected_contexts", "official_evaluation", "error",
        },
    )
    cases_sheet.set_column(0, 16, 20)
    cases_sheet.set_column(17, len(case_rows[0]) - 1 if case_rows else 35, 35)

    native_sheet = workbook.add_worksheet("Native Metrics")
    _configure_sheet(native_sheet, {0: 22, 1: 22, 2: 18, 3: 45})
    native_rows = []
    for metric in official_metrics:
        for key, value in _flatten(metric.get("metrics", {})):
            native_rows.append({
                "model_id": metric.get("model_id"), "benchmark_id": metric.get("benchmark_id"),
                "metric": key, "value": value,
            })
    _write_table(native_sheet, 0, 0, native_rows, formats, table_name="NativeMetrics", wrapped_columns={"value"})

    coverage_sheet = workbook.add_worksheet("Taxonomy Coverage")
    _configure_sheet(coverage_sheet)
    coverage_rows = []
    for benchmark_id in sorted({case.benchmark_id for case in cases}):
        coverage = BENCHMARK_COVERAGE.get(benchmark_id, {})
        coverage_rows.append({
            "benchmark": benchmark_id, "track": coverage.get("track", UNKNOWN),
            "entry_vectors": "; ".join(coverage.get("entry_vectors", [])),
            "pipeline_stages": "; ".join(coverage.get("pipeline_stages", [])),
            "objectives": "; ".join(coverage.get("objectives", [])),
            "techniques": "; ".join(coverage.get("techniques", [])),
            "cia": "; ".join(coverage.get("cia", [])),
            "adapter_qualification": coverage.get("adapter", UNKNOWN), "provenance": coverage.get("provenance", UNKNOWN),
        })
    _write_table(
        coverage_sheet, 0, 0, coverage_rows, formats, table_name="TaxonomyCoverage",
        wrapped_columns={"entry_vectors", "pipeline_stages", "objectives", "techniques", "adapter_qualification", "provenance"},
    )
    coverage_sheet.set_column(0, 1, 22)
    coverage_sheet.set_column(2, 8, 35)
    for row_index in range(1, len(coverage_rows) + 1):
        coverage_sheet.set_row(row_index, 48)
    gap_row = len(coverage_rows) + 3
    coverage_sheet.write(gap_row, 0, "Known coverage gaps", formats["section"])
    for offset, gap in enumerate(KNOWN_GAPS, start=1):
        coverage_sheet.write(gap_row + offset, 0, gap, formats["text"])

    metadata_sheet = workbook.add_worksheet("Run Metadata")
    _configure_sheet(metadata_sheet, {0: 45, 1: 70})
    configuration_rows = [
        {"field": key, "value": _csv_value(value)}
        for key, value in _flatten({"execution": execution, "configuration": configuration or {}})
    ]
    _write_table(metadata_sheet, 0, 0, configuration_rows, formats, table_name="RunMetadata", wrapped_columns={"value"})
    workbook.close()


def _write_result_guide(path: Path, *, group: bool) -> None:
    model_note = "- models/<model-id>/: filtered cases.csv and native metrics.json\n" if group else ""
    path.write_text(
        "RAGnarok result folder\n\nStart here:\n"
        "- report.xlsx: scientific report with execution metadata, taxonomy, paired quantization analysis, and raw cases\n"
        "- summary.csv: compact model-level metrics\n- cases.csv: flat case-level analysis dataset\n"
        "- report.json: machine-readable report aggregates\n"
        f"{model_note}\nAudit data:\n- results.sqlite: canonical result database\n"
        "- data/: lossless normalized JSONL exports\n- artifacts/: native benchmark outputs and request logs\n"
        "- suite_manifest.json: exact run configuration, timing, provenance, and job status\n",
        encoding="utf-8",
    )


def generate_reports(
    result_dir: Path,
    cases: list[UniversalCase],
    *,
    official_metrics: list[dict] | None = None,
    model_calls: list[dict] | None = None,
    suite_status: str = "complete",
    execution: dict[str, object] | None = None,
    configuration: dict[str, object] | None = None,
    postprocess_workers: int = 0,
) -> list[Path]:
    """Create one analysis-first XLSX report plus auditable flat exports."""

    del postprocess_workers  # XLSX generation is intentionally single-writer and deterministic.
    official_metrics = official_metrics or []
    model_calls = model_calls or []
    now = datetime.now(timezone.utc).isoformat()
    local_now = datetime.now().astimezone()
    execution = {
        "suite_id": cases[0].suite_id if cases else result_dir.name,
        "started_at_utc": now, "started_at_local": local_now.isoformat(),
        "completed_at_utc": now, "completed_at_local": local_now.isoformat(), "elapsed_seconds": 0.0,
        "local_timezone": local_now.tzname(),
        "utc_offset": f"{local_now.strftime('%z')[:3]}:{local_now.strftime('%z')[3:]}",
        **(execution or {}),
    }
    grouped: dict[str, list[UniversalCase]] = defaultdict(list)
    for case in cases:
        grouped[case.model_id].append(case)
    summaries = {model_id: _rate_summary(model_cases) for model_id, model_cases in sorted(grouped.items())}
    model_meta = _model_metadata(configuration)
    taxonomy_rows = _taxonomy_rows(cases)
    retrieval_rows = _retrieval_rows(cases)
    comparison_rows = _comparison_rows(cases, model_meta)
    performance_rows = _performance_rows(model_calls)
    case_rows = [_case_record(case) for case in cases]
    report_data = {
        "generated_at": now, "suite_status": suite_status, "execution": execution, "models": summaries,
        "taxonomy": taxonomy_rows, "quantization_comparison": comparison_rows,
        "retrieval_security": retrieval_rows, "performance": performance_rows,
        "known_coverage_gaps": KNOWN_GAPS,
    }
    result_dir.mkdir(parents=True, exist_ok=True)
    report_path = result_dir / "report.xlsx"
    _build_workbook(
        report_path, cases=cases, model_calls=model_calls, official_metrics=official_metrics,
        suite_status=suite_status, execution=execution, configuration=configuration, summaries=summaries,
        taxonomy_rows=taxonomy_rows, retrieval_rows=retrieval_rows, comparison_rows=comparison_rows,
        performance_rows=performance_rows, case_rows=case_rows,
    )
    _write_csv(result_dir / "cases.csv", case_rows)
    _write_csv(result_dir / "summary.csv", [{"model_id": model_id, **summary} for model_id, summary in summaries.items()])
    (result_dir / "report.json").write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    if len(grouped) > 1:
        for model_id, model_cases in sorted(grouped.items()):
            model_dir = result_dir / "models" / model_id
            _write_csv(model_dir / "cases.csv", [_case_record(case) for case in model_cases])
            _write_model_metrics(model_dir / "metrics.json", official_metrics, model_id)
    elif grouped:
        _write_model_metrics(result_dir / "metrics.json", official_metrics, next(iter(grouped)))
    _write_result_guide(result_dir / "README.txt", group=len(grouped) > 1)
    return [report_path]
