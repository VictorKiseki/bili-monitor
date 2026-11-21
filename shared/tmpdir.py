"""临时目录统一入口：测试/自测默认用系统临时目录，不再散落到项目一级目录。

背景：Git Bash（TEMP=/tmp）或受限沙箱下启动 Python 时，tempfile 在 Windows 上
找不到可写临时目录就会退回“当前目录”，于是 `tests/` 与 `scripts/selftest_*.py`
把 `harness-*/agent-*/ai-cfg-*/tasks_*` 之类的一次性目录拉在项目根。

约定：
- **默认**走系统临时目录链：TMPDIR → TEMP → TMP → %LOCALAPPDATA%\\\\Temp →
  %USERPROFILE%\\\\AppData\\\\Local\\\\Temp → C:\\\\Windows\\\\Temp（逐个用探针验证真实可写），
  系统盘上小文件 IO 快，测试不会被拖慢。
- **显式要求**时才用仓库内 `.tmp/`（已 gitignore、发布打包排除）：
    * `BBOT_TMPDIR=<路径>`：把临时目录指到指定位置（相对路径按仓库根解析，
      因此 `BBOT_TMPDIR=.tmp` 就是仓库内 `.tmp/`；设成别的盘可用于加速）；
    * `BBOT_USE_PROJECT_TMP=1`：让测试临时目录落在仓库内 `.tmp/`，方便查看与
      随手清理。
- 系统临时目录全部不可用时，最后兜底到仓库内 `.tmp/`；仍失败才抛错，
  绝不静默回落到一级目录。

用法：
    from shared.tmpdir import ensure, mkdtemp, prune
    ensure()                 # 进程级钉住 tempfile.tempdir，后续 TemporaryDirectory 都跟它走
    tmp = mkdtemp("harness-")
    prune(3)                 # 清掉 .tmp 下 3 天没动过的残留
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 仓库内的测试临时目录（gitignore + 发布排除），仅在显式要求时使用
PROJECT_TMP = PROJECT_ROOT / ".tmp"

#: 显式覆盖临时目录（相对路径按仓库根解析）
ENV_OVERRIDE = "BBOT_TMPDIR"

#: 显式要求落在仓库内 .tmp/ 的开关（测试环境用）
ENV_PROJECT_TMP = "BBOT_USE_PROJECT_TMP"

_TRUTHY = {"1", "true", "yes", "on"}

_PROBE = ".bbot-tmp-probe-%d" % os.getpid()


def _override() -> Path | None:
    """环境变量指定的临时目录；未设置返回 None。"""
    value = os.environ.get(ENV_OVERRIDE)
    if not value or not value.strip():
        return None
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _wants_project_tmp() -> bool:
    """测试环境是否显式要求落在仓库内 `.tmp/`。"""
    return os.environ.get(ENV_PROJECT_TMP, "").strip().lower() in _TRUTHY


def _system_tmpdirs() -> Iterator[Path]:
    """系统临时目录候选链（不保证已存在）。"""
    for name in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            yield Path(value)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield Path(local) / "Temp"
    user = os.environ.get("USERPROFILE")
    if user:
        yield Path(user) / "AppData" / "Local" / "Temp"
    yield Path(r"C:\Windows\Temp")


def _candidates() -> Iterator[Path]:
    """按优先级给出候选临时目录（不保证已存在）。"""
    override = _override()
    if override is not None:  # 显式指定后不再回退，避免静默用错地方
        yield override
        return
    if _wants_project_tmp():
        yield PROJECT_TMP
        return
    yield from _system_tmpdirs()
    yield PROJECT_TMP  # 系统临时目录全不可用时的兜底（仍不落在项目一级目录）


def _writable(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
        probe = path / _PROBE
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve() -> Path:
    """返回一个真实可写的临时目录；拒绝落在“当前目录”本身。"""
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = None
    try:
        project_tmp = PROJECT_TMP.resolve()
    except OSError:
        project_tmp = PROJECT_TMP
    seen: set[Path] = set()
    for candidate in _candidates():
        try:
            path = candidate.expanduser().resolve()
        except OSError:
            continue
        if path in seen:
            continue
        seen.add(path)
        if cwd is not None and path == cwd:
            continue
        if candidate == PROJECT_TMP or path == project_tmp:  # 仓库内 .tmp 按需创建
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
        if _writable(path):
            return path
    raise RuntimeError(
        "找不到可写的临时目录：系统 Temp（TMPDIR/TEMP/TMP、%LOCALAPPDATA%\\Temp、"
        "%USERPROFILE%\\AppData\\Local\\Temp、C:\\Windows\\Temp）与仓库内 .tmp 全部不可用"
    )


def ensure() -> Path:
    """把进程内 tempfile 的临时目录钉到可写位置，返回该目录。"""
    root = resolve()
    tempfile.tempdir = str(root)
    if root == PROJECT_TMP or root == PROJECT_TMP.resolve():
        try:
            prune()
        except OSError:
            pass
    return root


def mkdtemp(prefix: str = "") -> Path:
    """建一个一次性临时目录（默认系统临时目录，不会落到项目根）。"""
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(ensure())))


def tempdir(prefix: str = "") -> tempfile.TemporaryDirectory:
    """开一个自动清理的 TemporaryDirectory（默认系统临时目录）。"""
    return tempfile.TemporaryDirectory(prefix=prefix, dir=str(ensure()))


def prune(max_age_days: float = 3.0) -> int:
    """清掉 `.tmp/` 下超过 max_age_days 天没动过的残留，返回删除条目数。"""
    if not PROJECT_TMP.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for entry in PROJECT_TMP.iterdir():
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed
