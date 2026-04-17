from __future__ import annotations

import asyncio
import time

from server.playout_sync import SessionPlayoutGate, SessionPresentationClock, video_due_pts_for_elapsed


def test_playout_gate_shares_one_start_across_waiters() -> None:
    async def scenario() -> None:
        gate = SessionPlayoutGate(startup_guard_sec=0.02)
        await gate.reset("session-1")

        ready_waiter = asyncio.create_task(gate.wait_until_ready("session-1", timeout=1.0))
        start_waiter = asyncio.create_task(gate.wait_for_start("session-1", timeout=1.0))
        await asyncio.sleep(0)

        before_start = time.monotonic()
        assert await gate.mark_audio_ready("session-1") is True
        assert await gate.mark_video_ready("session-1") is True
        started_at = await gate.ensure_started("session-1")

        assert await ready_waiter is True
        assert await start_waiter == started_at
        assert started_at is not None
        assert started_at >= before_start
        assert started_at < before_start + 0.25

    asyncio.run(scenario())


def test_playout_gate_resets_and_stops_old_session_waiters() -> None:
    async def scenario() -> None:
        gate = SessionPlayoutGate()
        await gate.reset("session-1")

        stale_ready_waiter = asyncio.create_task(gate.wait_until_ready("session-1", timeout=1.0))
        stale_start_waiter = asyncio.create_task(gate.wait_for_start("session-1", timeout=1.0))
        await asyncio.sleep(0)

        await gate.reset("session-2")
        assert await stale_ready_waiter is False
        assert await stale_start_waiter is None

        assert await gate.mark_audio_ready("session-2") is True
        assert await gate.mark_video_ready("session-2") is True
        pending_start_waiter = asyncio.create_task(gate.wait_for_start("session-2", timeout=1.0))
        await asyncio.sleep(0)
        await gate.stop()
        assert await pending_start_waiter is None

        await gate.reset("session-3")
        assert await gate.mark_audio_ready("session-3") is True
        assert await gate.mark_video_ready("session-3") is True
        session_3_start = await gate.ensure_started("session-3")
        assert session_3_start is not None

    asyncio.run(scenario())


def test_playout_gate_requires_audio_and_video_ready() -> None:
    async def scenario() -> None:
        gate = SessionPlayoutGate()
        await gate.reset("session-1")

        ready_waiter = asyncio.create_task(gate.wait_until_ready("session-1", timeout=0.05))
        await asyncio.sleep(0)
        assert await gate.mark_audio_ready("session-1") is True
        assert await ready_waiter is False

        ready_waiter = asyncio.create_task(gate.wait_until_ready("session-1", timeout=1.0))
        await asyncio.sleep(0)
        assert await gate.mark_video_ready("session-1") is True
        assert await ready_waiter is True

    asyncio.run(scenario())


def test_session_presentation_clock_projects_video_pts_from_audio_timeline() -> None:
    clock = SessionPresentationClock(sample_rate=16_000, fps=25)

    first_audio_start = clock.reserve_audio_samples(2_560)
    first_pts = clock.assign_video_pts(
        audio_start_sample=first_audio_start,
        audio_sample_count=2_560,
        frame_count=4,
    )
    assert first_audio_start == 0
    assert first_pts == [0, 1, 2, 3]

    second_audio_start = clock.reserve_audio_samples(1_920)
    second_pts = clock.assign_video_pts(
        audio_start_sample=second_audio_start,
        audio_sample_count=1_920,
        frame_count=3,
    )
    assert second_audio_start == 2_560
    assert second_pts == [4, 5, 6]


def test_session_presentation_clock_keeps_video_pts_monotonic_when_rounding_overlaps() -> None:
    clock = SessionPresentationClock(sample_rate=16_000, fps=25)

    audio_start = clock.reserve_audio_samples(2_560)
    pts = clock.assign_video_pts(
        audio_start_sample=audio_start,
        audio_sample_count=2_560,
        frame_count=5,
    )

    assert pts == [0, 1, 2, 3, 4]


def test_video_due_pts_for_elapsed_applies_small_due_window() -> None:
    assert video_due_pts_for_elapsed(elapsed_sec=0.000, fps=25, due_slack_sec=0.005) == 0
    assert video_due_pts_for_elapsed(elapsed_sec=0.034, fps=25, due_slack_sec=0.005) == 0
    assert video_due_pts_for_elapsed(elapsed_sec=0.036, fps=25, due_slack_sec=0.005) == 1
    assert video_due_pts_for_elapsed(elapsed_sec=0.076, fps=25, due_slack_sec=0.005) == 2
