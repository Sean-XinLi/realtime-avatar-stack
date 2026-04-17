from __future__ import annotations

import asyncio
import time


class SessionPresentationClock:
    """Project per-batch audio duration onto one shared session timeline."""

    def __init__(self, sample_rate: int, fps: int) -> None:
        self.sample_rate = max(1, int(sample_rate))
        self.fps = max(1, int(fps))
        self._audio_samples_emitted = 0
        self._last_video_pts = -1

    def reserve_audio_samples(self, sample_count: int) -> int:
        safe_count = max(0, int(sample_count))
        start = self._audio_samples_emitted
        self._audio_samples_emitted += safe_count
        return start

    def assign_video_pts(
        self,
        *,
        audio_start_sample: int,
        audio_sample_count: int,
        frame_count: int,
    ) -> list[int]:
        safe_frame_count = max(0, int(frame_count))
        safe_audio_start = max(0, int(audio_start_sample))
        safe_audio_count = max(0, int(audio_sample_count))
        if safe_frame_count <= 0:
            return []

        if safe_audio_count <= 0:
            start_pts = self._last_video_pts + 1
            assigned = [start_pts + index for index in range(safe_frame_count)]
            self._last_video_pts = assigned[-1]
            return assigned

        assigned: list[int] = []
        step = safe_audio_count / safe_frame_count
        for index in range(safe_frame_count):
            sample_index = safe_audio_start + (index * step)
            pts = int(round(sample_index * self.fps / self.sample_rate))
            if pts <= self._last_video_pts:
                pts = self._last_video_pts + 1
            assigned.append(pts)
            self._last_video_pts = pts
        return assigned


def video_due_pts_for_elapsed(*, elapsed_sec: float, fps: int, due_slack_sec: float = 0.0) -> int:
    safe_fps = max(1, int(fps))
    safe_elapsed = max(0.0, float(elapsed_sec))
    safe_slack = max(0.0, float(due_slack_sec))
    return int((safe_elapsed + safe_slack) * safe_fps)


class SessionPlayoutGate:
    """Coordinate a shared playout start for audio/video within one session."""

    def __init__(self, startup_guard_sec: float = 0.0) -> None:
        self.startup_guard_sec = max(0.0, float(startup_guard_sec))
        self._condition = asyncio.Condition()
        self._session_id: str | None = None
        self._audio_ready = False
        self._video_ready = False
        self._start_monotonic: float | None = None

    async def reset(self, session_id: str) -> None:
        async with self._condition:
            self._session_id = session_id
            self._audio_ready = False
            self._video_ready = False
            self._start_monotonic = None
            self._condition.notify_all()

    async def stop(self) -> None:
        async with self._condition:
            self._session_id = None
            self._audio_ready = False
            self._video_ready = False
            self._start_monotonic = None
            self._condition.notify_all()

    async def mark_audio_ready(self, session_id: str) -> bool:
        async with self._condition:
            if self._session_id != session_id:
                return False
            if self._audio_ready:
                return True
            self._audio_ready = True
            self._condition.notify_all()
            return True

    async def mark_video_ready(self, session_id: str) -> bool:
        async with self._condition:
            if self._session_id != session_id:
                return False
            if self._video_ready:
                return True
            self._video_ready = True
            self._condition.notify_all()
            return True

    async def mark_ready(self, session_id: str) -> bool:
        async with self._condition:
            if self._session_id != session_id:
                return False
            self._audio_ready = True
            self._video_ready = True
            self._condition.notify_all()
            return True

    def _is_ready_locked(self) -> bool:
        return self._audio_ready and self._video_ready

    async def wait_until_ready(self, session_id: str, timeout: float | None = None) -> bool:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(timeout, 0.0)
        async with self._condition:
            while True:
                if self._session_id != session_id:
                    return False
                if self._is_ready_locked():
                    return True
                if deadline is None:
                    await self._condition.wait()
                    continue
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return False

    async def ensure_started(self, session_id: str) -> float | None:
        async with self._condition:
            if self._session_id != session_id or not self._is_ready_locked():
                return None
            if self._start_monotonic is None:
                self._start_monotonic = time.monotonic() + self.startup_guard_sec
                self._condition.notify_all()
            return self._start_monotonic

    async def wait_for_start(self, session_id: str, timeout: float | None = None) -> float | None:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(timeout, 0.0)
        async with self._condition:
            while True:
                if self._session_id != session_id:
                    return None
                if self._start_monotonic is not None:
                    return self._start_monotonic
                if deadline is None:
                    await self._condition.wait()
                    continue
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return None
