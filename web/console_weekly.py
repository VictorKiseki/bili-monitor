"""Console 的周表日程 / 候选 / AI 部分（由 web/console.py 拆出）。"""
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
from store.sqlite import SCHEMA, migrate  # noqa: E402
from web.cleanup import CleanupError, CleanupManager  # noqa: E402

from web.console_core import ApiError  # noqa: E402

class ConsoleWeekly:
    def weekly(self, week: str = "", limit: int = 24) -> dict:
        """一周日程表：按天汇总各主播的开播安排 + 待确认的候选（2026-11-03 新增）。

        数据链路见 docs/21：采集端只把「疑似周表」的图塞进 weekly_candidate，
        视觉回合的判定结果经 scripts/weekly_ingest.py 落 weekly_schedule。
        这里只读，不触发识别，避免管理台轮询把模型调用打爆。
        """
        from core import weekly as W
        with self._lock:
            conn = self._db()
            weeks = [r["week_start"] for r in conn.execute(
                "SELECT DISTINCT week_start FROM weekly_schedule"
                " ORDER BY week_start DESC LIMIT 26").fetchall()]
            ts = W._valid_week(week) or (weeks[0] if weeks else W.infer_week_start(time.time()))
            view = W.week_view(conn, ts)
            cands = W.pending_batch(conn, limit=limit)
            live = {}
            for r in conn.execute(
                    "SELECT st.uid, COALESCE(NULLIF(st.room_id, 0), sub.room_id, 0) AS room_id,"
                    " COALESCE(NULLIF(ls.title, ''), sub.name, '') AS title"
                    " FROM subject_state st"
                    " LEFT JOIN subject sub ON sub.uid=st.uid"
                    " LEFT JOIN live_session ls ON ls.uid=st.uid"
                    " WHERE st.live_status=1").fetchall():
                live[r["uid"]] = {"room_id": r["room_id"], "title": r["title"]}
        for c in cands:
            c["thumb"] = "/api/weekly/image?event=" + c["event_id"]
            c.pop("images", None)
        W.annotate_live(view, live)
        view["live"] = len(live)
        view["weeks"] = weeks
        view["prev"] = W.prev_next_week(ts, -1)
        view["next"] = W.prev_next_week(ts, 1)
        view["candidates"] = cands
        view["external"] = self._external_info(view.get("external") or [])
        view["cache"] = W.cache_stats()
        return view

    def weekly_cache_clean(self, force: bool = False) -> dict:
        """手动清一次已判定候选图（默认按 meta 里的保留天数，force 立刻删）。

        自动清理在每轮候选扫描后跑；这个按钮是给「想立刻腾空间」用的。
        """
        from core import weekly as W
        with self._lock:
            conn = self._db()
            stat = W.purge_cache(conn, keep_days=0.0 if force else None)
        stat["cache"] = W.cache_stats()
        stat["ok"] = True
        return stat

    def weekly_programs(self) -> dict:
        """常驻节目清单（主持 uid + 名字、固定时段）——给日程表上的「常驻节目」条用。"""
        from core import weekly as W
        with self._lock:
            conn = self._db()
            progs = W.load_programs(conn)
            subs = {r["uid"]: r["name"] for r in conn.execute(
                "SELECT uid, name FROM subject").fetchall()}
            return {"ok": True, "programs": W.program_public(progs, subs)}

    def weekly_program_save(self, body: dict) -> dict:
        """新增/修改常驻节目：节目名、别名、常驻主持、固定星期+时间、备注。"""
        from core import weekly as W
        try:
            with self._lock:
                return W.program_save(self._db(), body.get("name") or "",
                                      aliases=body.get("aliases"),
                                      hosts=body.get("hosts"),
                                      weekday=body.get("weekday") or "",
                                      time_=body.get("time") or "",
                                      note=body.get("note") or "",
                                      enabled=body.get("enabled", True),
                                      program_id=body.get("program_id"))
        except ValueError as e:
            raise ApiError(str(e))

    def weekly_program_delete(self, program_id: int) -> dict:
        from core import weekly as W
        try:
            with self._lock:
                return W.program_delete(self._db(), int(program_id))
        except ValueError as e:
            raise ApiError(str(e), 404)

    def _external_info(self, names: list[str]) -> list[dict]:
        """名单外、但被联动文案点到的名字：有缓存就给缓存，没查过的挂后台线程去 B 站搜。"""
        names = [n for n in dict.fromkeys(names or []) if n]
        if not names:
            return []
        out: list[dict] = []
        todo: list[str] = []
        with self._lock:
            conn = self._db()
            for name in names:
                row = conn.execute("SELECT name, uid, uname, fans, face, sign, status, note"
                                   " FROM streamer_search WHERE name=?", (name,)).fetchone()
                if row is None:
                    conn.execute("INSERT OR IGNORE INTO streamer_search(name, status, updated_at)"
                                 " VALUES(?, 'pending', ?)", (name, time.time()))
                    conn.commit()
                    row = {"name": name, "uid": None, "uname": "", "fans": 0,
                           "face": "", "sign": "", "status": "pending", "note": ""}
                d = dict(row)
                if d["status"] in ("pending", "doing"):
                    todo.append(name)
                out.append(d)
        if todo:
            self._kick_search()
        return out

    def _kick_search(self) -> None:
        with self._search_lock:
            if self._search_thread and self._search_thread.is_alive():
                return
            self._search_thread = threading.Thread(target=self._search_worker,
                                                   name="weekly-search", daemon=True)
            self._search_thread.start()

    def _search_worker(self) -> None:
        """把 status=pending 的名字逐条搜掉。失败只记 note，不影响日程表轮询。"""
        from collector.bili import BiliClient
        client = None
        try:
            client = BiliClient(min_interval=1.5)
            while True:
                with self._lock:
                    conn = self._db()
                    row = conn.execute("SELECT name FROM streamer_search"
                                       " WHERE status='pending' ORDER BY updated_at LIMIT 1").fetchone()
                    if row is None:
                        return
                    name = row["name"]
                    conn.execute("UPDATE streamer_search SET status='doing', updated_at=?"
                                 " WHERE name=?", (time.time(), name))
                    conn.commit()
                hit: dict | None = None
                status, note = "none", ""
                try:
                    low = name.lower()
                    for it in client.search_user(name, limit=5):
                        un = (it.get("uname") or "").lower()
                        if low and (low in un or un in low):
                            hit = it
                            break
                    if hit:
                        status = "done"
                    else:
                        note = "B 站没搜到同名 UP 主"
                except Exception as e:                     # noqa: BLE001
                    status, note = "error", "%s: %s" % (type(e).__name__, e)
                with self._lock:
                    conn = self._db()
                    if hit:
                        conn.execute("UPDATE streamer_search SET uid=?, uname=?, fans=?,"
                                     " face=?, sign=?, status=?, note=?, updated_at=?"
                                     " WHERE name=?",
                                     (hit["uid"], hit["uname"], hit["fans"], hit["face"],
                                      hit["sign"], status, note, time.time(), name))
                    else:
                        conn.execute("UPDATE streamer_search SET status=?, note=?, updated_at=?"
                                     " WHERE name=?", (status, note, time.time(), name))
                    conn.commit()
                time.sleep(0.8)
        except Exception as e:                             # noqa: BLE001
            print("[周表] 外部对象检索线程退出：%s: %s" % (type(e).__name__, e))
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:                          # noqa: BLE001
                    pass

    def weekly_skip(self, event_id: str) -> dict:
        n = self._x("UPDATE weekly_candidate SET status='skipped', note=?, decided_at=?"
                    " WHERE event_id=? AND status='pending'",
                    ("管理台手动标注：非周表", time.time(), event_id))
        if not n:
            raise ApiError("没有这条待确认候选", 404)
        return {"ok": True, "event_id": event_id}

    def weekly_candidate_verdict(self, body: dict) -> dict:
        """人工给待确认候选定性质（支持批量）：week / single / none。"""
        from core import weekly as W
        try:
            with self._lock:
                return W.set_candidate_scope(self._db(), body.get("event_ids") or [],
                                             str(body.get("scope") or ""))
        except ValueError as e:
            raise ApiError(str(e))

    def weekly_manual_save(self, body: dict) -> dict:
        """人工新增/修改/移除一条日程（2026-11-03）。"""
        from core import weekly as W
        uid = webutil.safe_uid(body.get("uid"), name="uid") or 0
        if not uid:
            raise ApiError("缺少 uid")
        try:
            with self._lock:
                return W.manual_add(self._db(), uid, str(body.get("week_start") or ""),
                                    str(body.get("day") or ""), str(body.get("op") or "add"),
                                    body.get("time"), str(body.get("text") or ""),
                                    body.get("target_time"), body.get("target_text"),
                                    str(body.get("note") or ""), str(body.get("uname") or ""),
                                    body.get("collab_uids"))
        except ValueError as e:
            raise ApiError(str(e))

    def weekly_manual_update(self, manual_id: int, body: dict) -> dict:
        from core import weekly as W
        try:
            with self._lock:
                return W.manual_update(self._db(), int(manual_id), **body)
        except ValueError as e:
            raise ApiError(str(e))

    def weekly_manual_delete(self, manual_id: int) -> dict:
        from core import weekly as W
        try:
            with self._lock:
                return W.manual_delete(self._db(), int(manual_id))
        except ValueError as e:
            raise ApiError(str(e), 404)

    def weekly_session_delete(self, session_id: str) -> dict:
        from core import weekly as W
        try:
            with self._lock:
                return W.session_delete(self._db(), session_id)
        except ValueError as e:
            raise ApiError(str(e), 404)

    def weekly_image(self, event_id: str):
        """把候选图从本地缓存吐给页面（B 站图床有防盗链，浏览器直连会 403）。"""
        rows = self._q("SELECT images_json FROM weekly_candidate WHERE event_id=?", (event_id,))
        if not rows:
            raise ApiError("没有这张候选图", 404)
        imgs = json.loads(rows[0]["images_json"] or "[]")
        if not imgs or not imgs[0].get("path"):
            raise ApiError("候选没有落盘图片", 404)
        p = Path(imgs[0]["path"])
        # M8：白名单根锚在 __file__ 上（ROOT = 项目根），不再跟着启动目录（Path.cwd()）走，
        # 否则从别的目录启动服务时 relative_to 判定的根就漂了
        if not p.is_absolute():
            p = ROOT / p
        try:
            p = p.resolve()
            p.relative_to((ROOT / "data" / "schedules").resolve())
        except ValueError:
            raise ApiError("图片不在周表缓存目录", 403)
        if not p.is_file():
            raise ApiError("图片文件已不存在", 404)
        ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".webp": "image/webp", ".gif": "image/gif"}.get(p.suffix.lower(), "image/jpeg")
        return p.read_bytes(), ctype

    _AI_DEFAULTS = {"weekly_ai_enabled": "1", "weekly_ai_limit": "12",
                    "weekly_ai_slots": "08:00,13:00,19:00,00:00"}

    def _ai_cfg(self, conn) -> dict:
        d = dict(self._AI_DEFAULTS)
        for r in conn.execute("SELECT k, v FROM meta WHERE k LIKE 'weekly_ai_%'").fetchall():
            d[r["k"]] = r["v"]
        d.setdefault("weekly_ai_request_at", "0")
        d.setdefault("weekly_ai_last_run", "0")
        return d

    def weekly_ai_status(self) -> dict:
        """定时识别流程的开关/条数/待确认，以及有没有排队的立刻执行请求。

        真正的执行者是 DSH 定时会话，这里只改「跑不跑、跑几条」的口径；
        会话第一步用 scripts/weekly_ai.py gate 读同一份配置。
        """
        from core import weekly as W
        with self._lock:
            conn = self._db()
            d = self._ai_cfg(conn)
            pend = W.pending_count(conn)          # 已扣掉日程表监控关掉的主播
        last_run = float(d.get("weekly_ai_last_run") or 0)
        req = float(d.get("weekly_ai_request_at") or 0)
        return {"ok": True,
                "enabled": str(d.get("weekly_ai_enabled", "1")) not in ("0", "false", "no", ""),
                "limit": int(d.get("weekly_ai_limit") or 12),
                "slots": [s.strip() for s in str(d.get("weekly_ai_slots") or "").split(",") if s.strip()],
                "pending": pend,
                "last_run": last_run,
                "request_pending": req > last_run}

    def weekly_ai_update(self, body: dict) -> dict:
        updates: dict[str, str] = {}
        if "enabled" in body:
            updates["weekly_ai_enabled"] = "1" if body.get("enabled") else "0"
        if "limit" in body:
            try:
                updates["weekly_ai_limit"] = str(max(1, min(int(body.get("limit") or 12), 60)))
            except (TypeError, ValueError):
                raise ApiError("每次处理的条数要是 1~60 的整数")
        if "slots" in body:
            slots = [str(s).strip() for s in (body.get("slots") or []) if str(s).strip()]
            for s in slots:
                if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", s):
                    raise ApiError("时段要写成 HH:MM，例如 08:00")
            updates["weekly_ai_slots"] = ",".join(slots)
        if updates:
            with self._lock:
                conn = self._db()
                for k, v in updates.items():
                    conn.execute("INSERT INTO meta(k, v) VALUES(?, ?)"
                                 " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
                conn.commit()
        return self.weekly_ai_status()

    def weekly_ai_request(self) -> dict:
        """「立刻执行」：写排队标记，下一次定时会话看到就直接干活（控制台唤不醒 DSH）。"""
        now = time.time()
        with self._lock:
            conn = self._db()
            conn.execute("INSERT INTO meta(k, v) VALUES('weekly_ai_request_at', ?)"
                         " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(now),))
            conn.commit()
        return {"ok": True, "request_at": now,
                "note": "已排队：下一次定时识别会优先执行（控制台无法直接唤醒 DSH 会话）"}

    # ---------------- 日程识别 Harness 交接（2026-11-08） ----------------
    # 产品侧只保留闸门与文件交接：到点由 weekly_agent_loop 导出任务包，
    # DSH 会话读图后把结果写进 out/ai_results，导回即入库。这里不配置也不调用模型。

    def weekly_agent_status(self) -> dict:
        """开关/队列 + 上次结果 + Harness 任务统计 + 下一个时段。"""
        from core.weekly import agent as A
        from core.weekly import harness as H
        base = self.weekly_ai_status()
        with self._lock:
            conn = self._db()
            agent_meta = {r["k"]: r["v"] for r in conn.execute(
                "SELECT k, v FROM meta WHERE k LIKE 'weekly_agent_%'").fetchall()}
            slots = A.slot_due(conn) if A.pending_count(conn) else {"next_run": 0}
        base.update({
            "harness": H.stats(),
            "agent": {"last_run": float(agent_meta.get("weekly_agent_last_run") or 0),
                      "last_items": int(agent_meta.get("weekly_agent_last_items") or 0),
                      "last_error": agent_meta.get("weekly_agent_last_error") or ""},
            "next_run": int(slots.get("next_run") or 0),
        })
        return base

    def weekly_agent_export(self, limit: int | None = None, event_ids=None) -> dict:
        """导出任务包（out/ai_tasks/*.json），返回路径与可粘给会话的提示词。

        已在未完成任务里的候选不会重复导出。
        """
        from core.weekly import agent as A
        from core.weekly import harness as H
        with self._lock:
            conn = self._db()
            res = A.harness_export(conn, limit=limit, event_ids=event_ids)
        res["harness"] = H.stats()
        return res

    def weekly_agent_import(self, body: dict) -> dict:
        """把会话输出的 JSON 贴回来 → 落结果文件 → 校验入库。

        body: {"json": "<粘贴的文本>", "batch_id": "可选，缺省按任务目录推断"}
        """
        from core.weekly import agent as A
        from core.weekly import harness as H
        body = body if isinstance(body, dict) else {"json": body}
        raw = body.get("json")
        if raw is None:
            raw = body.get("results")
        if raw is None:
            raise ApiError("没有收到结果 JSON（把会话输出的内容整段贴进来）")
        if isinstance(raw, str) and not raw.strip():
            raise ApiError("结果 JSON 是空的")
        with self._lock:
            conn = self._db()
            try:
                path = H.save_result(raw, batch_id=str(body.get("batch_id") or "").strip() or None)
            except ValueError as e:
                raise ApiError("结果格式不对：%s" % e)
            res = A.harness_ingest(conn)
        out = {"ok": True, "saved": str(path), "count": res.get("count") or 0,
               "ingest": res.get("ingest") or {}, "problems": res.get("problems") or [],
               "harness": H.stats()}
        if out["problems"]:
            out["note"] = "有 %d 条没通过校验，结果文件先留在原处，改好再导一次" % len(out["problems"])
        return out

    # ---------------- 日程功能总开关（2026-11-08） ----------------
    # 首次配置默认关闭：关掉时前端隐藏日程入口，守护进程也不拉起日程进程组。
    # 入口在「监控名单」的添加框：weekly_open 开启，weekly_close 关闭。

    def weekly_feature_status(self) -> dict:
        from core.weekly import feature as F
        from core.weekly import push as P
        with self._lock:
            conn = self._db()
            data = F.status(conn)
            data["push_at"] = P.get_push_at(conn)
            data["push_at_default"] = P.DEFAULT_PUSH_AT
        data["note"] = ("日程功能已开启：周表扫描、预约刷新、日程交接与每日推送由日程进程组执行"
                        if data.get("enabled")
                        else "日程功能默认关闭：在「监控名单」添加框输入 weekly_open 并点「添加」即可开启")
        return data

    def weekly_feature_update(self, body: dict) -> dict:
        from core.weekly import feature as F
        from core.weekly import push as P
        body = body if isinstance(body, dict) else {}
        has_enabled = "enabled" in body
        has_push = "push_at" in body
        if not has_enabled and not has_push:
            raise ApiError("缺少 enabled（true 开启 / false 关闭）或 push_at（每日推送时间 H:mm）")
        on = bool(body.get("enabled")) if has_enabled else None
        with self._lock:
            conn = self._db()
            if has_push:
                try:
                    at = P.normalize_push_at(body.get("push_at"))
                except ValueError as e:
                    raise ApiError(str(e))
                if not at:
                    raise ApiError("推送时间不能为空；要停止推送请关闭日程功能或暂停 daily_push")
                P.set_push_at(conn, at)
            if has_enabled:
                F.apply_enabled(conn, bool(on))
            data = F.status(conn)
            data["push_at"] = P.get_push_at(conn)
            data["push_at_default"] = P.DEFAULT_PUSH_AT
        if has_push and has_enabled:
            data["note"] = ("已%s日程功能；每日推送时间：每天 %s（1 分钟内生效）"
                            % ("开启" if on else "关闭", data["push_at"]))
        elif has_push:
            data["note"] = "已保存每日推送时间：每天 %s（1 分钟内生效）" % data["push_at"]
        else:
            data["note"] = ("已开启日程功能：守护进程会在几秒内拉起日程进程组"
                            if on else
                            "已关闭日程功能：守护进程会收起日程进程组，相关入口已隐藏")
        return data
