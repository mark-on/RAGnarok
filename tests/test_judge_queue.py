import asyncio

import pytest

from ragnarok.benchmarks._judge_queue import (
    AdaptiveJudgePump,
    BufferedJudgeQueue,
    DiskJudgeQueue,
    JudgePumpFatalError,
)


def _item(index: int) -> dict:
    return {
        "item_key": f"case-{index}",
        "sequence": index,
        "payload": {"case_id": f"case-{index}"},
    }


@pytest.mark.asyncio
async def test_buffer_uses_ram_then_spills_to_disk_in_fifo_order(tmp_path):
    disk = DiskJudgeQueue(tmp_path / "judge_queue.sqlite")
    queue = BufferedJudgeQueue(disk, memory_limit=2)

    await queue.put(_item(1))
    await queue.put(_item(2))
    await queue.put(_item(3))

    assert queue.memory.qsize() == 2
    assert disk.pending_count() == 1
    first = await queue.get("worker")
    second = await queue.get("worker")
    third = await queue.get("worker")
    assert [first["sequence"], second["sequence"], third["sequence"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_flush_and_expired_lease_preserve_every_item(tmp_path):
    disk = DiskJudgeQueue(tmp_path / "judge_queue.sqlite")
    queue = BufferedJudgeQueue(disk, memory_limit=10)
    await queue.put(_item(1))
    await queue.put(_item(2))
    await queue.flush()

    leased = disk.lease_next("dead-worker", lease_seconds=-1)
    assert leased["item_key"] == "case-1"
    recovered = disk.lease_next("replacement")
    assert recovered["item_key"] == "case-1"
    disk.complete("case-1")
    assert disk.pending_count() == 1


def test_queue_is_idempotent_and_never_reopens_completed_items(tmp_path):
    disk = DiskJudgeQueue(tmp_path / "judge_queue.sqlite")
    disk.enqueue("case-1", 1, {"value": "first"})
    item = disk.lease_next("worker")
    disk.complete(item["item_key"])
    disk.enqueue("case-1", 1, {"value": "duplicate"})

    assert disk.pending_count() == 0
    assert disk.counts()["completed"] == 1


@pytest.mark.asyncio
async def test_transient_outage_defers_without_losing_the_queue(tmp_path):
    queue = BufferedJudgeQueue(DiskJudgeQueue(tmp_path / "queue.sqlite"))

    async def unavailable(_payload):
        raise ConnectionError("offline")

    pump = AdaptiveJudgePump(
        queue,
        unavailable,
        lambda _item, _result: asyncio.sleep(0),
        lambda _error: "transient",
        outage_seconds=0.02,
        heartbeat_seconds=0.01,
        retry_ceiling_seconds=0.01,
    )
    await queue.put(_item(1))
    pump.finish_producing()

    assert await asyncio.wait_for(pump.wait_until_releasable(), timeout=1) == "judging_deferred"
    await pump.close()
    assert queue.disk.pending_count() == 1


@pytest.mark.asyncio
async def test_repeated_404_becomes_fatal_after_heartbeat_window(tmp_path):
    queue = BufferedJudgeQueue(DiskJudgeQueue(tmp_path / "queue.sqlite"))

    async def missing(_payload):
        raise RuntimeError("HTTP 404")

    pump = AdaptiveJudgePump(
        queue,
        missing,
        lambda _item, _result: asyncio.sleep(0),
        lambda _error: "not_found",
        outage_seconds=1,
        not_found_seconds=0.03,
        heartbeat_seconds=0.01,
    )
    await queue.put(_item(1))
    pump.finish_producing()

    with pytest.raises(JudgePumpFatalError, match="404"):
        await asyncio.wait_for(pump.wait_until_releasable(), timeout=1)
    await pump.close()
