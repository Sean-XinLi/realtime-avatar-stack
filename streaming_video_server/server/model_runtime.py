from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np
from PIL import Image, ImageDraw

from .config import ServerConfig
from .playout_sync import SessionPresentationClock

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class InferParams:
    sample_rate: int
    tgt_fps: int
    frame_num: int
    motion_frames_num: int
    cached_audio_duration: int

    @property
    def slice_len(self) -> int:
        return self.frame_num - self.motion_frames_num

    @property
    def slice_samples(self) -> int:
        return self.slice_len * self.sample_rate // self.tgt_fps

    @property
    def cached_samples(self) -> int:
        return self.sample_rate * self.cached_audio_duration

    @property
    def audio_end_idx(self) -> int:
        return self.cached_audio_duration * self.tgt_fps

    @property
    def audio_start_idx(self) -> int:
        return self.audio_end_idx - self.frame_num


def compute_startup_min_samples(
    params: InferParams,
    input_chunk_samples: int,
    config: ServerConfig,
) -> int:
    floor_samples = params.sample_rate * config.startup_min_audio_floor_ms // 1000
    ratio_samples = int(round(params.slice_samples * config.startup_partial_ratio))
    return min(
        params.slice_samples,
        max(input_chunk_samples, floor_samples, ratio_samples),
    )


def compute_dispatch_step_samples(
    params: InferParams,
    input_chunk_samples: int,
    config: ServerConfig,
) -> int:
    if config.inference_step_ms <= 0:
        return params.slice_samples
    requested_samples = params.sample_rate * config.inference_step_ms // 1000
    return min(
        params.slice_samples,
        max(input_chunk_samples, requested_samples),
    )


@dataclass(slots=True)
class SessionModelState:
    person_name: str | None = None
    latent_motion_frames: Any | None = None
    generator_state: Any | None = None

    def clone(self) -> "SessionModelState":
        return SessionModelState(
            person_name=self.person_name,
            latent_motion_frames=self._clone_value(self.latent_motion_frames),
            generator_state=self._clone_value(self.generator_state),
        )

    @staticmethod
    def _clone_value(value: Any) -> Any:
        if hasattr(value, "clone"):
            return value.clone()
        return value


@dataclass(slots=True)
class RuntimeSessionBinding:
    worker: "_SoulXRuntimeWorker"
    worker_index: int
    avatar_id: str
    model_state: SessionModelState


@dataclass(slots=True)
class ScheduledVideoFrame:
    pts: int
    image: np.ndarray


class AudioSampleQueue:
    def __init__(self) -> None:
        self._chunks: deque[np.ndarray] = deque()
        self._sample_count = 0

    def __len__(self) -> int:
        return self._sample_count

    def clear(self) -> None:
        self._chunks.clear()
        self._sample_count = 0

    def append(self, samples: np.ndarray) -> None:
        array = np.asarray(samples, dtype=np.float32).reshape(-1)
        if array.size == 0:
            return
        self._chunks.append(array)
        self._sample_count += int(array.size)

    def pop(self, sample_count: int) -> np.ndarray:
        target = min(max(sample_count, 0), self._sample_count)
        if target <= 0:
            return np.empty(0, dtype=np.float32)

        output = np.empty(target, dtype=np.float32)
        offset = 0
        while offset < target and self._chunks:
            head = self._chunks[0]
            take = min(target - offset, head.shape[0])
            output[offset : offset + take] = head[:take]
            offset += take
            self._sample_count -= take
            if take == head.shape[0]:
                self._chunks.popleft()
            else:
                self._chunks[0] = head[take:]
        return output

    def drop_oldest(self, sample_count: int) -> int:
        remaining = min(max(sample_count, 0), self._sample_count)
        dropped = 0
        while remaining > 0 and self._chunks:
            head = self._chunks[0]
            take = min(remaining, head.shape[0])
            remaining -= take
            dropped += take
            self._sample_count -= take
            if take == head.shape[0]:
                self._chunks.popleft()
            else:
                self._chunks[0] = head[take:]
        return dropped


class Float32RingBuffer:
    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, capacity)
        self._buffer = np.zeros(self.capacity, dtype=np.float32)
        self._write_index = 0
        self._filled = self.capacity

    def extend(self, samples: np.ndarray) -> None:
        array = np.asarray(samples, dtype=np.float32).reshape(-1)
        sample_count = int(array.size)
        if sample_count <= 0:
            return

        if sample_count >= self.capacity:
            self._buffer[:] = array[-self.capacity :]
            self._write_index = 0
            self._filled = self.capacity
            return

        end_index = self._write_index + sample_count
        if end_index <= self.capacity:
            self._buffer[self._write_index : end_index] = array
        else:
            split = self.capacity - self._write_index
            self._buffer[self._write_index :] = array[:split]
            self._buffer[: end_index - self.capacity] = array[split:]
        self._write_index = end_index % self.capacity
        self._filled = min(self.capacity, self._filled + sample_count)

    def snapshot(self) -> np.ndarray:
        if self._filled < self.capacity:
            output = np.zeros(self.capacity, dtype=np.float32)
            output[-self._filled :] = self._buffer[: self._filled]
            return output
        if self._write_index == 0:
            return self._buffer.copy()
        return np.concatenate((self._buffer[self._write_index :], self._buffer[: self._write_index]))


class _SoulXRuntimeWorker:
    def __init__(
        self,
        config: ServerConfig,
        worker_index: int,
        avatar_idle_frames: dict[str, np.ndarray],
        default_avatar_id: str,
    ) -> None:
        self.config = config
        self.worker_index = worker_index
        self.avatar_idle_frames = {
            avatar_id: frame.copy() for avatar_id, frame in avatar_idle_frames.items()
        }
        self.default_avatar_id = default_avatar_id
        self.idle_frame = self.avatar_idle_frames[self.default_avatar_id]
        self.pipeline: Any | None = None
        self.api: dict[str, Any] = {}
        self.params: InferParams | None = None
        self.initial_session_states: dict[str, SessionModelState] = {}
        self._inference_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.config.mock_inference:
            self.params = InferParams(
                sample_rate=self.config.sample_rate,
                tgt_fps=self.config.output_fps,
                frame_num=33,
                motion_frames_num=29,
                cached_audio_duration=8,
            )
            self.initial_session_states = {
                avatar_id: SessionModelState(person_name=avatar_id)
                for avatar_id in self.avatar_idle_frames
            }
            return

        await asyncio.to_thread(self._initialize_sync)
        await self._startup_warmup()

    def _initialize_sync(self) -> None:
        soulx_root = self.config.soulx_root.resolve()
        if not soulx_root.exists():
            raise FileNotFoundError(f"SoulX root not found: {soulx_root}")

        sys.path.insert(0, str(soulx_root))
        previous_cwd = os.getcwd()
        try:
            os.chdir(soulx_root)
            from flash_head.inference import (
                get_audio_embedding,
                get_base_data,
                get_infer_params,
                get_pipeline,
                run_pipeline,
            )

            pipeline = get_pipeline(
                world_size=1,
                ckpt_dir=str(self.config.ckpt_dir),
                wav2vec_dir=str(self.config.wav2vec_dir),
                model_type=self.config.model_type,
            )
            get_base_data(
                pipeline,
                cond_image_path_or_dir=str(self.config.cond_image),
                base_seed=self.config.base_seed,
                use_face_crop=self.config.use_face_crop,
            )
            infer_params = get_infer_params()
        finally:
            os.chdir(previous_cwd)

        self.pipeline = pipeline
        self.api = {
            "get_audio_embedding": get_audio_embedding,
            "run_pipeline": run_pipeline,
        }
        self.params = InferParams(
            sample_rate=int(infer_params["sample_rate"]),
            tgt_fps=int(infer_params["tgt_fps"]),
            frame_num=int(infer_params["frame_num"]),
            motion_frames_num=int(infer_params["motion_frames_num"]),
            cached_audio_duration=int(infer_params["cached_audio_duration"]),
        )
        available_avatar_ids = tuple(getattr(pipeline, "cond_image_dict", {}).keys())
        if not available_avatar_ids:
            raise RuntimeError("pipeline did not expose any condition images")

        missing = [avatar_id for avatar_id in self.avatar_idle_frames if avatar_id not in available_avatar_ids]
        if missing:
            raise RuntimeError(f"configured avatars were not loaded by pipeline: {missing}")

        self.initial_session_states = {}
        for avatar_id in self.avatar_idle_frames:
            pipeline.reset_person_name(avatar_id)
            self.initial_session_states[avatar_id] = self._capture_session_state(pipeline)
            cond_image = pipeline.cond_image_dict.get(avatar_id)
            if cond_image is not None:
                self.avatar_idle_frames[avatar_id] = np.asarray(
                    cond_image.convert("RGB").resize((512, 512)),
                    dtype=np.uint8,
                )
        self.idle_frame = self.avatar_idle_frames[self.default_avatar_id]
        self._restore_session_state(self.pipeline, self.initial_session_states[self.default_avatar_id].clone())

    def create_session_state(self, avatar_id: str | None = None) -> SessionModelState:
        resolved_avatar_id = avatar_id or self.default_avatar_id
        if resolved_avatar_id not in self.initial_session_states:
            raise ValueError(f"unknown avatarId: {resolved_avatar_id}")
        return self.initial_session_states[resolved_avatar_id].clone()

    async def infer_frames(self, cached_audio: np.ndarray, session_state: SessionModelState) -> list[np.ndarray]:
        if self.params is None:
            raise RuntimeError("runtime not initialized")
        if self.config.mock_inference:
            return self._mock_frames(cached_audio, session_state.person_name)
        async with self._inference_lock:
            return await asyncio.to_thread(self._infer_frames_locked, cached_audio, session_state)

    def _infer_frames_locked(self, cached_audio: np.ndarray, session_state: SessionModelState) -> list[np.ndarray]:
        if self.pipeline is None or self.params is None:
            raise RuntimeError("pipeline not initialized")

        self._restore_session_state(self.pipeline, session_state)
        audio_embedding = self.api["get_audio_embedding"](
            self.pipeline,
            cached_audio,
            self.params.audio_start_idx,
            self.params.audio_end_idx,
        )
        video = self.api["run_pipeline"](self.pipeline, audio_embedding)
        updated_state = self._capture_session_state(self.pipeline)
        session_state.person_name = updated_state.person_name
        session_state.latent_motion_frames = updated_state.latent_motion_frames
        session_state.generator_state = updated_state.generator_state
        video = video[self.params.motion_frames_num :].cpu().numpy().astype(np.uint8)
        return [frame for frame in video]

    def _capture_session_state(self, pipeline: Any) -> SessionModelState:
        return SessionModelState(
            person_name=getattr(pipeline, "person_name", None),
            latent_motion_frames=SessionModelState._clone_value(getattr(pipeline, "latent_motion_frames", None)),
            generator_state=SessionModelState._clone_value(pipeline.generator.get_state()),
        )

    def _restore_session_state(self, pipeline: Any, session_state: SessionModelState) -> None:
        pipeline.reset_person_name(session_state.person_name)
        if session_state.latent_motion_frames is not None:
            pipeline.latent_motion_frames = session_state.latent_motion_frames
        if session_state.generator_state is not None:
            pipeline.generator.set_state(session_state.generator_state)

    async def _startup_warmup(self) -> None:
        if self.config.mock_inference or self.config.startup_warmup_runs <= 0:
            return
        if self.params is None or not self.initial_session_states:
            raise RuntimeError("runtime warmup requested before initialization completed")

        logger.info(
            "runtime worker startup warmup begin worker=%s runs=%s",
            self.worker_index,
            self.config.startup_warmup_runs,
        )
        silent_audio = np.zeros(self.params.cached_samples, dtype=np.float32)
        warmup_state = self.create_session_state(self.default_avatar_id)
        for run_idx in range(self.config.startup_warmup_runs):
            started_at = time.monotonic()
            await asyncio.to_thread(self._infer_frames_locked, silent_audio, warmup_state)
            elapsed_ms = (time.monotonic() - started_at) * 1000.0
            logger.info(
                "runtime worker startup warmup worker=%s run=%s elapsed_ms=%.2f",
                self.worker_index,
                run_idx + 1,
                elapsed_ms,
            )

        if self.pipeline is not None:
            self._restore_session_state(self.pipeline, self.create_session_state(self.default_avatar_id))
        logger.info("runtime worker startup warmup complete worker=%s", self.worker_index)

    def _mock_frames(self, cached_audio: np.ndarray, avatar_id: str | None) -> list[np.ndarray]:
        if self.params is None:
            raise RuntimeError("runtime not initialized")
        idle_frame = self.avatar_idle_frames.get(avatar_id or self.default_avatar_id, self.idle_frame)
        energy = float(np.clip(np.abs(cached_audio[-self.params.slice_samples :]).mean() * 4.0, 0.0, 1.0))
        frames: list[np.ndarray] = []
        for idx in range(self.params.slice_len):
            image = Image.fromarray(idle_frame.copy())
            draw = ImageDraw.Draw(image)
            mouth_h = int(18 + energy * 90 * abs(np.sin((idx + 1) / 2.0)))
            draw.ellipse((196, 308 - mouth_h // 2, 316, 308 + mouth_h // 2), fill=(30, 20, 20))
            draw.text((20, 20), f"MOCK {idx + 1:02d}", fill=(255, 255, 255))
            frames.append(np.asarray(image, dtype=np.uint8))
        return frames


class SoulXRuntime:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._avatar_idle_frames = self._load_avatar_idle_frames()
        self._default_avatar_id = next(iter(self._avatar_idle_frames))
        self.idle_frame = self._avatar_idle_frames[self._default_avatar_id]
        self.params: InferParams | None = None
        self._workers: list[_SoulXRuntimeWorker] = []
        self._worker_session_counts: list[int] = []
        self._assignment_lock = asyncio.Lock()

    async def initialize(self) -> None:
        workers: list[_SoulXRuntimeWorker] = []
        for worker_index in range(self.config.inference_workers):
            worker = _SoulXRuntimeWorker(
                self.config,
                worker_index,
                self._avatar_idle_frames,
                self._default_avatar_id,
            )
            await worker.initialize()
            workers.append(worker)

        if not workers or workers[0].params is None:
            raise RuntimeError("runtime initialization failed: missing infer params")

        reference = workers[0].params
        for worker in workers[1:]:
            if worker.params != reference:
                raise RuntimeError("runtime workers initialized with mismatched infer params")

        self._workers = workers
        self._worker_session_counts = [0 for _ in workers]
        self.params = reference
        self._avatar_idle_frames = {
            avatar_id: frame.copy() for avatar_id, frame in workers[0].avatar_idle_frames.items()
        }
        self.idle_frame = self._avatar_idle_frames[self._default_avatar_id]

    def _configured_avatar_paths(self) -> list[Path]:
        source = self.config.cond_image
        if source.is_dir():
            avatar_paths = sorted(
                path
                for pattern in ("*.png", "*.jpg", "*.jpeg")
                for path in source.glob(pattern)
            )
            if not avatar_paths:
                raise FileNotFoundError(f"no avatar images found in directory: {source}")
            return avatar_paths
        if not source.exists():
            raise FileNotFoundError(f"avatar image not found: {source}")
        return [source]

    def _load_avatar_idle_frames(self) -> dict[str, np.ndarray]:
        avatar_frames: dict[str, np.ndarray] = {}
        for avatar_path in self._configured_avatar_paths():
            avatar_id = avatar_path.stem
            if avatar_id in avatar_frames:
                raise ValueError(f"duplicate avatar id from image name: {avatar_id}")
            image = Image.open(avatar_path).convert("RGB").resize((512, 512))
            avatar_frames[avatar_id] = np.asarray(image, dtype=np.uint8)
        if not avatar_frames:
            raise RuntimeError("no avatars were loaded")
        return avatar_frames

    def default_avatar_id(self) -> str:
        return self._default_avatar_id

    def available_avatars(self) -> list[dict[str, Any]]:
        return [
            {
                "id": avatar_id,
                "label": avatar_id,
                "isDefault": avatar_id == self._default_avatar_id,
            }
            for avatar_id in self._avatar_idle_frames
        ]

    def resolve_avatar_id(self, avatar_id: str | None = None) -> str:
        resolved_avatar_id = avatar_id or self._default_avatar_id
        if resolved_avatar_id not in self._avatar_idle_frames:
            raise ValueError(f"unknown avatarId: {resolved_avatar_id}")
        return resolved_avatar_id

    def get_idle_frame(self, avatar_id: str | None = None) -> np.ndarray:
        resolved_avatar_id = self.resolve_avatar_id(avatar_id)
        return self._avatar_idle_frames[resolved_avatar_id]

    def startup_min_audio_ms(self, input_chunk_ms: int | None = None) -> int:
        if self.params is None:
            raise RuntimeError("runtime not initialized")
        chunk_ms = input_chunk_ms if input_chunk_ms is not None else self.config.input_chunk_ms
        input_chunk_samples = self.params.sample_rate * chunk_ms // 1000
        min_samples = compute_startup_min_samples(self.params, input_chunk_samples, self.config)
        return int(round(min_samples * 1000.0 / self.params.sample_rate))

    def dispatch_step_audio_ms(self, input_chunk_ms: int | None = None) -> int:
        if self.params is None:
            raise RuntimeError("runtime not initialized")
        chunk_ms = input_chunk_ms if input_chunk_ms is not None else self.config.input_chunk_ms
        input_chunk_samples = self.params.sample_rate * chunk_ms // 1000
        step_samples = compute_dispatch_step_samples(self.params, input_chunk_samples, self.config)
        return int(round(step_samples * 1000.0 / self.params.sample_rate))

    async def create_session_binding(self, avatar_id: str | None = None) -> RuntimeSessionBinding:
        if not self._workers:
            raise RuntimeError("runtime not initialized")
        resolved_avatar_id = self.resolve_avatar_id(avatar_id)
        async with self._assignment_lock:
            worker_index = min(
                range(len(self._workers)),
                key=lambda index: (self._worker_session_counts[index], index),
            )
            self._worker_session_counts[worker_index] += 1
        worker = self._workers[worker_index]
        try:
            model_state = worker.create_session_state(resolved_avatar_id)
        except Exception:
            async with self._assignment_lock:
                if self._worker_session_counts[worker_index] > 0:
                    self._worker_session_counts[worker_index] -= 1
            raise
        return RuntimeSessionBinding(
            worker=worker,
            worker_index=worker_index,
            avatar_id=resolved_avatar_id,
            model_state=model_state,
        )

    async def release_session_binding(self, binding: RuntimeSessionBinding) -> None:
        async with self._assignment_lock:
            if binding.worker_index < 0 or binding.worker_index >= len(self._worker_session_counts):
                return
            if self._worker_session_counts[binding.worker_index] > 0:
                self._worker_session_counts[binding.worker_index] -= 1

    async def infer_frames(self, binding: RuntimeSessionBinding, cached_audio: np.ndarray) -> list[np.ndarray]:
        return await binding.worker.infer_frames(cached_audio, binding.model_state)


class StreamingInferenceSession:
    def __init__(
        self,
        runtime: SoulXRuntime,
        binding: RuntimeSessionBinding,
        capture_ms: int,
        input_chunk_ms: int,
        session_id: str,
        client_name: str | None = None,
        on_output_audio_ready: Callable[[str, np.ndarray], Awaitable[None]] | None = None,
        on_output_video_ready: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        if runtime.params is None:
            raise RuntimeError("runtime not initialized")
        self.runtime = runtime
        self.binding = binding
        self.params = runtime.params
        self.avatar_id = binding.avatar_id
        self.idle_frame = runtime.get_idle_frame(binding.avatar_id)
        self.session_id = session_id
        self.client_name = client_name
        self.on_output_audio_ready = on_output_audio_ready
        self.on_output_video_ready = on_output_video_ready
        self.capture_samples = self.params.sample_rate * capture_ms // 1000
        self.input_chunk_samples = self.params.sample_rate * input_chunk_ms // 1000
        self.startup_min_samples = compute_startup_min_samples(
            self.params,
            self.input_chunk_samples,
            self.runtime.config,
        )
        self.dispatch_step_samples = compute_dispatch_step_samples(
            self.params,
            self.input_chunk_samples,
            self.runtime.config,
        )
        self.max_pending_samples = max(
            self.params.slice_samples,
            self.params.sample_rate * self.runtime.config.max_pending_audio_ms // 1000,
        )
        self.audio_context = Float32RingBuffer(self.params.cached_samples)
        self.capture_buffer = AudioSampleQueue()
        self.input_buffer = AudioSampleQueue()
        self.pending_model_buffer = AudioSampleQueue()
        self.video_queue: asyncio.Queue[ScheduledVideoFrame] = asyncio.Queue(maxsize=256)
        self.presentation_clock = SessionPresentationClock(
            sample_rate=self.params.sample_rate,
            fps=self.params.tgt_fps,
        )
        self.worker_task: asyncio.Task[None] | None = None
        self.closed = False
        self._binding_released = False
        self.audio_frames_seen = 0
        self.inference_batches = 0
        self.incremental_dispatches = 0
        self.idle_returns = 0
        self.started_at = 0.0
        self.ended_at = 0.0
        self.generated_frames = 0
        self.generated_frames_served = 0
        self.idle_frames_served = 0
        self.total_frames_served = 0
        self.first_generated_frame_produced_at = 0.0
        self.first_generated_frame_served_at = 0.0
        self.first_idle_frame_served_at = 0.0
        self.inference_elapsed_total_ms = 0.0
        self.inference_elapsed_max_ms = 0.0
        self.inference_elapsed_min_ms: float | None = None
        self.pending_model_buffer_high_watermark = 0
        self.video_queue_high_watermark = 0
        self.dropped_audio_samples = 0
        self.startup_fast_path_used = False
        self.startup_audio_samples_used = 0

    def _select_output_frames(self, frames: list[np.ndarray], consume_samples: int) -> list[np.ndarray]:
        if not frames or consume_samples <= 0:
            return []
        if self.params.sample_rate <= 0 or self.params.tgt_fps <= 0:
            return frames

        target_frame_count = max(
            1,
            int(round(consume_samples * self.params.tgt_fps / self.params.sample_rate)),
        )
        if target_frame_count >= len(frames):
            return frames

        # For startup partial or smaller steady-state dispatches, only emit the tail
        # that corresponds to the newly consumed audio duration.
        return frames[-target_frame_count:]

    @property
    def worker_index(self) -> int:
        return self.binding.worker_index

    async def start(self) -> None:
        self.started_at = time.monotonic()
        logger.info(
            "streaming session started session_id=%s client_name=%s worker=%s capture_samples=%s input_chunk_samples=%s slice_samples=%s dispatch_step_samples=%s startup_min_samples=%s mock_inference=%s",
            self.session_id,
            self.client_name,
            self.worker_index,
            self.capture_samples,
            self.input_chunk_samples,
            self.params.slice_samples,
            self.dispatch_step_samples,
            self.startup_min_samples,
            self.runtime.config.mock_inference,
        )
        self.worker_task = asyncio.create_task(self._worker(), name=f"inference-worker-{self.worker_index}")

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.ended_at = time.monotonic()
        logger.info(
            "streaming session closing session_id=%s worker=%s audio_frames_seen=%s inference_batches=%s queued_frames=%s idle_returns=%s",
            self.session_id,
            self.worker_index,
            self.audio_frames_seen,
            self.inference_batches,
            self.video_queue.qsize(),
            self.idle_returns,
        )
        if self.worker_task is not None:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        await self._release_binding()

    async def add_audio_samples(self, samples: np.ndarray) -> None:
        if self.closed:
            return
        self.audio_frames_seen += 1
        self.capture_buffer.append(samples)
        while len(self.capture_buffer) >= self.capture_samples:
            capture_chunk = self.capture_buffer.pop(self.capture_samples)
            self.input_buffer.append(capture_chunk)
            while len(self.input_buffer) >= self.input_chunk_samples:
                input_chunk = self.input_buffer.pop(self.input_chunk_samples)
                self.pending_model_buffer.append(input_chunk)
                self.pending_model_buffer_high_watermark = max(
                    self.pending_model_buffer_high_watermark,
                    len(self.pending_model_buffer),
                )
                self._trim_pending_audio()
        if self.audio_frames_seen <= 5 or self.audio_frames_seen % 100 == 0:
            logger.info(
                "audio buffered session_id=%s frames_seen=%s capture_buffer=%s input_buffer=%s pending_model_buffer=%s",
                self.session_id,
                self.audio_frames_seen,
                len(self.capture_buffer),
                len(self.input_buffer),
                len(self.pending_model_buffer),
            )

    async def _worker(self) -> None:
        while not self.closed:
            available_samples = len(self.pending_model_buffer)
            startup_partial = (
                self.inference_batches == 0
                and available_samples >= self.startup_min_samples
                and available_samples < self.params.slice_samples
            )
            steady_incremental = (
                self.inference_batches > 0
                and self.dispatch_step_samples < self.params.slice_samples
                and available_samples >= self.dispatch_step_samples
                and available_samples < self.params.slice_samples
            )
            if available_samples < self.params.slice_samples and not startup_partial and not steady_incremental:
                await asyncio.sleep(0.01)
                continue

            if startup_partial:
                consume_samples = min(available_samples, self.params.slice_samples)
            elif steady_incremental:
                consume_samples = min(available_samples, self.dispatch_step_samples)
                self.incremental_dispatches += 1
            else:
                consume_samples = self.params.slice_samples

            new_audio = self.pending_model_buffer.pop(consume_samples)
            audio_start_sample = self.presentation_clock.reserve_audio_samples(new_audio.size)
            self.audio_context.extend(new_audio)
            cached_audio = self.audio_context.snapshot()
            inference_started = asyncio.get_running_loop().time()
            raw_frames = await self.runtime.infer_frames(self.binding, cached_audio)
            inference_elapsed_ms = (asyncio.get_running_loop().time() - inference_started) * 1000.0
            frames = self._select_output_frames(raw_frames, consume_samples)
            video_pts = self.presentation_clock.assign_video_pts(
                audio_start_sample=audio_start_sample,
                audio_sample_count=new_audio.size,
                frame_count=len(frames),
            )

            if startup_partial and not self.startup_fast_path_used:
                self.startup_fast_path_used = True
                self.startup_audio_samples_used = consume_samples
                logger.info(
                    "startup partial inference enabled session_id=%s worker=%s audio_ms=%.2f target_slice_ms=%.2f",
                    self.session_id,
                    self.worker_index,
                    consume_samples * 1000.0 / self.params.sample_rate,
                    self.params.slice_samples * 1000.0 / self.params.sample_rate,
                )

            self.inference_batches += 1
            self.generated_frames += len(frames)
            if frames and self.first_generated_frame_produced_at <= 0:
                self.first_generated_frame_produced_at = time.monotonic()
            self.inference_elapsed_total_ms += inference_elapsed_ms
            self.inference_elapsed_max_ms = max(self.inference_elapsed_max_ms, inference_elapsed_ms)
            if self.inference_elapsed_min_ms is None:
                self.inference_elapsed_min_ms = inference_elapsed_ms
            else:
                self.inference_elapsed_min_ms = min(self.inference_elapsed_min_ms, inference_elapsed_ms)
            logger.info(
                "inference batch=%s session_id=%s worker=%s produced_frames=%s raw_frames=%s queue_before=%s pending_after=%s elapsed_ms=%.2f incremental=%s",
                self.inference_batches,
                self.session_id,
                self.worker_index,
                len(frames),
                len(raw_frames),
                self.video_queue.qsize(),
                len(self.pending_model_buffer),
                inference_elapsed_ms,
                steady_incremental,
            )
            for pts, frame in zip(video_pts, frames):
                if self.closed:
                    return
                try:
                    self.video_queue.put_nowait(ScheduledVideoFrame(pts=pts, image=frame))
                except asyncio.QueueFull:
                    _ = self.video_queue.get_nowait()
                    self.video_queue.put_nowait(ScheduledVideoFrame(pts=pts, image=frame))
                self.video_queue_high_watermark = max(self.video_queue_high_watermark, self.video_queue.qsize())
            if frames and self.on_output_video_ready is not None:
                await self.on_output_video_ready(self.session_id)
            if self.on_output_audio_ready is not None and new_audio.size > 0:
                await self.on_output_audio_ready(self.session_id, new_audio)

    async def wait_for_first_generated_frame(self) -> ScheduledVideoFrame | None:
        while True:
            try:
                frame = await asyncio.wait_for(self.video_queue.get(), timeout=0.1)
                self._record_generated_frame_served()
                return frame
            except asyncio.TimeoutError:
                if self.closed:
                    return None

    async def get_next_frame(self, timeout: float, *, record_timeout: bool = True) -> ScheduledVideoFrame | None:
        try:
            frame = await asyncio.wait_for(self.video_queue.get(), timeout=timeout)
            self._record_generated_frame_served()
            return frame
        except asyncio.TimeoutError:
            if not record_timeout:
                return None
            self.idle_returns += 1
            self._record_idle_frame_served()
            if self.idle_returns <= 5 or self.idle_returns % 50 == 0:
                logger.warning(
                    "video queue timeout idle_frame returned session_id=%s count=%s queue_size=%s pending_model_buffer=%s",
                    self.session_id,
                    self.idle_returns,
                    self.video_queue.qsize(),
                    len(self.pending_model_buffer),
                )
            return None

    async def _release_binding(self) -> None:
        if self._binding_released:
            return
        self._binding_released = True
        await self.runtime.release_session_binding(self.binding)

    def _record_generated_frame_served(self) -> None:
        self.total_frames_served += 1
        self.generated_frames_served += 1
        if self.first_generated_frame_served_at <= 0:
            self.first_generated_frame_served_at = time.monotonic()

    def _record_idle_frame_served(self) -> None:
        self.total_frames_served += 1
        self.idle_frames_served += 1
        if self.first_idle_frame_served_at <= 0:
            self.first_idle_frame_served_at = time.monotonic()

    def _trim_pending_audio(self) -> None:
        if self.max_pending_samples <= 0:
            return
        overflow_samples = len(self.pending_model_buffer) - self.max_pending_samples
        if overflow_samples <= 0:
            return
        drop_samples = overflow_samples - (overflow_samples % self.input_chunk_samples)
        if drop_samples <= 0:
            return
        dropped = self.pending_model_buffer.drop_oldest(drop_samples)
        self.dropped_audio_samples += dropped
        logger.warning(
            "dropping stale pending audio session_id=%s worker=%s drop_ms=%.2f remaining_ms=%.2f",
            self.session_id,
            self.worker_index,
            dropped * 1000.0 / self.params.sample_rate,
            len(self.pending_model_buffer) * 1000.0 / self.params.sample_rate,
        )

    def stats_snapshot(self) -> dict[str, Any]:
        ended_at = self.ended_at or time.monotonic()
        duration_sec = max(ended_at - self.started_at, 0.0) if self.started_at > 0 else 0.0
        avg_inference_ms = self.inference_elapsed_total_ms / self.inference_batches if self.inference_batches else 0.0
        first_generated_frame_produced_ms = (
            max(self.first_generated_frame_produced_at - self.started_at, 0.0) * 1000.0
            if self.started_at > 0 and self.first_generated_frame_produced_at > 0
            else 0.0
        )
        first_generated_frame_served_ms = (
            max(self.first_generated_frame_served_at - self.started_at, 0.0) * 1000.0
            if self.started_at > 0 and self.first_generated_frame_served_at > 0
            else 0.0
        )
        first_idle_frame_served_ms = (
            max(self.first_idle_frame_served_at - self.started_at, 0.0) * 1000.0
            if self.started_at > 0 and self.first_idle_frame_served_at > 0
            else 0.0
        )
        return {
            "sessionId": self.session_id,
            "clientName": self.client_name,
            "avatarId": self.avatar_id,
            "workerIndex": self.worker_index,
            "closed": self.closed,
            "durationSec": duration_sec,
            "audioFramesSeen": self.audio_frames_seen,
            "inferenceBatches": self.inference_batches,
            "incrementalDispatches": self.incremental_dispatches,
            "dispatchStepMs": self.dispatch_step_samples * 1000.0 / self.params.sample_rate,
            "generatedFrames": self.generated_frames,
            "generatedFramesServed": self.generated_frames_served,
            "idleFramesServed": self.idle_frames_served,
            "totalFramesServed": self.total_frames_served,
            "idleRatio": (self.idle_frames_served / self.total_frames_served) if self.total_frames_served else 0.0,
            "firstGeneratedFrameProducedMs": first_generated_frame_produced_ms,
            "firstGeneratedFrameServedMs": first_generated_frame_served_ms,
            "firstIdleFrameServedMs": first_idle_frame_served_ms,
            "avgInferenceMs": avg_inference_ms,
            "maxInferenceMs": self.inference_elapsed_max_ms,
            "minInferenceMs": self.inference_elapsed_min_ms or 0.0,
            "pendingAudioMs": len(self.pending_model_buffer) * 1000.0 / self.params.sample_rate,
            "pendingAudioMsHighWatermark": self.pending_model_buffer_high_watermark * 1000.0 / self.params.sample_rate,
            "droppedAudioMs": self.dropped_audio_samples * 1000.0 / self.params.sample_rate,
            "startupFastPathUsed": self.startup_fast_path_used,
            "startupAudioMs": self.startup_audio_samples_used * 1000.0 / self.params.sample_rate,
            "videoQueueSize": self.video_queue.qsize(),
            "videoQueueHighWatermark": self.video_queue_high_watermark,
            "generatedFps": (self.generated_frames / duration_sec) if duration_sec > 0 else 0.0,
            "servedGeneratedFps": (self.generated_frames_served / duration_sec) if duration_sec > 0 else 0.0,
            "servedIdleFps": (self.idle_frames_served / duration_sec) if duration_sec > 0 else 0.0,
        }
