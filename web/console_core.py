"""Console 基础设施 / 名单 / 分组 / 全局模式（由 web/console.py 拆出）。"""
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

import core.tasks as T  # noqa: E402
from shared import webutil  # noqa: E402
from store.sqlite import SCHEMA, migrate  # noqa: E402
from web.cleanup import CleanupError, CleanupManager  # noqa: E402

class ApiError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code

class ConsoleCore:
    """管理台的数据层：直接用 sqlite3 读库，改名单时同步写回 config/subjects.json。"""

    def __init__(self, db_path: str | Path = "data/bot.db",
                 subjects_path: str | Path = "config/subjects.json",
                 log_file: str | Path | None = None,
                 client_factory=None,
                 cleanup: CleanupManager | None = None,
                 log_dir: str | Path | None = None):
        self.db_path = str(db_path)
        # 进程页的开关落库在 meta['tasks_off']，而 Board 是进程级单例：谁起管理台，
        # 谁负责把库绑上。2026-11-11 的教训 —— run_console.py 这条新链路没人 bind，
        # 页面点一次开关就 500「没有绑定数据库，无法保存开关」。绑在 __init__ 里，
        # 以后新增入口（脚手架 / 测试 / 脚本）都不会漏。
        T.BOARD.bind(self.db_path)
        self.subjects_path = Path(subjects_path)
        self.log_file = Path(log_file) if log_file else None
        # 按日日志目录（logs/collector-YYYY-MM-DD.log、logs/pusher-YYYY-MM-DD.log），筛选按目录扫。
        # 入口要显式传 log_dir：日志页读的是「按日结构化日志」，而监控台主进程的 --log-file
        # 落在 out/console.log（stdout 捕获），从它的父目录推会指到 out/ 去 —— 2026-11-14
        # 日志页一直空白就是这么来的。老入口（run_bot --web）不传时仍按 --log-file 的目录。
        self.log_dir = Path(log_dir) if log_dir else (
            self.log_file.parent if self.log_file else Path("logs"))
        self._client_factory = client_factory
        self.cleanup = cleanup or CleanupManager()
        self._client = None
        # make_server() 注入：进程页「主进程」那行要显示真实端口，「停止」要能关掉服务器
        self._web_server = None
        self._web_port = 0
        # 可重入锁（H7）：名单读-改-写与 _x/_q 共用同一把锁，
        # 锁内再调 _x 不会自锁（旧版是 Lock，一嵌套就死等）
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.started_at = time.time()
        self._auth_cache: dict | None = None
        self._auth_at = 0.0
        # 联动外部对象的 B 站查证（2026-11-04）：一次一个请求的后台线程
        self._search_lock = threading.Lock()
        self._search_thread: threading.Thread | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=4000")
            conn.executescript(SCHEMA)
            migrate(conn)
            # M5：迁移（v4 重建表）期间必须关外键，跑完再打开；管理台的写路径
            # （删主播等）也要级联，否则又会造出孤儿行。
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
        return self._conn

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db().execute(sql, args).fetchall())

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            conn = self._db()
            cur = conn.execute(sql, args)
            # 必须提交：python sqlite3 默认不自动提交，留着写事务会把库锁死，
            # 主循环那边就会 sqlite3.OperationalError: database is locked（2026-10-28 长跑暴露）
            conn.commit()
            return cur.rowcount

    def _client_get(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                from collector.bili import BiliClient
                self._client = BiliClient()
        return self._client

    def _read_config_locked(self) -> dict:
        """读镜像。调用方必须已持有 self._lock。"""
        if not self.subjects_path.exists():
            return {"subjects": [], "poll": {}}
        return json.loads(self.subjects_path.read_text("utf-8"))

    def _write_config_locked(self, cfg: dict) -> None:
        """原子写镜像（临时文件 + os.replace）。调用方必须已持有 self._lock。"""
        cfg.setdefault("subjects", [])
        self.subjects_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.subjects_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")
        os.replace(tmp, self.subjects_path)

    def load_config(self) -> dict:
        with self._lock:
            return self._read_config_locked()

    def save_config(self, cfg: dict) -> None:
        with self._lock:
            self._write_config_locked(cfg)

    def _update_config(self, mutate) -> None:
        """锁内读-改-写：mutate(cfg) 就地改，返回时已原子落盘。

        需要「保留镜像里别的字段」时用它（例如只翻某人的 _disabled），
        不要在锁外 load_config() 改完再 save_config()，那样会丢别人的更新。
        """
        with self._lock:
            cfg = self._read_config_locked()
            mutate(cfg)
            self._write_config_locked(cfg)

    def subjects(self) -> list[dict]:
        rows = self._q(
            "SELECT s.uid, s.name, s.room_id, s.enabled, s.grp, s.push_live, s.push_dynamic,"
            " s.weekly_on,"
            " st.live_status, st.last_poll_ts, st.last_dynamic_id, st.last_live_start_ts"
            " FROM subject s LEFT JOIN subject_state st ON st.uid = s.uid ORDER BY s.uid")
        now = int(time.time())
        out = []
        for r in rows:
            out.append({
                "uid": r["uid"], "name": r["name"] or ("UID%d" % r["uid"]),
                "room_id": r["room_id"] or 0, "enabled": bool(r["enabled"]),
                "group": r["grp"] or "",
                "push_live": bool(r["push_live"]), "push_dynamic": bool(r["push_dynamic"]),
                "weekly": bool(r["weekly_on"]),
                "live_status": r["live_status"] if r["live_status"] is not None else -1,
                "last_poll_ts": r["last_poll_ts"] or 0,
                "last_dynamic_id": r["last_dynamic_id"] or "",
                "live_start_ts": r["last_live_start_ts"] or 0,
                "baseline_done": bool(r["last_poll_ts"]),
                "poll_age_s": (now - r["last_poll_ts"]) if r["last_poll_ts"] else None,
            })
        return out

    def resolve_uid(self, uid: int) -> dict:
        """补全昵称与房间号；取不到房间号也允许加（只监控动态）。"""
        c = self._client_get()
        name, room_id = "", 0
        try:
            live = c.fetch_live_batch([uid])
            st = live.get(uid)
            if st is not None:
                name, room_id = st.uname or "", st.room_id or 0
        except Exception:                                   # noqa: BLE001
            pass
        if not room_id:
            try:
                info = c.get_room_by_mid(uid)
                room_id = int(info.get("roomid") or info.get("room_id") or 0)
                name = name or str(info.get("uname") or "")
            except Exception:                               # noqa: BLE001
                pass
        return {"uid": uid, "name": name or ("UID%d" % uid), "room_id": room_id}

    def search(self, keyword: str, limit: int = 8) -> list[dict]:
        from collector.bili import API
        c = self._client_get()
        body = c._get("%s/x/web-interface/wbi/search/type" % API,
                      {"search_type": "bili_user", "keyword": keyword,
                       "page": 1, "page_size": max(limit, 10)},
                      wbi=True, allow_code=(-352, -412, -799))
        res = ((body.get("data") or {}).get("result") or [])[:limit]
        return [{"uid": r.get("mid"), "name": r.get("uname"),
                 "fans": r.get("fans"), "sign": (r.get("usign") or "")[:60]} for r in res]

    def add_subject(self, uid: int | None = None, keyword: str | None = None,
                    group: str = "") -> dict:
        group = self._clean_group(group)
        if not uid and keyword:
            cands = self.search(keyword, 1)
            if not cands:
                raise ApiError("搜索不到这个用户名，请直接填 UID", 404)
            uid = int(cands[0]["uid"])
        if not uid:
            raise ApiError("需要 uid 或 keyword")
        row = self._q("SELECT uid, enabled FROM subject WHERE uid=?", (uid,))
        if row and row[0]["enabled"]:
            raise ApiError("UID %d 已在名单里" % uid, 409)
        readd = bool(row)                     # 库里有但被名单对账停用过 → 当作重新加入
        info = self.resolve_uid(uid)          # 网络查询放在锁外，别拿锁等 B 站
        entry = {"uid": info["uid"], "room_id": info["room_id"], "name": info["name"]}
        if group:
            entry["group"] = group
        if info["room_id"] == 0:
            entry["_no_room"] = True
        with self._lock:                      # 双写一起做：先库（权威）后镜像
            self._x("INSERT INTO subject(uid,name,room_id,enabled,grp) VALUES(?,?,?,1,?) "
                    "ON CONFLICT(uid) DO UPDATE SET name=excluded.name, room_id=excluded.room_id,"
                    " enabled=1,"
                    " grp=CASE WHEN excluded.grp<>'' THEN excluded.grp ELSE subject.grp END",
                    (info["uid"], info["name"], info["room_id"], group))
            try:
                cfg = self._read_config_locked()
                subs = [s for s in cfg.get("subjects", []) if int(s.get("uid") or 0) != info["uid"]]
                subs.append(entry)
                cfg["subjects"] = subs
                self._write_config_locked(cfg)
            except Exception:
                # 补偿：镜像没写成 → 新行删掉；重新加入的只把 enabled 退回 0，别丢水位
                try:
                    if readd:
                        self._x("UPDATE subject SET enabled=0 WHERE uid=?", (info["uid"],))
                    else:
                        self._x("DELETE FROM subject WHERE uid=?", (info["uid"],))
                except Exception:                              # noqa: BLE001
                    pass
                raise
        note = ("已重新加入名单，下一轮恢复轮询（水位和推送开关都保留）" if readd
                else "已加入，下一轮动态轮询会建立基线（不推历史）")
        return {"ok": True, "subject": entry, "note": note}

    def set_subject_flags(self, uid: int, live=None, dynamic=None, weekly=None) -> dict:
        """单个主播的推送 / 日程表监控开关（2026-11-07）。只改库，不回写 config。

        live/dynamic 是「推不推 QQ」；weekly 是「要不要把 TA 的图收进周表候选池、
        让视觉回合去判」——关掉不产新候选，已判出来的历史日程照常留在表里。
        """
        if not uid:
            raise ApiError("缺少 uid")
        sets, args = [], []
        if live is not None:
            sets.append("push_live=?")
            args.append(1 if live else 0)
        if dynamic is not None:
            sets.append("push_dynamic=?")
            args.append(1 if dynamic else 0)
        if weekly is not None:
            sets.append("weekly_on=?")
            args.append(1 if weekly else 0)
        if not sets:
            raise ApiError("至少要给 live / dynamic / weekly 其中一个")
        args.append(uid)
        if not self._x("UPDATE subject SET %s WHERE uid=?" % ", ".join(sets), tuple(args)):
            raise ApiError("没有这个 UID", 404)
        r = self._q("SELECT push_live, push_dynamic, weekly_on FROM subject WHERE uid=?", (uid,))[0]
        return {"ok": True, "uid": uid,
                "push_live": bool(r["push_live"]), "push_dynamic": bool(r["push_dynamic"]),
                "weekly": bool(r["weekly_on"])}

    SNAPSHOT_TABLES = ("subject", "subject_state", "live_session")

    def _snapshot_uids(self, uids) -> list[tuple]:
        """拍下这些 UID 在上述表里的整行（调用方持锁）。返回 [(表, 列名, 行...)]。"""
        out = []
        uids = list(uids or [])
        if not uids:
            return out
        ph = ",".join("?" * len(uids))
        for table in self.SNAPSHOT_TABLES:
            cur = self._db().execute(
                "SELECT * FROM %s WHERE uid IN (%s)" % (table, ph), tuple(uids))
            cols = [d[0] for d in cur.description]
            out.append((table, cols, [tuple(r) for r in cur.fetchall()]))
        return out

    def _restore_uids(self, snapshot) -> None:
        """按快照还原：先删这些 UID 再整体插回（等于撤销这次库内改动）。"""
        for table, cols, rows in snapshot or []:
            if not rows:
                continue
            uids = tuple(sorted({r[cols.index("uid")] for r in rows}))
            ph = ",".join("?" * len(uids))
            self._db().execute("DELETE FROM %s WHERE uid IN (%s)" % (table, ph), uids)
            self._db().execute(
                "INSERT OR REPLACE INTO %s(%s) VALUES(%s)"
                % (table, ",".join(cols), ",".join("?" * len(cols))),
                tuple(v for r in rows for v in r))
            self._db().commit()

    def remove_subject(self, uid: int) -> dict:
        with self._lock:
            snap = self._snapshot_uids([uid])
            if not self._x("DELETE FROM subject WHERE uid=?", (uid,)):
                raise ApiError("没有这个 UID", 404)
            self._x("DELETE FROM subject_state WHERE uid=?", (uid,))
            self._x("DELETE FROM live_session WHERE uid=?", (uid,))
            try:
                self._update_config(lambda cfg: cfg.__setitem__(
                    "subjects",
                    [s for s in cfg.get("subjects", []) if int(s.get("uid", 0)) != uid]))
            except Exception:
                # 补偿：镜像没写成 → 三张表里刚删的行全部还原
                self._restore_uids(snap)
                raise
        return {"ok": True, "uid": uid, "note": "已移除，历史事件与已见动态保留（重新加入不会补推历史）"}

    MAX_GROUP_NAME = 24

    def _clean_group(self, name) -> str:
        g = str(name or "").strip()
        if len(g) > self.MAX_GROUP_NAME:
            raise ApiError("分组名最多 %d 个字" % self.MAX_GROUP_NAME)
        if "\n" in g or "\r" in g:
            raise ApiError("分组名不能包含换行")
        return g

    GROUP_KEY = "group_names"

    def _group_registry(self) -> list[str]:
        """新建过分组的名字（存 meta），让「空分组」也能存在。"""
        raw = self._meta_get(self.GROUP_KEY, "[]") or "[]"
        try:
            names = [self._clean_group(n) for n in json.loads(raw)]
        except (ValueError, TypeError):
            return []
        out: list[str] = []
        for n in names:
            if n and n not in out:
                out.append(n)
        return out

    def _save_group_registry(self, names: list[str]) -> None:
        self._meta_set(self.GROUP_KEY, json.dumps(list(dict.fromkeys(names)), ensure_ascii=False))

    def _ensure_group(self, name: str) -> None:
        if not name:
            return
        names = self._group_registry()
        if name not in names:
            names.append(name)
            self._save_group_registry(names)

    def _drop_group(self, name: str) -> None:
        self._save_group_registry([n for n in self._group_registry() if n != name])

    COLOR_KEY = "group_colors"

    @staticmethod
    def _is_color(v) -> bool:
        return isinstance(v, str) and bool(re.fullmatch(r"#[0-9a-fA-F]{6}", v.strip()))

    def _group_colors(self) -> dict[str, str]:
        raw = self._meta_get(self.COLOR_KEY, "{}") or "{}"
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in data.items():
            name = str(k or "").strip()
            if name and self._is_color(v):
                out[name] = str(v).strip().lower()
        return out

    def _save_group_colors(self, colors: dict[str, str]) -> None:
        self._meta_set(self.COLOR_KEY, json.dumps(colors, ensure_ascii=False, sort_keys=True))

    def _drop_color(self, name: str) -> None:
        colors = self._group_colors()
        if name in colors:
            colors.pop(name)
            self._save_group_colors(colors)

    def set_group_color(self, name: str, color: str) -> dict:
        """给分组设标签色；color 为空 = 恢复默认。只认 #rrggbb。"""
        name = self._clean_group(name)
        if not name:
            raise ApiError("不能给「未分组」上色")
        n = self._q("SELECT COUNT(*) c FROM subject WHERE grp=?", (name,))[0]["c"]
        if not n and name not in self._group_registry():
            raise ApiError("没有这个分组：%s" % name, 404)
        color = str(color or "").strip()
        if color:
            if not self._is_color(color):
                raise ApiError("颜色要写成 #rrggbb，例如 #f472b6")
            color = color.lower()
        colors = self._group_colors()
        if color:
            colors[name] = color
        else:
            colors.pop(name, None)
        self._save_group_colors(colors)
        return {"ok": True, "group": name, "color": color,
                "note": ("已把「%s」的标签色设为 %s" % (name, color)) if color
                        else "已恢复「%s」的默认标签色" % name}

    def groups(self) -> list[dict]:
        """所有分组（含还没有成员的空分组）；空名字 = 未分组，排最后，没有成员时不返回。"""
        rows = self._q(
            "SELECT s.grp g, COUNT(*) c, SUM(s.enabled) e,"
            " SUM(CASE WHEN st.live_status=1 THEN 1 ELSE 0 END) living"
            " FROM subject s LEFT JOIN subject_state st ON st.uid=s.uid"
            " GROUP BY s.grp")
        counts = {r["g"] or "": {"total": r["c"], "enabled": r["e"] or 0,
                                 "living": r["living"] or 0} for r in rows}
        none = counts.pop("", None)                    # 未分组：没人时整段不返回
        out: list[dict] = []
        for n in self._group_registry():
            out.append({"name": n, **(counts.pop(n, None) or {"total": 0, "enabled": 0, "living": 0})})
        for n in sorted(counts):                       # 库里有、注册表漏了的组
            out.append({"name": n, **counts[n]})
        if none and none["total"]:                     # 未分组固定排在最后
            out.append({"name": "", **none})
        colors = self._group_colors()
        for item in out:
            item["color"] = colors.get(item["name"], "")
        return out

    def set_group_order(self, names) -> dict:
        """调整分组顺序（拖拽排序）。只认数组；'' 与重复项忽略，没给到的组按原顺序排后面。"""
        if not isinstance(names, (list, tuple)):
            raise ApiError("names 要是数组")
        given: list[str] = []
        for n in names:
            g = self._clean_group(n)
            if g and g not in given:
                given.append(g)
        new = given + [n for n in self._group_registry() if n not in given]
        for row in self._q("SELECT DISTINCT grp FROM subject WHERE grp<>''"):
            if row["grp"] not in new:
                new.append(row["grp"])
        self._save_group_registry(new)
        return {"ok": True, "order": new, "note": "分组顺序已保存"}

    def create_group(self, name: str, color: str = "") -> dict:
        name = self._clean_group(name)
        if not name:
            raise ApiError("分组名不能为空")
        color = str(color or "").strip()
        if color and not self._is_color(color):
            raise ApiError("颜色要写成 #rrggbb，例如 #f472b6")
        if name in self._group_registry():
            raise ApiError("分组「%s」已存在" % name)
        self._ensure_group(name)
        if color:
            colors = self._group_colors()
            colors[name] = color.lower()
            self._save_group_colors(colors)
            color = color.lower()
        return {"ok": True, "group": name, "color": color, "created": True,
                "note": "已新建分组「%s」，勾选主播后点「移到该分组」即可加入" % name}

    def batch_subjects(self, uids, action: str, group: str = "") -> dict:
        """批量移除 / 移动分组。一次只做一件事，全部 UID 一起提交。

        2026-11-10 起名单不再支持「停用」：所有主播一律轮询，只剩 remove / group。
        """
        if not isinstance(uids, (list, tuple)) or not uids:
            raise ApiError("uids 不能为空")
        clean: list[int] = []
        for u in uids:
            try:
                clean.append(int(u))
            except (TypeError, ValueError):
                raise ApiError("uids 里有非法 UID：%r" % (u,)) from None
        clean = sorted(set(clean))
        if action not in ("remove", "group"):
            raise ApiError("action 只能是 remove / group")
        ph = ",".join("?" * len(clean))
        exists = [r["uid"] for r in self._q(
            "SELECT uid FROM subject WHERE uid IN (%s)" % ph, tuple(clean))]
        if not exists:
            raise ApiError("这些 UID 都不在名单里", 404)
        where = "uid IN (%s)" % ",".join("?" * len(exists))
        g = ""
        with self._lock:                       # 批量改库 + 回写镜像，锁内一次做完
            snap = self._snapshot_uids(exists)
            if action == "group":
                g = self._clean_group(group)
                self._x("UPDATE subject SET grp=? WHERE %s" % where, (g,) + tuple(exists))
                self._ensure_group(g)
                note = "已把 %d 个号移到「%s」" % (len(exists), g or "未分组")
            elif action == "remove":
                for uid in exists:
                    self._x("DELETE FROM subject WHERE uid=?", (uid,))
                    self._x("DELETE FROM subject_state WHERE uid=?", (uid,))
                    self._x("DELETE FROM live_session WHERE uid=?", (uid,))
                note = "已移除 %d 个号（历史事件与已见动态保留）" % len(exists)
            try:
                self._sync_config_subjects()
            except Exception:
                # 补偿：镜像没写成 → 把这几行按快照还原，宁可不生效也不留半截状态
                self._restore_uids(snap)
                raise
        return {"ok": True, "action": action, "affected": len(exists), "uids": exists,
                "group": g, "note": note}

    def _sync_config_subjects(self) -> None:
        """以库为准整体重建镜像的 subjects 段：uid / name / room_id / group（只含启用行）。

        H7：全程持锁，且是「读库 → 整份覆盖」，不是增量读-改-写，所以并发的
        批量操作不会互相把对方的更新覆盖掉。镜像写盘是 os.replace（原子），
        失败时镜像保持原样，库也没动，两边依然一致，不需要额外补偿。
        """
        with self._lock:
            # 只镜像 enabled=1：停用行（名单外历史遗留）不能写进 config，否则下次启动
            # 会把它当成「监控名单」重新启用，形成「停用→镜像→再启用」的回环。
            rows = self._q("SELECT uid,name,room_id,grp FROM subject WHERE enabled=1 ORDER BY uid")
            cfg = self._read_config_locked()
            old = {int(s.get("uid", 0)): s for s in cfg.get("subjects", []) if s.get("uid")}
            out = []
            for r in rows:
                uid = r["uid"]
                e = dict(old.get(uid) or {"uid": uid, "room_id": r["room_id"] or 0,
                                          "name": r["name"] or ("UID%d" % uid)})
                e["room_id"] = r["room_id"] or 0
                e["name"] = r["name"] or e.get("name") or ("UID%d" % uid)
                g = r["grp"] or ""
                if g:
                    e["group"] = g
                else:
                    e.pop("group", None)
                e.pop("_disabled", None)             # 2026-11-10 起不再有停用，镜像不写标记
                if r["room_id"]:
                    e.pop("_no_room", None)
                else:
                    e["_no_room"] = True
                out.append(e)
            cfg["subjects"] = out
            self._write_config_locked(cfg)

    def rename_group(self, old: str, new: str) -> dict:
        old = self._clean_group(old)
        if not old:
            raise ApiError("不能重命名「未分组」")
        n = self._q("SELECT COUNT(*) c FROM subject WHERE grp=?", (old,))[0]["c"]
        if not n and old not in self._group_registry():
            raise ApiError("没有这个分组：%s" % old, 404)
        new = self._clean_group(new)
        if new == old:
            return {"ok": True, "from": old, "to": new, "affected": 0, "note": "分组名没变"}
        with self._lock:
            affected = [r["uid"] for r in self._q("SELECT uid FROM subject WHERE grp=?", (old,))]
            snap = self._snapshot_uids(affected)
            colors_before = self._group_colors()
            had_new = (new in self._group_registry()) if new else True
            try:
                dup = self._q("SELECT COUNT(*) c FROM subject WHERE grp=?", (new,))[0]["c"] if new else 0
                self._x("UPDATE subject SET grp=? WHERE grp=?", (new, old))
                if new:
                    self._ensure_group(new)
                self._drop_group(old)
                colors = self._group_colors()
                if old in colors:
                    old_color = colors.pop(old)
                    if new and new not in colors:
                        colors[new] = old_color    # 合并到已有分组时，目标分组自己的颜色优先
                    self._save_group_colors(colors)
                self._sync_config_subjects()
            except Exception:
                # 补偿：镜像没写成 → grp 行、分组登记、颜色全部还原后再抛
                self._restore_uids(snap)
                if new and not had_new:
                    self._drop_group(new)
                self._ensure_group(old)
                self._save_group_colors(colors_before)
                raise
        if not new:
            note = ("已取消分组，%d 个号回到未分组" % n) if n else "已删除空分组「%s」" % old
        elif dup:
            note = "已并入已有分组「%s」，共 %d 个号" % (new, n + dup)
        else:
            note = "已重命名为「%s」，%d 个号" % (new, n)
        return {"ok": True, "from": old, "to": new, "affected": n, "note": note}

    def delete_group(self, name: str) -> dict:
        name = self._clean_group(name)
        if not name:
            raise ApiError("不能删除「未分组」")
        with self._lock:
            n = self._q("SELECT COUNT(*) c FROM subject WHERE grp=?", (name,))[0]["c"]
            if not n and name not in self._group_registry():
                raise ApiError("没有这个分组：%s" % name, 404)
            affected = [r["uid"] for r in self._q("SELECT uid FROM subject WHERE grp=?", (name,))]
            snap = self._snapshot_uids(affected)
            colors_before = self._group_colors()
            try:
                self._x("UPDATE subject SET grp='' WHERE grp=?", (name,))
                self._drop_group(name)
                self._drop_color(name)
                self._sync_config_subjects()
            except Exception:
                # 补偿：镜像没写成 → grp 行、分组登记、颜色还原后再抛
                self._restore_uids(snap)
                self._ensure_group(name)
                self._save_group_colors(colors_before)
                raise
        note = ("已删除分组，%d 个号回到未分组（号本身没删）" % n) if n else "已删除空分组「%s」" % name
        return {"ok": True, "group": name, "affected": n, "note": note}

    MODES = [("full", "全开", "采集 + 推送，正常工作"),
             ("collect", "只采集", "继续采集入库，但不发 QQ 消息"),
             ("off", "全停", "不采集也不推送，进程仍活着（守护不会重启它）")]

    def _meta_get(self, key: str, default: str | None = None):
        rows = self._q("SELECT v FROM meta WHERE k=?", (key,))
        return rows[0]["v"] if rows else default

    def _meta_set(self, key: str, value: str) -> None:
        self._x("INSERT INTO meta(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

    def global_state(self) -> dict:
        mode = self._meta_get("global_mode", "full") or "full"
        labels = {k: v for k, v in ((m[0], m[1]) for m in self.MODES)}
        pending = self._q("SELECT COUNT(*) c FROM delivery WHERE status='pending'")[0]["c"]
        return {"ok": True, "mode": mode, "label": labels.get(mode, mode),
                "modes": [{"value": m[0], "label": m[1], "hint": m[2]} for m in self.MODES],
                "pending": pending}

    def set_global_mode(self, mode: str) -> dict:
        valid = {m[0] for m in self.MODES}
        if mode not in valid:
            raise ApiError("mode 只能是 %s 之一" % "/".join(sorted(valid)))
        self._meta_set("global_mode", mode)
        st = self.global_state()
        st["changed"] = True
        return st

    def flush_pending(self) -> dict:
        """丢弃积压的待发投递（总开关从暂停恢复时，避免一次补发一堆旧消息）。"""
        n = self._x("UPDATE delivery SET status='failed', last_error='手动丢弃（积压待发）', "
                    "next_at=0 WHERE status='pending'")
        return {"ok": True, "dropped": n}

