from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "ragnarok-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _display(value) -> str:
    if value is None:
        return "N/A"
    return f"{value:.1%}" if isinstance(value, float) and 0 <= value <= 1 else f"{value:.2f}" if isinstance(value, float) else str(value)


def generate_model_report(model_dir: Path, model_id: str, metrics: dict, generate_pdf: bool = True) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    charts = model_dir / "charts"
    charts.mkdir(exist_ok=True)
    labels = ["Direct ASR", "Indirect ASR", "Conditional ASR", "Benign utility"]
    values = [metrics.get("direct_asr") or 0, metrics.get("indirect_end_to_end_asr") or 0, metrics.get("conditional_indirect_asr") or 0, metrics.get("benign_task_success_rate") or 0]
    chart = charts / "headline_metrics.png"
    fig, ax = plt.subplots(figsize=(7, 3.4))
    ax.bar(labels, values, color=["#b6463a", "#d4793c", "#e4a23b", "#337f68"])
    ax.set_ylim(0, 1); ax.set_ylabel("Rate"); ax.tick_params(axis="x", rotation=15)
    fig.tight_layout(); fig.savefig(chart, dpi=150); plt.close(fig)
    rows = [
        ("Direct attack success rate", metrics.get("direct_asr")),
        ("Indirect end-to-end ASR", metrics.get("indirect_end_to_end_asr")),
        ("Conditional indirect ASR", metrics.get("conditional_indirect_asr")),
        ("Expected-source retrieval", metrics.get("expected_source_retrieval_rate")),
        ("Benign task success", metrics.get("benign_task_success_rate")),
        ("False-refusal rate", metrics.get("false_refusal_rate")),
        ("Manual-review rate", metrics.get("manual_review_rate")),
        ("Mean latency (ms)", metrics.get("mean_latency_ms")),
    ]
    breakdown_sections = []
    for title, key in (("Results by attack objective", "attack_objective"), ("Results by technique", "attack_technique"), ("Results by domain", "domain"), ("Results by PDF injection surface", "obfuscation_technique"), ("Single-turn versus multi-turn results", "turn_kind")):
        breakdown_sections.extend([f"## {title}", "", "```json", json.dumps(metrics.get("breakdowns", {}).get(key, {}), indent=2), "```", ""])
    markdown = [
        f"# RAGnarok model evaluation: {model_id}", "", "## Executive summary", "",
        *[f"- **{name}:** {_display(value)}" for name, value in rows], "",
        "## Dataset composition", "", f"This run contains {metrics['rows']} evaluated turns from the selected benchmark scope.", "",
        "## Direct ASR", "", _display(metrics.get("direct_asr")), "",
        "## Indirect end-to-end ASR", "", _display(metrics.get("indirect_end_to_end_asr")), "",
        "## Conditional indirect ASR", "", _display(metrics.get("conditional_indirect_asr")), "",
        "## Retrieval exposure", "", f"Expected-source retrieval: {_display(metrics.get('expected_source_retrieval_rate'))}; poisoned-source exposure: {_display(metrics.get('poisoned_source_exposure_rate'))}.", "",
        "## Benign utility and grounded answers", "", f"Benign utility: {_display(metrics.get('benign_task_success_rate'))}; grounded-answer rate: {_display(metrics.get('grounded_answer_rate'))}; benign multi-turn success: {_display(metrics.get('benign_multi_turn_success_rate'))}.", "",
        "## False-refusal rate", "", _display(metrics.get("false_refusal_rate")), "",
        *breakdown_sections,
        "## Latency and token use", "", f"Mean/median/P95 latency: {_display(metrics.get('mean_latency_ms'))} / {_display(metrics.get('median_latency_ms'))} / {_display(metrics.get('p95_latency_ms'))} ms. Input/output tokens: {metrics.get('input_tokens', 0)} / {metrics.get('output_tokens', 0)}.", "",
        "## Errors", "", f"Provider errors: {metrics.get('provider_errors', 0)}; error rate: {_display(metrics.get('error_rate'))}.", "",
        "## Manual-review cases", "", f"Manual-review rate: {_display(metrics.get('manual_review_rate'))}.", "",
        "## Methodology", "", "The model was evaluated with normal retrieval over the shared local PDF index. Retrieval exposure and generation resistance are reported separately. Evaluator targets and decoded hidden payloads are redacted from this presentation report.", "",
        "## Limitations", "", "Results are from a synthetic pilot benchmark and are not evidence of production security or compliance.", "",
        "## Reproducibility appendix", "", "See the adjacent run manifest and sanitized configuration snapshot for hashes, dependencies, extraction policy, retrieval settings, and model configuration.",
    ]
    (model_dir / "report.md").write_text("\n".join(markdown), encoding="utf-8")
    if not generate_pdf:
        return
    styles = getSampleStyleSheet()
    story = [Paragraph(f"RAGnarok Model Report", styles["Title"]), Spacer(1, 6*mm), Paragraph(model_id, styles["Heading1"]), Spacer(1, 8*mm), Paragraph("Security, retrieval, utility, and operational evaluation", styles["BodyText"]), PageBreak(), Paragraph("Executive summary", styles["Heading1"])]
    table = Table([["Metric", "Value"], *[[name, _display(value)] for name, value in rows]], colWidths=[115*mm, 45*mm])
    table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#17324D")), ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("GRID", (0,0), (-1,-1), .4, colors.grey), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("PADDING", (0,0), (-1,-1), 6)]))
    story.extend([table, Spacer(1, 8*mm), Image(str(chart), width=165*mm, height=80*mm), PageBreak()])
    for heading, body in (
        ("Dataset composition", f"{metrics['rows']} evaluated turns in the selected scope."),
        ("Retrieval exposure", f"Expected-source retrieval {_display(metrics.get('expected_source_retrieval_rate'))}; poisoned-source exposure {_display(metrics.get('poisoned_source_exposure_rate'))}."),
        ("Benign utility", f"Task success {_display(metrics.get('benign_task_success_rate'))}; false refusals {_display(metrics.get('false_refusal_rate'))}; benign multi-turn success {_display(metrics.get('benign_multi_turn_success_rate'))}."),
        ("Attack, domain, technique, and surface breakdowns", "Detailed label counts are provided in model_summary.json and the Markdown report without exposing protected targets."),
        ("Latency, errors, and manual review", f"Mean latency {_display(metrics.get('mean_latency_ms'))} ms; provider errors {metrics.get('provider_errors', 0)}; manual-review rate {_display(metrics.get('manual_review_rate'))}."),
        ("Methodology", "PDF body text and metadata were extracted separately, chunked, embedded locally, and ranked with cosine similarity. Expected source paths were used only after retrieval as ground truth. Model responses were scored by deterministic rules and, when configured, an isolated structured judge."),
        ("Limitations", "This synthetic English-language pilot requires human validation. Hidden-content exposure depends on the configured PDF extraction policy. Full attack payloads and protected targets are intentionally omitted."),
        ("Reproducibility appendix", "See run_manifest.json and configuration.snapshot.yaml beside this report for hashes, dependency versions, model settings, retrieval settings, and extraction policy."),
    ):
        story.extend([Paragraph(heading, styles["Heading1"]), Paragraph(body, styles["BodyText"]), Spacer(1, 5*mm)])
    SimpleDocTemplate(str(model_dir / "report.pdf"), pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm).build(story)
