"""dist/ rebuild and the shipped model-cache staging legs of `brain update`."""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .update_channels import Runner
from .update_plugins import _default_runner

# --------------------------------------------------------------------------
# dist/ rebuild — `pip install -e` (engine venv refresh, above) refreshes the
# installed package/venv but NEVER regenerates the gitignored dist/COMPAT +
# dist/cowork-skills/*.skill artifacts that restage_workspaces' cowork-vm leg
# copies (tools/package_clients.py is the only thing that builds those).
# Skipping this is the exact bug observed live twice (0.10.2->0.10.3 and
# 0.10.3->0.10.4): restage_workspaces silently staged one-build-stale .skill
# bundles and `brain doctor` flagged "Staged skill bundles stale" / "dist/COMPAT
# stale" until a human ran the packager by hand and re-ran `brain update`.
# --------------------------------------------------------------------------

def rebuild_dist(engine_src: Path, run: Runner = _default_runner) -> dict:
    packager = engine_src / "tools" / "package_clients.py"
    try:
        out = run([sys.executable, str(packager)], cwd=str(engine_src))
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    ok = out.returncode == 0
    return {"ok": ok, "detail": (out.stdout or out.stderr or "").strip()}


_VERSION_STAMP_RE = re.compile(r'(?m)^__version__ = "([^"]+)"$')


def _read_version_stamp(stamp_path: Path) -> Optional[str]:
    if not stamp_path.exists():
        return None
    m = _VERSION_STAMP_RE.search(stamp_path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _resolve_shipped_model_source() -> tuple[Path, str]:
    """Resolve the exact model snapshot/file selected by the running engine."""
    from .embed import OnnxEmbedder

    embedder = OnnxEmbedder()
    onnx_path, base = embedder._resolve_model_files()
    if Path(onnx_path).name != Path(embedder._onnx_file).name:
        raise RuntimeError(
            f"resolved ONNX file {onnx_path!r} does not match "
            f"the selected model contract {embedder._onnx_file!r}"
        )
    return Path(base), embedder._onnx_file


def _stage_model_cache(brain_dir: Path, source: tuple[Path, str]) -> dict:
    """Atomically replace ``.brain/model`` with the selected minimal snapshot."""
    base, onnx_file = source
    required = (onnx_file, "tokenizer.json")
    optional = ("tokenizer_config.json", "special_tokens_map.json", "config.json")
    missing = [rel for rel in required if not (base / rel).is_file()]
    if missing:
        raise FileNotFoundError(
            f"selected model snapshot {base} is incomplete; missing {', '.join(missing)}"
        )

    brain_dir.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".model-stage-", dir=brain_dir))
    try:
        copied: list[str] = []
        for rel in (*required, *optional):
            src = base / rel
            if not src.is_file():
                continue
            dst = staged / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst, follow_symlinks=True)
            copied.append(rel)

        model_dir = brain_dir / "model"
        old = brain_dir / ".model-previous"
        if old.exists():
            shutil.rmtree(old)
        if model_dir.exists():
            model_dir.replace(old)
        try:
            staged.replace(model_dir)
        except Exception:
            if old.exists() and not model_dir.exists():
                old.replace(model_dir)
            raise
        if old.exists():
            shutil.rmtree(old)
        return {
            "model_dir": str(model_dir),
            "model_file": onnx_file,
            "model_files": copied,
            "model_bytes": sum((model_dir / rel).stat().st_size for rel in copied),
        }
    finally:
        if staged.exists():
            shutil.rmtree(staged)
