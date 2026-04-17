from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections import deque
from fractions import Fraction
from typing import Any

import av
import numpy as np
from aiohttp import WSMsgType, web
import aioice.ice
from aiortc import AudioStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.rtcicetransport import RTCIceCandidate
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import SessionDescription as SdpSessionDescription
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp

from .config import ServerConfig
from .model_runtime import ScheduledVideoFrame, SoulXRuntime, StreamingInferenceSession
from .playout_sync import SessionPlayoutGate, video_due_pts_for_elapsed

logger = logging.getLogger(__name__)
_ORIGINAL_GET_HOST_ADDRESSES = aioice.ice.get_host_addresses
DISCONNECTED_CLEANUP_GRACE_SEC = 5.0
DEFAULT_AUDIO_FRAME_MS = 20


class AudioPlayoutBuffer:
    def __init__(self, min_buffer_samples: int = 0) -> None:
        self._condition = asyncio.Condition()
        self._min_buffer_samples = max(0, int(min_buffer_samples))
        self._session_id: str | None = None
        self._playback_ready = False
        self._chunks: deque[np.ndarray] = deque()
        self._buffered_samples = 0

    async def reset(self, session_id: str) -> None:
        async with self._condition:
            self._session_id = session_id
            self._playback_ready = False
            self._chunks.clear()
            self._buffered_samples = 0
            self._condition.notify_all()

    async def stop(self) -> None:
        async with self._condition:
            self._session_id = None
            self._playback_ready = False
            self._chunks.clear()
            self._buffered_samples = 0
            self._condition.notify_all()

    async def append(self, session_id: str, samples: np.ndarray) -> bool:
        if samples.size == 0:
            return False
        async with self._condition:
            if self._session_id != session_id:
                return False
            self._chunks.append(samples.astype(np.float32, copy=True))
            self._buffered_samples += samples.shape[0]
            newly_ready = False
            if not self._playback_ready and self._buffered_samples >= self._min_buffer_samples:
                self._playback_ready = True
                newly_ready = True
            self._condition.notify_all()
            return newly_ready

    async def read(self, sample_count: int, timeout: float) -> np.ndarray:
        if sample_count <= 0:
            return np.empty(0, dtype=np.float32)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0.0)
        async with self._condition:
            while True:
                if self._session_id is None or not self._playback_ready:
                    return np.zeros(sample_count, dtype=np.float32)
                if self._buffered_samples > 0:
                    return self._pop_locked(sample_count)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return np.zeros(sample_count, dtype=np.float32)
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return np.zeros(sample_count, dtype=np.float32)

    def _pop_locked(self, sample_count: int) -> np.ndarray:
        output = np.zeros(sample_count, dtype=np.float32)
        offset = 0
        while offset < sample_count and self._chunks:
            head = self._chunks[0]
            take = min(sample_count - offset, head.shape[0])
            output[offset : offset + take] = head[:take]
            offset += take
            self._buffered_samples -= take
            if take == head.shape[0]:
                self._chunks.popleft()
            else:
                self._chunks[0] = head[take:]
        return output


class GeneratedVideoTrack(VideoStreamTrack):
    def __init__(self, peer: "PeerContext", fps: int, idle_frame: np.ndarray) -> None:
        super().__init__()
        self.peer = peer
        self.fps = fps
        self.idle_frame = idle_frame
        self._epoch: float | None = None
        self._frame_index = 0
        self._session_id: str | None = None
        self._session_start: float | None = None
        self._pending_frame: ScheduledVideoFrame | None = None
        self._last_real_frame: np.ndarray | None = None
        self._frame_fetch_grace_sec = 0.5 / max(self.fps, 1)
        self._due_slack_sec = 0.005

    async def recv(self) -> av.VideoFrame:
        if self._epoch is None:
            self._epoch = time.monotonic()

        current_session_id = await self.peer.get_active_session_id()
        session_changed = current_session_id != self._session_id

        if current_session_id is not None and session_changed:
            ready = await self.peer.wait_for_playout_ready(current_session_id)
            if ready:
                first_frame = await self.peer.wait_for_first_frame(current_session_id)
                session_start = await self.peer.ensure_playout_started(current_session_id)
                if session_start is not None:
                    self._session_start = session_start
                    self._epoch = session_start - (self._frame_index / self.fps)
                else:
                    self._session_start = None
                self._pending_frame = first_frame
                self._last_real_frame = None
                self._session_id = current_session_id
            else:
                self._session_id = None
                self._session_start = None
                self._pending_frame = None
                self._last_real_frame = None
        else:
            if session_changed:
                self._session_id = current_session_id
                self._session_start = None
                self._pending_frame = None
                self._last_real_frame = None

        frame_nd = self._last_real_frame if self._last_real_frame is not None else self.idle_frame

        wait_until = self._epoch + (self._frame_index / self.fps)
        delay = wait_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        if self._session_id is not None and self._pending_frame is None:
            self._pending_frame = await self.peer.get_next_video_frame(
                timeout=self._frame_fetch_grace_sec,
                record_timeout=False,
            )

        if self._session_id is not None and self._session_start is not None:
            session_elapsed = max(0.0, time.monotonic() - self._session_start)
            due_pts = video_due_pts_for_elapsed(
                elapsed_sec=session_elapsed,
                fps=self.fps,
                due_slack_sec=self._due_slack_sec,
            )
            served_due_frames = 0
            last_due_pts: int | None = None

            while True:
                if self._pending_frame is None:
                    self._pending_frame = await self.peer.get_next_video_frame(timeout=0.0, record_timeout=False)
                    if self._pending_frame is None:
                        break
                if self._pending_frame.pts > due_pts:
                    break
                frame_nd = self._pending_frame.image
                self._last_real_frame = frame_nd
                last_due_pts = self._pending_frame.pts
                served_due_frames += 1
                self._pending_frame = None

            if served_due_frames > 1:
                logger.info(
                    "coalesced due video frames peer_id=%s session_id=%s count=%s last_due_pts=%s due_pts=%s current_pts=%s",
                    self.peer.peer_id,
                    self._session_id,
                    served_due_frames,
                    last_due_pts,
                    due_pts,
                    self._frame_index,
                )

        frame = av.VideoFrame.from_ndarray(frame_nd, format="rgb24")
        frame.pts = self._frame_index
        frame.time_base = Fraction(1, self.fps)
        self._frame_index += 1
        return frame


class GeneratedAudioTrack(AudioStreamTrack):
    def __init__(self, peer: "PeerContext", sample_rate: int, frame_ms: int = 20) -> None:
        super().__init__()
        self.peer = peer
        self.sample_rate = sample_rate
        self.frame_samples = max(1, sample_rate * frame_ms // 1000)
        self._epoch: float | None = None
        self._timestamp = 0
        self._session_id: str | None = None

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError

        if self._epoch is None:
            self._epoch = time.monotonic()

        current_session_id = await self.peer.get_active_session_id()
        session_changed = current_session_id != self._session_id
        if current_session_id is not None and session_changed:
            ready = await self.peer.wait_for_playout_ready(current_session_id)
            if ready:
                session_start = await self.peer.ensure_playout_started(current_session_id)
                if session_start is not None:
                    self._epoch = session_start - (self._timestamp / self.sample_rate)
                self._session_id = current_session_id
            else:
                self._session_id = None
        elif session_changed:
            self._session_id = current_session_id

        wait_until = self._epoch + (self._timestamp / self.sample_rate)
        delay = wait_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        samples = await self.peer.get_next_audio_output(
            self.frame_samples,
            timeout=self.frame_samples / self.sample_rate,
        )
        pcm16 = np.clip(np.round(samples * 32767.0), -32768, 32767).astype(np.int16)

        frame = av.AudioFrame(format="s16", layout="mono", samples=self.frame_samples)
        frame.sample_rate = self.sample_rate
        frame.pts = self._timestamp
        frame.time_base = Fraction(1, self.sample_rate)
        for plane in frame.planes:
            plane.update(pcm16.tobytes())
        self._timestamp += self.frame_samples
        return frame


class SessionLease:
    def __init__(self, manager: "SessionManager") -> None:
        self.manager = manager
        self._released = False
        self._lock = asyncio.Lock()

    async def release(self) -> None:
        async with self._lock:
            if self._released:
                return
            self._released = True
        await self.manager.release()


class SessionManager:
    def __init__(self, max_sessions: int) -> None:
        self.max_sessions = max_sessions
        self._lock = asyncio.Lock()
        self._active_sessions = 0

    async def acquire(self) -> SessionLease | None:
        async with self._lock:
            if self._active_sessions >= self.max_sessions:
                return None
            self._active_sessions += 1
            return SessionLease(self)

    async def release(self) -> None:
        async with self._lock:
            if self._active_sessions > 0:
                self._active_sessions -= 1

    async def snapshot(self) -> dict[str, int]:
        async with self._lock:
            return {
                "activeSessions": self._active_sessions,
                "maxConcurrentSessions": self.max_sessions,
            }


class SessionStatsRegistry:
    def __init__(self, completed_limit: int = 128) -> None:
        self._lock = asyncio.Lock()
        self._active_sessions: dict[str, StreamingInferenceSession] = {}
        self._completed_sessions: deque[dict[str, Any]] = deque(maxlen=completed_limit)

    async def register(self, session: StreamingInferenceSession) -> None:
        async with self._lock:
            self._active_sessions[session.session_id] = session

    async def complete(self, session: StreamingInferenceSession) -> None:
        async with self._lock:
            if session.session_id not in self._active_sessions:
                return
            self._active_sessions.pop(session.session_id, None)
            self._completed_sessions.append(session.stats_snapshot())

    async def snapshot(self, session_ids: set[str] | None = None) -> dict[str, Any]:
        async with self._lock:
            active = [session.stats_snapshot() for session in self._active_sessions.values()]
            completed = list(self._completed_sessions)
        if session_ids:
            active = [item for item in active if item["sessionId"] in session_ids]
            completed = [item for item in completed if item["sessionId"] in session_ids]
        return {"active": active, "completed": completed}


class SessionBusyError(RuntimeError):
    pass


class PeerContext:
    def __init__(
        self,
        peer_id: str,
        pc: RTCPeerConnection,
        session_manager: SessionManager,
        stats_registry: SessionStatsRegistry,
        runtime: SoulXRuntime,
    ) -> None:
        self.peer_id = peer_id
        self.pc = pc
        self.session_manager = session_manager
        self.stats_registry = stats_registry
        self.runtime = runtime
        self.active_session_id: str | None = None
        self._candidate_lock = asyncio.Lock()
        self._session_op_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._session_ready = asyncio.Event()
        self._local_candidates: list[dict[str, Any]] = []
        self._local_candidates_complete = False
        self._session: StreamingInferenceSession | None = None
        self._lease: SessionLease | None = None
        self._closed = False
        audio_preroll_samples = runtime.params.sample_rate * runtime.config.playout_buffer_ms // 1000
        self._audio_output = AudioPlayoutBuffer(min_buffer_samples=audio_preroll_samples)
        self._playout_gate = SessionPlayoutGate(
            startup_guard_sec=max(
                1.0 / max(runtime.params.tgt_fps, 1),
                DEFAULT_AUDIO_FRAME_MS / 1000.0,
            )
        )
        self._signal_lock = asyncio.Lock()
        self._signal_sockets: set[web.WebSocketResponse] = set()

    async def set_local_candidates(self, candidates: list[dict[str, Any]], complete: bool) -> None:
        async with self._candidate_lock:
            self._local_candidates = candidates
            self._local_candidates_complete = complete
        await self.broadcast_signal(
            {
                "type": "candidates",
                "candidates": candidates,
                "nextCursor": len(candidates),
                "complete": complete,
            }
        )

    async def candidates_snapshot(self, cursor: int) -> dict[str, Any]:
        async with self._candidate_lock:
            safe_cursor = max(0, min(cursor, len(self._local_candidates)))
            return {
                "candidates": self._local_candidates[safe_cursor:],
                "nextCursor": len(self._local_candidates),
                "complete": self._local_candidates_complete,
            }

    async def register_signal_socket(self, ws: web.WebSocketResponse) -> None:
        async with self._signal_lock:
            self._signal_sockets.add(ws)

    async def unregister_signal_socket(self, ws: web.WebSocketResponse) -> None:
        async with self._signal_lock:
            self._signal_sockets.discard(ws)

    async def send_signal_snapshot(self, ws: web.WebSocketResponse) -> None:
        session_id = await self.get_active_session_id()
        payload = await self.candidates_snapshot(0)
        await ws.send_json(
            {
                "type": "snapshot",
                "peerId": self.peer_id,
                "sessionId": session_id,
                **payload,
            }
        )

    async def broadcast_signal(self, payload: dict[str, Any]) -> None:
        async with self._signal_lock:
            sockets = list(self._signal_sockets)
        stale: list[web.WebSocketResponse] = []
        for ws in sockets:
            if ws.closed:
                stale.append(ws)
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        if not stale:
            return
        async with self._signal_lock:
            for ws in stale:
                self._signal_sockets.discard(ws)

    async def close_signal_sockets(self) -> None:
        async with self._signal_lock:
            sockets = list(self._signal_sockets)
            self._signal_sockets.clear()
        for ws in sockets:
            try:
                await ws.close()
            except Exception:
                continue

    async def start_session(
        self,
        capture_ms: int,
        input_chunk_ms: int,
        client_name: str | None = None,
        avatar_id: str | None = None,
    ) -> StreamingInferenceSession:
        async with self._session_op_lock:
            await self._stop_session_locked()
            lease = await self.session_manager.acquire()
            if lease is None:
                raise SessionBusyError("server is busy; max concurrent session limit reached")
            try:
                binding = await self.runtime.create_session_binding(avatar_id)
            except Exception:
                await lease.release()
                raise

            session = StreamingInferenceSession(
                self.runtime,
                binding,
                capture_ms,
                input_chunk_ms,
                session_id=uuid.uuid4().hex,
                client_name=client_name,
                on_output_audio_ready=self.enqueue_output_audio,
                on_output_video_ready=self.mark_video_output_ready,
            )
            try:
                await session.start()
                await self.stats_registry.register(session)
            except Exception:
                await self.runtime.release_session_binding(binding)
                await lease.release()
                raise

            async with self._session_lock:
                if self._closed:
                    await session.close()
                    await self.stats_registry.complete(session)
                    await lease.release()
                    raise RuntimeError("peer is already closed")
                self._session = session
                self._lease = lease
                self.active_session_id = session.session_id
                self._session_ready.set()
            await self._audio_output.reset(session.session_id)
            await self._playout_gate.reset(session.session_id)
            await self.broadcast_signal(
                {
                    "type": "session",
                    "state": "started",
                    "peerId": self.peer_id,
                    "sessionId": session.session_id,
                    "avatarId": session.avatar_id,
                }
            )
            return session

    async def stop_session(self) -> StreamingInferenceSession | None:
        async with self._session_op_lock:
            return await self._stop_session_locked()

    async def _stop_session_locked(self) -> StreamingInferenceSession | None:
        async with self._session_lock:
            session = self._session
            lease = self._lease
            self._session = None
            self._lease = None
            self.active_session_id = None
            self._session_ready.clear()
        await self._audio_output.stop()
        await self._playout_gate.stop()
        if session is not None:
            await session.close()
            await self.stats_registry.complete(session)
        if lease is not None:
            await lease.release()
        if session is not None:
            await self.broadcast_signal(
                {
                    "type": "session",
                    "state": "stopped",
                    "peerId": self.peer_id,
                    "sessionId": session.session_id,
                }
            )
        return session

    async def close(self) -> None:
        async with self._session_lock:
            if self._closed:
                return
            self._closed = True
        await self.stop_session()
        await self.close_signal_sockets()

    async def wait_for_first_frame(
        self,
        session_id: str | None = None,
    ) -> ScheduledVideoFrame | None:
        session = await self.wait_for_active_session(timeout=1.0, session_id=session_id)
        if session is None:
            return None
        return await session.wait_for_first_generated_frame()

    async def get_next_video_frame(
        self,
        timeout: float,
        *,
        record_timeout: bool = True,
    ) -> ScheduledVideoFrame | None:
        session = await self.get_active_session()
        if session is None:
            return None
        return await session.get_next_frame(timeout, record_timeout=record_timeout)

    async def add_audio_samples(self, samples: np.ndarray) -> None:
        session = await self.get_active_session()
        if session is None:
            return
        await session.add_audio_samples(samples)

    async def get_active_session(self) -> StreamingInferenceSession | None:
        async with self._session_lock:
            return self._session

    async def get_active_session_id(self) -> str | None:
        async with self._session_lock:
            return self.active_session_id

    async def enqueue_output_audio(self, session_id: str, samples: np.ndarray) -> None:
        newly_ready = await self._audio_output.append(session_id, samples)
        if newly_ready:
            logger.info(
                "audio playout preroll ready peer_id=%s session_id=%s buffer_ms=%s",
                self.peer_id,
                session_id,
                self.runtime.config.playout_buffer_ms,
            )
            await self._playout_gate.mark_audio_ready(session_id)

    async def mark_video_output_ready(self, session_id: str) -> None:
        await self._playout_gate.mark_video_ready(session_id)

    async def get_next_audio_output(self, sample_count: int, timeout: float) -> np.ndarray:
        return await self._audio_output.read(sample_count, timeout)

    async def wait_for_playout_ready(self, session_id: str, timeout: float | None = None) -> bool:
        return await self._playout_gate.wait_until_ready(session_id, timeout=timeout)

    async def ensure_playout_started(self, session_id: str) -> float | None:
        return await self._playout_gate.ensure_started(session_id)

    async def wait_for_active_session(
        self,
        timeout: float,
        session_id: str | None = None,
    ) -> StreamingInferenceSession | None:
        session = await self.get_active_session()
        if session is not None and (session_id is None or session.session_id == session_id):
            return session
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        session = await self.get_active_session()
        if session is None:
            return None
        if session_id is not None and session.session_id != session_id:
            return None
        return session


class PeerRegistry:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._peers: dict[str, PeerContext] = {}

    async def register(self, peer: PeerContext) -> None:
        async with self._lock:
            self._peers[peer.peer_id] = peer

    async def get(self, peer_id: str) -> PeerContext | None:
        async with self._lock:
            return self._peers.get(peer_id)

    async def get_by_session_id(self, session_id: str) -> PeerContext | None:
        async with self._lock:
            for peer in self._peers.values():
                if peer.active_session_id == session_id:
                    return peer
        return None

    async def remove(self, peer_id: str) -> None:
        async with self._lock:
            self._peers.pop(peer_id, None)


def build_rtc_configuration(config: ServerConfig) -> RTCConfiguration:
    if config.ice_mode == "tailscale" or not config.ice_server_urls:
        return RTCConfiguration(iceServers=[])
    return RTCConfiguration(iceServers=[RTCIceServer(urls=list(config.ice_server_urls))])


async def make_app(config: ServerConfig) -> web.Application:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    configure_ice_host_address_policy(config)
    runtime = SoulXRuntime(config)
    await runtime.initialize()
    session_manager = SessionManager(config.max_concurrent_sessions)
    stats_registry = SessionStatsRegistry()
    peer_registry = PeerRegistry()
    logger.info(
        "server initialized host=%s port=%s mock_inference=%s model_type=%s max_concurrent_sessions=%s inference_workers=%s inference_step_ms=%s",
        config.host,
        config.port,
        config.mock_inference,
        config.model_type,
        config.max_concurrent_sessions,
        config.inference_workers,
        config.inference_step_ms,
    )

    app = web.Application()
    app["config"] = config
    app["runtime"] = runtime
    app["session_manager"] = session_manager
    app["stats_registry"] = stats_registry
    app["peer_registry"] = peer_registry
    app["pcs"] = set()

    app.add_routes(
        [
            web.get("/healthz", healthz),
            web.post("/offer", offer),
            web.options("/offer", offer),
            web.post("/session/start", post_session_start),
            web.options("/session/start", handle_options),
            web.post("/session/stop", post_session_stop),
            web.options("/session/stop", handle_options),
            web.post("/candidate", post_candidate),
            web.options("/candidate", handle_options),
            web.get("/candidates", get_candidates),
            web.get("/ws", websocket_signal),
            web.post("/bootstrap-audio", post_bootstrap_audio),
            web.options("/bootstrap-audio", handle_options),
            web.get("/config", get_config),
            web.get("/stats", get_stats),
            web.get("/", index),
            web.static("/debug-client", "debug_client"),
        ]
    )

    async def on_shutdown(application: web.Application) -> None:
        pcs = list(application["pcs"])
        for pc in pcs:
            await pc.close()

    app.on_shutdown.append(on_shutdown)
    return app


def configure_ice_host_address_policy(config: ServerConfig) -> None:
    if config.ice_mode != "tailscale":
        aioice.ice.get_host_addresses = _ORIGINAL_GET_HOST_ADDRESSES
        return

    def get_tailscale_host_addresses(use_ipv4: bool, use_ipv6: bool) -> list[str]:
        addresses = _ORIGINAL_GET_HOST_ADDRESSES(use_ipv4, use_ipv6)
        filtered = [address for address in addresses if ip_matches_policy(address, config)]
        if filtered:
            logger.info("aioice host addresses filtered for tailscale mode addresses=%s", filtered)
            return filtered
        logger.warning(
            "tailscale ICE mode enabled but no local addresses matched prefixes ipv4=%s ipv6=%s; "
            "falling back to all host addresses",
            config.ice_tailscale_ipv4_prefixes,
            config.ice_tailscale_ipv6_prefixes,
        )
        return addresses

    aioice.ice.get_host_addresses = get_tailscale_host_addresses


def with_cors(response: web.StreamResponse, origin: str) -> web.StreamResponse:
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response


async def index(request: web.Request) -> web.Response:
    raise web.HTTPFound("/debug-client/index.html")


async def healthz(request: web.Request) -> web.Response:
    response = web.json_response({"ok": True})
    return with_cors(response, request.app["config"].public_origin)


async def handle_options(request: web.Request) -> web.Response:
    return with_cors(web.Response(status=204), request.app["config"].public_origin)


async def get_config(request: web.Request) -> web.Response:
    config: ServerConfig = request.app["config"]
    runtime: SoulXRuntime = request.app["runtime"]
    session_manager: SessionManager = request.app["session_manager"]
    params = runtime.params
    session_stats = await session_manager.snapshot()
    payload = {
        "sampleRate": config.sample_rate,
        "captureMs": config.capture_ms,
        "inputChunkMs": config.input_chunk_ms,
        "outputFps": config.output_fps,
        "playoutBufferMs": config.playout_buffer_ms,
        "modelType": config.model_type,
        "modelSliceMs": int(params.slice_samples * 1000 / params.sample_rate) if params else None,
        "inferenceWorkers": config.inference_workers,
        "inferenceStepMs": runtime.dispatch_step_audio_ms() if params else None,
        "startupPartialRatio": config.startup_partial_ratio,
        "startupMinAudioMs": runtime.startup_min_audio_ms() if params else None,
        "iceMode": config.ice_mode,
        "iceServers": config.ice_servers_payload,
        "iceTailscaleIpv4Prefixes": list(config.ice_tailscale_ipv4_prefixes),
        "iceTailscaleIpv6Prefixes": list(config.ice_tailscale_ipv6_prefixes),
        "supportsSessionReuse": True,
        "supportsSignalWebSocket": True,
        "signalWebSocketPath": "/ws",
        "avatars": runtime.available_avatars(),
        "defaultAvatarId": runtime.default_avatar_id(),
        **session_stats,
    }
    response = web.json_response(payload)
    return with_cors(response, config.public_origin)


async def get_stats(request: web.Request) -> web.Response:
    config: ServerConfig = request.app["config"]
    stats_registry: SessionStatsRegistry = request.app["stats_registry"]
    session_ids = {value for value in request.query.getall("sessionId", []) if value}
    payload = await stats_registry.snapshot(session_ids or None)
    response = web.json_response(payload)
    return with_cors(response, config.public_origin)


async def resolve_peer(
    peer_registry: PeerRegistry,
    peer_id: str | None = None,
    session_id: str | None = None,
) -> PeerContext | None:
    if peer_id:
        peer = await peer_registry.get(peer_id)
        if peer is not None:
            return peer
    if session_id:
        return await peer_registry.get_by_session_id(session_id)
    return None


async def apply_remote_candidate(
    peer: PeerContext,
    candidate_payload: dict[str, Any] | None,
    config: ServerConfig,
) -> dict[str, Any]:
    if candidate_payload is None:
        await peer.pc.addIceCandidate(None)
        return {"ok": True, "complete": True}
    if not isinstance(candidate_payload, dict):
        raise web.HTTPBadRequest(text='{"error":"candidate payload must be an object"}', content_type="application/json")

    candidate_line = str(candidate_payload.get("candidate", "")).strip()
    if not candidate_line:
        raise web.HTTPBadRequest(text='{"error":"candidate.candidate is required"}', content_type="application/json")

    if candidate_line.startswith("candidate:"):
        candidate_line = candidate_line[len("candidate:") :]
    candidate = candidate_from_sdp(candidate_line)
    candidate.sdpMid = candidate_payload.get("sdpMid")
    candidate.sdpMLineIndex = candidate_payload.get("sdpMLineIndex")
    if not candidate_matches_policy(candidate, config):
        logger.info(
            "filtered remote candidate peer_id=%s session_id=%s ip=%s protocol=%s type=%s",
            peer.peer_id,
            peer.active_session_id,
            candidate.ip,
            candidate.protocol,
            candidate.type,
        )
        return {"ok": True, "filtered": True}
    await peer.pc.addIceCandidate(candidate)
    return {"ok": True}


async def websocket_signal(request: web.Request) -> web.StreamResponse:
    config: ServerConfig = request.app["config"]
    peer_registry: PeerRegistry = request.app["peer_registry"]
    peer_id = str(request.query.get("peerId", "")).strip()
    if not peer_id:
        raise web.HTTPBadRequest(text='{"error":"peerId is required"}', content_type="application/json")

    peer = await peer_registry.get(peer_id)
    if peer is None:
        raise web.HTTPNotFound(text='{"error":"peer session not found"}', content_type="application/json")

    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)
    await peer.register_signal_socket(ws)
    try:
        await peer.send_signal_snapshot(ws)
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                if message.type == WSMsgType.ERROR:
                    logger.warning("signal websocket closed with error peer_id=%s", peer.peer_id)
                continue
            try:
                payload = json.loads(message.data)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "error": "invalid json"})
                continue
            if not isinstance(payload, dict):
                await ws.send_json({"type": "error", "error": "invalid payload"})
                continue
            message_type = str(payload.get("type", "")).strip()
            if message_type == "candidate":
                try:
                    result = await apply_remote_candidate(peer, payload.get("candidate"), config)
                except web.HTTPException as exc:
                    await ws.send_json({"type": "error", "error": exc.text or exc.reason})
                    continue
                await ws.send_json({"type": "candidate-ack", **result})
                continue
            await ws.send_json({"type": "error", "error": f"unsupported message type: {message_type or 'unknown'}"})
    finally:
        await peer.unregister_signal_socket(ws)
    return ws


async def post_candidate(request: web.Request) -> web.Response:
    config: ServerConfig = request.app["config"]
    peer_registry: PeerRegistry = request.app["peer_registry"]
    data = await request.json()
    peer_id = str(data.get("peerId", "")).strip() or None
    session_id = str(data.get("sessionId", "")).strip() or None
    if not peer_id and not session_id:
        response = web.json_response({"error": "peerId or sessionId is required"}, status=400)
        return with_cors(response, config.public_origin)

    peer = await resolve_peer(peer_registry, peer_id=peer_id, session_id=session_id)
    if peer is None:
        response = web.json_response({"error": "peer session not found"}, status=404)
        return with_cors(response, config.public_origin)

    try:
        result = await apply_remote_candidate(peer, data.get("candidate"), config)
    except web.HTTPBadRequest:
        response = web.json_response({"error": "invalid candidate payload"}, status=400)
        return with_cors(response, config.public_origin)
    response = web.json_response(result)
    return with_cors(response, config.public_origin)


async def get_candidates(request: web.Request) -> web.Response:
    config: ServerConfig = request.app["config"]
    peer_registry: PeerRegistry = request.app["peer_registry"]
    peer_id = str(request.query.get("peerId", "")).strip() or None
    session_id = str(request.query.get("sessionId", "")).strip() or None
    cursor = int(request.query.get("cursor", "0"))
    if not peer_id and not session_id:
        response = web.json_response({"error": "peerId or sessionId is required"}, status=400)
        return with_cors(response, config.public_origin)

    peer = await resolve_peer(peer_registry, peer_id=peer_id, session_id=session_id)
    if peer is None:
        response = web.json_response({"error": "peer session not found"}, status=404)
        return with_cors(response, config.public_origin)

    payload = await peer.candidates_snapshot(cursor)
    response = web.json_response(payload)
    return with_cors(response, config.public_origin)


async def post_bootstrap_audio(request: web.Request) -> web.Response:
    config: ServerConfig = request.app["config"]
    peer_registry: PeerRegistry = request.app["peer_registry"]
    data = await request.json()
    peer_id = str(data.get("peerId", "")).strip() or None
    session_id = str(data.get("sessionId", "")).strip() or None
    if not peer_id and not session_id:
        response = web.json_response({"error": "peerId or sessionId is required"}, status=400)
        return with_cors(response, config.public_origin)

    peer = await resolve_peer(peer_registry, peer_id=peer_id, session_id=session_id)
    if peer is None:
        response = web.json_response({"error": "peer session not found"}, status=404)
        return with_cors(response, config.public_origin)

    session = await peer.get_active_session()
    if session is None or (session_id and session.session_id != session_id):
        response = web.json_response({"error": "active session not found"}, status=404)
        return with_cors(response, config.public_origin)

    audio_payload = data.get("audio")
    if not isinstance(audio_payload, dict):
        response = web.json_response({"error": "audio payload is required"}, status=400)
        return with_cors(response, config.public_origin)

    samples = decode_bootstrap_audio(audio_payload, session.params.sample_rate)
    await session.add_audio_samples(samples)
    response = web.json_response(
        {
            "ok": True,
            "bootstrapAudioMs": samples.shape[0] * 1000.0 / session.params.sample_rate,
        }
    )
    return with_cors(response, config.public_origin)


async def post_session_start(request: web.Request) -> web.Response:
    config: ServerConfig = request.app["config"]
    peer_registry: PeerRegistry = request.app["peer_registry"]
    session_manager: SessionManager = request.app["session_manager"]
    data = await request.json()
    peer_id = str(data.get("peerId", "")).strip()
    if not peer_id:
        response = web.json_response({"error": "peerId is required"}, status=400)
        return with_cors(response, config.public_origin)

    peer = await peer_registry.get(peer_id)
    if peer is None:
        response = web.json_response({"error": "peer session not found"}, status=404)
        return with_cors(response, config.public_origin)

    requested_capture_ms = int(data.get("captureMs", config.capture_ms))
    requested_input_chunk_ms = int(data.get("inputChunkMs", config.input_chunk_ms))
    avatar_id_raw = data.get("avatarId")
    avatar_id = str(avatar_id_raw).strip() if avatar_id_raw is not None else None
    if avatar_id == "":
        avatar_id = None
    client_name_raw = data.get("clientName")
    client_name = str(client_name_raw).strip() if client_name_raw is not None else None
    if client_name == "":
        client_name = None

    try:
        session = await peer.start_session(
            capture_ms=requested_capture_ms,
            input_chunk_ms=requested_input_chunk_ms,
            client_name=client_name,
            avatar_id=avatar_id,
        )
    except ValueError as exc:
        response = web.json_response({"error": str(exc)}, status=400)
        return with_cors(response, config.public_origin)
    except SessionBusyError:
        session_stats = await session_manager.snapshot()
        response = web.json_response(
            {
                "error": "server is busy; max concurrent session limit reached",
                **session_stats,
            },
            status=409,
        )
        return with_cors(response, config.public_origin)

    response = web.json_response(
        {
            "ok": True,
            "peerId": peer.peer_id,
            "sessionId": session.session_id,
            "captureMs": requested_capture_ms,
            "inputChunkMs": requested_input_chunk_ms,
            "avatarId": session.avatar_id,
        }
    )
    return with_cors(response, config.public_origin)


async def post_session_stop(request: web.Request) -> web.Response:
    config: ServerConfig = request.app["config"]
    peer_registry: PeerRegistry = request.app["peer_registry"]
    data = await request.json()
    peer_id = str(data.get("peerId", "")).strip()
    if not peer_id:
        response = web.json_response({"error": "peerId is required"}, status=400)
        return with_cors(response, config.public_origin)

    peer = await peer_registry.get(peer_id)
    if peer is None:
        response = web.json_response({"error": "peer session not found"}, status=404)
        return with_cors(response, config.public_origin)

    session = await peer.stop_session()
    response = web.json_response(
        {
            "ok": True,
            "peerId": peer.peer_id,
            "stoppedSessionId": session.session_id if session is not None else None,
        }
    )
    return with_cors(response, config.public_origin)


async def offer(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return with_cors(web.Response(status=204), request.app["config"].public_origin)

    config: ServerConfig = request.app["config"]
    runtime: SoulXRuntime = request.app["runtime"]
    session_manager: SessionManager = request.app["session_manager"]
    stats_registry: SessionStatsRegistry = request.app["stats_registry"]
    peer_registry: PeerRegistry = request.app["peer_registry"]

    pc: RTCPeerConnection | None = None
    peer_context: PeerContext | None = None
    disconnect_cleanup_task: asyncio.Task[None] | None = None

    try:
        data = await request.json()
        peer_id = uuid.uuid4().hex
        client_name_raw = data.get("clientName")
        client_name = str(client_name_raw).strip() if client_name_raw is not None else None
        if client_name == "":
            client_name = None
        auto_start_session = bool(data.get("autoStartSession", True))
        requested_capture_ms = int(data.get("captureMs", config.capture_ms))
        requested_input_chunk_ms = int(data.get("inputChunkMs", config.input_chunk_ms))
        avatar_id_raw = data.get("avatarId")
        requested_avatar_id = str(avatar_id_raw).strip() if avatar_id_raw is not None else None
        if requested_avatar_id == "":
            requested_avatar_id = None
        resolved_avatar_id = runtime.resolve_avatar_id(requested_avatar_id)
        logger.info(
            "received offer remote=%s peer_id=%s client_name=%s capture_ms=%s input_chunk_ms=%s auto_start_session=%s avatar_id=%s",
            request.remote,
            peer_id,
            client_name,
            requested_capture_ms,
            requested_input_chunk_ms,
            auto_start_session,
            resolved_avatar_id,
        )

        pc = RTCPeerConnection(configuration=build_rtc_configuration(config))
        request.app["pcs"].add(pc)
        peer_context = PeerContext(
            peer_id=peer_id,
            pc=pc,
            session_manager=session_manager,
            stats_registry=stats_registry,
            runtime=runtime,
        )
        await peer_registry.register(peer_context)

        @pc.on("track")
        def on_track(track: Any) -> None:
            logger.info("received remote track kind=%s id=%s", track.kind, getattr(track, "id", "unknown"))
            if track.kind != "audio":
                return

            pc.addTrack(GeneratedAudioTrack(peer_context, runtime.params.sample_rate))
            pc.addTrack(GeneratedVideoTrack(peer_context, runtime.params.tgt_fps, runtime.idle_frame))
            asyncio.create_task(
                consume_audio_track(
                    track,
                    peer_context,
                    runtime.params.sample_rate,
                    request.app["pcs"],
                    peer_registry,
                )
            )

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            nonlocal disconnect_cleanup_task
            logger.info("peer connection state=%s", pc.connectionState)

            if pc.connectionState == "connected" and disconnect_cleanup_task is not None:
                disconnect_cleanup_task.cancel()
                disconnect_cleanup_task = None
                return

            if pc.connectionState == "disconnected":
                if disconnect_cleanup_task is None:
                    disconnect_cleanup_task = asyncio.create_task(
                        cleanup_disconnected_peer_after_grace(
                            peer_context,
                            request.app["pcs"],
                            peer_registry,
                            DISCONNECTED_CLEANUP_GRACE_SEC,
                        )
                    )
                return

            if disconnect_cleanup_task is not None:
                disconnect_cleanup_task.cancel()
                disconnect_cleanup_task = None

            if pc.connectionState in {"failed", "closed"}:
                await cleanup_peer(peer_context, request.app["pcs"], peer_registry)

        offer_sdp = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
        await pc.setRemoteDescription(offer_sdp)
        answer = await pc.createAnswer()
        asyncio.create_task(
            finalize_local_description(
                peer_context,
                answer,
                peer_registry,
                stats_registry,
                request.app["pcs"],
                config,
            )
        )

        payload: dict[str, Any] = {
            "sdp": answer.sdp,
            "type": answer.type,
            "peerId": peer_context.peer_id,
            "sessionId": None,
            "avatarId": None,
        }
        if auto_start_session:
            session = await peer_context.start_session(
                capture_ms=requested_capture_ms,
                input_chunk_ms=requested_input_chunk_ms,
                client_name=client_name,
                avatar_id=resolved_avatar_id,
            )
            payload["sessionId"] = session.session_id
            payload["avatarId"] = session.avatar_id
        logger.info("local answer created for remote=%s", request.remote)
        response = web.json_response(payload)
        return with_cors(response, config.public_origin)
    except SessionBusyError:
        if disconnect_cleanup_task is not None:
            disconnect_cleanup_task.cancel()
        if peer_context is not None:
            await cleanup_peer(peer_context, request.app["pcs"], peer_registry)
        elif pc is not None:
            request.app["pcs"].discard(pc)
            await pc.close()
        session_stats = await session_manager.snapshot()
        response = web.json_response(
            {
                "error": "server is busy; max concurrent session limit reached",
                **session_stats,
            },
            status=409,
        )
        return with_cors(response, config.public_origin)
    except ValueError as exc:
        if disconnect_cleanup_task is not None:
            disconnect_cleanup_task.cancel()
        if peer_context is not None:
            await cleanup_peer(peer_context, request.app["pcs"], peer_registry)
        elif pc is not None:
            request.app["pcs"].discard(pc)
            await pc.close()
        response = web.json_response({"error": str(exc)}, status=400)
        return with_cors(response, config.public_origin)
    except Exception:
        logger.exception("offer handling failed")
        if disconnect_cleanup_task is not None:
            disconnect_cleanup_task.cancel()
        if peer_context is not None:
            await cleanup_peer(peer_context, request.app["pcs"], peer_registry)
        elif pc is not None:
            request.app["pcs"].discard(pc)
            await pc.close()
        raise


async def cleanup_peer(
    peer: PeerContext,
    pcs: set[RTCPeerConnection],
    peer_registry: PeerRegistry,
) -> None:
    if peer.pc in pcs:
        pcs.discard(peer.pc)
    logger.info("cleaning up peer connection")
    try:
        await peer.close()
    finally:
        try:
            await peer.pc.close()
        finally:
            await peer_registry.remove(peer.peer_id)


async def cleanup_disconnected_peer_after_grace(
    peer: PeerContext,
    pcs: set[RTCPeerConnection],
    peer_registry: PeerRegistry,
    grace_sec: float,
) -> None:
    try:
        await asyncio.sleep(max(grace_sec, 0.0))
        if peer.pc.connectionState == "disconnected":
            logger.info(
                "peer remained disconnected after grace period; cleaning up peer_id=%s grace_sec=%.1f",
                peer.peer_id,
                grace_sec,
            )
            await cleanup_peer(peer, pcs, peer_registry)
    except asyncio.CancelledError:
        return


async def consume_audio_track(
    track: Any,
    peer: PeerContext,
    target_rate: int,
    pcs: set[RTCPeerConnection],
    peer_registry: PeerRegistry,
) -> None:
    resampler = av.AudioResampler(format="s16", layout="mono", rate=target_rate)
    frame_count = 0
    try:
        while True:
            frame = await track.recv()
            resampled = resampler.resample(frame)
            if resampled is None:
                continue
            frames = resampled if isinstance(resampled, list) else [resampled]
            for audio_frame in frames:
                samples = audio_frame.to_ndarray().astype(np.float32).reshape(-1)
                samples /= 32768.0
                frame_count += 1
                if frame_count <= 5 or frame_count % 100 == 0:
                    logger.info(
                        "received audio frame count=%s samples=%s rms=%.6f",
                        frame_count,
                        samples.shape[0],
                        float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0,
                    )
                await peer.add_audio_samples(samples)
    except MediaStreamError:
        logger.info("audio track ended after %s frames", frame_count)
        await cleanup_peer(peer, pcs, peer_registry)
    except Exception:
        logger.exception("audio track consumption failed after %s frames", frame_count)
        await cleanup_peer(peer, pcs, peer_registry)


async def finalize_local_description(
    peer: PeerContext,
    answer: RTCSessionDescription,
    peer_registry: PeerRegistry,
    stats_registry: SessionStatsRegistry,
    pcs: set[RTCPeerConnection],
    config: ServerConfig,
) -> None:
    try:
        await peer.pc.setLocalDescription(answer)
        candidates, complete = extract_local_candidates(peer.pc.localDescription, config)
        await peer.set_local_candidates(candidates, complete)
    except Exception:
        logger.exception("background local description finalization failed peer_id=%s", peer.peer_id)
        await cleanup_peer(peer, pcs, peer_registry)


def extract_local_candidates(
    local_description: RTCSessionDescription | None,
    config: ServerConfig,
) -> tuple[list[dict[str, Any]], bool]:
    if local_description is None:
        return [], False
    parsed = SdpSessionDescription.parse(local_description.sdp)
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, int]] = set()
    complete = True
    for media_index, media in enumerate(parsed.media):
        complete = complete and bool(media.ice_candidates_complete)
        mid = media.rtp.muxId
        for candidate in media.ice_candidates:
            if not candidate_matches_policy(candidate, config):
                continue
            payload = {
                "candidate": f"candidate:{candidate_to_sdp(candidate)}",
                "sdpMid": mid,
                "sdpMLineIndex": media_index,
            }
            key = (payload["candidate"], payload["sdpMid"], payload["sdpMLineIndex"])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(payload)
    return candidates, complete


def candidate_matches_policy(candidate: RTCIceCandidate, config: ServerConfig) -> bool:
    if config.ice_mode != "tailscale":
        return True
    if (candidate.type or "").lower() != "host":
        return False
    if (candidate.protocol or "").lower() != "udp":
        return False
    return ip_matches_policy(candidate.ip or "", config)


def ip_matches_policy(ip: str, config: ServerConfig) -> bool:
    normalized = ip.lower()
    if not normalized:
        return False
    if any(normalized.startswith(prefix.lower()) for prefix in config.ice_tailscale_ipv4_prefixes):
        return True
    if any(normalized.startswith(prefix.lower()) for prefix in config.ice_tailscale_ipv6_prefixes):
        return True
    return False


def decode_bootstrap_audio(audio_payload: dict[str, Any], target_rate: int) -> np.ndarray:
    pcm16_b64 = str(audio_payload.get("pcm16Base64", "")).strip()
    if not pcm16_b64:
        return np.empty(0, dtype=np.float32)
    sample_rate = int(audio_payload.get("sampleRate", target_rate))
    raw = base64.b64decode(pcm16_b64)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if sample_rate == target_rate or samples.size == 0:
        return samples
    target_len = max(1, int(round(samples.shape[0] * target_rate / sample_rate)))
    src_positions = np.arange(samples.shape[0], dtype=np.float32)
    dst_positions = np.linspace(0, samples.shape[0] - 1, num=target_len, dtype=np.float32)
    return np.interp(dst_positions, src_positions, samples).astype(np.float32)
