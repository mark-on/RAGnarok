import io

import ragnarok.ui as ui


class _TTY(io.StringIO):
    def isatty(self):
        return True


def test_live_ui_disables_itself_for_server_logs(monkeypatch):
    monkeypatch.setattr(ui.sys, "stdout", io.StringIO())
    assert ui.supports_live_ui() is False


def test_no_color_disables_live_ui_even_on_tty(monkeypatch):
    monkeypatch.setattr(ui.sys, "stdout", _TTY())
    monkeypatch.setenv("NO_COLOR", "1")
    assert ui.supports_live_ui() is False


def test_plain_display_has_no_ansi_sequences(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(ui.sys, "stdout", output)
    with ui.TerminalDisplay("Test", plain=True) as display:
        display.update("download", 1, 3, "Dataset")
        display.finish("Ready")
    assert "\x1b[" not in output.getvalue()
    assert "[download] Dataset" in output.getvalue()
    assert "Completed\nReady" in output.getvalue()


def test_plain_display_prints_explicit_error(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(ui.sys, "stdout", output)
    display = ui.TerminalDisplay("Test", plain=True)
    with display:
        display.update("inference", 1, 3, "Case 1")
    display.fail("connection lost")
    assert "Error: connection lost" in output.getvalue()


def test_inference_stats_include_generation_speed_and_eta(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(ui.sys, "stdout", output)
    with ui.TerminalDisplay("Test", plain=True) as display:
        display.update(
            "inference",
            25,
            300,
            "Case 25",
            {"tokens_per_second": 12.75, "eta_seconds": 3723},
        )
        assert display.state.tokens_per_second == 12.75
        assert display.state.eta_seconds == 3723
        assert display.state.eta_label == "ETA"
    assert ui._format_duration(3723) == "01:02:03"


def test_display_accepts_suite_eta_label(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(ui.sys, "stdout", output)
    with ui.TerminalDisplay("Test", plain=True) as display:
        display.update("inference", 1, 10, "Case 1", {"eta_seconds": 90, "eta_label": "Suite ETA"})
        assert display.state.eta_label == "Suite ETA"


def test_interrupt_notice_is_rendered_without_ansi_in_plain_mode(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(ui.sys, "stdout", output)
    with ui.TerminalDisplay("Test", plain=True) as display:
        display.set_interrupt_notice("Press Ctrl+C again to confirm exit.")
    assert "[exit] Press Ctrl+C again to confirm exit." in output.getvalue()
    assert "\x1b[" not in output.getvalue()


def test_interactive_display_uses_one_synchronous_alternate_screen(monkeypatch):
    calls = {}

    class FakeLive:
        def __init__(self, renderable, **kwargs):
            calls["kwargs"] = kwargs

        def start(self, *, refresh=False):
            calls["start_refresh"] = refresh

        def update(self, _renderable, *, refresh=False):
            calls.setdefault("updates", []).append(refresh)

        def stop(self):
            calls["stopped"] = True

    monkeypatch.setattr(ui.sys, "stdout", _TTY())
    monkeypatch.setattr(ui, "supports_live_ui", lambda **_kwargs: True)
    monkeypatch.setattr(ui, "Live", FakeLive)
    with ui.TerminalDisplay("Test") as display:
        display.update("inference", 1, 300, "Case 1")
        display.finish("Ready")

    assert calls["kwargs"]["screen"] is True
    assert calls["kwargs"]["auto_refresh"] is False
    assert calls["kwargs"]["redirect_stdout"] is False
    assert calls["start_refresh"] is True
    assert all(calls["updates"])
    assert calls["stopped"] is True
