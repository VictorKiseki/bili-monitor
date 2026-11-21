"""Console 的概览 / 事件 / 投递 / 目标 / 日志只读视图（由 web/console.py 拆出）。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent

import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import webutil  # noqa: E402
from shared.version import read_version  # noqa: E402
from store.sqlite import SCHEMA, migrate  # noqa: E402
from web.cleanup import CleanupError, CleanupManager  # noqa: E402

TYPE_LABEL = {
    "live_start": "开播", "live_end": "下播", "dynamic": "动态",
    "video": "视频", "article": "专栏",
}

from web.console_core import ApiError  # noqa: E402

class ConsoleViews:
    def version(self) -> dict:
        """关于弹窗用：版本号唯一来源 VERSION，前端不写死（AGENTS R5）。"""
        return {"ok": True, "version": read_version()}

    def update(self, client=None) -> dict:
        """「更新检查」用：读取 GitHub 最新正式发行版做版本比对。

        `client` 可注入（AGENTS §5）：测试传假 client，不打真实接口。
        """
        from shared.update import check as check_update

        result = check_update(client)
        result["online_update"] = not ConsoleViews.online_update_unavailable()
        return result

    @staticmethod
    def online_update_unavailable() -> str:
        import os
        from shared import online_update

        if os.name != "nt" or os.environ.get("BILI_ONLINE_SUPERVISED") != "1":
            return "在线更新需要由 Windows 守护进程启动监控台"
        if (online_update.ROOT / ".git").exists():
            return "源码工作区请用 Git 更新"
        try:
            if online_update.locked_path(online_update.ROOT, online_update.KEY).is_file():
                return ""
        except online_update.UpdateError:
            pass
        return "当前安装缺少更新签名公钥"

    def online_update_status(self) -> dict:
        from shared.online_update import OnlineUpdate

        unavailable = self.online_update_unavailable()
        return {"ok": True, "available": not unavailable, "reason": unavailable,
                **(OnlineUpdate().status() if not unavailable else {"state": "idle"})}

    def queue_online_update(self, target: str) -> dict:
        from shared.online_update import OnlineUpdate, UpdateError

        unavailable = self.online_update_unavailable()
        if unavailable:
            raise ApiError(unavailable, 409)
        try:
            OnlineUpdate().queue(target)
        except UpdateError as exc:
            raise ApiError(str(exc), 409) from exc
        return {"ok": True, "state": "queued"}

    def proxy(self) -> dict:
        """「设置 → 网络代理」：当前保存的配置（地址本身不是凭据，可以回显）。"""
        from shared import proxy as proxy_cfg

        return proxy_cfg.status()

    def save_proxy(self, body: dict) -> dict:
        """保存代理配置：地址不合法抛 ApiError(400)；存完立刻写本进程环境变量。"""
        from shared import proxy as proxy_cfg

        try:
            cfg = proxy_cfg.save(body or {})
        except (TypeError, ValueError) as exc:
            raise ApiError(str(exc), 400) from None
        st = proxy_cfg.apply_to_env(cfg)
        # 2026-11-20：采集 / 推送由 StackManager 盯着 config/proxy.json 的指纹自动重启
        # （默认 2 秒一轮对账），所以这里不再要求用户手动重启；监控室自己每 5 秒重建连接池。
        st["note"] = "本进程立刻换用；采集 / 推送会在几秒内自动重启换用新配置"
        return st

    def test_proxy(self, body: dict, client=None) -> dict:
        """「测试连接」：用给定代理（缺省用已保存的）访问一次更新检查地址。

        `client` 可注入（AGENTS §5）：测试传假 client，不打真实接口。
        """
        from shared import proxy as proxy_cfg

        body = body or {}
        saved = proxy_cfg.load()
        mode = str(body.get("mode") or saved["mode"]).strip().lower()
        url = str(body.get("url") or saved["url"]).strip()
        if mode == "custom":
            return proxy_cfg.probe(url, client=client)
        if mode == "system":
            return proxy_cfg.probe(client=client, use_system=True)
        return {"ok": True, "passed": False, "status": 0, "ms": 0, "target": "",
                "error": "当前是「不使用代理」，不用测试"}

    def overview(self) -> dict:
        subs = self._q("SELECT COUNT(*) c, SUM(enabled) e FROM subject")[0]
        living = self._q("SELECT COUNT(*) c FROM subject_state WHERE live_status=1")[0]["c"]
        ev = self._q("SELECT COUNT(*) c, MAX(created_at) m FROM event")[0]
        dl = {r["status"]: r["c"] for r in self._q(
            "SELECT status, COUNT(*) c FROM delivery GROUP BY status")}
        targets = [dict(r) for r in self._q("SELECT * FROM target")]
        last = self._q("SELECT event_id, subject_uid, type, title, occurred_at, created_at "
                       "FROM event ORDER BY created_at DESC LIMIT 1")
        db_bytes = Path(self.db_path).stat().st_size if Path(self.db_path).exists() else 0
        return {
            "ok": True,
            "now": int(time.time()),
            "uptime_s": int(time.time() - self.started_at),
            "db": {"path": self.db_path, "bytes": db_bytes},
            "subjects": {"total": subs["c"], "enabled": subs["e"] or 0, "living": living},
            "events": {"total": ev["c"], "last_created_at": ev["m"]},
            "deliveries": {"pending": dl.get("pending", 0), "sent": dl.get("sent", 0),
                           "failed": dl.get("failed", 0)},
            "targets": targets,
            "last_event": dict(last[0]) if last else None,
            "global": self.global_state(),
        }

    def stats(self) -> dict:
        """右上角计数的轻量接口：页面每 3 秒轮询一次，只做几条 COUNT。

        2026-11-01 新增。原来这些数字只随 overview() 一起下发，而 overview 只在
        「概览」页签被定时器调用 —— 切到名单/事件/投递/日志后，右上角就冻住了。
        """
        subs = self._q("SELECT COUNT(*) c, SUM(enabled) e FROM subject")[0]
        living = self._q("SELECT COUNT(*) c FROM subject_state WHERE live_status=1")[0]["c"]
        ev = self._q("SELECT COUNT(*) c FROM event")[0]
        dl = {r["status"]: r["c"] for r in self._q(
            "SELECT status, COUNT(*) c FROM delivery GROUP BY status")}
        return {
            "ok": True,
            "now": int(time.time()),
            "subjects": {"total": subs["c"], "enabled": subs["e"] or 0, "living": living},
            "events": {"total": ev["c"]},
            "deliveries": {"pending": dl.get("pending", 0), "sent": dl.get("sent", 0),
                           "failed": dl.get("failed", 0)},
        }

    def events(self, limit: int = 50, uid: int | None = None) -> list[dict]:
        sql = ("SELECT e.event_id, e.subject_uid, e.type, e.occurred_at, e.created_at,"
               " e.title, e.url, e.summary, e.extra, s.name,"
               " (SELECT COUNT(*) FROM delivery d WHERE d.event_id=e.event_id) targets,"
               " (SELECT COUNT(*) FROM delivery d WHERE d.event_id=e.event_id AND d.status='sent') sent,"
               " (SELECT COUNT(*) FROM delivery d WHERE d.event_id=e.event_id AND d.status='pending') pending,"
               " (SELECT COUNT(*) FROM delivery d WHERE d.event_id=e.event_id AND d.status='failed') failed"
               " FROM event e LEFT JOIN subject s ON s.uid=e.subject_uid")
        args: tuple = ()
        if uid:
            sql += " WHERE e.subject_uid=?"
            args = (uid,)
        sql += " ORDER BY e.created_at DESC LIMIT ?"
        args = args + (limit,)
        out = []
        for r in self._q(sql, args):
            extra = json.loads(r["extra"] or "{}")
            out.append({
                "event_id": r["event_id"], "uid": r["subject_uid"],
                "name": r["name"] or extra.get("uname") or ("UID%d" % r["subject_uid"]),
                "type": r["type"], "type_label": TYPE_LABEL.get(r["type"], r["type"]),
                "occurred_at": r["occurred_at"], "created_at": r["created_at"],
                "title": r["title"], "url": r["url"], "summary": r["summary"] or "",
                "extra": extra, "targets": r["targets"], "sent": r["sent"],
                "pending": r["pending"], "failed": r["failed"],
            })
        return out

    def events_total(self, uid: int | None = None) -> int:
        """H10：事件页的「共 N 条」必须是库里的总数，不是本页 80 条的数组长度。

        以前页面写 o.events.length，而 /api/events 只回当前页 —— 页脚永远显示
        「共 80 条」，与概览页的「事件总数 1219」互相矛盾，看着像事件被删了。
        """
        if uid:
            return int(self._q("SELECT COUNT(*) c FROM event WHERE subject_uid=?", (uid,))[0]["c"])
        return int(self._q("SELECT COUNT(*) c FROM event")[0]["c"])

    def _delivery_has_raw(self) -> bool:
        """M11：delivery.raw 只在 schema v3 之后存在，旧库未迁移时降级为不查。"""
        try:
            return any(r[1] == "raw" for r in self._q("PRAGMA table_info(delivery)"))
        except Exception:                        # noqa: BLE001
            return False

    def deliveries(self, status: str = "", limit: int = 50) -> list[dict]:
        has_raw = self._delivery_has_raw()
        sql = ("SELECT d.delivery_id, d.event_id, d.target_id, d.attempts, d.next_at, d.status,"
               " d.last_error, e.subject_uid, e.type, e.title, e.url, e.occurred_at, s.name, t.display_name,"
               " t.external_id"
               + (", d.raw" if has_raw else "")
               + " FROM delivery d LEFT JOIN event e ON e.event_id=d.event_id"
               " LEFT JOIN subject s ON s.uid=e.subject_uid"
               " LEFT JOIN target t ON t.target_id=d.target_id")
        args: tuple = ()
        if status:
            sql += " WHERE d.status=?"
            args = (status,)
        sql += " ORDER BY d.delivery_id DESC LIMIT ?"
        args = args + (limit,)
        return [{"delivery_id": r["delivery_id"], "event_id": r["event_id"],
                 "target": r["display_name"] or str(r["target_id"]),
                 "name": r["name"] or ("UID%s" % r["subject_uid"]),
                 "type": r["type"], "type_label": TYPE_LABEL.get(r["type"], r["type"] or "?"),
                 "title": r["title"] or "", "url": r["url"] or "",
                 "status": r["status"], "attempts": r["attempts"],
                 "next_at": r["next_at"], "error": r["last_error"] or "",
                 "has_raw": (bool(r["raw"]) if has_raw else False),
                 "raw": ((r["raw"] or "")[:4000] if has_raw else "")}
                for r in self._q(sql, args)]

    def retry_delivery(self, delivery_id: int) -> dict:
        """H7：把失败/已忽略的投递重置为待发。

        只允许 failed / dismissed 重投（以前任何状态都能重置，已发的会被再发一遍）。
        重置 attempts 是刻意的：用户点「重投」等于给这条投递一次全新的重试预算。
        """
        n = self._x("UPDATE delivery SET status='pending', next_at=0, attempts=0, last_error=NULL, "
                    "claimed_at=NULL WHERE delivery_id=? AND status IN ('failed','dismissed')",
                    (delivery_id,))
        if not n:
            row = self._q("SELECT status FROM delivery WHERE delivery_id=?", (delivery_id,))
            if not row:
                raise ApiError("没有这条投递", 404)
            raise ApiError("只有失败或已忽略的投递可以重投（当前状态：%s）" % row[0]["status"], 409)
        return {"ok": True, "delivery_id": delivery_id}

    def dismiss_delivery(self, delivery_id: int) -> dict:
        """H7：把失败/死信投递标为「已忽略」——不再计入「失败」计数，也不会被重发。

        与重投互补：永久性错误（例如消息长度超限 40054007）重投只会再失败一次，
        用户需要的是「这条我知道，别再提醒」。
        """
        n = self._x("UPDATE delivery SET status='dismissed', claimed_at=NULL "
                    "WHERE delivery_id=? AND status IN ('failed','dismissed')", (delivery_id,))
        if not n:
            row = self._q("SELECT status FROM delivery WHERE delivery_id=?", (delivery_id,))
            if not row:
                raise ApiError("没有这条投递", 404)
            raise ApiError("只有失败或已忽略的投递可以忽略（当前状态：%s）" % row[0]["status"], 409)
        return {"ok": True, "delivery_id": delivery_id, "status": "dismissed"}

    def targets(self) -> list[dict]:
        return [{"target_id": r["target_id"], "channel_type": r["channel_type"],
                 "kind": r["kind"], "external_id": r["external_id"],
                 "display_name": r["display_name"], "enabled": bool(r["enabled"])}
                for r in self._q("SELECT * FROM target")]

    def set_target_enabled(self, target_id: int, enabled: bool) -> dict:
        if not self._x("UPDATE target SET enabled=? WHERE target_id=?", (1 if enabled else 0, target_id)):
            raise ApiError("没有这个目标", 404)
        return {"ok": True, "target_id": target_id, "enabled": enabled}

    def logs(self, lines: int = 200, date: str = "", tag: str = "",
             level: str = "", q: str = "") -> dict:
        """按日期 / 类型 / 严重程度 / 关键字筛选日志（见 core/logfmt.py）"""
        from core.logfmt import read_logs
        if level and level not in ("error", "warn", "info"):
            raise ApiError("level 只能是 error / warn / info", 400)
        # H8：非法 lines 由 safe_int 统一变 400，不再抛 ValueError 变 500
        lines = webutil.safe_int(lines, 200, 1, 2000, name="lines") or 200
        out = read_logs(self.log_dir, date=date, tag=tag, level=level, q=q, limit=lines)
        out["dir"] = str(self.log_dir)
        out["path"] = str(self.log_file or "")
        return out

