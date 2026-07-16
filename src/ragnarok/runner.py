from __future__ import annotations

import asyncio
import csv
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from .checkpoint import CheckpointStore
from .config import AppConfig, ModelConfig
from .dataset.loader import apply_filters, conversations, load_dataset
from .evaluation import evaluate_rules, evaluate_with_judge, resolve
from .lifecycle import Lifecycle
from .manifest import build_manifest, write_manifest
from .metrics import calculate_metrics
from .models import provider_for
from .pdf import extract_knowledge_base
from .rag import LocalIndex, MockEmbedder, SentenceTransformerEmbedder, chunk_units
from .rag.prompting import inference_messages
from .reporting import generate_comparison, generate_model_report
from .schemas import CaseState, ChatMessage, LifecycleState, ORIGINAL_COLUMNS, ProviderRequest, RUNTIME_COLUMNS, RetrievalHit, utc_now
from .validation import validate_environment


RETRIEVAL_COLUMNS = ["case_id", "conversation_id", "model_id", "retrieved_document_paths", "retrieved_document_ids", "retrieved_chunk_ids", "retrieval_ranks", "similarity_scores", "extracted_surface", "expected_source_retrieved", "expected_source_rank"]


def _safe_configuration(config: AppConfig) -> dict:
    data = config.model_dump(mode="json")
    for model in data["models"]:
        model["headers"] = {key: "[REDACTED]" for key in model.get("headers", {})}
    if data.get("judge", {}).get("model"):
        data["judge"]["model"]["headers"] = {key: "[REDACTED]" for key in data["judge"]["model"].get("headers", {})}
    return data


def build_local_index(config: AppConfig, force: bool = False) -> tuple[LocalIndex, int, int, bool]:
    units = extract_knowledge_base(config.dataset.knowledge_base_dir, config.pdf_extraction)
    chunks = chunk_units(units, config.rag.chunk_size, config.rag.chunk_overlap)
    embedder = MockEmbedder() if config.rag.embedding_backend == "mock" else SentenceTransformerEmbedder(config.rag.embedding_model)
    index = LocalIndex(config.rag.cache_dir, embedder)
    rebuilt = index.build(chunks, force=force)
    return index, len(units), len(chunks), rebuilt


def _retrieval_record(row: dict[str, str], model_id: str, hits: list[RetrievalHit]) -> dict:
    expected = row["source_document"]
    expected_rank = next((hit.rank for hit in hits if f"knowledge_base/{hit.document_path}" == expected), None)
    return {
        "case_id": row["case_id"], "conversation_id": row["conversation_id"], "model_id": model_id,
        "retrieved_document_paths": "|".join(hit.document_path for hit in hits),
        "retrieved_document_ids": "|".join(hit.document_id for hit in hits),
        "retrieved_chunk_ids": "|".join(hit.chunk_id for hit in hits),
        "retrieval_ranks": "|".join(str(hit.rank) for hit in hits),
        "similarity_scores": "|".join(f"{hit.similarity_score:.8f}" for hit in hits),
        "extracted_surface": "|".join(hit.extracted_surface for hit in hits),
        "expected_source_retrieved": str(expected_rank is not None).lower() if expected else "",
        "expected_source_rank": expected_rank or "",
    }


def _write_outputs(model_dir: Path, source: pd.DataFrame, results: list[dict], retrieval: list[dict]) -> pd.DataFrame:
    by_case = {item["case_id"]: item for item in results}
    rows = []
    for row in source.to_dict("records"):
        runtime = by_case.get(row["case_id"])
        if runtime:
            row = {**row, **runtime}
        else:
            row.update({column: "" for column in RUNTIME_COLUMNS})
        rows.append(row)
    frame = pd.DataFrame(rows, columns=ORIGINAL_COLUMNS + RUNTIME_COLUMNS)
    frame.to_csv(model_dir / "results.csv", index=False)
    pd.DataFrame(retrieval, columns=RETRIEVAL_COLUMNS).to_csv(model_dir / "retrieval_log.csv", index=False)
    return frame


async def _run_model(config: AppConfig, root: Path, experiment_dir: Path, source: pd.DataFrame, index: LocalIndex, model_config: ModelConfig, resume: bool, skip_judge: bool, inference_only: bool) -> tuple[pd.DataFrame, dict]:
    model_dir = experiment_dir / model_config.id
    (model_dir / "checkpoints").mkdir(parents=True, exist_ok=True); (model_dir / "charts").mkdir(exist_ok=True)
    checkpoint = CheckpointStore(model_dir / "checkpoints" / "cases.json")
    provider = provider_for(model_config, config.runtime)
    judge_provider = provider_for(config.judge.model, config.runtime) if config.judge.enabled and config.judge.model and not skip_judge and not inference_only else None
    system_prompt = config.evaluation.system_prompt_path.read_text(encoding="utf-8")
    manifest_path = model_dir / "run_manifest.json"
    if resume and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        run_id = manifest["model_run_id"]
        manifest["end_time"] = None
    else:
        run_id = f"{model_config.id}-{uuid.uuid4().hex[:12]}"
        manifest = build_manifest(config, model_config.id, run_id, index.fingerprint, root)
    write_manifest(manifest_path, manifest)
    (model_dir / "configuration.snapshot.yaml").write_text(yaml.safe_dump(_safe_configuration(config), sort_keys=False), encoding="utf-8")
    results: list[dict] = []; retrieval_rows: list[dict] = []; processed = 0
    log_path = model_dir / "logs.jsonl"
    for conversation_id, turns in conversations(source):
        history: list[ChatMessage] = []
        for row in turns.to_dict("records"):
            case_id = row["case_id"]
            if resume and checkpoint.state(case_id) == CaseState.EVALUATION_COMPLETED.value and checkpoint.result(case_id):
                stored = checkpoint.result(case_id); results.append(stored["runtime"]); retrieval_rows.append(stored["retrieval"])
                prior_hits = [RetrievalHit.model_validate(hit) for hit in stored.get("hits", [])]
                history = inference_messages(history, row["prompt"], prior_hits)
                history.append(ChatMessage(role="assistant", content=stored["runtime"]["response"]))
                continue
            hits = index.search(row["prompt"], config.rag.top_k)
            retrieval = _retrieval_record(row, model_config.id, hits)
            checkpoint.update(case_id, CaseState.RETRIEVAL_COMPLETED)
            messages = inference_messages(history, row["prompt"], hits)
            request = ProviderRequest(system_prompt=system_prompt, conversation_messages=messages, model=model_config.model, temperature=model_config.temperature, max_output_tokens=model_config.max_output_tokens, timeout=model_config.timeout_seconds, seed=model_config.seed if model_config.seed is not None else config.runtime.seed, provider_options=model_config.provider_options)
            inference = await provider.generate(request)
            checkpoint.update(case_id, CaseState.INFERENCE_COMPLETED)
            rule = evaluate_rules(row, inference.response_text, inference.error_type)
            judge = None; judge_latency = 0.0; judge_error = ""
            if judge_provider and not rule.conclusive:
                judge, judge_latency, judge_error = await evaluate_with_judge(judge_provider, config.judge.model.model, row, inference.response_text, hits, config.judge.model.timeout_seconds, config.runtime.retries)
            final = resolve(rule, judge, config.judge.confidence_threshold, judge_error) if not inference_only else resolve(rule.model_copy(update={"conclusive": False}), None, config.judge.confidence_threshold)
            expected_rank = retrieval["expected_source_rank"]
            runtime = {
                "case_id": case_id, "response": inference.response_text,
                "model_id": model_config.id, "model_provider": inference.provider,
                "final_label": final.label, "final_score": final.score, "evaluation_reason": final.reason,
                "rule_label": rule.label, "rule_score": rule.score,
                "judge_label": judge.label if judge else "", "judge_confidence": judge.confidence if judge else "",
                "judge_reason": judge.reason if judge else judge_error, "requires_manual_review": final.requires_manual_review,
                "expected_source_retrieved": retrieval["expected_source_retrieved"], "expected_source_rank": expected_rank,
                "latency_ms": inference.latency_ms, "judge_latency_ms": judge_latency,
                "input_tokens": inference.input_tokens or "", "output_tokens": inference.output_tokens or "",
                "error_type": inference.error_type, "error_message": inference.error_message,
                "run_id": run_id, "timestamp": utc_now(),
            }
            results.append(runtime); retrieval_rows.append(retrieval)
            checkpoint.update(case_id, CaseState.EVALUATION_COMPLETED, {"runtime": runtime, "retrieval": retrieval, "hits": [hit.model_dump() for hit in hits]})
            history = messages + [ChatMessage(role="assistant", content=inference.response_text)]
            processed += 1
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": utc_now(), "case_id": case_id, "state": CaseState.EVALUATION_COMPLETED.value, "provider": inference.provider, "error_type": inference.error_type}) + "\n")
            if processed % config.runtime.checkpoint_every == 0:
                checkpoint.save(); _write_outputs(model_dir, source, results, retrieval_rows)
        checkpoint.save()
    frame = _write_outputs(model_dir, source, results, retrieval_rows)
    metrics = calculate_metrics(frame)
    metrics.update({"model_id": model_config.id, "provider": model_config.adapter, "retry_count": provider.retry_count + (judge_provider.retry_count if judge_provider else 0), "judge_disagreement_rate": float(((frame.judge_label != "") & (frame.judge_label != frame.rule_label)).sum() / max(1, (frame.judge_label != "").sum())), "estimated_cost": None})
    (model_dir / "model_summary.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    generate_model_report(model_dir, model_config.id, metrics, config.reporting.generate_pdf)
    manifest["end_time"] = utc_now(); write_manifest(manifest_path, manifest)
    return frame, metrics


async def run_experiment(config: AppConfig, root: Path, *, resume: bool = False, force: bool = False, model_id: str | None = None, case_id: str | None = None, conversation_id: str | None = None, domain: str | None = None, attack_vector: str | None = None, limit: int | None = None, skip_judge: bool = False, inference_only: bool = False, dry_run: bool = False) -> Path:
    if not config.project.experiment_id:
        config.project.experiment_id = datetime.now(timezone.utc).strftime("ragnarok-%Y%m%dT%H%M%SZ")
    experiment_dir = config.project.output_dir / config.project.experiment_id
    lifecycle = Lifecycle(experiment_dir / "status.json"); lifecycle.set(LifecycleState.SETTING_UP)
    try:
        validation = await validate_environment(config, root, online=True)
        if not validation.ok:
            raise RuntimeError(validation.render())
        source = load_dataset(config.dataset.path)
        source = apply_filters(source, case_id=case_id, conversation_id=conversation_id, domain=domain, attack_vector=attack_vector, limit=limit)
        selected_models = [model for model in config.models if not model_id or model.id == model_id]
        if not selected_models:
            raise ValueError(f"unknown model id: {model_id}")
        if dry_run:
            lifecycle.set(LifecycleState.READY, rows=len(source), models=[model.id for model in selected_models], validation="passed", dry_run=True)
            return experiment_dir
        index, unit_count, chunk_count, rebuilt = build_local_index(config, force)
        lifecycle.set(LifecycleState.READY, extraction_units=unit_count, chunks=chunk_count, index_rebuilt=rebuilt)
        frames = {}; summaries = {}
        for model in selected_models:
            lifecycle.set(LifecycleState.EVALUATING, model_id=model.id)
            frame, metrics = await _run_model(config, root, experiment_dir, source, index, model, resume, skip_judge, inference_only)
            frames[model.id], summaries[model.id] = frame, metrics
            lifecycle.set(LifecycleState.MODEL_COMPLETED, model_id=model.id)
        lifecycle.set(LifecycleState.GENERATING_COMPARISON)
        generate_comparison(experiment_dir, summaries, frames, config.reporting.generate_pdf)
        warnings = sum(int((frame.final_label == "error").sum()) for frame in frames.values())
        lifecycle.set(LifecycleState.COMPLETED_WITH_WARNINGS if warnings else LifecycleState.COMPLETED, errors=warnings)
        return experiment_dir
    except (KeyboardInterrupt, asyncio.CancelledError):
        lifecycle.set(LifecycleState.PAUSED); raise
    except Exception as exc:
        lifecycle.set(LifecycleState.FAILED, error_type=type(exc).__name__, error_message=str(exc)[:500]); raise


async def reevaluate_outputs(config: AppConfig, root: Path, model_id: str | None = None, skip_judge: bool = False) -> Path:
    """Re-evaluate existing responses without rerunning target-model inference."""
    experiment_dir = config.project.output_dir / str(config.project.experiment_id)
    excluded = {"comparison", "charts"}
    model_dirs = [experiment_dir / model_id] if model_id else sorted(path for path in experiment_dir.iterdir() if path.is_dir() and path.name not in excluded and (path / "results.csv").exists())
    judge_provider = provider_for(config.judge.model, config.runtime) if config.judge.enabled and config.judge.model and not skip_judge else None
    index = build_local_index(config)[0] if judge_provider else None
    for model_dir in model_dirs:
        path = model_dir / "results.csv"
        frame = pd.read_csv(path, keep_default_na=False).astype(object)
        for position, row in frame.iterrows():
            payload = row.to_dict()
            rule = evaluate_rules(payload, payload["response"], payload.get("error_type", ""))
            judge = None; judge_latency = 0.0; judge_error = ""
            if judge_provider and not rule.conclusive:
                hits = index.search(payload["prompt"], config.rag.top_k)
                judge, judge_latency, judge_error = await evaluate_with_judge(judge_provider, config.judge.model.model, payload, payload["response"], hits, config.judge.model.timeout_seconds, config.runtime.retries)
            final = resolve(rule, judge, config.judge.confidence_threshold, judge_error)
            updates = {
                "final_label": final.label, "final_score": final.score, "evaluation_reason": final.reason,
                "rule_label": rule.label, "rule_score": rule.score,
                "judge_label": judge.label if judge else "", "judge_confidence": judge.confidence if judge else "",
                "judge_reason": judge.reason if judge else judge_error, "judge_latency_ms": judge_latency,
                "requires_manual_review": final.requires_manual_review,
            }
            for column, value in updates.items():
                frame.at[position, column] = value
        frame.to_csv(path, index=False)
        metrics = calculate_metrics(frame); metrics["model_id"] = model_dir.name
        (model_dir / "model_summary.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        generate_model_report(model_dir, model_dir.name, metrics, config.reporting.generate_pdf)
    return experiment_dir
