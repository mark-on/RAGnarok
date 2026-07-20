from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .config import config_from_data
from .credentials import delete_credential, get_stored_credential, store_credential
from .runner import run_experiment


app = typer.Typer(
    help="Run CSV prompts through one local RAG pipeline",
    no_args_is_help=True,
)


@app.callback()
def main():
    """RAGnarok command line."""


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
def run_command():
    """Choose models and process every prompt in dataset/dataset.csv."""

    from .wizard import RunCancelled, run_configuration_wizard

    root = Path.cwd().resolve()
    try:
        configuration, credentials = run_configuration_wizard(root)
    except RunCancelled:
        typer.echo("Run cancelled. No prompts were processed.")
        raise typer.Exit(1)
    _store_credentials(credentials)
    config = config_from_data(configuration, root)

    console = Console(stderr=False)
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        refresh_per_second=10,
    ) as live:
        task = live.add_task("Preparing", total=None)

        def update(_phase: str, current: int, total: int | None, detail: str) -> None:
            values = {"description": detail, "completed": current}
            if total is not None:
                values["total"] = total
            live.update(task, **values)

        try:
            outputs = asyncio.run(run_experiment(config, progress=update))
        except (OSError, RuntimeError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from exc

    typer.echo("Completed response files:")
    for path in outputs:
        typer.echo(f"  {path}")


@app.command("talk")
def talk_command():
    """Choose one model and chat through the same RAG pipeline."""

    from .talk import run_talk_terminal
    from .wizard import RunCancelled, talk_configuration_wizard

    root = Path.cwd().resolve()
    try:
        configuration, credentials = talk_configuration_wizard(root)
    except RunCancelled:
        typer.echo("Chat cancelled.")
        raise typer.Exit(1)
    _store_credentials(credentials)
    config = config_from_data(configuration, root)
    try:
        run_talk_terminal(config)
    except (OSError, RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


if __name__ == "__main__":
    app()
