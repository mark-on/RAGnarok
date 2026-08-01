from __future__ import annotations

import csv
import re
from collections.abc import Callable
from pathlib import Path

from .config import AppConfig, ModelConfig
from .dataset import conversations, load_dataset
from .judging import JudgeDecision, judge_request, parse_judge_result
from .models import provider_for
from .models.base import ModelProvider
from .pdf import extract_knowledge_base
from .rag import LocalIndex, SentenceTransformerEmbedder, chunk_units
from .rag.prompting import inference_messages
from .schemas import ChatMessage, OUTPUT_COLUMNS, ProviderRequest, RetrievalHit


ProgressCallback = Callable[[str, int, int | None, str], None]


def _progress(
    callback: ProgressCallback | None,
    phase: str,
    current: int,
    total: int | None,
    detail: str,
) -> None:
    if callback:
        callback(phase, current, total, detail)


def build_local_index(config: AppConfig) -> tuple[LocalIndex, int, int, bool]:
    units = extract_knowledge_base(config.dataset.knowledge_base_dir)
    chunks = chunk_units(units, config.rag.chunk_size, config.rag.chunk_overlap)
    embedder = SentenceTransformerEmbedder(config.rag.embedding_model)
    index = LocalIndex(config.rag.cache_dir, embedder)
    rebuilt = index.build(chunks)
    return index, len(units), len(chunks), rebuilt


def retrieve_local_context(config: AppConfig, index: LocalIndex, query: str) -> list[RetrievalHit]:
    return index.search(query, config.rag.top_k)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower() or "model"


def _output_path(output_root: Path, model: ModelConfig, reserved: set[str]) -> Path:
    base = _safe_name(model.model)
    name = base
    suffix = 2
    while name in reserved or (output_root / name).exists():
        name = f"{base}_{suffix}"
        suffix += 1
    reserved.add(name)
    directory = output_root / name
    directory.mkdir(parents=True, exist_ok=False)
    return directory / "responses.csv"


def _source_summary(hits: list[RetrievalHit]) -> str:
    return "|".join(
        f"{hit.rank}:{hit.document_path}:{hit.chunk_id}:{hit.similarity_score:.4f}"
        for hit in hits
    )


def _result_row(
    source: dict[str, str], model: ModelConfig, provider_name: str,
    hits: list[RetrievalHit], response: str, error: str,
    judge_mode: str, judge_model: str, judge_provider: str,
    decision: JudgeDecision,
) -> dict[str, str]:
    return {
        "case_id": source["case_id"],
        "conversation_id": source["conversation_id"],
        "turn_index": source["turn_index"],
        "is_continuation": source["is_continuation"],
        "prompt": source["prompt"],
        "is_attack": source.get("is_attack", ""),
        "attack_vector": source.get("attack_vector", ""),
        "expected_behavior": source.get("expected_behavior", ""),
        "success_criteria": source.get("success_criteria", ""),
        "evaluation_target": source.get("evaluation_target", ""),
        "model_name": model.model,
        "model_provider": provider_name,
        "retrieved_sources": _source_summary(hits),
        "response": response,
        "status": decision.status,
        "judge_mode": judge_mode,
        "judge_model": judge_model,
        "judge_provider": judge_provider,
        "judge_response": decision.raw_response,
        "judge_reason": decision.reason,
        "judge_error": decision.error,
        "error": error,
    }


async def _judge_response(
    config: AppConfig,
    source: dict[str, str],
    response: str,
    inference_model: ModelConfig,
    inference_provider: ModelProvider,
    external_judge_provider: ModelProvider | None,
) -> tuple[str, str, JudgeDecision]:
    if config.judge.mode == "none":
        return "", "", JudgeDecision("", "", "")
    if not response:
        return "", "", JudgeDecision("", "", "", "judge skipped because inference failed")

    if config.judge.mode == "same_as_inference":
        judge_model = inference_model
        judge_provider = inference_provider
    else:
        if config.judge.model is None or external_judge_provider is None:
            return "", "", JudgeDecision("", "", "", "judge model is unavailable")
        judge_model = config.judge.model
        judge_provider = external_judge_provider

    result = await judge_provider.generate(judge_request(
        source,
        response,
        model=judge_model.model,
        timeout=judge_model.timeout_seconds,
        max_output_tokens=judge_model.max_output_tokens,
    ))
    return judge_model.model, result.provider, parse_judge_result(result)


async def run_experiment(
    config: AppConfig,
    *,
    progress: ProgressCallback | None = None,
) -> list[Path]:
    if not config.system_prompt_path.is_file():
        raise ValueError(f"system prompt does not exist: {config.system_prompt_path}")
    table = load_dataset(config.dataset.path)
    system_prompt = config.system_prompt_path.read_text(encoding="utf-8").strip()

    _progress(progress, "index", 0, None, "Building or loading the RAG index")
    index, units, chunks, rebuilt = build_local_index(config)
    cache_state = "rebuilt" if rebuilt else "loaded from cache"
    _progress(progress, "index", 1, 1, f"RAG index {cache_state}: {chunks} chunks from {units} units")

    retrievals: dict[str, list[RetrievalHit]] = {}
    for position, row in enumerate(table.rows, 1):
        _progress(progress, "retrieval", position - 1, len(table.rows), f"Retrieving prompt {position}/{len(table.rows)}")
        retrievals[row["case_id"]] = retrieve_local_context(config, index, row["prompt"])
    _progress(progress, "retrieval", len(table.rows), len(table.rows), f"Retrieved context for {len(table.rows)} prompts")

    config.output_dir.mkdir(parents=True, exist_ok=True)
    external_judge_provider = (
        provider_for(config.judge.model, config.runtime)
        if config.judge.mode == "model" and config.judge.model is not None
        else None
    )
    output_paths: list[Path] = []
    reserved: set[str] = set()
    for model_position, model in enumerate(config.models, 1):
        provider = provider_for(model, config.runtime)
        output_path = _output_path(config.output_dir, model, reserved)
        output_paths.append(output_path)
        completed = 0
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
            writer.writeheader()
            for turns in conversations(table.rows):
                clean_history: list[ChatMessage] = []
                for row in turns:
                    hits = retrievals[row["case_id"]]
                    _progress(
                        progress,
                        "inference",
                        completed,
                        len(table.rows),
                        f"Model {model_position}/{len(config.models)}: {model.model} · prompt {completed + 1}/{len(table.rows)}",
                    )
                    messages = inference_messages(clean_history, row["prompt"], hits)
                    result = await provider.generate(ProviderRequest(
                        system_prompt=system_prompt,
                        conversation_messages=messages,
                        model=model.model,
                        temperature=model.temperature,
                        max_output_tokens=model.max_output_tokens,
                        timeout=model.timeout_seconds,
                    ))
                    judge_model, judge_provider, decision = await _judge_response(
                        config,
                        row,
                        result.response_text,
                        model,
                        provider,
                        external_judge_provider,
                    )
                    if config.judge.mode != "none":
                        _progress(
                            progress,
                            "judge",
                            completed + 1,
                            len(table.rows),
                            f"Judged prompt {completed + 1}/{len(table.rows)}: {decision.status or 'error'}",
                        )
                    writer.writerow(_result_row(
                        row,
                        model,
                        result.provider,
                        hits,
                        result.response_text,
                        result.error_message,
                        config.judge.mode,
                        judge_model,
                        judge_provider,
                        decision,
                    ))
                    handle.flush()
                    if not result.error_type:
                        clean_history.extend([
                            ChatMessage(role="user", content=row["prompt"]),
                            ChatMessage(role="assistant", content=result.response_text),
                        ])
                    completed += 1
        _progress(
            progress,
            "model_completed",
            len(table.rows),
            len(table.rows),
            f"Model {model_position}/{len(config.models)} complete: {output_path}",
        )
    return output_paths
