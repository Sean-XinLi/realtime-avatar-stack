from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOULX_ROOT = PROJECT_ROOT.parent / "SoulX-FlashHead"
DEFAULT_SOULX_CKPT_DIR = DEFAULT_SOULX_ROOT / "models" / "SoulX-FlashHead-1_3B"
DEFAULT_SOULX_WAV2VEC_DIR = DEFAULT_SOULX_ROOT / "models" / "wav2vec2-base-960h"
DEFAULT_COND_IMAGE = PROJECT_ROOT / "assets" / "avatars"


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    sample_rate: int = 16000
    output_fps: int = 25
    playout_buffer_ms: int = 300
    max_concurrent_sessions: int = 3
    inference_workers: int = 1
    inference_step_ms: int = 0
    max_pending_audio_ms: int = 2000
    startup_warmup_runs: int = 1
    capture_ms: int = 20
    input_chunk_ms: int = 40
    startup_partial_ratio: float = 0.75
    startup_min_audio_floor_ms: int = 640
    model_type: str = "lite"
    soulx_root: Path = DEFAULT_SOULX_ROOT
    ckpt_dir: Path = DEFAULT_SOULX_CKPT_DIR
    wav2vec_dir: Path = DEFAULT_SOULX_WAV2VEC_DIR
    cond_image: Path = DEFAULT_COND_IMAGE
    use_face_crop: bool = False
    base_seed: int = 42
    public_origin: str = "*"
    mock_inference: bool = False
    ice_mode: str = "auto"
    ice_server_urls: tuple[str, ...] = ("stun:stun.l.google.com:19302",)
    ice_tailscale_ipv4_prefixes: tuple[str, ...] = ("100.",)
    ice_tailscale_ipv6_prefixes: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "ServerConfig":
        ice_mode = os.getenv("STREAM_ICE_MODE", "auto").strip().lower() or "auto"
        if ice_mode not in {"auto", "tailscale"}:
            ice_mode = "auto"
        return cls(
            host=os.getenv("STREAM_HOST", "0.0.0.0"),
            port=int(os.getenv("STREAM_PORT", "8080")),
            sample_rate=int(os.getenv("STREAM_SAMPLE_RATE", "16000")),
            output_fps=int(os.getenv("STREAM_OUTPUT_FPS", "25")),
            playout_buffer_ms=max(0, int(os.getenv("STREAM_PLAYOUT_BUFFER_MS", "300"))),
            max_concurrent_sessions=max(1, int(os.getenv("STREAM_MAX_CONCURRENT_SESSIONS", "3"))),
            inference_workers=max(1, int(os.getenv("STREAM_INFERENCE_WORKERS", "1"))),
            inference_step_ms=max(0, int(os.getenv("STREAM_INFERENCE_STEP_MS", "0"))),
            max_pending_audio_ms=max(0, int(os.getenv("STREAM_MAX_PENDING_AUDIO_MS", "2000"))),
            startup_warmup_runs=max(0, int(os.getenv("STREAM_STARTUP_WARMUP_RUNS", "1"))),
            capture_ms=int(os.getenv("STREAM_CAPTURE_MS", "20")),
            input_chunk_ms=int(os.getenv("STREAM_INPUT_CHUNK_MS", "40")),
            startup_partial_ratio=min(
                1.0,
                max(0.25, float(os.getenv("STREAM_STARTUP_PARTIAL_RATIO", "0.75"))),
            ),
            startup_min_audio_floor_ms=max(
                0,
                int(os.getenv("STREAM_STARTUP_MIN_AUDIO_FLOOR_MS", "640")),
            ),
            model_type=os.getenv("STREAM_MODEL_TYPE", "lite"),
            soulx_root=Path(os.getenv("SOULX_ROOT", str(DEFAULT_SOULX_ROOT))),
            ckpt_dir=Path(os.getenv("SOULX_CKPT_DIR", str(DEFAULT_SOULX_CKPT_DIR))),
            wav2vec_dir=Path(os.getenv("SOULX_WAV2VEC_DIR", str(DEFAULT_SOULX_WAV2VEC_DIR))),
            cond_image=Path(os.getenv("SOULX_COND_IMAGE", str(DEFAULT_COND_IMAGE))),
            use_face_crop=os.getenv("SOULX_USE_FACE_CROP", "0") == "1",
            base_seed=int(os.getenv("SOULX_BASE_SEED", "42")),
            public_origin=os.getenv("STREAM_PUBLIC_ORIGIN", "*"),
            mock_inference=os.getenv("STREAM_MOCK_INFERENCE", "0") == "1",
            ice_mode=ice_mode,
            ice_server_urls=_split_csv(os.getenv("STREAM_ICE_SERVER_URLS", "stun:stun.l.google.com:19302")),
            ice_tailscale_ipv4_prefixes=_split_csv(os.getenv("STREAM_ICE_TAILSCALE_IPV4_PREFIXES", "100.")),
            ice_tailscale_ipv6_prefixes=_split_csv(os.getenv("STREAM_ICE_TAILSCALE_IPV6_PREFIXES", "")),
        )

    @property
    def capture_samples(self) -> int:
        return self.sample_rate * self.capture_ms // 1000

    @property
    def input_chunk_samples(self) -> int:
        return self.sample_rate * self.input_chunk_ms // 1000

    @property
    def ice_servers_payload(self) -> list[dict[str, list[str]]]:
        if self.ice_mode == "tailscale":
            return []
        if not self.ice_server_urls:
            return []
        return [{"urls": list(self.ice_server_urls)}]
