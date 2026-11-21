"""Console 的「动态采集方式」部分：关注流 / 逐人轮询的开关与状态。

为什么单独一页：关注流和逐人轮询的差别不只是快慢——关注流一次请求拿到所有关注
的新动态，但会吞条目（101 个关注实测漏 16.4%）。所以这一页要同时把「靠什么取数、
最近一轮多久、有没有出错、兜底补漏抓到几条」摊开，出问题一眼能看出来。

按钮走 data/*.request 文件（和 data/restart.request 同一套），由采集进程下一轮消费；
管理台和采集进程是两个进程，这样不需要额外 IPC。
"""
from __future__ import annotations

import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import feed_collect as feedmod  # noqa: E402

from web.console_core import ApiError  # noqa: E402

MODES = [("feed", "关注流", "一次请求拿到所有关注的新动态，60 秒一轮"),
         ("per_user", "逐人轮询", "每个主播单独请求一次，间隔 240 秒")]
ACTIONS = {"feed": ("立即取流", "feed"),
           "sweep": ("立即补漏", "sweep"),
           "sync": ("同步关注", "follow_sync")}


def _fmt_ts(ts: int) -> str:
    if not ts:
        return ""
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def _ago(ts: int, now: float | None = None) -> int:
    if not ts:
        return -1
    return max(0, int((now or time.time()) - ts))


class ConsoleFeed:
    # ---- 读 ------------------------------------------------------------
    def _feed_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self._meta_get(key, str(default)) or default)
        except (TypeError, ValueError):
            return default

    def _feed_str(self, key: str, default: str = "") -> str:
        val = self._meta_get(key, default)
        return default if val is None else str(val)

    def feed_status(self) -> dict:
        cfg = self.load_config()
        poll = cfg.get("poll") or {}
        configured = str(poll.get("mode") or "auto")
        try:
            cookies = feedmod.load_follow_cookies()
        except Exception as e:                      # noqa: BLE001  凭据读不出来只是不能取流
            cookies, cred_error = None, f"{type(e).__name__}: {e}"
        else:
            cred_error = ""
        try:
            effective = feedmod.resolve_mode(configured, cookies)
        except Exception:                           # noqa: BLE001
            effective = "per_user"
        why = "" if cookies is None else feedmod.fallback_reason(cookies)
        subs = self.subjects()
        enabled = [s for s in subs if s.get("enabled")]
        last_at = self._feed_int("feed_last_at")
        rounds = self._feed_int("feed_rounds")
        return {
            "ok": True,
            "mode": feedmod.normalize_mode(configured),
            "mode_configured": configured,
            "mode_effective": effective,
            "mode_label": dict((k, v) for k, v, _h in MODES).get(effective, effective),
            "modes": [{"value": k, "label": v, "hint": h} for k, v, h in MODES],
            "creds": {
                "can_read": bool(cookies and cookies.can_read),
                "can_follow": bool(cookies and cookies.can_follow),
                "sessdata": bool(cookies and getattr(cookies, "sessdata", "")),
                "jct": bool(cookies and getattr(cookies, "bili_jct", "")),
                "file": str(getattr(cookies, "source", "") or ""),
                "error": cred_error,
                "why": why,
            },
            "subjects": {"total": len(subs), "enabled": len(enabled)},
            "poll": {
                "live_interval_s": poll.get("live_interval_s"),
                "dynamic_interval_s": poll.get("dynamic_interval_s"),
                "feed_interval_s": poll.get("feed_interval_s", 60),
                "feed_pages": poll.get("feed_pages", 3),
                "feed_burst_threshold": poll.get("feed_burst_threshold", 15),
                "feed_burst_pages": poll.get("feed_burst_pages", 3),
                "sweep_hours": poll.get("sweep_hours", 6),
                "sweep_max_age_min": poll.get("sweep_max_age_min", 720),
                "sweep_push_late": poll.get("sweep_push_late", True),
                "sweep_late_max": poll.get("sweep_late_max", 30),
                "follow_sync": poll.get("follow_sync", True),
                "follow_sync_hours": poll.get("follow_sync_hours", 6),
            },
            "feed": {
                "last_at": _fmt_ts(last_at),
                "ago_s": _ago(last_at),
                "rounds": rounds,
                "errors": self._feed_int("feed_errors"),
                "last_error": self._feed_str("feed_last_error"),
                "ms": self._feed_int("feed_last_ms"),
                "kb": round(self._feed_int("feed_last_bytes") / 1024.0, 1),
                "pages": self._feed_int("feed_last_pages"),
                "entries": self._feed_int("feed_last_entries"),
                "matched": self._feed_int("feed_last_matched"),
                # 关注了但不在监控名单里：只统计、不产事件（2026-11-10）
                "ignored": max(0, self._feed_int("feed_last_entries")
                               - self._feed_int("feed_last_matched")),
                "risk": self._feed_int("feed_last_risk"),
                "known_hit": self._feed_str("feed_known_hit") == "1",
                "newest_ts": self._feed_int("feed_newest_ts"),
                "newest_at": _fmt_ts(self._feed_int("feed_newest_ts")),
            },
            "sweep": {
                "last_at": _fmt_ts(self._feed_int("sweep_last_at")),
                "ago_s": _ago(self._feed_int("sweep_last_at")),
                "runs": self._feed_int("sweep_runs"),
                "missing": self._feed_int("sweep_missing"),
            },
            "follow_sync": {
                "last_at": _fmt_ts(self._feed_int("follow_sync_last_at")),
                "ok": self._feed_str("follow_sync_ok") == "1",
                "following": self._feed_int("follow_sync_following"),
                "planned": self._feed_int("follow_sync_planned"),
                "added": self._feed_int("follow_sync_added"),
                "failed": self._feed_int("follow_sync_failed"),
                "reason": self._feed_str("follow_sync_reason"),
            },
            "pending": [k for k in ACTIONS if self._feed_request_path(k).exists()],
            "notes": [
                "关注同步只关注、不取关；你自己的关注列表不会被删。",
            ],
            "need_restart_hint": "切换取数方式需要重启采集进程（管理台「服务」页可点重启）",
        }

    # ---- 写 ------------------------------------------------------------
    def _feed_request_path(self, action: str) -> Path:
        name = ACTIONS.get(action, (action, action))[1]
        return Path(self.db_path).parent / f"{name}.request"

    def feed_action(self, action: str) -> dict:
        """放一个请求文件，采集进程下一轮消费（进程不在就留着，等它起来）。"""
        if action not in ACTIONS:
            raise ApiError("action 只能是 %s 之一" % "/".join(ACTIONS))
        path = self._feed_request_path(action)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(str(int(time.time())), "utf-8")
        except OSError as e:
            raise ApiError("写请求文件失败：%s" % e, 500) from e
        label = ACTIONS[action][0]
        return {"ok": True, "action": action, "label": label, "file": str(path),
                "note": "已请求「%s」，采集进程下一轮执行（通常 1 分钟内）；"
                        "进程没在跑时会一直等到下次启动" % label}

    def feed_set_mode(self, mode: str) -> dict:
        """改 config/subjects.json 的 poll.mode；采集进程启动时读，所以要重启才换实现。"""
        mode = feedmod.normalize_mode(mode)
        if mode not in ("feed", "per_user", "auto"):
            raise ApiError("mode 只能是 auto/feed/per_user 之一")

        def mutate(cfg: dict) -> None:
            poll = cfg.setdefault("poll", {})
            poll["mode"] = mode

        self._update_config(mutate)
        st = self.feed_status()
        st["changed"] = True
        st["need_restart"] = True
        return st
