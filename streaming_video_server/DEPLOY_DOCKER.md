# Docker Deployment

This project now supports both delivery modes:

- local build mode: build the image on the target host
- registry mode: publish the image once, then let users pull it without building

Model weights stay outside the image in a Docker volume so the runtime image stays small enough to rebuild and distribute.

## Files

- `docker-compose.yml`: registry-first runtime stack
- `docker-compose.build.yml`: local build overlay
- `deploy.sh`: local build deployment
- `deploy-registry.sh`: pull-only deployment for end users
- `publish.sh`: build and push the image to a registry
- `scripts/sync_soulx_vendor.sh`: copies `SoulX-FlashHead` source into a temporary local `vendor/` directory before building

## Prerequisites

- Linux host
- Docker with Compose v2
- NVIDIA driver and `nvidia-container-toolkit`
- A GPU supported by the current SoulX-FlashHead runtime

## Publisher Workflow

On the machine that has the full source tree:

```bash
cd /path/to/realtime-avatar-stack/streaming_video_server
./publish.sh ghcr.io/sean-xinli/streaming-video-server:0.1.0 ghcr.io/sean-xinli/streaming-video-server:latest
```

That will:

1. sync `../SoulX-FlashHead` or `SOULX_SOURCE_DIR` into a temporary local `vendor/SoulX-FlashHead`
2. build the image
3. push the requested tag
4. optionally push a second tag such as `latest`

## End User Workflow

End users only need the deployment bundle plus the image name in `.env.registry`.

```bash
cd /path/to/realtime-avatar-stack/streaming_video_server
cp .env.registry.example .env.registry
# edit IMAGE_NAME=...
./deploy-registry.sh
```

That command will:

1. pull the published image from the registry
2. download the models into the `soulx-models` volume
3. start the server and wait for `/healthz`

## Zero-Bundle Workflow

If you do not want to send any deployment files, users can run the published image directly:

```bash
docker run -d --name streaming-video-server \
  --gpus all \
  --network host \
  --ipc host \
  -e STREAM_ICE_MODE=auto \
  -v soulx-models:/models \
  ghcr.io/sean-xinli/streaming-video-server:latest
```

On first start, the container will automatically download the model weights into the `soulx-models` volume if they are missing.

If the user wants custom avatars:

```bash
docker run -d --name streaming-video-server \
  --gpus all \
  --network host \
  --ipc host \
  -e STREAM_ICE_MODE=auto \
  -e SOULX_COND_IMAGE=/data/avatars \
  -v soulx-models:/models \
  -v $(pwd)/avatars:/data/avatars:ro \
  ghcr.io/sean-xinli/streaming-video-server:latest
```

After the container has been created once, later restarts can use:

```bash
docker stop streaming-video-server
docker start streaming-video-server
```

## Local Build Workflow

If you still want to build on the target host:

```bash
cd /path/to/realtime-avatar-stack/streaming_video_server
./deploy.sh
```

## Common Commands

```bash
./deploy.sh build
./deploy.sh init-models
./deploy.sh logs
./deploy.sh status
./deploy.sh down

./deploy-registry.sh pull
./deploy-registry.sh init-models
./deploy-registry.sh logs
./deploy-registry.sh status
./deploy-registry.sh down
```

## Configuration

Edit `.env.docker` for local builds or `.env.registry` for registry deployments:

- `STREAM_PORT`: HTTP/WebRTC signaling port
- `STREAM_MODEL_TYPE`: `lite` or `pro`
- `STREAM_ICE_MODE`: `auto` by default; adjust for your network
- `STREAM_ICE_SERVER_URLS`: STUN or TURN server list
- `HF_ENDPOINT`: set `https://hf-mirror.com` in mainland China if needed
- `HF_TOKEN`: Hugging Face token if your environment requires authenticated downloads
- `IMAGE_NAME`: registry image tag used by `deploy-registry.sh`
- `MODEL_AUTO_DOWNLOAD`: set `0` to disable automatic first-start model downloads

Container paths are fixed internally:

- SoulX source: `/opt/soulx`
- model weights: `/models`
- avatar directory: `/data/avatars`

The service uses `./assets/avatars` as the default avatar directory. Add or replace PNG and JPEG files there before starting the stack.

## Notes

- The current Compose stack uses `network_mode: host`. That is deliberate for WebRTC host candidates on Linux.
- Public internet deployment usually needs TURN in addition to STUN. This stack does not start `coturn` yet.
- The temporary vendored source intentionally excludes `SoulX-FlashHead/models/`; model weights live only in the Docker volume.
- If the current user cannot access Docker directly, `./deploy.sh` will automatically run Docker commands through `sudo`. For a permanent fix, add the user to the `docker` group and start a new shell.
- `./deploy-registry.sh` uses the same sudo fallback when Docker socket access is restricted.
