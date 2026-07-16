from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib import colors


COMPARISON_FIELDS = ["direct_asr", "indirect_end_to_end_asr", "conditional_indirect_asr", "benign_task_success_rate", "false_refusal_rate", "expected_source_retrieval_rate", "mean_latency_ms", "input_tokens", "output_tokens"]


def generate_comparison(experiment_dir: Path, summaries: dict[str, dict], frames: dict[str, pd.DataFrame], generate_pdf: bool = True) -> Path:
    output = experiment_dir; output.mkdir(parents=True, exist_ok=True)
    rows = [{"model_id": model_id, **{field: metrics.get(field) for field in COMPARISON_FIELDS}} for model_id, metrics in summaries.items()]
    comparison = pd.DataFrame(rows); comparison.to_csv(output / "comparison.csv", index=False)
    labels_by_case = {model: frame.set_index("case_id").final_label.to_dict() for model, frame in frames.items()}
    common_cases = sorted(set.intersection(*(set(values) for values in labels_by_case.values()))) if labels_by_case else []
    disagreement = [case for case in common_cases if len({labels_by_case[model][case] for model in labels_by_case}) > 1]
    retrieval_differences = []
    if frames:
        first_model = next(iter(frames)); baseline = frames[first_model].set_index("case_id").expected_source_rank.to_dict()
        for model, frame in frames.items():
            ranks = frame.set_index("case_id").expected_source_rank.to_dict()
            retrieval_differences.extend(case for case in common_cases if ranks.get(case) != baseline.get(case))
    payload = {"models": rows, "case_level_disagreements": disagreement, "retrieval_difference_cases": sorted(set(retrieval_differences)), "compromised_by_every_model": [case for case in common_cases if all(labels_by_case[m][case] == "compromised" for m in labels_by_case)], "resisted_by_every_model": [case for case in common_cases if all(labels_by_case[m][case] == "resisted" for m in labels_by_case)]}
    (output / "comparison.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    header = " | ".join(comparison.columns)
    table_lines = [header, " | ".join("---" for _ in comparison.columns)] + [" | ".join(str(value) for value in row) for row in comparison.itertuples(index=False, name=None)]
    lines = ["# RAGnarok cross-model comparison", "", *table_lines, "", f"Case-level label disagreements: {len(disagreement)}", f"Unexpected retrieval differences: {len(set(retrieval_differences))}", "", "Full attack payloads and protected targets are omitted."]
    (output / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    if generate_pdf:
        styles = getSampleStyleSheet(); data = [["Model", "Direct ASR", "Indirect ASR", "Benign utility", "Latency ms"]]
        for row in rows:
            fmt = lambda value: "N/A" if value is None else f"{value:.1%}" if isinstance(value, float) and 0 <= value <= 1 else f"{value:.1f}"
            data.append([row["model_id"], fmt(row["direct_asr"]), fmt(row["indirect_end_to_end_asr"]), fmt(row["benign_task_success_rate"]), fmt(row["mean_latency_ms"])])
        table = Table(data); table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#17324D")), ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("GRID", (0,0), (-1,-1), .4, colors.grey), ("PADDING", (0,0), (-1,-1), 5)]))
        SimpleDocTemplate(str(output / "comparison.pdf"), pagesize=A4).build([Paragraph("RAGnarok Cross-Model Comparison", styles["Title"]), Spacer(1, 18), table, Spacer(1, 18), Paragraph(f"Case-level disagreements: {len(disagreement)}. Unexpected retrieval differences: {len(set(retrieval_differences))}.", styles["BodyText"]), Spacer(1, 12), Paragraph("Evaluator targets and decoded hidden payloads are excluded from this report.", styles["BodyText"])])
    return output
