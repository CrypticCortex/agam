"""Shared file-copy helpers for agent wiring installs."""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent  # src/agam/


def hooks_src() -> Path:
    return _PKG_ROOT / "hooks"


def tools_src() -> Path:
    return _PKG_ROOT / "tools"


def transcripts_src() -> Path:
    return _PKG_ROOT / "transcripts.py"


def _make_executable(p: Path) -> None:
    st = p.stat()
    p.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def copy_files(names: list[str], src_dir: Path, dst_dir: Path, *, executable: bool) -> None:
    """Copy named files from src_dir to dst_dir; chmod +x when executable."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        src = src_dir / name
        if not src.exists():
            continue
        out = dst_dir / name
        shutil.copy2(src, out)
        if executable and out.suffix in (".py", ".sh"):
            _make_executable(out)


def copy_hooks_tree(dst_dir: Path) -> None:
    """Copy every hook script into dst_dir (chmod +x). Used for the shared
    ~/.agam/hooks copy the launchd watchdog runs from."""
    src = hooks_src()
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in ("__init__.py", "__pycache__"):
            continue
        if item.is_file():
            out = dst_dir / item.name
            shutil.copy2(item, out)
            if out.suffix in (".py", ".sh"):
                _make_executable(out)


def copy_tools_tree(dst_dir: Path, *, extra: list[Path] | None = None) -> None:
    """Copy every tool module into dst_dir (chmod +x .py), plus any extras.

    Skips package dunders. ``extra`` lets callers add non-tools modules the
    hooks vendor (e.g. transcripts.py for Cursor).
    """
    src = tools_src()
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in ("__init__.py", "__pycache__"):
            continue
        if item.is_file():
            out = dst_dir / item.name
            shutil.copy2(item, out)
            if out.suffix == ".py":
                _make_executable(out)
    for extra_path in extra or []:
        if extra_path.exists():
            out = dst_dir / extra_path.name
            shutil.copy2(extra_path, out)
            if out.suffix == ".py":
                _make_executable(out)
