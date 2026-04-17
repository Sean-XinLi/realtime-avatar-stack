# Streaming Video Project

This repository packages two related applications in one place:

- `avatar_console`: a local browser-based control surface that turns text or speech into streamed audio input
- `streaming_video_server`: a WebRTC server that receives audio, runs streaming inference, and returns synchronized audio and video

## Architecture

The intended flow is:

1. Start `streaming_video_server`.
2. Start `avatar_console`.
3. Point the console at the server URL.
4. Send text or speech from the console and play back the synchronized result.

`avatar_console` is the operator-facing entry point. `streaming_video_server` is the runtime service that handles WebRTC, inference scheduling, and media playout.

## Repository Layout

- `avatar_console/`: local console, TTS pipeline, Codex-driven reply mode, browser UI
- `streaming_video_server/`: WebRTC server, inference runtime, debug client, scripts, and tests

Each subproject keeps its own README with the detailed setup and API documentation:

- [avatar_console/README.md](./avatar_console/README.md)
- [streaming_video_server/README.md](./streaming_video_server/README.md)

## Quick Start

### 1. Start the server

```bash
cd streaming_video_server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.server.example .env.server
```

Update `.env.server` with the correct `SOULX_*` paths for your machine, then run:

```bash
python -m server.app
```

For protocol-only local testing without the real model:

```bash
STREAM_MOCK_INFERENCE=1 python -m server.app
```

### 2. Start the console

```bash
cd avatar_console
npm start
```

Open `http://127.0.0.1:3010` and point it at `http://127.0.0.1:8080` unless your server runs elsewhere.

## Notes

- `avatar_console` depends on macOS `say` and `afconvert`, plus a working `codex exec`.
- `streaming_video_server` needs `SoulX-FlashHead` and its model dependencies for real inference.
- The checked-in `.env.server.example` is intentionally generic. It is meant to be copied and edited locally rather than used as-is.
