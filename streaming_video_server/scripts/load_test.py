#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import time
from dataclasses import dataclass
from fractions import Fraction

import aiohttp
import av
import numpy as np
from aiortc import (
    AudioStreamTrack,
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import SessionDescription as SdpSessionDescription
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp

DEFAULT_BOOTSTRAP_FLOOR_MS = 640
BOOTSTRAP_SLACK_MS = 80
REMOTE_CANDIDATE_POLL_SEC = 0.2


class SineAudioStreamTrack(AudioStreamTrack):
    def __init__(self, sample_rate: int = 16000, frame_duration: float = 0.02, frequency: float = 220.0) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.frame_samples = int(sample_rate * frame_duration)
        self.frequency = frequency
        self._start: float | None = None
        self._pts = 0

    async def recv(self) -> av.AudioFrame:
        if self._start is None:
            self._start = time.monotonic()
        else:
            target_time = self._start + (self._pts / self.sample_rate)
            delay = target_time - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

        t = (np.arange(self.frame_samples, dtype=np.float32) + self._pts) / self.sample_rate
        samples = 0.15 * np.sin(2 * math.pi * self.frequency * t)
        pcm = np.clip(samples * 32767.0, -32768.0, 32767.0).astype(np.int16)

        frame = av.AudioFrame(format="s16", layout="mono", samples=self.frame_samples)
        frame.planes[0].update(pcm.tobytes())
        frame.sample_rate = self.sample_rate
        frame.time_base = Fraction(1, self.sample_rate)
        frame.pts = self._pts
        self._pts += self.frame_samples
        return frame


@dataclass(slots=True)
class ClientStats:
    name: str
    peer_id: str | None = None
    session_id: str | None = None
    first_frame_ms: float | None = None
    first_media_frame_ms: float | None = None
    video_frames: int = 0
    audio_frames: int = 0
    offer_started_at: float = 0.0
    started_at: float = 0.0
    ended_at: float = 0.0
    connection_state: str = "new"
    error: str | None = None
    server_stats: dict[str, object] | None = None

    @property
    def duration(self) -> float:
        if self.started_at <= 0 or self.ended_at <= self.started_at:
            return 0.0
        return self.ended_at - self.started_at

    @property
    def avg_video_fps(self) -> float:
        duration = self.duration
        if duration <= 0:
            return 0.0
        return self.video_frames / duration

    @property
    def post_first_fps(self) -> float:
        if self.first_media_frame_ms is None or self.video_frames <= 1:
            return 0.0
        active_duration = self.duration - (self.first_media_frame_ms / 1000.0)
        if active_duration <= 0:
            return 0.0
        return (self.video_frames - 1) / active_duration


@dataclass(slots=True)
class ServerRuntimeConfig:
    ice_servers: list[dict[str, object]]
    model_slice_ms: int
    startup_partial_ratio: float
    startup_min_audio_ms: int


def build_rtc_configuration(config: ServerRuntimeConfig) -> RTCConfiguration:
    if not config.ice_servers:
        return RTCConfiguration(iceServers=[])
    servers: list[RTCIceServer] = []
    for item in config.ice_servers:
        urls = item.get("urls", [])
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list):
            continue
        urls = [str(url) for url in urls if str(url).strip()]
        if urls:
            servers.append(RTCIceServer(urls=urls))
    return RTCConfiguration(iceServers=servers)


def get_startup_min_audio_ms(config: ServerRuntimeConfig, input_chunk_ms: int) -> int:
    ratio_based = int(round(config.model_slice_ms * config.startup_partial_ratio))
    return min(
        config.model_slice_ms,
        max(
            input_chunk_ms,
            DEFAULT_BOOTSTRAP_FLOOR_MS,
            config.startup_min_audio_ms,
            ratio_based,
        ),
    )


def compute_bootstrap_target_ms(config: ServerRuntimeConfig, input_chunk_ms: int) -> int:
    startup_min_audio_ms = get_startup_min_audio_ms(config, input_chunk_ms)
    optimistic_overlap_ms = min(200, max(0, config.model_slice_ms - startup_min_audio_ms))
    return min(
        config.model_slice_ms,
        max(startup_min_audio_ms, config.model_slice_ms - optimistic_overlap_ms + BOOTSTRAP_SLACK_MS),
    )


async def consume_video(track: MediaStreamTrack, stats: ClientStats) -> None:
    try:
        while True:
            await track.recv()
            now = time.monotonic()
            stats.video_frames += 1
            if stats.first_frame_ms is None:
                reference_started_at = stats.offer_started_at or stats.started_at
                stats.first_frame_ms = (now - reference_started_at) * 1000.0
            if stats.first_media_frame_ms is None and stats.started_at > 0:
                stats.first_media_frame_ms = (now - stats.started_at) * 1000.0
    except Exception:
        return


async def consume_audio(track: MediaStreamTrack, stats: ClientStats) -> None:
    try:
        while True:
            await track.recv()
            stats.audio_frames += 1
    except Exception:
        return


def extract_local_candidates(local_description: RTCSessionDescription | None) -> tuple[list[dict[str, object]], bool]:
    if local_description is None:
        return [], False
    parsed = SdpSessionDescription.parse(local_description.sdp)
    candidates: list[dict[str, object]] = []
    seen: set[tuple[str, str | None, int]] = set()
    complete = True
    for media_index, media in enumerate(parsed.media):
        complete = complete and bool(media.ice_candidates_complete)
        mid = media.rtp.muxId
        for candidate in media.ice_candidates:
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


def build_bootstrap_audio_payload(sample_rate: int, frequency: float, duration_ms: int) -> dict[str, object]:
    sample_count = max(1, int(round(sample_rate * duration_ms / 1000.0)))
    t = np.arange(sample_count, dtype=np.float32) / sample_rate
    samples = 0.15 * np.sin(2 * math.pi * frequency * t)
    pcm = np.clip(samples * 32767.0, -32768.0, 32767.0).astype(np.int16)
    return {
        "sampleRate": sample_rate,
        "pcm16Base64": base64.b64encode(pcm.tobytes()).decode("ascii"),
    }


async def post_candidate(
    http_session: aiohttp.ClientSession,
    server_url: str,
    peer_id: str | None,
    session_id: str | None,
    candidate: dict[str, object] | None,
) -> None:
    payload: dict[str, object] = {"candidate": candidate}
    if peer_id:
        payload["peerId"] = peer_id
    if session_id:
        payload["sessionId"] = session_id
    async with http_session.post(
        f"{server_url.rstrip('/')}/candidate",
        json=payload,
    ) as response:
        body = await response.text()
        if response.status != 200:
            raise RuntimeError(f"candidate failed: {response.status} {body}")


async def flush_local_candidates_after_gather(
    http_session: aiohttp.ClientSession,
    server_url: str,
    peer_id: str | None,
    session_id: str | None,
    pc: RTCPeerConnection,
    local_description_task: asyncio.Task[None],
) -> None:
    await local_description_task
    candidates, complete = extract_local_candidates(pc.localDescription)
    for candidate in candidates:
        await post_candidate(http_session, server_url, peer_id, session_id, candidate)
    if complete:
        await post_candidate(http_session, server_url, peer_id, session_id, None)


async def wait_for_local_offer_started(
    pc: RTCPeerConnection,
    local_description_task: asyncio.Task[None],
    timeout: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout
    while pc.signalingState != "have-local-offer":
        if local_description_task.done():
            await local_description_task
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting for local offer state")
        await asyncio.sleep(0.01)


async def poll_remote_candidates(
    http_session: aiohttp.ClientSession,
    server_url: str,
    peer_id: str | None,
    session_id: str | None,
    pc: RTCPeerConnection,
    stop_event: asyncio.Event,
) -> None:
    cursor = 0
    complete = False
    seen: set[tuple[str, str | None, int | None]] = set()
    while not stop_event.is_set() and not complete:
        async with http_session.get(
            f"{server_url.rstrip('/')}/candidates",
            params={
                **({"peerId": peer_id} if peer_id else {}),
                **({"sessionId": session_id} if session_id else {}),
                "cursor": cursor,
            },
        ) as response:
            body = await response.text()
            if response.status == 404 and stop_event.is_set():
                return
            if response.status != 200:
                raise RuntimeError(f"remote candidates failed: {response.status} {body}")
            payload = json.loads(body)

        for candidate_payload in payload.get("candidates", []):
            candidate_line = str(candidate_payload.get("candidate", "")).strip()
            if not candidate_line:
                continue
            key = (
                candidate_line,
                candidate_payload.get("sdpMid"),
                candidate_payload.get("sdpMLineIndex"),
            )
            if key in seen:
                continue
            seen.add(key)
            if candidate_line.startswith("candidate:"):
                candidate_line = candidate_line[len("candidate:") :]
            candidate = candidate_from_sdp(candidate_line)
            candidate.sdpMid = candidate_payload.get("sdpMid")
            candidate.sdpMLineIndex = candidate_payload.get("sdpMLineIndex")
            await pc.addIceCandidate(candidate)

        cursor = int(payload.get("nextCursor", cursor))
        complete = bool(payload.get("complete"))
        if complete:
            await pc.addIceCandidate(None)
            return
        await asyncio.sleep(REMOTE_CANDIDATE_POLL_SEC)


async def upload_bootstrap_audio(
    http_session: aiohttp.ClientSession,
    server_url: str,
    peer_id: str | None,
    session_id: str,
    sample_rate: int,
    frequency: float,
    duration_ms: int,
) -> None:
    payload = build_bootstrap_audio_payload(sample_rate, frequency, duration_ms)
    async with http_session.post(
        f"{server_url.rstrip('/')}/bootstrap-audio",
        json={"peerId": peer_id, "sessionId": session_id, "audio": payload},
    ) as response:
        body = await response.text()
        if response.status != 200:
            raise RuntimeError(f"bootstrap audio failed: {response.status} {body}")


async def run_client(
    server_url: str,
    client_index: int,
    duration: float,
    capture_ms: int,
    input_chunk_ms: int,
    sample_rate: int,
    runtime_config: ServerRuntimeConfig,
) -> ClientStats:
    stats = ClientStats(name=f"client-{client_index + 1}")
    pc = RTCPeerConnection(configuration=build_rtc_configuration(runtime_config))
    tasks: list[asyncio.Task[None]] = []
    background_tasks: list[asyncio.Task[None]] = []
    local_description_task: asyncio.Task[None] | None = None
    stop_event = asyncio.Event()
    http_session = aiohttp.ClientSession()

    @pc.on("connectionstatechange")
    async def on_connectionstatechange() -> None:
        stats.connection_state = pc.connectionState

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        if track.kind == "video":
            tasks.append(asyncio.create_task(consume_video(track, stats)))
        elif track.kind == "audio":
            tasks.append(asyncio.create_task(consume_audio(track, stats)))

    try:
        frequency = 220.0 + client_index * 20.0
        audio_track = SineAudioStreamTrack(sample_rate=sample_rate, frequency=frequency)
        pc.addTrack(audio_track)
        pc.addTransceiver("audio", direction="recvonly")
        pc.addTransceiver("video", direction="recvonly")

        stats.offer_started_at = time.monotonic()
        offer = await pc.createOffer()
        local_description_task = asyncio.create_task(pc.setLocalDescription(offer))
        await wait_for_local_offer_started(pc, local_description_task)

        payload = {
            "sdp": offer.sdp,
            "type": offer.type,
            "clientName": stats.name,
            "captureMs": capture_ms,
            "inputChunkMs": input_chunk_ms,
        }
        async with http_session.post(f"{server_url.rstrip('/')}/offer", json=payload) as response:
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(f"offer failed: {response.status} {body}")
            answer = json.loads(body)
            stats.peer_id = answer.get("peerId")
            stats.session_id = answer.get("sessionId")

        if stats.session_id is None:
            raise RuntimeError("offer response missing sessionId")

        bootstrap_target_ms = compute_bootstrap_target_ms(runtime_config, input_chunk_ms)
        background_tasks.append(
            asyncio.create_task(
                poll_remote_candidates(http_session, server_url, stats.peer_id, stats.session_id, pc, stop_event)
            )
        )
        background_tasks.append(
            asyncio.create_task(
                upload_bootstrap_audio(
                    http_session,
                    server_url,
                    stats.peer_id,
                    stats.session_id,
                    sample_rate,
                    frequency,
                    bootstrap_target_ms,
                )
            )
        )

        await local_description_task
        await flush_local_candidates_after_gather(
            http_session,
            server_url,
            stats.peer_id,
            stats.session_id,
            pc,
            local_description_task,
        )
        stats.started_at = time.monotonic()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
        await asyncio.sleep(duration)
    except Exception as exc:
        stats.error = str(exc)
    finally:
        stats.ended_at = time.monotonic()
        stop_event.set()
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if local_description_task is not None and not local_description_task.done():
            local_description_task.cancel()
            await asyncio.gather(local_description_task, return_exceptions=True)
        await pc.close()
        await http_session.close()
    return stats


async def fetch_server_stats(server_url: str, session_ids: list[str], timeout: float = 5.0) -> dict[str, dict[str, object]]:
    if not session_ids:
        return {}

    deadline = time.monotonic() + timeout
    params = [("sessionId", session_id) for session_id in session_ids]
    while True:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{server_url.rstrip('/')}/stats", params=params) as response:
                response.raise_for_status()
                payload = await response.json()
        completed = {
            item["sessionId"]: item
            for item in payload.get("completed", [])
            if item.get("sessionId") in session_ids
        }
        if len(completed) == len(session_ids) or time.monotonic() >= deadline:
            return completed
        await asyncio.sleep(0.2)


async def fetch_runtime_config(server_url: str) -> ServerRuntimeConfig:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{server_url.rstrip('/')}/config") as response:
            response.raise_for_status()
            payload = await response.json()
    return ServerRuntimeConfig(
        ice_servers=list(payload.get("iceServers") or []),
        model_slice_ms=int(payload.get("modelSliceMs") or 960),
        startup_partial_ratio=float(payload.get("startupPartialRatio") or 0.75),
        startup_min_audio_ms=int(payload.get("startupMinAudioMs") or 720),
    )


async def run_test(args: argparse.Namespace) -> list[ClientStats]:
    runtime_config = await fetch_runtime_config(args.server_url)
    tasks = [
        asyncio.create_task(
            run_client(
                server_url=args.server_url,
                client_index=index,
                duration=args.duration,
                capture_ms=args.capture_ms,
                input_chunk_ms=args.input_chunk_ms,
                sample_rate=args.sample_rate,
                runtime_config=runtime_config,
            )
        )
        for index in range(args.clients)
    ]
    stats_list = await asyncio.gather(*tasks)
    session_ids = [stats.session_id for stats in stats_list if stats.session_id]
    server_stats = await fetch_server_stats(args.server_url, session_ids)
    for stats in stats_list:
        if stats.session_id and stats.session_id in server_stats:
            stats.server_stats = server_stats[stats.session_id]
    return stats_list


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WebRTC load test for the streaming video server")
    parser.add_argument("--server-url", default="http://127.0.0.1:18000")
    parser.add_argument("--clients", type=int, default=3)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--capture-ms", type=int, default=20)
    parser.add_argument("--input-chunk-ms", type=int, default=40)
    parser.add_argument("--sample-rate", type=int, default=16000)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    stats_list = asyncio.run(run_test(args))

    print(
        f"{'client':<10} {'state':<12} {'first_ms':>9} {'real_ms':>9} {'avg_fps':>8} {'post_fps':>9} {'srv_gen':>8} {'srv_idle':>8} {'idle_%':>7} {'avg_inf':>8} {'pend_end':>9} {'pend_hi':>8} {'drop_ms':>8} {'error':<24}"
    )
    for stats in stats_list:
        server_stats = stats.server_stats or {}
        print(
            f"{stats.name:<10} "
            f"{stats.connection_state:<12} "
            f"{stats.first_frame_ms if stats.first_frame_ms is not None else -1:9.2f} "
            f"{float(server_stats.get('firstGeneratedFrameServedMs', -1.0)):9.2f} "
            f"{stats.avg_video_fps:8.2f} "
            f"{stats.post_first_fps:9.2f} "
            f"{int(server_stats.get('generatedFramesServed', -1)):8d} "
            f"{int(server_stats.get('idleFramesServed', -1)):8d} "
            f"{float(server_stats.get('idleRatio', -1.0)) * 100:7.2f} "
            f"{float(server_stats.get('avgInferenceMs', -1.0)):8.2f} "
            f"{float(server_stats.get('pendingAudioMs', -1.0)):9.2f} "
            f"{float(server_stats.get('pendingAudioMsHighWatermark', -1.0)):8.2f} "
            f"{float(server_stats.get('droppedAudioMs', -1.0)):8.2f} "
            f"{(stats.error or '-'):24.24s}"
        )


if __name__ == "__main__":
    main()
