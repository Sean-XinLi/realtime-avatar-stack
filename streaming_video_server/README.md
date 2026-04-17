# Streaming Video Server

WebRTC-based real-time streaming server for avatar inference and synchronized audio/video playback.

This service receives uplink audio, runs streaming inference, and returns generated media aligned to the same session timeline. It can be run directly with Python or from the published Docker image.

The repository is intentionally kept lighter than a full model bundle. Model weights are not committed, and local image builds expect access to a separate `SoulX-FlashHead` checkout.

## What Is In This Directory

- `server/`: runtime server code, configuration, WebRTC handling, inference scheduling, and sync
- `assets/avatars/`: default avatar images used when no custom avatar directory is mounted
- `scripts/`: local run, model bootstrap, Docker helpers, and maintenance scripts
- `docker-compose.yml`: registry-first runtime stack
- `docker-compose.build.yml`: local build overlay
- `Dockerfile`: GPU runtime image for the server
- `DEPLOY_DOCKER.md`: more detailed Docker deployment notes

## Quick Start

### Option 1: Run with Python

```bash
cd streaming_video_server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.server.example .env.server
```

For protocol-only local validation without the real model:

```bash
STREAM_MOCK_INFERENCE=1 python -m server.app
```

For real inference, edit `.env.server` first so the `SOULX_*` paths match your machine, then run:

```bash
python -m server.app
```

The server listens on `http://127.0.0.1:8080` by default.

### Option 2: Run the published Docker image

Pull the image:

```bash
docker pull ghcr.io/sean-xinli/streaming-video-server:latest
```

For a quick mock smoke test:

```bash
docker run --rm \
  -p 8080:8080 \
  -e STREAM_MOCK_INFERENCE=1 \
  ghcr.io/sean-xinli/streaming-video-server:latest
```

For real inference on a Linux GPU host:

```bash
docker run --rm \
  --gpus all \
  --network host \
  --ipc host \
  -e STREAM_ICE_MODE=auto \
  -v soulx-models:/models \
  ghcr.io/sean-xinli/streaming-video-server:latest
```

If you want to serve custom avatars from the host:

```bash
docker run --rm \
  --gpus all \
  --network host \
  --ipc host \
  -e STREAM_ICE_MODE=auto \
  -e SOULX_COND_IMAGE=/data/avatars \
  -v soulx-models:/models \
  -v $(pwd)/avatars:/data/avatars:ro \
  ghcr.io/sean-xinli/streaming-video-server:latest
```

Then point `avatar_console` to `http://127.0.0.1:8080`, or to the reachable host address if the container is running on another machine.

For real inference with either Python or Docker, download and place the model files as described in [streaming-video-server-model-setup.pdf](./streaming-video-server-model-setup.pdf).

## Configuration

Copy the example config:

```bash
cp .env.server.example .env.server
```

The checked-in examples are generic and safe to publish:

- `.env.server.example`: local Python runtime example
- `.env.docker.example`: local Docker build example
- `.env.registry.example`: registry deployment example

Important variables:

- `STREAM_PORT`: HTTP and signaling port, default `8080`
- `STREAM_MODEL_TYPE`: `lite` or `pro`
- `STREAM_MOCK_INFERENCE`: set `1` to skip real model loading
- `STREAM_ICE_MODE`: `auto` or `tailscale`
- `STREAM_ICE_SERVER_URLS`: STUN/TURN server list for `auto` mode
- `SOULX_ROOT`: `SoulX-FlashHead` source directory
- `SOULX_CKPT_DIR`: model checkpoint directory
- `SOULX_WAV2VEC_DIR`: wav2vec model directory
- `SOULX_COND_IMAGE`: avatar image file or avatar directory

For local Docker builds or image publishing from this repo, keep a sibling checkout of `SoulX-FlashHead` next to this repository, or set `SOULX_SOURCE_DIR` before running the Docker helper scripts.

Model download, checkpoint placement, and setup details are in [streaming-video-server-model-setup.pdf](./streaming-video-server-model-setup.pdf).

## Docker Notes

The published image is meant for GPU hosts. The container expects:

- NVIDIA drivers and `nvidia-container-toolkit`
- model files available under `/models`, or auto-download enabled
- optional custom avatar files mounted into `/data/avatars`
- Linux host networking for the simplest WebRTC deployment path

For local image builds, `deploy.sh` and `publish.sh` call `scripts/sync_soulx_vendor.sh`, which copies source code from `../SoulX-FlashHead` or from `SOULX_SOURCE_DIR` into a temporary local `vendor/` directory before building the image.

For more advanced flows such as local image builds, registry deployment scripts, and model bootstrapping, see [DEPLOY_DOCKER.md](./DEPLOY_DOCKER.md).

## Security and Publishing Notes

- Do not commit `.env.server`, `.env.docker`, or `.env.registry`; only commit the `*.example` templates.
- Update local paths and credentials in copied env files on your own machine.
- The checked-in systemd units are examples and should be edited before use on a real host.
