from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text


def supports_live_ui(*, plain: bool = False) -> bool:
    if plain or not sys.stdout.isatty():
        return False
    if os.environ.get("CI") or os.environ.get("TERM", "").lower() == "dumb" or "NO_COLOR" in os.environ:
        return False
    return True


@dataclass
class DisplayState:
    phase: str = "Starting"
    detail: str = "Preparing"
    current: int = 0
    total: int | None = None
    note: str = ""
    tokens_per_second: float | None = None
    eta_seconds: float | None = None
    eta_label: str = "ETA"
    interrupt_notice: str = ""


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    value = max(0, round(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class TerminalDisplay:
    """Small auto-adapting dashboard for local terminals, SSH, pipes, and CI."""

    def __init__(self, title: str, *, plain: bool = False):
        self.title = title
        self.interactive = supports_live_ui(plain=plain)
        # Pin the original stream so third-party stdout redirection cannot capture the dashboard.
        self.console = Console(file=sys.stdout, no_color=not self.interactive, force_terminal=self.interactive)
        self.state = DisplayState()
        self._live: Live | None = None
        self._last_plain: str | None = None
        self._last_size: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def __enter__(self):
        if self.interactive:
            self.console.clear()
            self._last_size = (self.console.width, self.console.height)
            self._live = Live(
                self._render(),
                console=self.console,
                screen=True,
                auto_refresh=False,
                transient=False,
                redirect_stdout=False,
                redirect_stderr=False,
                vertical_overflow="crop",
            )
            self._live.start(refresh=True)
        else:
            self.console.print(f"{self.title}: starting", markup=False)
        return self

    def __exit__(self, exc_type, _exc, _traceback):
        if self._live is not None:
            if exc_type is not None:
                self.state.phase = "Error"
            self._live.update(self._render(), refresh=True)
            self._live.stop()
            self._live = None
            if exc_type is None and self.state.note:
                self.console.print(f"Completed\n{self.state.note}", style="green", markup=False)

    def update(
        self,
        phase: str,
        current: int,
        total: int | None,
        detail: str,
        stats: dict[str, object] | None = None,
    ) -> None:
        with self._lock:
            stats = stats or {}
            self.state = DisplayState(
                phase=phase,
                detail=detail,
                current=current,
                total=total,
                note=self.state.note,
                tokens_per_second=stats.get("tokens_per_second"),
                eta_seconds=stats.get("eta_seconds"),
                eta_label=str(stats.get("eta_label", "ETA")),
                interrupt_notice=self.state.interrupt_notice,
            )
            if self._live is not None:
                current_size = (self.console.width, self.console.height)
                if current_size != self._last_size:
                    self._last_size = current_size
                self._live.update(self._render(), refresh=True)
                return
            if phase != self._last_plain:
                self.console.print(f"[{phase}] {detail}", markup=False)
                self._last_plain = phase

    def set_interrupt_notice(self, message: str) -> None:
        with self._lock:
            previous = self.state.interrupt_notice
            self.state.interrupt_notice = message
            if self._live is not None:
                self._live.update(self._render(), refresh=True)
            elif message and message != previous:
                self.console.print(f"[exit] {message}", markup=False)

    def finish(self, note: str) -> None:
        self.state.phase = "Completed"
        self.state.detail = "Operation completed"
        self.state.note = note
        if self._live is not None:
            self._live.update(self._render(), refresh=True)
        else:
            self.console.print(f"Completed\n{note}", markup=False)

    def fail(self, message: str) -> None:
        self.state.phase = "Error"
        self.state.detail = f"Error: {message}"
        self.state.note = ""
        if self._live is not None:
            self._live.update(self._render(), refresh=True)
        else:
            self.console.print(f"Error: {message}", style="red", markup=False)

    def _render(self):
        table = Table.grid(expand=True)
        table.add_column(style="cyan", width=12)
        table.add_column()
        table.add_row("Phase", self.state.phase)
        table.add_row("Activity", self.state.detail)
        speed = "-" if self.state.tokens_per_second is None else f"{self.state.tokens_per_second:.1f} tokens/s"
        table.add_row("Generation", speed)
        table.add_row(self.state.eta_label, _format_duration(self.state.eta_seconds))
        if self.state.total is not None:
            total = max(self.state.total, 1)
            table.add_row("Progress", f"{self.state.current}/{self.state.total}")
            bar = ProgressBar(total=total, completed=min(self.state.current, total), width=None)
        else:
            table.add_row("Progress", "Running")
            bar = Text("Running...", style="yellow")
        content = [table, bar]
        if self.state.interrupt_notice:
            content.append(Text(self.state.interrupt_notice, style="bold yellow"))
        if self.state.note:
            content.append(Text(self.state.note, style="green"))
        return Panel(Group(*content), title=self.title, border_style="blue")
