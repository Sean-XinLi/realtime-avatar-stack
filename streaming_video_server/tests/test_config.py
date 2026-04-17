from pathlib import Path

from server.config import _resolve_default_paths


def test_resolve_default_paths_prefers_repo_vendor_and_repo_models(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    vendor_root = repo_root / "vendor" / "SoulX-FlashHead"
    ckpt_dir = repo_root / "models" / "SoulX-FlashHead-1_3B"
    wav2vec_dir = repo_root / "models" / "wav2vec2-base-960h"
    avatars_dir = repo_root / "assets" / "avatars"

    vendor_root.mkdir(parents=True)
    ckpt_dir.mkdir(parents=True)
    wav2vec_dir.mkdir(parents=True)
    avatars_dir.mkdir(parents=True)

    paths = _resolve_default_paths(repo_root)

    assert paths["soulx_root"] == vendor_root
    assert paths["ckpt_dir"] == ckpt_dir
    assert paths["wav2vec_dir"] == wav2vec_dir
    assert paths["cond_image"] == avatars_dir


def test_resolve_default_paths_falls_back_to_vendor_expected_layout(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    vendor_root = repo_root / "vendor" / "SoulX-FlashHead"
    example_image = vendor_root / "examples" / "girl.png"

    example_image.parent.mkdir(parents=True)
    example_image.touch()

    paths = _resolve_default_paths(repo_root)

    assert paths["soulx_root"] == vendor_root
    assert paths["cond_image"] == example_image
    assert paths["ckpt_dir"].name == "SoulX-FlashHead-1_3B"
    assert paths["wav2vec_dir"].name == "wav2vec2-base-960h"
