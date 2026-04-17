FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04

LABEL org.opencontainers.image.source="https://github.com/Sean-XinLi/realtime-avatar-stack/tree/main/streaming_video_server"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    ffmpeg \
    git \
    libgl1 \
    libglib2.0-0 \
    libsndfile1 \
    ninja-build \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv "${VIRTUAL_ENV}" \
    && pip install --upgrade pip setuptools wheel

WORKDIR /opt/streaming-video-server

COPY requirements.txt /tmp/streaming-video-server.requirements.txt
COPY vendor/SoulX-FlashHead/requirements.txt /tmp/soulx.requirements.txt

RUN pip install --index-url https://download.pytorch.org/whl/cu128 \
        torch==2.7.1 \
        torchvision==0.22.1 \
    && pip install -r /tmp/streaming-video-server.requirements.txt \
    && pip install -r /tmp/soulx.requirements.txt \
    && pip install "huggingface_hub[cli]" ninja \
    && pip install flash_attn==2.8.0.post2 --no-build-isolation

COPY server /opt/streaming-video-server/server
COPY scripts /opt/streaming-video-server/scripts
COPY assets /opt/streaming-video-server/assets
COPY vendor/SoulX-FlashHead /opt/soulx
COPY docker/entrypoint.sh /opt/streaming-video-server/docker/entrypoint.sh
COPY .env.server.example /opt/streaming-video-server/.env.server.example

RUN chmod +x \
    /opt/streaming-video-server/docker/entrypoint.sh \
    /opt/streaming-video-server/scripts/bootstrap_models.sh \
    /opt/streaming-video-server/scripts/sync_soulx_vendor.sh

ENV SOULX_ROOT=/opt/soulx \
    SOULX_CKPT_DIR=/models/SoulX-FlashHead-1_3B \
    SOULX_WAV2VEC_DIR=/models/wav2vec2-base-960h \
    SOULX_COND_IMAGE=/opt/streaming-video-server/assets/avatars \
    STREAM_HOST=0.0.0.0 \
    STREAM_PORT=8080 \
    STREAM_MODEL_TYPE=lite \
    STREAM_MAX_CONCURRENT_SESSIONS=1 \
    MODEL_AUTO_DOWNLOAD=1 \
    HF_HOME=/models/.hf-cache

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD curl -fsS http://127.0.0.1:${STREAM_PORT}/healthz || exit 1

ENTRYPOINT ["/opt/streaming-video-server/docker/entrypoint.sh"]
