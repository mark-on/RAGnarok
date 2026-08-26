from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .schemas import UniversalCase


class ResultStore:
    """Canonical SQLite store with lossless JSONL and flat CSV exports."""

    def __init__(self, suite_dir: Path):
        self.suite_dir = suite_dir
        self.universal_dir = suite_dir / "data"
        self.universal_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = suite_dir / "results.sqlite"
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    suite_id TEXT, benchmark_id TEXT, model_id TEXT, case_id TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (suite_id, benchmark_id, model_id, case_id)
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    suite_id TEXT, benchmark_id TEXT, model_id TEXT, namespace TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (suite_id, benchmark_id, model_id, namespace)
                );
                CREATE TABLE IF NOT EXISTS model_calls (
                    suite_id TEXT, benchmark_id TEXT, model_id TEXT, call_id TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (suite_id, benchmark_id, model_id, call_id)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    suite_id TEXT, benchmark_id TEXT, model_id TEXT, kind TEXT, path TEXT,
                    PRIMARY KEY (suite_id, benchmark_id, model_id, kind, path)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    suite_id TEXT, benchmark_id TEXT, model_id TEXT,
                    status TEXT NOT NULL, error TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY (suite_id, benchmark_id, model_id)
                );
                """
            )
        for filename in ("cases.jsonl", "model_calls.jsonl", "metrics.jsonl"):
            (self.universal_dir / filename).touch(exist_ok=True)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_cases(self, cases: Iterable[UniversalCase]) -> None:
        self.add_batch(cases=cases)

    def add_model_calls(self, rows: Iterable[dict]) -> None:
        self.add_batch(model_calls=rows)

    def add_metrics(self, suite_id: str, benchmark_id: str, model_id: str, namespace: str, metrics: dict) -> None:
        self.add_batch(metrics=[(suite_id, benchmark_id, model_id, namespace, metrics)])

    def add_artifact(self, suite_id: str, benchmark_id: str, model_id: str, kind: str, path: Path) -> None:
        self.add_batch(artifacts=[(suite_id, benchmark_id, model_id, kind, path)])

    def add_batch(
        self,
        *,
        cases: Iterable[UniversalCase] | None = None,
        model_calls: Iterable[dict] | None = None,
        metrics: Iterable[tuple[str, str, str, str, dict]] | None = None,
        artifacts: Iterable[tuple[str, str, str, str, Path]] | None = None,
    ) -> None:
        """Persist one adapter result in a single transaction and export each changed table once."""

        export_cases = cases is not None
        export_calls = model_calls is not None
        export_metrics = metrics is not None
        case_rows = list(cases or ())
        call_rows = list(model_calls or ())
        metric_rows = list(metrics or ())
        artifact_rows = list(artifacts or ())
        if not any((case_rows, call_rows, metric_rows, artifact_rows, export_cases, export_calls, export_metrics)):
            return

        with self._connect() as db:
            if case_rows:
                db.executemany(
                    "INSERT OR REPLACE INTO cases VALUES (?, ?, ?, ?, ?)",
                    [
                        (row.suite_id, row.benchmark_id, row.model_id, row.case_id, row.model_dump_json())
                        for row in case_rows
                    ],
                )
            if call_rows:
                # A successful rerun or rejudge supersedes canonical calls for
                # the same role. Native logs remain untouched for auditability.
                scopes = {
                    (
                        row["suite_id"],
                        row["benchmark_id"],
                        row["model_id"],
                        str(row.get("model_role", "")),
                    )
                    for row in call_rows
                }
                for suite_id, benchmark_id, model_id, model_role in scopes:
                    existing = db.execute(
                        "SELECT call_id, payload_json FROM model_calls "
                        "WHERE suite_id = ? AND benchmark_id = ? AND model_id = ?",
                        (suite_id, benchmark_id, model_id),
                    ).fetchall()
                    stale_ids = [
                        call_id
                        for call_id, payload_json in existing
                        if str(json.loads(payload_json).get("model_role", "")) == model_role
                    ]
                    db.executemany(
                        "DELETE FROM model_calls WHERE suite_id = ? AND benchmark_id = ? "
                        "AND model_id = ? AND call_id = ?",
                        [
                            (suite_id, benchmark_id, model_id, call_id)
                            for call_id in stale_ids
                        ],
                    )
                db.executemany(
                    "INSERT OR REPLACE INTO model_calls VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            row["suite_id"], row["benchmark_id"], row["model_id"], str(row["call_id"]),
                            json.dumps(row, ensure_ascii=False),
                        )
                        for row in call_rows
                    ],
                )
            if metric_rows:
                db.executemany(
                    "INSERT OR REPLACE INTO metrics VALUES (?, ?, ?, ?, ?)",
                    [
                        (suite_id, benchmark_id, model_id, namespace, json.dumps(payload, ensure_ascii=False))
                        for suite_id, benchmark_id, model_id, namespace, payload in metric_rows
                    ],
                )
            if artifact_rows:
                db.executemany(
                    "INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?, ?)",
                    [
                        (suite_id, benchmark_id, model_id, kind, str(path))
                        for suite_id, benchmark_id, model_id, kind, path in artifact_rows
                    ],
                )

        if export_cases:
            self._export_payloads("cases", "cases.jsonl")
        if export_calls:
            self._export_payloads("model_calls", "model_calls.jsonl")
        if export_metrics:
            self._export_metrics()

    def set_job_status(
        self,
        suite_id: str,
        benchmark_id: str,
        model_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        from datetime import datetime, timezone

        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
                (suite_id, benchmark_id, model_id, status, error, datetime.now(timezone.utc).isoformat()),
            )

    def job_status(self, suite_id: str, benchmark_id: str, model_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM jobs WHERE suite_id = ? AND benchmark_id = ? AND model_id = ?",
                (suite_id, benchmark_id, model_id),
            ).fetchone()
        return row[0] if row else None

    def jobs(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT suite_id, benchmark_id, model_id, status, error, updated_at FROM jobs"
            ).fetchall()
        return [
            {
                "suite_id": suite_id,
                "benchmark_id": benchmark_id,
                "model_id": model_id,
                "status": status,
                "error": error,
                "updated_at": updated_at,
            }
            for suite_id, benchmark_id, model_id, status, error, updated_at in rows
        ]

    def cases(self) -> list[UniversalCase]:
        with self._connect() as db:
            return [UniversalCase.model_validate_json(row[0]) for row in db.execute("SELECT payload_json FROM cases")]

    def metrics(self) -> list[dict]:
        with self._connect() as db:
            return [
                {"suite_id": a, "benchmark_id": b, "model_id": c, "namespace": d, "metrics": json.loads(e)}
                for a, b, c, d, e in db.execute("SELECT * FROM metrics")
            ]

    def model_calls(self) -> list[dict]:
        with self._connect() as db:
            return [
                json.loads(payload)
                for (payload,) in db.execute("SELECT payload_json FROM model_calls")
            ]

    def _export_payloads(self, table: str, filename: str) -> None:
        with self._connect() as db, (self.universal_dir / filename).open("w", encoding="utf-8") as handle:
            for (payload,) in db.execute(f"SELECT payload_json FROM {table}"):
                handle.write(payload)
                handle.write("\n")

    def _export_metrics(self) -> None:
        path = self.universal_dir / "metrics.jsonl"
        with self._connect() as db, path.open("w", encoding="utf-8") as handle:
            rows = db.execute("SELECT suite_id, benchmark_id, model_id, namespace, payload_json FROM metrics")
            for suite_id, benchmark_id, model_id, namespace, payload_json in rows:
                row = {
                    "suite_id": suite_id,
                    "benchmark_id": benchmark_id,
                    "model_id": model_id,
                    "namespace": namespace,
                    "metrics": json.loads(payload_json),
                }
                handle.write(json.dumps(row, ensure_ascii=False))
                handle.write("\n")
