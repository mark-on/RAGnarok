import asyncio

import pytest

import ragnarok.interrupts as interrupts


class FakeDisplay:
    def __init__(self):
        self.notices = []
        self.updates = []

    def set_interrupt_notice(self, message):
        self.notices.append(message)

    def update(self, *args, **kwargs):
        self.updates.append((args, kwargs))


def test_first_interrupt_requests_confirmation_and_second_confirms(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(interrupts.time, "monotonic", lambda: now[0])
    display = FakeDisplay()
    controller = interrupts.ConfirmedInterrupt(display, confirmation_seconds=5)

    controller._handle_interrupt(2, None)
    controller._refresh_notice()
    assert controller.confirmed is False
    assert "Press Ctrl+C again" in display.notices[-1]

    now[0] = 102.0
    controller._handle_interrupt(2, None)
    controller._refresh_notice()
    assert controller.confirmed is True
    assert "Stop confirmed" in display.notices[-1]
    with pytest.raises(interrupts.RunInterrupted):
        controller.progress("inference", 1, 2, "case")


def test_confirmation_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(interrupts.time, "monotonic", lambda: now[0])
    display = FakeDisplay()
    controller = interrupts.ConfirmedInterrupt(display, confirmation_seconds=5)
    controller._handle_interrupt(2, None)
    controller._refresh_notice()

    now[0] = 106.0
    controller._refresh_notice()
    assert controller.confirmed is False
    assert display.notices[-1] == ""


@pytest.mark.asyncio
async def test_operation_continues_after_unconfirmed_interrupt(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(interrupts.time, "monotonic", lambda: now[0])
    display = FakeDisplay()
    controller = interrupts.ConfirmedInterrupt(display)
    controller._handle_interrupt(2, None)

    async def operation():
        await asyncio.sleep(0)
        return "complete"

    assert await controller.run(operation()) == "complete"
