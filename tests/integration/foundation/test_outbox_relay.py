"""Outbox relay loop: poll cadence and failure survival (Task 5).

The relay is the dispatch half of the job pipeline; without it, committed
outbox rows queue forever (found live in Checkpoint B). The loop runs
against fakes: the live stack's dispatcher consumes the shared outbox
within one poll period, so end-to-end assertions would race it. The
commit->publish->mark composition is covered by the dispatcher tests.
"""

from __future__ import annotations

import asyncio

from apps.worker import relay


def test_relay_loop_polls_and_survives_failures(monkeypatch) -> None:
    published: list[str] = []

    class FlakyDispatcher:
        calls = 0

        def __init__(self, engine, broker) -> None:
            self.broker = broker

        async def relay(self) -> int:
            FlakyDispatcher.calls += 1
            if FlakyDispatcher.calls == 1:
                raise RuntimeError("broker down")
            self.broker.append(f"tick-{FlakyDispatcher.calls}")
            published.append(f"tick-{FlakyDispatcher.calls}")
            return 1

    monkeypatch.setattr(relay, "JobDispatcher", FlakyDispatcher)

    async def scenario() -> None:
        task = asyncio.create_task(relay._loop(None, []))
        await asyncio.sleep(relay.POLL_SECONDS * 4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    # The first tick failed, the loop stayed alive, and later ticks published.
    assert FlakyDispatcher.calls >= 3
    assert published
