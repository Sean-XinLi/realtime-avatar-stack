FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04 AS builder

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
    rsync \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv "${VIRTUAL_ENV}" \
    && pip install --upgrade pip setuptools wheel

WORKDIR /opt/streaming-video-server

COPY requirements.txt /tmp/streaming-video-server.requirements.txt
COPY vendor/SoulX-FlashHead/requirements.txt /tmp/soulx.requirements.txt

RUN pip install --index-url https://download.pytorch.org/whl/cu128 \
        torch==2.7.1 \
        torchvision==0.22.1

RUN pip install -r /tmp/streaming-video-server.requirements.txt

RUN pip install -r /tmp/soulx.requirements.txt \
    && pip install "huggingface_hub[cli]" ninja

RUN pip install flash_attn==2.8.0.post2 --no-build-isolation

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

RUN set -eux; \
    site_packages_dir="/opt/venv/lib/python3.10/site-packages"; \
    split_root="/opt/venv-split"; \
    mkdir -p "${split_root}/01-core"; \
    rsync -a \
      --exclude 'lib/python3.10/site-packages/nvidia' \
      --exclude 'lib/python3.10/site-packages/torch' \
      --exclude 'lib/python3.10/site-packages/flash_attn_2_cuda.cpython-310-x86_64-linux-gnu.so' \
      --exclude 'lib/python3.10/site-packages/triton' \
      --exclude 'lib/python3.10/site-packages/xformers' \
      --exclude 'lib/python3.10/site-packages/cusparselt' \
      /opt/venv/ "${split_root}/01-core/"; \
    mkdir -p "${split_root}/02-nvidia-cudnn/lib/python3.10/site-packages/nvidia"; \
    cp -a "${site_packages_dir}/nvidia/cudnn" "${split_root}/02-nvidia-cudnn/lib/python3.10/site-packages/nvidia/"; \
    mkdir -p "${split_root}/03-nvidia-cublas/lib/python3.10/site-packages/nvidia"; \
    cp -a "${site_packages_dir}/nvidia/cublas" "${split_root}/03-nvidia-cublas/lib/python3.10/site-packages/nvidia/"; \
    mkdir -p "${split_root}/04-nvidia-cusparse/lib/python3.10/site-packages/nvidia"; \
    cp -a "${site_packages_dir}/nvidia/cusparse" "${split_root}/04-nvidia-cusparse/lib/python3.10/site-packages/nvidia/"; \
    mkdir -p "${split_root}/05-nvidia-cusolver/lib/python3.10/site-packages/nvidia"; \
    cp -a "${site_packages_dir}/nvidia/cusolver" "${split_root}/05-nvidia-cusolver/lib/python3.10/site-packages/nvidia/"; \
    mkdir -p "${split_root}/06-nvidia-cufft/lib/python3.10/site-packages/nvidia"; \
    cp -a "${site_packages_dir}/nvidia/cufft" "${split_root}/06-nvidia-cufft/lib/python3.10/site-packages/nvidia/"; \
    mkdir -p "${split_root}/07-nvidia-nccl/lib/python3.10/site-packages/nvidia"; \
    cp -a "${site_packages_dir}/nvidia/nccl" "${split_root}/07-nvidia-nccl/lib/python3.10/site-packages/nvidia/"; \
    mkdir -p "${split_root}/08-nvidia-other/lib/python3.10/site-packages/nvidia"; \
    cp -a \
      "${site_packages_dir}/nvidia/__init__.py" \
      "${site_packages_dir}/nvidia/cuda_cupti" \
      "${site_packages_dir}/nvidia/cuda_nvrtc" \
      "${site_packages_dir}/nvidia/cuda_runtime" \
      "${site_packages_dir}/nvidia/cufile" \
      "${site_packages_dir}/nvidia/curand" \
      "${site_packages_dir}/nvidia/nvjitlink" \
      "${site_packages_dir}/nvidia/nvtx" \
      "${split_root}/08-nvidia-other/lib/python3.10/site-packages/nvidia/"; \
    mkdir -p "${split_root}/09-torch-lib/lib/python3.10/site-packages/torch"; \
    cp -a "${site_packages_dir}/torch/lib" "${split_root}/09-torch-lib/lib/python3.10/site-packages/torch/"; \
    mkdir -p "${split_root}/10-torch-rest/lib/python3.10/site-packages/torch"; \
    rsync -a --exclude 'lib' "${site_packages_dir}/torch/" "${split_root}/10-torch-rest/lib/python3.10/site-packages/torch/"; \
    mkdir -p "${split_root}/11-flash-attn/lib/python3.10/site-packages"; \
    cp -a "${site_packages_dir}/flash_attn_2_cuda.cpython-310-x86_64-linux-gnu.so" "${split_root}/11-flash-attn/lib/python3.10/site-packages/"; \
    mkdir -p "${split_root}/12-triton/lib/python3.10/site-packages"; \
    cp -a "${site_packages_dir}/triton" "${split_root}/12-triton/lib/python3.10/site-packages/"; \
    mkdir -p "${split_root}/13-xformers/lib/python3.10/site-packages"; \
    cp -a "${site_packages_dir}/xformers" "${split_root}/13-xformers/lib/python3.10/site-packages/"; \
    mkdir -p "${split_root}/14-cusparselt/lib/python3.10/site-packages"; \
    cp -a "${site_packages_dir}/cusparselt" "${split_root}/14-cusparselt/lib/python3.10/site-packages/"

FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

LABEL org.opencontainers.image.source="https://github.com/Sean-XinLi/realtime-avatar-stack"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsndfile1 \
    python3 \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/streaming-video-server

COPY --from=builder /opt/venv-split/01-core/ /opt/venv/
COPY --from=builder /opt/venv-split/02-nvidia-cudnn/ /opt/venv/
COPY --from=builder /opt/venv-split/03-nvidia-cublas/ /opt/venv/
COPY --from=builder /opt/venv-split/04-nvidia-cusparse/ /opt/venv/
COPY --from=builder /opt/venv-split/05-nvidia-cusolver/ /opt/venv/
COPY --from=builder /opt/venv-split/06-nvidia-cufft/ /opt/venv/
COPY --from=builder /opt/venv-split/07-nvidia-nccl/ /opt/venv/
COPY --from=builder /opt/venv-split/08-nvidia-other/ /opt/venv/
COPY --from=builder /opt/venv-split/09-torch-lib/ /opt/venv/
COPY --from=builder /opt/venv-split/10-torch-rest/ /opt/venv/
COPY --from=builder /opt/venv-split/11-flash-attn/ /opt/venv/
COPY --from=builder /opt/venv-split/12-triton/ /opt/venv/
COPY --from=builder /opt/venv-split/13-xformers/ /opt/venv/
COPY --from=builder /opt/venv-split/14-cusparselt/ /opt/venv/
COPY --from=builder /opt/streaming-video-server /opt/streaming-video-server
COPY --from=builder /opt/soulx /opt/soulx

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
