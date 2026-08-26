from __future__ import annotations

import asyncio
import json
import subprocess
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import typer

from .benchmarks import available_benchmarks
from .bootstrap import bootstrap_environment, find_project_root, torch_backend_summary
from .config import config_from_data
from .credentials import CredentialError, delete_credential, get_stored_credential, store_credential
from .interrupts import ConfirmedInterrupt, RunInterrupted
from .runner import run_experiment
from .ui import TerminalDisplay


app = typer.Typer(
    help="Run pinned third-party LLM security benchmarks with user-selected models",
    no_args_is_help=True,
)


_INCOMPLETE_SESSION_STATUSES = {"running", "partial", "failed", "cancelled"}


def _latest_incomplete_session(output_dir: Path) -> tuple[Path, dict] | None:
    """Return the latest suite only when that suite is unfinished."""

    if not output_dir.is_dir():
        return None
    sessions: list[tuple[datetime, Path, dict]] = []
    for manifest_path in output_dir.glob("*/suite_manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(str(manifest.get("created_at")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        sessions.append((created_at, manifest_path.parent, manifest))
    if not sessions:
        return None
    _, suite_dir, manifest = max(sessions, key=lambda item: item[0])
    if manifest.get("status") not in _INCOMPLETE_SESSION_STATUSES:
        return None
    return suite_dir, manifest


def _delete_incomplete_session(suite_dir: Path, output_dir: Path) -> None:
    resolved_suite = suite_dir.resolve()
    resolved_output = output_dir.resolve()
    if resolved_suite.parent != resolved_output or not (resolved_suite / "suite_manifest.json").is_file():
        raise ValueError(f"refusing to delete an invalid session path: {resolved_suite}")
    shutil.rmtree(resolved_suite)


def _resume_last_session(root: Path):
    """Prompt for the latest interrupted run before opening the normal wizard."""

    import questionary
    from questionary import Choice
    from .wizard import STYLE, _answer

    output_dir = root / "outputs"
    found = _latest_incomplete_session(output_dir)
    if found is None:
        return None
    suite_dir, manifest = found
    typer.echo(
        f"Incomplete session found: {suite_dir.name} "
        f"({manifest.get('status', 'unknown')})"
    )
    resume = _answer(questionary.select(
        "Do you want to continue the last session? If not, it will be deleted.",
        choices=[Choice("Yes", True), Choice("No", False)],
        default=True,
        style=STYLE,
        pointer="»",
        instruction="(use ↑/↓ and Enter)",
    ))
    if not resume:
        _delete_incomplete_session(suite_dir, output_dir)
        typer.echo(f"Deleted incomplete session: {suite_dir.name}")
        return None
    configuration = manifest.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError(f"session manifest has no valid configuration: {suite_dir}")
    deferred_mpib = any(
        isinstance(job, dict)
        and job.get("benchmark_id") == "mpib"
        and job.get("status") == "judging_deferred"
        for job in manifest.get("jobs", [])
    )
    if deferred_mpib:
        saved_judge = next(
            (
                selection.get("judge")
                for selection in configuration.get("benchmarks", [])
                if isinstance(selection, dict) and selection.get("id") == "mpib"
            ),
            None,
        )
        action = _answer(questionary.select(
            "MPIB has unfinished Judge evaluations. How should they be resumed?",
            choices=[
                Choice(
                    f"Continue with saved Judge: {(saved_judge or {}).get('model', 'unknown')}",
                    "continue",
                ),
                Choice(
                    "Select a new Judge and re-evaluate all saved Subject responses",
                    "replace",
                ),
            ],
            default="continue",
            style=STYLE,
            pointer="»",
            instruction="(use ↑/↓ and Enter)",
        ))
        if action == "replace":
            from .results import ResultStore
            from .wizard import configure_named_role_model

            pending: dict[str, str] = {}
            replacement = configure_named_role_model(root, pending, role="Judge")
            _store_credentials(pending)
            for selection in configuration.get("benchmarks", []):
                if isinstance(selection, dict) and selection.get("id") == "mpib":
                    selection["judge"] = replacement
            store = ResultStore(suite_dir)
            suite_id = str(manifest.get("suite_id") or suite_dir.name)
            for model in configuration.get("models", []):
                if isinstance(model, dict) and model.get("id"):
                    store.set_job_status(
                        suite_id, "mpib", str(model["id"]), "judging_deferred"
                    )
            manifest.setdefault("judge_model_changes", []).append({
                "changed_at": datetime.now(timezone.utc).isoformat(),
                "benchmark_id": "mpib",
                "previous_model": (saved_judge or {}).get("model"),
                "replacement_model": replacement.get("model"),
                "scope": "full_rejudge_from_saved_subject_responses",
            })
            manifest["configuration"] = configuration
            manifest["jobs"] = store.jobs()
            (suite_dir / "suite_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
    config = config_from_data(configuration, root)
    return config, (str(manifest.get("suite_id") or suite_dir.name), suite_dir)


def _require_complete_suite(suite_dir: Path) -> None:
    manifest = json.loads((suite_dir / "suite_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") == "complete":
        return
    errors = manifest.get("benchmark_errors") or []
    details = "; ".join(
        f"{item.get('benchmark_id', 'benchmark')}: {item.get('error', 'unknown error')}"
        for item in errors if isinstance(item, dict)
    )
    raise RuntimeError(details or str(manifest.get("error") or "suite did not complete"))


def _offer_judge_provider_change(root: Path, suite_dir: Path, *, plain: bool) -> None:
    """Offer a provider-only Judge reconfiguration after a sustained outage."""

    manifest_path = suite_dir / "suite_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return
    errors = manifest.get("benchmark_errors") or []
    timed_out_ids = {
        str(item.get("benchmark_id"))
        for item in errors
        if isinstance(item, dict)
        and "Judge provider unavailable" in str(item.get("error", ""))
    }
    if not timed_out_ids:
        return
    if plain:
        typer.echo(
            "The Judge provider was unavailable for 10 minutes. "
            "Run interactively to configure a replacement provider; the partial session is preserved."
        )
        return

    import questionary
    from questionary import Choice
    from .wizard import STYLE, _answer, configure_named_role_model

    change = _answer(questionary.select(
        "The Judge provider was unavailable for 10 minutes. Configure a replacement provider now?",
        choices=[Choice("Yes", True), Choice("No", False)],
        default=True,
        style=STYLE,
        pointer="»",
        instruction="(use ↑/↓ and Enter)",
    ))
    if not change:
        typer.echo("The partial session was preserved with its existing Judge configuration.")
        return

    configuration = manifest.get("configuration")
    if not isinstance(configuration, dict):
        typer.echo("Cannot update the Judge provider: the saved configuration is invalid.", err=True)
        return
    selections = configuration.get("benchmarks")
    if not isinstance(selections, list):
        typer.echo("Cannot update the Judge provider: no saved benchmark configuration was found.", err=True)
        return

    pending: dict[str, str] = {}
    changes: list[dict[str, object]] = []
    for selection in selections:
        if not isinstance(selection, dict) or str(selection.get("id")) not in timed_out_ids:
            continue
        previous = selection.get("judge")
        if not isinstance(previous, dict):
            continue
        replacement = configure_named_role_model(root, pending, role="Judge")
        if replacement.get("model") != previous.get("model"):
            typer.echo(
                "Judge model change rejected: resume must keep the frozen model "
                f"'{previous.get('model')}'. Configure that model through another provider.",
                err=True,
            )
            return
        selection["judge"] = replacement
        changes.append({
            "changed_at": datetime.now(timezone.utc).isoformat(),
            "benchmark_id": selection.get("id"),
            "model": previous.get("model"),
            "previous_adapter": previous.get("adapter"),
            "previous_base_url": previous.get("base_url"),
            "replacement_adapter": replacement.get("adapter"),
            "replacement_base_url": replacement.get("base_url"),
        })
    if not changes:
        typer.echo("No timed-out Judge configuration could be changed.", err=True)
        return
    _store_credentials(pending)
    manifest.setdefault("judge_provider_changes", []).extend(changes)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    typer.echo(
        "Replacement Judge provider saved. Run 'ragnarok run' again to resume the preserved session."
    )


@app.callback()
def main():
    """RAGnarok benchmark framework."""


@app.command("benchmarks")
def benchmarks_command():
    """List installed benchmarks and their validation status."""

    for benchmark in available_benchmarks():
        problems = [*benchmark.validate_installation(), *benchmark.validate_prepared()]
        status = "ready" if not problems else "setup required"
        typer.echo(f"{benchmark.info.name} [{status}]")
        typer.echo(f"  upstream: {benchmark.info.upstream_url}")
        typer.echo(f"  commit:   {benchmark.info.upstream_commit}")
        for problem in problems:
            typer.echo(f"  - {problem}")


@app.command("setup")
def setup_command(
    plain: bool = typer.Option(False, "--plain", help="Disable the interactive terminal UI."),
    workers: int = typer.Option(0, "--workers", min=0, max=8, help="Parallel benchmark preparation workers; 0 selects automatically."),
):
    """Install dependencies and prepare every benchmark for immediate execution."""

    display = TerminalDisplay("RAGnarok setup", plain=plain)
    root: Path | None = None
    setup_manifest: Path | None = None
    setup_record: dict[str, object] | None = None
    try:
        root = find_project_root(Path.cwd())
        benchmarks = available_benchmarks()
        requires_huggingface = any(
            getattr(item.info, "id", None) == "mpib" and bool(item.validate_prepared())
            for item in benchmarks
        )
        if requires_huggingface:
            if plain:
                try:
                    stored_token = get_stored_credential("huggingface")
                except CredentialError:
                    stored_token = None
                if not (os.environ.get("HF_TOKEN") or stored_token):
                    raise ValueError("MPIB gated access requires HF_TOKEN when --plain is used")
            else:
                token = typer.prompt(
                    "Hugging Face token for the accepted MPIB gated dataset",
                    hide_input=True,
                )
                if not token.startswith("hf_"):
                    raise ValueError("the Hugging Face token must start with 'hf_'")
                store_credential("huggingface", token)
                # Preparation runs in worker threads after dependency installation.
                # Keep this setup's token available independently of keyring reloads.
                os.environ["HF_TOKEN"] = token
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = root / ".ragnarok" / "logs" / f"setup-{stamp}.log"
        setup_manifest = root / ".ragnarok" / "setup_manifest.json"
        benchmark_ids = {
            id(benchmark): getattr(benchmark.info, "id", benchmark.info.name.lower().replace(" ", "_"))
            for benchmark in benchmarks
        }
        setup_record = {
            "framework": "RAGnarok",
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "log": str(log_path),
            "dependencies": {"status": "pending"},
            "benchmarks": {
                benchmark_ids[id(benchmark)]: {"name": benchmark.info.name, "status": "pending"}
                for benchmark in benchmarks
            },
        }
        with display:
            report = bootstrap_environment(
                root,
                benchmarks,
                progress=display.update,
                log_path=log_path,
            )
            setup_record["dependencies"] = {
                "status": "ready",
                "python_executable": report.python_executable,
                "extras": list(report.extras),
            }
            worker_count = workers or min(4, max(len(benchmarks), 1))

            def prepare_benchmark(benchmark):
                benchmark_id = getattr(benchmark.info, "id", benchmark.info.name)
                benchmark_log = log_path.with_name(f"{log_path.stem}-{benchmark_id}{log_path.suffix}")
                return benchmark.prepare(progress=display.update, log_path=benchmark_log)

            preparation_failures: list[str] = []
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ragnarok-setup") as executor:
                futures = {executor.submit(prepare_benchmark, benchmark): benchmark for benchmark in benchmarks}
                for index, future in enumerate(as_completed(futures), start=1):
                    benchmark = futures[future]
                    benchmark_record = setup_record["benchmarks"][benchmark_ids[id(benchmark)]]
                    try:
                        metadata = future.result()
                        problems = [*benchmark.validate_installation(), *benchmark.validate_prepared()]
                        if problems:
                            raise ValueError("; ".join(problems))
                    except Exception as exc:
                        message = f"{benchmark.info.name}: {type(exc).__name__}: {exc}"
                        preparation_failures.append(message)
                        benchmark_record.update({"status": "failed", "error": message})
                        benchmark_log = log_path.with_name(
                            f"{log_path.stem}-{benchmark_ids[id(benchmark)]}{log_path.suffix}"
                        )
                        benchmark_log.parent.mkdir(parents=True, exist_ok=True)
                        with benchmark_log.open("a", encoding="utf-8") as handle:
                            handle.write(f"\n[setup error] {message}\n")
                        display.update("benchmark", index, len(benchmarks), f"Failed {benchmark.info.name}")
                    else:
                        benchmark_record.update({"status": "ready", "metadata": metadata})
                        display.update("benchmark", index, len(benchmarks), f"Prepared {benchmark.info.name}")

            failures = []
            for benchmark in benchmarks:
                failures.extend(f"{benchmark.info.name}: {problem}" for problem in benchmark.validate_installation())
                failures.extend(f"{benchmark.info.name}: {problem}" for problem in benchmark.validate_prepared())
            failures = list(dict.fromkeys([*preparation_failures, *failures]))
            if failures:
                raise ValueError("setup incomplete:\n  - " + "\n  - ".join(failures))
            mpib_metadata = setup_record["benchmarks"].get("mpib", {}).get("metadata", {})
            reconstruction_mode = mpib_metadata.get("reconstruction_mode") if isinstance(mpib_metadata, dict) else None
            reconstruction_note = f" · MPIB V2 {reconstruction_mode}" if reconstruction_mode else ""
            setup_record.update({
                "status": "ready",
                "error": None,
                "python_executable": report.python_executable,
                "backend": torch_backend_summary(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            setup_manifest.parent.mkdir(parents=True, exist_ok=True)
            setup_manifest.write_text(json.dumps(setup_record, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
            display.finish(
                f"Ready · backend {setup_record['backend']}{reconstruction_note} · "
                f"manifest {setup_manifest} · log {log_path}"
            )
    except Exception as exc:
        if setup_record is not None and setup_manifest is not None:
            dependencies = setup_record.get("dependencies")
            if isinstance(dependencies, dict) and dependencies.get("status") == "pending":
                dependencies.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            setup_record.update({
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            setup_manifest.parent.mkdir(parents=True, exist_ok=True)
            setup_manifest.write_text(json.dumps(setup_record, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        typer.echo(f"Setup failed: {exc}", err=True)
        raise typer.Exit(1) from None


def _store_credentials(credentials: dict[str, str]) -> None:
    previous: dict[str, str | None] = {}
    try:
        for credential_id, secret in credentials.items():
            previous[credential_id] = get_stored_credential(credential_id)
            store_credential(credential_id, secret)
    except Exception:
        for credential_id, old_secret in previous.items():
            if old_secret is None:
                delete_credential(credential_id)
            else:
                store_credential(credential_id, old_secret)
        raise


@app.command("run")
def run_command(plain: bool = typer.Option(False, "--plain", help="Disable the interactive terminal UI.")):
    """Choose one or more benchmarks and run them with one or more models."""

    from .wizard import RunCancelled, run_configuration_wizard

    utc_started = datetime.now(timezone.utc)
    local_started = datetime.now().astimezone()
    execution_start = {
        "started_at_utc": utc_started.isoformat(),
        "started_at_local": local_started.isoformat(),
        "local_timezone": local_started.tzname(),
        "utc_offset": f"{local_started.strftime('%z')[:3]}:{local_started.strftime('%z')[3:]}",
    }
    root = Path.cwd().resolve()
    try:
        resumed = _resume_last_session(root)
        if resumed is None:
            configuration, credentials = run_configuration_wizard(root)
            _store_credentials(credentials)
            config = config_from_data(configuration, root)
            suite = None
            resume = False
        else:
            config, suite = resumed
            resume = True
    except RunCancelled:
        typer.echo("Run cancelled. No prompts were processed.")
        raise typer.Exit(1)

    selected_names = [selection.id for selection in config.benchmarks]
    display = TerminalDisplay("RAGnarok · " + " + ".join(selected_names), plain=plain)
    result_suite: Path | None = None
    try:
        with display:
            with ConfirmedInterrupt(display) as interrupt:
                outputs = asyncio.run(interrupt.run(run_experiment(
                    config,
                    progress=interrupt.progress,
                    suite=suite,
                    resume=resume,
                    execution_start=execution_start,
                    preflight=True,
                    warm_models=True,
                )))
            result_suite = outputs[0]
            _require_complete_suite(outputs[0])
            reports = [path for path in outputs if path.suffix.lower() == ".xlsx"]
            note = f"Results saved in:\n{outputs[0]}"
            if reports:
                note += f"\nMain report:\n{reports[0]}"
            display.finish(note)
    except RunInterrupted:
        typer.echo("Run stopped safely after confirmation. Partial artifacts were preserved.")
        raise typer.Exit(130)
    except (OSError, RuntimeError, ValueError) as exc:
        display.fail(str(exc))
        if result_suite is None:
            latest = _latest_incomplete_session(config.output_dir)
            if latest is not None:
                result_suite = latest[0]
        if result_suite is not None:
            _offer_judge_provider_change(root, result_suite, plain=plain)
        raise typer.Exit(1) from exc


@app.command("auto")
def auto_command(
    file: Path = typer.Option(Path("automation.toml"), "--file", "-f", help="Automation file to execute."),
    plain: bool = typer.Option(False, "--plain", help="Disable the interactive terminal UI."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate the plan without downloading or evaluating."),
):
    """Run a serial Subject queue with bounded background model prefetch."""

    from .automation import load_automation, run_automation

    root = Path.cwd().resolve()
    path = file if file.is_absolute() else root / file
    display = TerminalDisplay("RAGnarok · automation", plain=plain)
    try:
        configuration = load_automation(path, root)
        with display:
            with ConfirmedInterrupt(display) as interrupt:
                result_dir = asyncio.run(interrupt.run(
                    run_automation(configuration, progress=interrupt.progress, dry_run=dry_run)
                ))
            report = result_dir / "report.xlsx"
            note = f"Results saved in:\n{result_dir}"
            if report.is_file():
                note += f"\nMain report:\n{report}"
            display.finish(note)
    except RunInterrupted:
        typer.echo("Automation stopped safely after confirmation. Completed checkpoints were preserved.")
        raise typer.Exit(130)
    except (FileNotFoundError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        display.fail(str(exc))
        raise typer.Exit(1) from exc


@app.command("report")
def report_command(
    run: list[Path] | None = typer.Option(
        None,
        "--run",
        help="Result directory to include. Repeat for non-interactive multi-run reports.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Destination directory. Defaults to outputs/reports/<generated-id>.",
    ),
):
    """Select completed runs and generate one single or comparative PDF report."""

    from .pdf_report import default_report_directory, discover_report_runs, generate_pdf_report_bundle

    root = find_project_root(Path.cwd())
    output_root = root / "outputs"
    available = discover_report_runs(output_root)
    if not available:
        typer.echo("Report failed: no result runs with normalized cases were found.", err=True)
        raise typer.Exit(1)

    try:
        if run:
            by_path = {item.path.resolve(): item for item in available}
            by_name = {item.path.name: item for item in available}
            selected = []
            for requested in run:
                candidate = requested if requested.is_absolute() else output_root / requested
                item = by_path.get(candidate.resolve()) or by_name.get(str(requested))
                if item is None:
                    raise ValueError(f"result run was not found or has no normalized cases: {requested}")
                if item not in selected:
                    selected.append(item)
        else:
            import questionary
            from questionary import Choice
            from .wizard import STYLE, _answer

            choices = [Choice(item.label, item) for item in available]
            selected = _answer(questionary.checkbox(
                "Select one or more result runs for the PDF report",
                choices=choices,
                style=STYLE,
                instruction="(Space selects, Enter confirms)",
            ))
            if not selected:
                raise ValueError("select at least one result run")

        destination = output if output is not None else default_report_directory(output_root, selected)
        if not destination.is_absolute():
            destination = (root / destination).resolve()
        pdf_path, csv_path, manifest_path = generate_pdf_report_bundle(selected, destination)
        typer.echo("Completed")
        typer.echo(f"PDF report: {pdf_path}")
        typer.echo(f"Combined cases: {csv_path}")
        typer.echo(f"Report manifest: {manifest_path}")
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"Report failed: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("preflight")
def preflight_command(
    file: Path = typer.Option(Path("automation.toml"), "--file", "-f", help="Automation file to validate."),
):
    """Validate disk, runtime, accelerator, Ollama, and benchmark readiness."""

    from .automation import load_automation
    from .cloud import cloud_preflight

    root = Path.cwd().resolve()
    path = file if file.is_absolute() else root / file
    try:
        report = cloud_preflight(load_automation(path, root))
        for check in report["checks"]:
            marker = "OK" if check["ok"] else ("WARN" if not check["required"] else "FAIL")
            typer.echo(f"[{marker}] {check['name']}: {check['detail']}")
        if not report["ready"]:
            raise typer.Exit(1)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"Preflight failed: {exc}", err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
