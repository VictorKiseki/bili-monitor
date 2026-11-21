"""项目版本号的唯一来源。

约定：仓库根目录的 `VERSION` 文件里只有一行版本号（语义化版本，例如 `0.2.0`）。
代码、安装脚本、打包脚本、管理台都从这里读，不要在多处硬编码版本号。

发版流程见 `docs/31-发布与交接手册.md`：
    python scripts/release.py bump patch     # 改 VERSION + 生成 CHANGELOG 小节（低位自动归零）
    python scripts/release.py check          # 密钥/凭据扫描
    python scripts/release.py build          # 产出 dist/bili-monitor-<version>.zip + SHA256SUMS
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"

#: 读不到 VERSION 时的兜底值（例如被裁剪的发布包里丢了文件）
FALLBACK = "0.0.0+unknown"

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")


def read_version(path: Path | str | None = None) -> str:
    """读取版本号；文件缺失或内容不合法时返回 :data:`FALLBACK`。"""
    p = Path(path) if path is not None else VERSION_FILE
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return FALLBACK
    first = text.splitlines()[0].strip() if text else ""
    return first or FALLBACK


def parse_version(version: str) -> tuple[int, int, int]:
    """把 `1.2.3` 解析成 `(1, 2, 3)`；不合法抛 :class:`ValueError`。"""
    m = _SEMVER.match(version.strip())
    if not m:
        raise ValueError(f"不是合法的语义化版本号：{version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def format_version(parts: tuple[int, int, int]) -> str:
    return "{}.{}.{}".format(*parts)


def is_valid(version: str) -> bool:
    return bool(_SEMVER.match(version.strip()))


def bump(version: str, part: str) -> str:
    """按 `major`/`minor`/`patch` 递增版本号（不做规则校验，规则见 :func:`bump_checked`）。

    低位一律归零：`0.1.1` 升第二位得 `0.2.0`，`0.9.4` 升第一位得 `1.0.0`；
    只有 patch 是在原位加一（`0.1.1` → `0.1.2`）。
    """
    major, minor, patch = parse_version(version)
    if part == "major":
        return format_version((major + 1, 0, 0))
    if part == "minor":
        return format_version((major, minor + 1, 0))
    if part == "patch":
        return format_version((major, minor, patch + 1))
    raise ValueError(f"未知的递增类型：{part!r}（可用 major / minor / patch）")


#: 本项目的版本号规则（与 docs/31 §11、CHANGELOG 抬头保持一致）
VERSION_RULES = (
    ("major", "第一位", "只能人工增加：整个项目架构级调整，或新增功能模块；右边两位归零"),
    ("minor", "第二位", "自动增加：多个重要功能统一合并迭代时；第三位归零"),
    ("patch", "第三位", "自动增加：缺陷修复、细节优化，以及尚未合并的小改动"),
)

#: 一句话说明低位归零，给文档与报错文案复用
RESET_NOTE = "低位归零：涨哪一位，它右边所有位清零（0.1.1 升第二位 → 0.2.0）"


class MajorBumpRefused(ValueError):
    """第一位不允许自动递增：必须人工确认并显式允许。"""


def bump_checked(version: str, part: str, *, allow_major: bool = False) -> str:
    """按项目规则递增版本号：major 必须显式允许，minor/patch 可直接自动递增。"""
    if part == "major" and not allow_major:
        raise MajorBumpRefused(
            "第一位（major）只能人工增加：确认这是架构级调整或新增功能模块后，"
            "加 --allow-major 再执行；普通迭代请用 minor（多个重要功能合并）或 patch（修复/细节）。"
            "（低位会自动归零，例如 0.9.4 升第一位得 1.0.0）"
        )
    return bump(version, part)


#: 导入时快照一次，方便 `from shared.version import __version__`
__version__ = read_version()

__all__ = [
    "ROOT",
    "VERSION_FILE",
    "FALLBACK",
    "VERSION_RULES",
    "RESET_NOTE",
    "MajorBumpRefused",
    "__version__",
    "bump",
    "bump_checked",
    "format_version",
    "is_valid",
    "parse_version",
    "read_version",
]
