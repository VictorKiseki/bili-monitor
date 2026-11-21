"""通过 GitHub Releases 检查正式版本。"""
from __future__ import annotations

import re
from typing import Any

from shared.version import read_version

REPOSITORY = "VictorKiseki/bili-monitor"
GITHUB_REPO = f"https://github.com/{REPOSITORY}"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases"
LATEST_RELEASE_URL = GITHUB_RELEASES_API + "/latest"
TIMEOUT_S = 6.0
_RELEASE_TAG = re.compile(r"v?([0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8})")
_HEADERS = {"User-Agent": "BiliMonitor-Update", "Accept": "application/vnd.github+json"}


def parse_version(text: str) -> tuple[int, ...]:
    """`v0.2.0` → `(0, 2, 0)`；允许 v 前缀与空白，任一段不是纯数字就当解析失败（空元组）。"""
    s = str(text or "").strip()
    if s[:1] in ("v", "V"):
        s = s[1:].strip()
    if not s:
        return ()
    parts: list[int] = []
    for chunk in s.split("."):
        chunk = chunk.strip()
        if not chunk.isdigit():
            return ()
        parts.append(int(chunk))
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    """远端版本是否比本地新；任一侧解析不出来都算「不比本地新」。"""
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return a > b


def _client():
    import httpx  # 延迟导入：离线机器没装也不会影响模块导入

    return httpx.Client(headers=_HEADERS, timeout=TIMEOUT_S, follow_redirects=False)


def fetch_latest(client: Any = None) -> str:
    """读取 GitHub 最新正式发行版的标签，返回本地 VERSION 使用的格式。"""
    cli = client if client is not None else _client()
    try:
        try:
            res = cli.get(LATEST_RELEASE_URL)
        except Exception as exc:
            raise RuntimeError(f"网络不可用（{type(exc).__name__}）") from exc
        if res.status_code == 404:
            raise RuntimeError("GitHub 仓库尚无可读取的正式发行版")
        if res.status_code != 200:
            raise RuntimeError(f"GitHub HTTP {res.status_code}")
        try:
            release = res.json()
        except ValueError as exc:
            raise RuntimeError("GitHub 发行版数据格式无效") from exc
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            raise RuntimeError("GitHub 未返回正式发行版")
        tag = _RELEASE_TAG.fullmatch(str(release.get("tag_name") or ""))
        if not tag:
            raise RuntimeError("GitHub 发行版标签不是有效的版本号")
        return tag[1]
    finally:
        if client is None:
            cli.close()


def check(client: Any = None) -> dict:
    """给 `/api/update` 用的结论；永远返回 dict，不让异常穿到 HTTP 层。"""
    current = read_version()
    out = {
        "ok": False,
        "current": current,
        "latest": "",
        "has_update": False,
        "url": GITHUB_REPO,
        "error": "",
    }
    try:
        latest = fetch_latest(client)
    except Exception as exc:
        out["error"] = str(exc) or "仓库不可访问"
        return out
    out["ok"] = True
    out["latest"] = latest
    out["has_update"] = is_newer(latest, current)
    return out
