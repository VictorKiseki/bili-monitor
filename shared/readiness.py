"""账号绑定状态判定（B 站登录态 / QQ 机器人凭据）。

为什么单独一层：监控台主进程要按「用户绑没绑」决定采集进程、推送进程起不起。
判定口径只能有一份——preflight、进程栈、管理台接口、首次引导都读这里，
免得「页面上说没绑，实际能跑」或反过来。

只读：本模块绝不写配置。写操作仍归 scripts/setup_wizard.py 与管理台。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 与 scripts/preflight.py 的 PLACEHOLDER_HINTS 保持同一口径（模板里的占位值不算绑定）
PLACEHOLDER_HINTS = ("填", "YOUR", "your_", "xxxx", "XXXX", "<", "替换", "示例", "example", "TODO", "todo")

MIN_SESSDATA = 20      # 真 SESSDATA 远长于此；短的基本是占位或半截
MIN_OPENID = 16


def read_kv(path: str | Path) -> dict[str, str]:
    """读 KEY=VALUE 形式的环境文件（BBOT.env / config/*.env），缺失就返回空表。"""
    p = Path(path)
    out: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def is_placeholder(value: str) -> bool:
    if not str(value or "").strip():
        return True
    return any(h in str(value) for h in PLACEHOLDER_HINTS)


def mask(value: str, head: int = 4, tail: int = 2) -> str:
    """掩码：只露头尾，够用户认出是哪一个，又不至于把凭据抄走。"""
    v = str(value or "")
    if not v:
        return ""
    if len(v) <= head + tail:
        return v[:2] + "****"
    return v[:head] + "****" + v[-tail:]


# ---------- B 站 ----------
def bili_env_path(root: str | Path | None = None) -> Path:
    return Path(root or ROOT) / "config" / "bili.env"


def bili_sessdata(root: str | Path | None = None) -> str:
    return read_kv(bili_env_path(root)).get("SESSDATA", "").strip()


def bili_bound(root: str | Path | None = None) -> tuple[bool, str]:
    """采集用的 B 站账号绑没绑：config/bili.env 里有真 SESSDATA 就算绑了。"""
    p = bili_env_path(root)
    if not p.is_file():
        return False, "还没有绑定 B 站账号（config/bili.env 不存在）"
    sess = bili_sessdata(root)
    if is_placeholder(sess) or len(sess) < MIN_SESSDATA:
        return False, "B 站登录态还没生效（SESSDATA 为空或已过期）"
    return True, "B 站账号已绑定（SESSDATA %s）" % mask(sess, 4, 3)


# ---------- QQ 机器人 ----------
def qq_env_path(root: str | Path | None = None) -> Path:
    return Path(root or ROOT) / "BBOT.env"


def qq_ids_path(root: str | Path | None = None) -> Path:
    return Path(root or ROOT) / ".p0_ids.json"


def qq_credentials(root: str | Path | None = None) -> tuple[str, str]:
    env = read_kv(qq_env_path(root))
    return env.get("QQBOT_APP_ID", "").strip(), env.get("QQBOT_APP_SECRET", "").strip()


def qq_openid(root: str | Path | None = None) -> str:
    p = qq_ids_path(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("user_openid") or "").strip()


def qq_bound(root: str | Path | None = None) -> tuple[bool, str]:
    """推送进程的前提：凭据齐 + 有推送目标（私聊 openid）。少一样都不起。"""
    app_id, secret = qq_credentials(root)
    if is_placeholder(app_id) or is_placeholder(secret):
        return False, "还没有配置 QQ 机器人 AppID / AppSecret"
    openid = qq_openid(root)
    if is_placeholder(openid) or len(openid) < MIN_OPENID:
        return False, "机器人凭据有了，还差推送目标（先私聊机器人一句话）"
    return True, "QQ 机器人已绑定（AppID %s）" % mask(app_id)


# ---------- 按进程角色问 ----------
ROLE_BINDING = {"collector": "bili", "pusher": "qq"}


def bound(role: str, root: str | Path | None = None) -> tuple[bool, str]:
    """采集进程要 B 站登录态，推送进程要 QQ 凭据 + 推送目标。"""
    which = ROLE_BINDING.get(str(role or "").strip().lower(), str(role or "").strip().lower())
    if which == "bili":
        return bili_bound(root)
    if which == "qq":
        return qq_bound(root)
    raise KeyError(role)


# ---------- 汇总 ----------
def summary(root: str | Path | None = None) -> dict:
    """给进程栈 / 管理台 / 首次引导共用的一份状态。"""
    root = Path(root or ROOT)
    b_ok, b_text = bili_bound(root)
    q_ok, q_text = qq_bound(root)
    app_id, _secret = qq_credentials(root)
    return {
        "root": str(root),
        "bili": {
            "bound": b_ok,
            "text": b_text,
            "path": str(bili_env_path(root)),
            "sessdata": mask(bili_sessdata(root), 4, 3),
        },
        "qq": {
            "bound": q_ok,
            "text": q_text,
            "app_id": mask(app_id),
            "openid": mask(qq_openid(root), 6, 4),
            "env_path": str(qq_env_path(root)),
            "ids_path": str(qq_ids_path(root)),
        },
        "ready": b_ok and q_ok,
    }
