# Streaming Video Server

A WebRTC-based real-time streaming server. It receives an uplink audio track, runs streaming inference with `SoulX-FlashHead`, and returns generated audio and video tracks aligned to inference batches.

This repository is mainly about server runtime, configuration, and protocol contracts. The `debug_client/` directory is still kept as bundled debug and load-test static assets, but this document does not go deep into the client interaction flow.

## Current Scope

- The server exposes HTTP and WebSocket endpoints for WebRTC offer/answer exchange and ICE candidate signaling.
- It receives remote audio tracks, rebuilds audio according to the configured capture/chunk sizes, and schedules inference work.
- It returns generated audio and video, with both streams aligned to the same inference batches.
- It supports multiple inference workers, and each worker maintains its own pipeline and session state.
- By default it supports up to `3` concurrent sessions. Model weights are shared, but each session keeps isolated streaming state.
- It supports `mock inference`, so you can debug the service, protocol, and load-test pipeline without loading the real model.
- It provides `systemd` unit files and startup scripts.

## Model Window Constraints

The ingress layer already allows configurable `20ms capture + 40ms chunk`, but first-frame latency is still mainly constrained by the `SoulX-FlashHead` model window itself:

- The `lite` model generates a new video segment roughly every `960ms` of new audio.
- The `pro` model generates a new video segment roughly every `1120ms` of new audio.

Because of that, 40ms chunking mainly affects scheduling and smoothness. It does not reduce first-frame latency to far below the model window itself.

## Layout

- `server/`: main server logic, including configuration, WebRTC handling, inference scheduling, and audio/video sync
- `scripts/`: startup scripts, load-test scripts, and the helper script for `systemd` installation
- `deploy/systemd/`: service unit files
- `debug_client/`: static debug pages and the warm-peer benchmark page
- `tests/`: currently focused on playout and sync logic

## Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` only covers the server-side dependencies in this repository. To run real inference, you still need to install PyTorch, `transformers`, `flash-attn`, and other dependencies required by `SoulX-FlashHead`.

## Configuration

Start by copying the config template:

```bash
cp .env.server.example .env.server
```

If the repository already contains `.env.server`, treat that file as the source of truth. See [`.env.server.example`](./.env.server.example) and [`server/config.py`](./server/config.py) for the full field list.

The checked-in `.env.server.example` is intentionally generic:

- `SOULX_*` entries are placeholders and should be updated for your machine.
- `SOULX_COND_IMAGE` defaults to the repo-local `./assets/avatars` directory.
- Python defaults in `server/config.py` assume a sibling checkout at `../SoulX-FlashHead` if you do not override `SOULX_ROOT`.

Common environment variables:

- `STREAM_HOST`: bind address, default `0.0.0.0`
- `STREAM_PORT`: listen port, default `8080`
- `STREAM_SAMPLE_RATE`: audio sample rate, default `16000`
- `STREAM_OUTPUT_FPS`: output video frame rate, default `25`
- `STREAM_CAPTURE_MS`: logical input capture granularity on the server, default `20`
- `STREAM_INPUT_CHUNK_MS`: logical input queue chunk size, default `40`
- `STREAM_PLAYOUT_BUFFER_MS`: minimum generated audio buffer required before playback starts, default `300`
- `STREAM_INFERENCE_WORKERS`: number of inference workers, default `1`
- `STREAM_INFERENCE_STEP_MS`: minimum steady-state incremental dispatch step, default `0`, which disables steady-state incremental dispatch
- `STREAM_STARTUP_PARTIAL_RATIO`: partial-window ratio used for early startup dispatch, default `0.75`
- `STREAM_STARTUP_MIN_AUDIO_FLOOR_MS`: minimum audio floor for early startup dispatch, default `640`
- `STREAM_MAX_CONCURRENT_SESSIONS`: maximum number of active sessions, default `3`
- `STREAM_MAX_PENDING_AUDIO_MS`: maximum queued audio backlog per session, default `2000`
- `STREAM_STARTUP_WARMUP_RUNS`: warmup run count after server start, default `1`
- `STREAM_MODEL_TYPE`: `lite` or `pro`
- `STREAM_MOCK_INFERENCE`: set to `1` to skip loading the real model and return mock audio/video
- `STREAM_PUBLIC_ORIGIN`: allowed CORS origin, default `*`
- `STREAM_ICE_MODE`: `auto` or `tailscale`
- `STREAM_ICE_SERVER_URLS`: ICE servers used in `auto` mode, comma-separated
- `STREAM_ICE_TAILSCALE_IPV4_PREFIXES`: allowed IPv4 prefixes in `tailscale` mode, default `100.`
- `SOULX_ROOT`: root directory of `SoulX-FlashHead`
- `SOULX_CKPT_DIR`: model checkpoint directory
- `SOULX_WAV2VEC_DIR`: wav2vec model directory
- `SOULX_COND_IMAGE`: conditional image file or directory; if set to a directory, the service exposes an avatar list
- `SOULX_USE_FACE_CROP`: whether to enable face crop, `0/1`
- `SOULX_BASE_SEED`: base random seed

## Start the Server

Using the module entry point directly is recommended:

```bash
python -m server.app
```

If you want to reuse `.env.server` and a fixed Python interpreter, you can also use the helper script:

```bash
./scripts/run_server.sh
```

If you only want to validate the protocol or integration path locally, start with mock mode:

```bash
STREAM_MOCK_INFERENCE=1 python -m server.app
```

After startup, the default listen address is `http://0.0.0.0:8080`. The health check endpoint is:

```bash
curl http://127.0.0.1:8080/healthz
```

## systemd Deployment

The service unit file lives at [`deploy/systemd/streaming-video-server.service`](./deploy/systemd/streaming-video-server.service).

The checked-in unit files use `%h/streaming_video_project/streaming_video_server` as an example installation path. Adjust the path and user before installing them on a real machine.

Installation flow:

```bash
sudo cp deploy/systemd/streaming-video-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now streaming-video-server.service
sudo systemctl status streaming-video-server.service
```

You can also inspect the helper script first:

```bash
./scripts/install_service.sh
```

## API Overview

### `GET /healthz`

Health check. Returns:

```json
{"ok": true}
```

### `GET /config`

Returns the server's current runtime parameters, including:

- `sampleRate`
- `captureMs`
- `inputChunkMs`
- `outputFps`
- `playoutBufferMs`
- `modelType`
- `modelSliceMs`
- `inferenceWorkers`
- `inferenceStepMs`
- `startupPartialRatio`
- `startupMinAudioMs`
- `iceMode`
- `iceServers`
- `avatars`
- `defaultAvatarId`

### `POST /offer`

Creates an `RTCPeerConnection` and returns the answer. It can also auto-create a session.

Request body fields:

- `sdp`: offer SDP generated by the browser or client
- `type`: usually `offer`
- `clientName`: optional, used for logs and stats
- `autoStartSession`: optional, default `true`; if set to `false`, only the peer is created and no inference session starts yet
- `captureMs`: optional, overrides the default capture size
- `inputChunkMs`: optional, overrides the default chunk size
- `avatarId`: optional, selects the avatar for this session

Example response:

```json
{
  "sdp": "...answer sdp...",
  "type": "answer",
  "peerId": "peer-uuid",
  "sessionId": "session-uuid",
  "avatarId": "girl"
}
```

When `autoStartSession=false`, `sessionId` is `null`, and you must call `/session/start` explicitly afterward.

### `POST /session/start`

Starts a new inference session on an existing `peerId`.

Request body fields:

- `peerId`: required
- `captureMs`: optional
- `inputChunkMs`: optional
- `clientName`: optional
- `avatarId`: optional

Successful response:

```json
{
  "ok": true,
  "peerId": "peer-uuid",
  "sessionId": "session-uuid",
  "captureMs": 20,
  "inputChunkMs": 40,
  "avatarId": "girl"
}
```

If the concurrency limit is already reached, the server returns `409` together with current session stats.

### `POST /session/stop`

Stops the currently active session on the specified `peerId`.

### `POST /bootstrap-audio`

Preloads one chunk of PCM into an active session to warm up the initial inference stage. Request body format:

```json
{
  "peerId": "peer-uuid",
  "audio": {
    "sampleRate": 16000,
    "pcm16Base64": "..."
  }
}
```

You can also pass `sessionId` to target a session directly.

### `GET /ws?peerId=...`

WebSocket signaling channel. The server pushes local ICE candidates through it, and the client can send remote ICE candidates back with messages like:

```json
{
  "type": "candidate",
  "candidate": {
    "candidate": "candidate:...",
    "sdpMid": "0",
    "sdpMLineIndex": 0
  }
}
```

### `POST /candidate`

Legacy HTTP endpoint for candidate reporting. The request body must include either `peerId` or `sessionId`.

### `GET /candidates`

Pulls the server-side collected candidates by cursor, for clients or load-test scripts that do not use WebSocket.

### `GET /stats`

Returns session stats. You can filter a specific session with `?sessionId=<id>`.

### `GET /`

Currently redirects to `/debug-client/index.html`. This is mainly for the bundled debug page. For server integration and end-to-end wiring, call the APIs above directly.

## Protocol Notes

- Uplink audio enters the server through a WebRTC audio track.
- The server does not loop back the raw input audio. It plays generated audio aligned to inference batches instead.
- Video is emitted at `25 FPS` from the generated frame queue, and playback waits for a real generated frame before the first frame by default.
- Audio and video start in sync on the server through the same session-level playout gate.
- The checked-in environment files currently default the ICE strategy to `tailscale`. In that mode, both browser and server keep only `host/udp` candidates that match the configured prefix rules.
- Server input buffering uses a chunk queue plus a ring buffer. When backlog grows too large, the system drops old audio first to preserve real-time behavior.
- A single peer can be reused. After `/offer` creates the connection, you can repeatedly call `/session/start` and `/session/stop`.

## Tests

The built-in unit tests currently focus on playout and sync:

```bash
pytest tests/test_playout_sync.py
```

## Load Testing

Basic concurrent load test:

```bash
python scripts/load_test.py --server-url http://127.0.0.1:${STREAM_PORT:-8080} --clients 3 --duration 20
```

Warm-peer first-frame benchmark:

```bash
python scripts/warm_peer_bench.py --server-url http://127.0.0.1:${STREAM_PORT:-8080} --sessions 3
```

Notes:

- `scripts/load_test.py` is based on `aiortc` and actively exercises `/offer`, `/candidate`, `/bootstrap-audio`, and `/stats`.
- `scripts/warm_peer_bench.py` opens the bundled benchmark page in headless Chrome, so the machine needs a working Chrome or Chromium installation.
- `first_ms` is the total client-side time from connection start to first frame arrival.
- `real_ms` is the server-side time from session creation to the first real generated frame.
- `srv_gen` / `srv_idle` / `idle_%` / `pend_end` / `pend_hi` / `drop_ms` represent real generated frame count, placeholder frame count, placeholder ratio, final backlog, backlog high-water mark, and proactively dropped old-audio duration.
