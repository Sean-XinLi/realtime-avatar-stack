# Streaming Video Project

This repository packages two related applications in one monorepo:

- `avatar_console`: a local browser-based control surface that turns text or speech into streamed audio input
- `streaming_video_server`: a WebRTC server that receives audio, runs streaming inference, and returns synchronized audio and video

Together they form a local-to-remote interaction loop for text or speech driven avatar playback.

## How It Works

The intended flow is:

1. Start `streaming_video_server`.
2. Start `avatar_console`.
3. Open the console in your browser.
4. Point the console at the server URL.
5. Send text or speech from the console.
6. Play back the synchronized audio and video returned by the server.

`avatar_console` is the operator-facing entry point. `streaming_video_server` is the runtime service that handles WebRTC, inference scheduling, and media playout.

## Quick Start: Mock Mode

This is the fastest way to verify that the repo wiring works before setting up the real model.

### 1. Start the server in mock mode

```bash
cd streaming_video_server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.server.example .env.server
STREAM_MOCK_INFERENCE=1 python -m server.app
```

The mock server listens on `http://127.0.0.1:8080`.

If you prefer a prebuilt server image instead of local Python setup, see the Docker section in [streaming_video_server/README.md](./streaming_video_server/README.md).

### 2. Start the console

```bash
cd avatar_console
npm start
```

The console listens on `http://127.0.0.1:3010`.

### 3. Use the app

1. Open `http://127.0.0.1:3010`.
2. Keep the default server URL `http://127.0.0.1:8080`.
3. Click `Start persistent session`.
4. Enter text and choose either direct playback or reply mode.
5. Confirm that the page receives audio and video from the server.

Mock mode is only for protocol and UX validation. It does not run the real `SoulX-FlashHead` model.

## Real Inference Setup

To run real inference instead of mock output:

1. Check out and install `SoulX-FlashHead` on the same machine.
2. Copy `streaming_video_server/.env.server.example` to `.env.server`.
3. Update the `SOULX_*` paths in `.env.server`.
4. Start the server normally:

```bash
cd streaming_video_server
python -m server.app
```

More detail is in [streaming_video_server/README.md](./streaming_video_server/README.md).

## Requirements and Current Limits

- `avatar_console` currently depends on macOS `say` and `afconvert`
- `avatar_console` also expects a working `codex exec`
- `streaming_video_server` needs `SoulX-FlashHead` and its model dependencies for real inference
- the checked-in `.env.server.example` is intentionally generic and must be edited locally

## Repository Layout

- `avatar_console/`: local console, TTS pipeline, Codex-driven reply mode, browser UI
- `streaming_video_server/`: WebRTC server, inference runtime, debug client, scripts, and tests

Each subproject keeps its own detailed README:

- [avatar_console/README.md](./avatar_console/README.md)
- [streaming_video_server/README.md](./streaming_video_server/README.md)
