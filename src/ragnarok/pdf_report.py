from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Callable, Iterable

from .reports import _case_record, _case_taxonomy, _quantization, _write_csv
from .results import ResultStore
from .results.schemas import UniversalCase
from .taxonomy import BENCHMARK_COVERAGE, KNOWN_GAPS


@dataclass(frozen=True)
class ReportRun:
    path: Path
    manifest: dict
    case_count: int
    model_names: tuple[str, ...]

    @property
    def label(self) -> str:
        created = str(self.manifest.get("created_at") or "unknown date")[:19].replace("T", " ")
        models = ", ".join(self.model_names) or "unknown model"
        status = str(self.manifest.get("status") or "unknown")
        return f"{models} | {status} | {self.case_count} cases | {created} | {self.path.name}"


def validate_pdf_dependencies() -> None:
    try:
        import reportlab  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "PDF reporting dependency is missing; run 'ragnarok setup' and retry"
        ) from exc


def _manifest_models(manifest: dict) -> tuple[str, ...]:
    configuration = manifest.get("configuration")
    models = configuration.get("models", []) if isinstance(configuration, dict) else []
    return tuple(
        str(item.get("model") or item.get("id"))
        for item in models
        if isinstance(item, dict) and (item.get("model") or item.get("id"))
    )


def discover_report_runs(output_dir: Path) -> list[ReportRun]:
    """Return result suites that contain canonical cases, newest first."""

    if not output_dir.is_dir():
        return []
    discovered: list[ReportRun] = []
    for manifest_path in output_dir.glob("*/suite_manifest.json"):
        suite_dir = manifest_path.parent
        if not (suite_dir / "results.sqlite").is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            cases = ResultStore(suite_dir).cases()
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not cases:
            continue
        discovered.append(
            ReportRun(
                path=suite_dir,
                manifest=manifest,
                case_count=len(cases),
                model_names=_manifest_models(manifest),
            )
        )
    return sorted(
        discovered,
        key=lambda item: str(item.manifest.get("created_at") or ""),
        reverse=True,
    )


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._-") or "report"


def _attack_value(case: UniversalCase) -> bool | None:
    if not case.official_evaluation.get("is_attack_case", True):
        return None
    value = case.official_evaluation.get("attack_success")
    return value if isinstance(value, bool) else None


def _rate(cases: Iterable[UniversalCase]) -> tuple[int, int, float | None]:
    values = [value for case in cases if (value := _attack_value(case)) is not None]
    successes = sum(values)
    return successes, len(values), successes / len(values) if values else None


def _utility(cases: Iterable[UniversalCase]) -> tuple[int, int, float | None]:
    values: list[bool] = []
    for case in cases:
        value = case.official_evaluation.get("utility")
        if isinstance(value, bool):
            values.append(value)
    successes = sum(values)
    return successes, len(values), successes / len(values) if values else None


def _ordered_models(model_metadata: dict[str, dict]) -> list[str]:
    rank = {"FP16": 0, "BF16": 1, "Q8": 2, "Q6": 3, "Q5": 4, "Q4": 5, "Q3": 6, "Q2": 7}

    def key(model_id: str) -> tuple[int, str]:
        name = str(model_metadata.get(model_id, {}).get("model") or model_id)
        return rank.get(_quantization(name), 99), name.lower()

    return sorted(model_metadata, key=key)


def _display_model(model_id: str, model_metadata: dict[str, dict]) -> str:
    return str(model_metadata.get(model_id, {}).get("model") or model_id)


def _merge_runs(runs: list[ReportRun]) -> tuple[list[UniversalCase], list[dict], list[dict], dict[str, dict], list[dict]]:
    all_cases: list[UniversalCase] = []
    all_metrics: list[dict] = []
    all_calls: list[dict] = []
    model_metadata: dict[str, dict] = {}
    selected_records: list[dict] = []
    occurrences: defaultdict[str, int] = defaultdict(int)
    for run in runs:
        configuration = run.manifest.get("configuration")
        configured_models = configuration.get("models", []) if isinstance(configuration, dict) else []
        store = ResultStore(run.path)
        run_cases = store.cases()
        run_model_ids = sorted({case.model_id for case in run_cases})
        aliases: dict[str, str] = {}
        for model_id in run_model_ids:
            occurrences[model_id] += 1
            alias = model_id if occurrences[model_id] == 1 else f"{model_id}@{run.path.name}"
            aliases[model_id] = alias
            metadata = next(
                (item for item in configured_models if isinstance(item, dict) and str(item.get("id")) == model_id),
                {"id": model_id, "model": model_id},
            )
            model_metadata[alias] = {**metadata, "id": alias, "source_suite": run.path.name}
        all_cases.extend(
            case.model_copy(update={"model_id": aliases.get(case.model_id, case.model_id)})
            for case in run_cases
        )
        for row in store.metrics():
            all_metrics.append({**row, "model_id": aliases.get(str(row.get("model_id")), str(row.get("model_id")))})
        for row in store.model_calls():
            all_calls.append({**row, "model_id": aliases.get(str(row.get("model_id")), str(row.get("model_id")))})
        selected_records.append({
            "suite_id": run.manifest.get("suite_id") or run.path.name,
            "path": str(run.path),
            "status": run.manifest.get("status"),
            "created_at": run.manifest.get("created_at"),
            "execution": run.manifest.get("execution"),
            "configuration_sha256": run.manifest.get("configuration_sha256"),
            "models": list(run.model_names),
            "cases": len(run_cases),
            "profiles": sorted({
                str(item.get("options", {}).get("profile"))
                for item in (configuration.get("benchmarks", []) if isinstance(configuration, dict) else [])
                if isinstance(item, dict) and item.get("options", {}).get("profile")
            }),
            "judges": sorted({
                str(item.get("judge", {}).get("model"))
                for item in (configuration.get("benchmarks", []) if isinstance(configuration, dict) else [])
                if isinstance(item, dict) and isinstance(item.get("judge"), dict) and item.get("judge", {}).get("model")
            }),
        })
    return all_cases, all_metrics, all_calls, model_metadata, selected_records


def _table(rows: list[list[object]], widths: list[float], *, header: bool = True):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Table, TableStyle

    header_style = ParagraphStyle(
        name="TableHeaderCell",
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9,
        textColor=colors.white,
        splitLongWords=1,
    )
    body_style = ParagraphStyle(
        name="TableBodyCell",
        fontName="Helvetica",
        fontSize=7.5,
        leading=9,
        textColor=colors.HexColor("#344054"),
        splitLongWords=1,
    )
    wrapped_rows = [
        [
            Paragraph(escape(str(cell)) if cell not in {None, ""} else "&#160;", header_style if header and row_index == 0 else body_style)
            for cell in row
        ]
        for row_index, row in enumerate(rows)
    ]

    table = Table(wrapped_rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    commands = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17324D")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("LEADING", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7FA")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if not header:
        commands = [command for command in commands if command[0] not in {"BACKGROUND", "TEXTCOLOR"}]
    table.setStyle(TableStyle(commands))
    return table


def _line_chart(
    categories: list[str],
    models: list[str],
    values: dict[str, list[float]],
    labels: dict[str, str],
    palette,
):
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.graphics.widgets.markers import makeMarker
    from reportlab.lib import colors

    drawing = Drawing(445, 265)
    chart = HorizontalLineChart()
    chart.x, chart.y, chart.width, chart.height = 48, 125, 375, 110
    chart.data = [values[model] for model in models]
    chart.categoryAxis.categoryNames = categories
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.angle = 20
    chart.categoryAxis.labels.dy = -8
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = 100
    chart.valueAxis.valueStep = 20
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.labelTextFormat = "%d%%"
    chart.joinedLines = 1
    for index, model in enumerate(models):
        chart.lines[index].strokeColor = palette[index % len(palette)]
        chart.lines[index].strokeWidth = 1.8
        chart.lines[index].strokeDashArray = [5, 2]
        chart.lines[index].symbol = makeMarker("FilledCircle")
        chart.lines[index].symbol.size = 4
        chart.lines[index].symbol.fillColor = palette[index % len(palette)]
    drawing.add(chart)
    drawing.add(String(8, 175, "ASR", fontName="Helvetica-Bold", fontSize=8, fillColor=colors.HexColor("#344054")))
    for index, model in enumerate(models):
        label = labels[model]
        y = 43 - index * 10
        drawing.add(Rect(48, y - 1, 7, 7, fillColor=palette[index % len(palette)], strokeColor=colors.HexColor("#17324D"), strokeWidth=0.5))
        drawing.add(String(60, y, label, fontName="Helvetica", fontSize=7, fillColor=colors.HexColor("#344054")))
    return drawing


def _bar_chart(categories: list[str], models: list[str], values: dict[str, list[float]], labels: dict[str, str], palette):
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.lib import colors

    drawing = Drawing(445, 265)
    chart = VerticalBarChart()
    chart.x, chart.y, chart.width, chart.height = 48, 125, 375, 110
    chart.data = [values[model] for model in models]
    chart.categoryAxis.categoryNames = categories
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.angle = 20 if len(categories) > 3 else 0
    chart.categoryAxis.labels.dy = -8
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = 100
    chart.valueAxis.valueStep = 20
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.labelTextFormat = "%d%%"
    chart.groupSpacing = 8
    for index, model in enumerate(models):
        chart.bars[index].fillColor = palette[index % len(palette)]
        chart.bars[index].strokeColor = palette[index % len(palette)]
    drawing.add(chart)
    drawing.add(String(8, 175, "ASR", fontName="Helvetica-Bold", fontSize=8, fillColor=colors.HexColor("#344054")))
    for index, model in enumerate(models):
        label = labels[model]
        y = 43 - index * 10
        drawing.add(Rect(48, y - 1, 7, 7, fillColor=palette[index % len(palette)], strokeColor=colors.HexColor("#17324D"), strokeWidth=0.5))
        drawing.add(String(60, y, label, fontName="Helvetica", fontSize=7, fillColor=colors.HexColor("#344054")))
    return drawing


def _model_family(labels: Iterable[str]) -> str:
    names = list(labels)
    if not names:
        return "UNKNOWN MODEL"
    base = re.sub(
        r"[-_:](?:bf16|bfloat16|fp16|f16|q[2-8](?:_[A-Za-z0-9]+)*)$",
        "",
        names[0],
        flags=re.IGNORECASE,
    )
    return re.sub(r"[-_:]+", " ", base).strip().upper()


def _duration_label(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d} m"
    if minutes:
        return f"{minutes} m {seconds:02d} s"
    return f"{seconds} s"


def _security_utility_score(asr: float | None, utility: float | None) -> float | None:
    """Return the equal-weight harmonic balance of resistance and legitimate-task utility."""

    if asr is None or utility is None:
        return None
    security_score = 100 * (1 - asr)
    utility_score = 100 * utility
    denominator = security_score + utility_score
    return 0.0 if denominator == 0 else 2 * security_score * utility_score / denominator


def _rate_matrix(
    cases: list[UniversalCase],
    models: list[str],
    category: Callable[[UniversalCase], str],
) -> tuple[list[str], dict[str, list[float]], dict[tuple[str, str], tuple[int, int, float | None]]]:
    categories = sorted({category(case) for case in cases})
    details: dict[tuple[str, str], tuple[int, int, float | None]] = {}
    values: dict[str, list[float]] = {}
    for model in models:
        model_values = []
        for item in categories:
            summary = _rate(case for case in cases if case.model_id == model and category(case) == item)
            details[(model, item)] = summary
            model_values.append((summary[2] or 0) * 100)
        values[model] = model_values
    return categories, values, details


def _benchmark_label(value: str) -> str:
    aliases = {"poisonedrag": "PoisonedRAG", "mpib": "MPIB", "spikee": "SPIKEE", "agentdojo": "AgentDojo"}
    return aliases.get(value, value)


def _interpretation(cases: list[UniversalCase], models: list[str], labels: dict[str, str]) -> list[str]:
    observations = []
    rates = {model: _rate(case for case in cases if case.model_id == model)[2] for model in models}
    valid = {model: value for model, value in rates.items() if value is not None}
    if valid:
        lowest = min(valid, key=valid.get)
        highest = max(valid, key=valid.get)
        spread = (valid[highest] - valid[lowest]) * 100
        observations.append(
            f"Observed overall ASR ranges from {valid[lowest]:.1%} ({labels[lowest]}) to "
            f"{valid[highest]:.1%} ({labels[highest]}), a {spread:.1f} percentage-point spread."
        )
    utilities = {model: _utility(case for case in cases if case.model_id == model)[2] for model in models}
    valid_utility = {model: value for model, value in utilities.items() if value is not None}
    if valid_utility:
        observations.append(
            "Legitimate-task utility is reported separately from ASR; lower ASR is not treated as improved security "
            "when it coincides with reduced task completion."
        )
        subs = {
            model: _security_utility_score(rates.get(model), valid_utility.get(model))
            for model in models
            if model in valid and model in valid_utility
        }
        valid_subs = {model: value for model, value in subs.items() if value is not None}
        if valid_subs:
            best = max(valid_subs, key=valid_subs.get)
            observations.append(
                f"Highest observed Security-Utility Balance Score: {valid_subs[best]:.1f}/100 "
                f"({labels[best]})."
            )
    observations.append(
        "These are descriptive benchmark results. They do not establish that quantization caused a difference without "
        "paired uncertainty analysis and repeated runs under the same protocol."
    )
    return observations


def _build_pdf(
    path: Path,
    *,
    cases: list[UniversalCase],
    model_metadata: dict[str, dict],
    selected_runs: list[dict],
) -> None:
    validate_pdf_dependencies()
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
    )

    palette = [
        colors.HexColor("#176B57"), colors.HexColor("#C53B3B"), colors.HexColor("#2F6B9A"),
        colors.HexColor("#D9862C"), colors.HexColor("#7253A6"), colors.HexColor("#2B8CBE"),
    ]
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=colors.HexColor("#17324D"), alignment=TA_CENTER, spaceAfter=8))
    styles.add(ParagraphStyle(name="ModelTitle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=colors.HexColor("#176B57"), alignment=TA_CENTER, spaceAfter=12))
    styles.add(ParagraphStyle(name="SectionTitle", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=14, leading=17, textColor=colors.HexColor("#17324D"), spaceBefore=4, spaceAfter=8))
    styles.add(ParagraphStyle(name="SmallBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=8.5, leading=11, textColor=colors.HexColor("#344054")))
    styles.add(ParagraphStyle(name="Note", parent=styles["BodyText"], fontName="Helvetica-Oblique", fontSize=8, leading=10, textColor=colors.HexColor("#667085")))

    models = _ordered_models(model_metadata)
    labels = {model: _display_model(model, model_metadata) for model in models}
    generated_at = datetime.now(timezone.utc)
    page_size = landscape(A4)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(path), pagesize=page_size, leftMargin=13 * mm, rightMargin=13 * mm,
        topMargin=15 * mm, bottomMargin=13 * mm,
        title="RAGnarok security evaluation report", author="RAGnarok",
    )

    def page(canvas, doc):
        canvas.saveState()
        width, height = page_size
        if doc.page == 1:
            canvas.setFillColor(colors.HexColor("#17324D"))
            canvas.rect(0, height - 25 * mm, width, 25 * mm, fill=1, stroke=0)
            canvas.setFillColor(colors.white)
            canvas.setFont("Helvetica-Bold", 17)
            canvas.drawString(13 * mm, height - 15 * mm, "Quantization Security Report")
            profiles = sorted({profile for row in selected_runs for profile in row.get("profiles", [])})
            judges = sorted({judge for row in selected_runs for judge in row.get("judges", [])})
            profile_text = "/".join(profile.title() for profile in profiles) or "Stored suite"
            date_text = generated_at.astimezone().strftime("%d %b %Y")
            judge_text = f" | {judges[0]} Judge" if len(judges) == 1 else (" | Multiple Judges" if judges else "")
            canvas.setFont("Helvetica", 7.5)
            canvas.drawRightString(width - 13 * mm, height - 14 * mm, f"{profile_text} | {date_text}{judge_text}")
        else:
            canvas.setStrokeColor(colors.HexColor("#D0D5DD"))
            canvas.line(13 * mm, height - 10 * mm, width - 13 * mm, height - 10 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#667085"))
        canvas.drawString(13 * mm, 7 * mm, "RAGnarok - generated from canonical normalized results")
        canvas.drawRightString(width - 13 * mm, 7 * mm, f"Page {doc.page}")
        canvas.restoreState()

    total_successes, _, _ = _rate(cases)
    attack_cases = sum(bool(case.official_evaluation.get("is_attack_case", True)) for case in cases)
    runtime_errors = sum(bool(case.error) for case in cases)
    elapsed_seconds = sum(float((row.get("execution") or {}).get("elapsed_seconds") or 0) for row in selected_runs)
    quantizations = [_quantization(labels[model]) for model in models]

    model_card = Table(
        [[Paragraph(_model_family(labels.values()), styles["ReportTitle"])],
         [Paragraph("  |  ".join(quantizations), styles["ModelTitle"])]],
        colWidths=[790], rowHeights=[42, 28], hAlign="LEFT",
        style=[
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#D6E1EC")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ],
    )

    metric_data = [
        ("TOTAL CASES", f"{len(cases):,}"),
        ("ATTACK CASES", f"{attack_cases:,}"),
        ("SUCCESSES", f"{total_successes:,}"),
        ("RUNTIME ERRORS", f"{runtime_errors:,}"),
        ("DURATION", _duration_label(elapsed_seconds)),
    ]
    metric_cards = []
    for index, (title, value) in enumerate(metric_data):
        title_style = ParagraphStyle(name=f"MetricTitle{index}", parent=styles["SmallBody"], fontName="Helvetica-Bold", fontSize=7, textColor=colors.HexColor("#58779A"))
        value_style = ParagraphStyle(name=f"MetricValue{index}", parent=styles["SmallBody"], fontName="Helvetica-Bold", fontSize=14, leading=16, textColor=colors.HexColor("#17324D"))
        metric_cards.append(Table(
            [[Paragraph(title, title_style)], [Paragraph(value, value_style)]],
            colWidths=[148], rowHeights=[16, 27],
            style=[
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#D6E1EC")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ],
        ))
    metric_row = Table([metric_cards], colWidths=[154] * 5, hAlign="LEFT", style=[("VALIGN", (0, 0), (-1, -1), "MIDDLE")])

    rates = {model: _rate(case for case in cases if case.model_id == model) for model in models}
    baseline_model = next((model for model in models if _quantization(labels[model]) in {"FP16", "BF16"}), models[0])
    baseline_asr = rates[baseline_model][2]
    cover_rows = [["Quant.", "Success", "ASR", "Resistance", "Utility", "SUBS", f"vs {_quantization(labels[baseline_model])}"]]
    for model in models:
        successes, evaluated, asr = rates[model]
        _, _, utility_rate = _utility(case for case in cases if case.model_id == model)
        subs = _security_utility_score(asr, utility_rate)
        delta = None if asr is None or baseline_asr is None else (asr - baseline_asr) * 100
        cover_rows.append([
            _quantization(labels[model]), f"{successes}/{evaluated}",
            "N/A" if asr is None else f"{asr:.2%}",
            "N/A" if asr is None else f"{1 - asr:.2%}",
            "N/A" if utility_rate is None else f"{utility_rate:.2%}",
            "N/A" if subs is None else f"{subs:.1f}",
            "N/A" if delta is None else f"{delta:+.2f} pp",
        ])
    observation_rows = [[Paragraph("Automatically derived observations", styles["SectionTitle"])]]
    for item in _interpretation(cases, models, labels):
        observation_rows.append([Paragraph(f"- {item}", styles["SmallBody"])])
    summary_table = Table(
        [[
            Table(
                [[Paragraph("Overall results", styles["SectionTitle"])], [_table(cover_rows, [48, 62, 48, 58, 50, 42, 60])]],
                colWidths=[485],
                style=[("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#D6E1EC")), ("BACKGROUND", (0, 0), (-1, -1), colors.white), ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)],
            ),
            Table(
                observation_rows, colWidths=[275],
                style=[("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#D6E1EC")), ("BACKGROUND", (0, 0), (-1, -1), colors.white), ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)],
            ),
        ]],
        colWidths=[500, 290], hAlign="LEFT", style=[("VALIGN", (0, 0), (-1, -1), "TOP")],
    )

    subs_heading_style = ParagraphStyle(
        name="SubsHeading", parent=styles["SmallBody"], fontName="Helvetica-Bold",
        fontSize=10, leading=12, textColor=colors.HexColor("#17324D"),
    )
    subs_formula_style = ParagraphStyle(
        name="SubsFormula", parent=styles["SmallBody"], fontName="Helvetica-Bold",
        fontSize=9, leading=11, textColor=colors.HexColor("#176B57"),
    )
    subs_explanation_box = Table(
        [
            [Paragraph("Security-Utility Balance Score (SUBS)", subs_heading_style)],
            [Paragraph(
                "SUBS = 2 * SecurityScore * UtilityScore / (SecurityScore + UtilityScore)",
                subs_formula_style,
            )],
            [Paragraph(
                "SecurityScore = 100 * (1 - ASR). UtilityScore = 100 * Utility. "
                "The resulting score ranges from 0 to 100: a higher SUBS indicates a better balance between "
                "attack resistance and legitimate-task performance under equal weighting. "
                "ASR means Attack Success Rate; lower ASR values indicate fewer successful attacks.",
                styles["SmallBody"],
            )],
        ],
        colWidths=[790], hAlign="LEFT",
        style=[
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EAF2F8")),
            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#8FB3C9")),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ],
    )

    story = [
        Spacer(1, 17 * mm),
        model_card,
        Spacer(1, 5 * mm),
        metric_row,
        Spacer(1, 5 * mm),
        summary_table,
        Spacer(1, 3 * mm),
        subs_explanation_box,
        PageBreak(),
    ]

    benchmarks, benchmark_values, benchmark_details = _rate_matrix(cases, models, lambda case: _benchmark_label(case.benchmark_id))
    overall_rows = [["Model", "Quant.", "Successful", "Evaluated", "ASR", "Utility"]]
    for model in models:
        successes, evaluated, asr = _rate(case for case in cases if case.model_id == model)
        utility_successes, utility_total, utility_rate = _utility(case for case in cases if case.model_id == model)
        overall_rows.append([
            labels[model], _quantization(labels[model]), successes, evaluated,
            "N/A" if asr is None else f"{asr:.1%}",
            "N/A" if utility_rate is None else f"{utility_rate:.1%} ({utility_successes}/{utility_total})",
        ])
    chart_and_table = Table(
        [[_line_chart(benchmarks, models, benchmark_values, labels, palette), _table(overall_rows, [93, 35, 48, 45, 40, 75])]],
        colWidths=[452, 342], hAlign="LEFT", style=[("VALIGN", (0, 0), (-1, -1), "TOP")],
    )
    story.extend([
        Paragraph("RAGnarok Security Evaluation", styles["ReportTitle"]),
        Paragraph("<br/>".join(labels[model] for model in models), styles["ModelTitle"]),
        Paragraph("Security outcome by benchmark", styles["SectionTitle"]),
        chart_and_table,
        PageBreak(),
    ])

    objective_categories, objective_values, objective_details = _rate_matrix(
        cases, models, lambda case: _case_taxonomy(case)["security_objective"]
    )
    objective_rows = [["Attack objective", *[_quantization(labels[model]) for model in models]]]
    for category in objective_categories:
        objective_rows.append([
            category,
            *[
                "N/A" if objective_details[(model, category)][2] is None else f"{objective_details[(model, category)][2]:.1%}"
                for model in models
            ],
        ])
    objective_widths = [115] + [max(38, 190 / max(len(models), 1))] * len(models)
    story.extend([
        Paragraph("Attack taxonomy comparison", styles["SectionTitle"]),
        Table(
            [[_bar_chart(objective_categories, models, objective_values, labels, palette), _table(objective_rows, objective_widths)]],
            colWidths=[452, 342], hAlign="LEFT", style=[("VALIGN", (0, 0), (-1, -1), "TOP")],
        ),
        Spacer(1, 3 * mm),
        Paragraph(
            "Security objectives are derived from native case metadata when available and otherwise from the frozen "
            "RAGnarok benchmark taxonomy mapping.", styles["Note"],
        ),
        PageBreak(),
    ])

    for benchmark in sorted({case.benchmark_id for case in cases}):
        benchmark_cases = [case for case in cases if case.benchmark_id == benchmark]
        category = lambda case: str(case.official_evaluation.get("dataset") or case.attack_family or "All cases")
        categories, values, details = _rate_matrix(benchmark_cases, models, category)
        rows = [["Dataset / family", *[_quantization(labels[model]) for model in models]]]
        for item in categories:
            rows.append([
                item,
                *[
                    "N/A" if details[(model, item)][2] is None else f"{details[(model, item)][2]:.1%}"
                    for model in models
                ],
            ])
        coverage = BENCHMARK_COVERAGE.get(benchmark, {})
        story.extend([
            Paragraph(f"{_benchmark_label(benchmark)} results", styles["SectionTitle"]),
            Table(
                [[_bar_chart(categories, models, values, labels, palette), _table(rows, [115] + [max(38, 190 / max(len(models), 1))] * len(models))]],
                colWidths=[452, 342], hAlign="LEFT", style=[("VALIGN", (0, 0), (-1, -1), "TOP")],
            ),
            Spacer(1, 3 * mm),
            Paragraph(
                f"Track: {coverage.get('track', 'Unclassified')}. "
                f"Objectives: {', '.join(coverage.get('objectives', [])) or 'not recorded'}. "
                f"Techniques: {', '.join(coverage.get('techniques', [])) or 'not recorded'}.",
                styles["Note"],
            ),
            PageBreak(),
        ])

    story.extend([Paragraph("Data-grounded interpretation", styles["SectionTitle"])])
    for observation in _interpretation(cases, models, labels):
        story.append(Paragraph(f"- {observation}", styles["SmallBody"]))
        story.append(Spacer(1, 2 * mm))
    story.extend([
        Spacer(1, 3 * mm),
        Paragraph("Selected source runs", styles["SectionTitle"]),
        _table(
            [["Suite", "Status", "Started", "Cases", "Models"]] + [
                [
                    str(row["suite_id"])[:38], row["status"],
                    str((row.get("execution") or {}).get("started_at_local") or row.get("created_at") or "")[:19],
                    row["cases"], ", ".join(row["models"]),
                ]
                for row in selected_runs
            ],
            [205, 55, 105, 45, 330],
        ),
        Spacer(1, 4 * mm),
        Paragraph("Known coverage gaps", styles["SectionTitle"]),
    ])
    for gap in KNOWN_GAPS:
        story.append(Paragraph(f"- {gap}", styles["SmallBody"]))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "Native benchmark evaluators remain authoritative. The PDF is a comparative visualization of stored results; "
        "it does not re-score responses or call any model or Judge.", styles["Note"],
    ))

    if story and isinstance(story[-2] if len(story) > 1 else None, PageBreak):
        story.pop(-2)
    document.build(story, onFirstPage=page, onLaterPages=page)


def generate_pdf_report_bundle(
    runs: list[ReportRun],
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    """Combine selected canonical suites into a PDF and auditable supporting files."""

    if not runs:
        raise ValueError("select at least one result run")
    cases, metrics, calls, model_metadata, selected_records = _merge_runs(runs)
    if not cases:
        raise ValueError("the selected runs contain no normalized cases")
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "report.pdf"
    csv_path = output_dir / "combined_results.csv"
    manifest_path = output_dir / "report_manifest.json"
    _build_pdf(pdf_path, cases=cases, model_metadata=model_metadata, selected_runs=selected_records)
    _write_csv(csv_path, [_case_record(case) for case in cases])
    score_summary = {}
    for model_id in sorted(model_metadata):
        model_cases = [case for case in cases if case.model_id == model_id]
        _, _, asr = _rate(model_cases)
        _, _, utility = _utility(model_cases)
        score_summary[model_id] = {
            "asr": asr,
            "security_score": None if asr is None else 100 * (1 - asr),
            "utility": utility,
            "utility_score": None if utility is None else 100 * utility,
            "security_utility_balance_score": _security_utility_score(asr, utility),
        }
    manifest_path.write_text(
        json.dumps(
            {
                "framework": "RAGnarok",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source_runs": selected_records,
                "models": list(model_metadata.values()),
                "cases": len(cases),
                "security_utility_scores": score_summary,
                "official_metrics": metrics,
                "model_call_records": len(calls),
                "pdf": str(pdf_path),
                "combined_results": str(csv_path),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ) + "\n",
        encoding="utf-8",
    )
    return pdf_path, csv_path, manifest_path


def default_report_directory(output_dir: Path, runs: list[ReportRun]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = _safe_name(runs[0].path.name) if len(runs) == 1 else "comparison"
    return output_dir / "reports" / f"{prefix}_{stamp}"
