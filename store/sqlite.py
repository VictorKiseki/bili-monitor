"""生产存储：SQLiteStore（契约 Store 实现，WAL，单文件）

与 MemoryStore 语义完全一致：insert_event 幂等、enqueue 唯一、finish 排期重试。
额外提供主循环要用的三块：
  · seen_dynamic  已见过的动态 id（防置顶/重复）
  · live_session  场次统计（开播时间 / 最高人气），下播事件用
  · all_states    一轮取全部水位，省逐个查询
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from shared.contracts import (
    ChannelType, Delivery, Event, EventType, SendStatus, Subject, SubjectState, Target, TargetKind,
)

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS subject(
  uid INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', room_id INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1, grp TEXT NOT NULL DEFAULT '',
  push_live INTEGER NOT NULL DEFAULT 1, push_dynamic INTEGER NOT NULL DEFAULT 1,
  weekly_on INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS subject_state(
  uid INTEGER PRIMARY KEY REFERENCES subject(uid) ON DELETE CASCADE,
  room_id INTEGER, live_status INTEGER NOT NULL DEFAULT 0,
  last_live_start_ts INTEGER, last_dynamic_id TEXT, last_poll_ts INTEGER
);
CREATE TABLE IF NOT EXISTS target(
  target_id INTEGER PRIMARY KEY, channel_type TEXT NOT NULL, kind TEXT NOT NULL,
  external_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS event(
  -- 刻意不给 subject_uid 加外键：移除主播时历史事件要保留（remove_subject 的既有承诺）
  event_id TEXT PRIMARY KEY, subject_uid INTEGER NOT NULL, type TEXT NOT NULL,
  occurred_at INTEGER NOT NULL, dedup_key TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '', cover_url TEXT, summary TEXT, extra TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
-- M5：按主播查事件（管理台 / 清理）走索引
CREATE INDEX IF NOT EXISTS idx_event_subject ON event(subject_uid);
CREATE TABLE IF NOT EXISTS delivery(
  delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL REFERENCES event(event_id) ON DELETE CASCADE,
  target_id INTEGER NOT NULL REFERENCES target(target_id) ON DELETE CASCADE,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_at REAL NOT NULL DEFAULT 0, claimed_at REAL,
  status TEXT NOT NULL DEFAULT 'pending',    -- pending / sending / sent / failed
  last_error TEXT,
  raw TEXT,                                  -- M11：渲染失败的输入原文（event+target JSON），死信复盘用
  UNIQUE(event_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_delivery_due ON delivery(status, next_at);
-- M5：死信排查的主路径是「按事件找投递」，没有这条索引就是全表扫
CREATE INDEX IF NOT EXISTS idx_delivery_event ON delivery(event_id);
CREATE TABLE IF NOT EXISTS seen_dynamic(
  -- 同样不加外键：已见动态是去重水位，移除后重新加入不能补推历史
  uid INTEGER NOT NULL, id_str TEXT NOT NULL, pub_ts INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(uid, id_str)
);
-- H1：seen_ids(uid, since_ts) 的时间窗查询走这条索引
CREATE INDEX IF NOT EXISTS idx_seen_uid_pub ON seen_dynamic(uid, pub_ts);
CREATE TABLE IF NOT EXISTS live_session(
  uid INTEGER PRIMARY KEY REFERENCES subject(uid) ON DELETE CASCADE,
  start_ts INTEGER NOT NULL DEFAULT 0,
  online_max INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

-- 周表（2026-11-03）：候选队列 + 结构化结果。
-- 只做「识别 + 记录」，不参与投递；candidate 记录处理状态，schedule 按 (uid, week_start) 去重。
CREATE TABLE IF NOT EXISTS weekly_candidate(
  event_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, uname TEXT NOT NULL DEFAULT '',
  dyn_id TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
  pub_ts INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL DEFAULT '',
  images_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending',      -- pending / done / skipped / error
  note TEXT, decided_at REAL,
  human_scope TEXT,                            -- 人工定性质：week / single / none
  human_at REAL
);
CREATE INDEX IF NOT EXISTS idx_weekly_cand ON weekly_candidate(status, pub_ts);
CREATE TABLE IF NOT EXISTS weekly_schedule(
  uid INTEGER NOT NULL, week_start TEXT NOT NULL, uname TEXT NOT NULL DEFAULT '',
  week_label TEXT, week_inferred INTEGER NOT NULL DEFAULT 0,
  event_id TEXT, dyn_id TEXT, url TEXT, image_url TEXT, posted_at INTEGER,
  days_json TEXT NOT NULL DEFAULT '[]', confidence REAL,
  extractor TEXT NOT NULL DEFAULT 'vlm', needs_review INTEGER NOT NULL DEFAULT 0,
  note TEXT, created_at REAL, updated_at REAL,
  ocr_signal TEXT NOT NULL DEFAULT '',           -- agree/partial/diff/na（2026-11-06）
  ocr_text TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(uid, week_start)
);
CREATE INDEX IF NOT EXISTS idx_weekly_week ON weekly_schedule(week_start);
-- 主播昵称（2026-11-04）：联动识别用，自动拆名覆盖不到「鸽 / 狍」这类外号，人工补
CREATE TABLE IF NOT EXISTS streamer_alias(
  uid INTEGER NOT NULL, alias TEXT NOT NULL, note TEXT, created_at REAL,
  PRIMARY KEY(uid, alias)
);

-- 联动对象查证（2026-11-04）：日程文案点到、但不在监控名单里的名字，
-- 到 B 站按昵称搜一次，把 uid/粉丝数记下来，供日程表上外链展示。
CREATE TABLE IF NOT EXISTS streamer_search(
  name TEXT PRIMARY KEY, uid INTEGER, uname TEXT, fans INTEGER,
  face TEXT, sign TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending/doing/done/none/error
  note TEXT, updated_at REAL
);

-- 单场预告（2026-11-03）：周表之外的补丁。
-- 周表要求「多天多日程」；「今晚 8 点歌杂」这类单场只进这张表，
-- 按 date 挂到对应那天，kind 决定它是新增、改档还是取消。
CREATE TABLE IF NOT EXISTS weekly_session(
  session_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, uname TEXT NOT NULL DEFAULT '',
  date TEXT NOT NULL, week_start TEXT NOT NULL,
  time TEXT, time_text TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'supplement',     -- supplement / override / cancel
  cancel_kind TEXT NOT NULL DEFAULT '',         -- reserve=预约被取消（2026-11-06）
  replaces TEXT,                                -- 被改/被取消的基础条目 JSON
  event_id TEXT, url TEXT, image_path TEXT,
  dyn_text TEXT NOT NULL DEFAULT '',            -- 同条动态的正文，供「卡片+正文」合并留痕
  confidence REAL, needs_review INTEGER NOT NULL DEFAULT 0,
  note TEXT, created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_weekly_session_week ON weekly_session(week_start, uid);

-- 人工修改（2026-11-03）：管理台上手动加/改/删，叠加在自动识别结果之上。
-- 单独存表而不是改 days_json，是为了下次识别覆盖整周时人工修改不丢。
CREATE TABLE IF NOT EXISTS weekly_manual(
  manual_id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid INTEGER NOT NULL, uname TEXT NOT NULL DEFAULT '',
  week_start TEXT NOT NULL, day TEXT NOT NULL,
  op TEXT NOT NULL DEFAULT 'add',            -- add / modify / remove
  time TEXT, time_text TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',
  target_time TEXT, target_text TEXT,        -- modify/remove 指向的原条目
  collab_json TEXT NOT NULL DEFAULT '[]',    -- 人工联动：一起上表的其他 uid（JSON 数组）
  note TEXT, created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_weekly_manual_week ON weekly_manual(week_start, uid);

-- 周表变更流水（2026-11-03）：每次识别到日程类动态就记一笔，管理台上看「这周改了什么」。
CREATE TABLE IF NOT EXISTS weekly_change(
  change_id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid INTEGER NOT NULL, week_start TEXT NOT NULL, event_id TEXT,
  scope TEXT NOT NULL DEFAULT 'week',           -- week / single
  added INTEGER NOT NULL DEFAULT 0, changed INTEGER NOT NULL DEFAULT 0,
  removed INTEGER NOT NULL DEFAULT 0,
  diff_json TEXT NOT NULL DEFAULT '[]', created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_weekly_change_week ON weekly_change(week_start, created_at);

-- 常驻联动节目（2026-11-05）：固定主持的固定节目（屋里吃饭 / Potluck 等）。
-- 动态里常常只写嘉宾、不写常驻主持；周视图按节目名/别名认出这一场，
-- 把常驻主持补进同一条，日程表才不会漏人。主持人 uid 存 JSON 数组。
CREATE TABLE IF NOT EXISTS weekly_program(
  program_id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  hosts_json TEXT NOT NULL DEFAULT '[]',
  weekday TEXT NOT NULL DEFAULT '',        -- 固定星期 MON..SUN（可空）
  time TEXT NOT NULL DEFAULT '',           -- 固定时间 HH:MM（可空）
  note TEXT, enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL, updated_at REAL
);

-- 周表 OCR 第二意见（2026-11-06）：Windows.Media.Ocr 的原始结果，按 event_id+图序缓存。
-- 用途只有两个：给视觉模型提供「另一边读到了什么」；与视觉结果对不上时标待复核。
-- 实测边界：印刷体裁切+2x 能读，连笔手写基本读不出（见 docs/21）。
CREATE TABLE IF NOT EXISTS weekly_ocr(
  event_id TEXT NOT NULL, image_index INTEGER NOT NULL DEFAULT 0,
  image_path TEXT, lang TEXT,
  lines_json TEXT NOT NULL DEFAULT '[]',
  text TEXT NOT NULL DEFAULT '',
  days_json TEXT NOT NULL DEFAULT '{}',
  ms INTEGER, created_at REAL,
  PRIMARY KEY (event_id, image_index)
);
"""


# ---------- schema 版本化（M6） ----------
# CREATE TABLE IF NOT EXISTS 只保证「表在」，已存在的表不会补列，所以列变更必须走迁移。
# 约定：只追加迁移、不改历史迁移；版本号存 meta('schema_version')。
# 库版本高于代码版本时拒绝启动 —— 用旧代码写新库比「起不来」危险得多。
SCHEMA_VERSION = 5

# M11：死信列表里 raw 的预览上限（字符）。库里原文完整保留，只在查询侧截断，
# 避免一条超大原文把接口/日志撑爆；has_raw 与 raw 一起返回，前端可判断有无原文。
RAW_PREVIEW_CHARS = 4000


class SchemaTooNewError(RuntimeError):
    """库里的 schema_version 比代码新：拒绝启动，避免旧代码把新库写坏。"""


def _migrate_001_columns(conn: sqlite3.Connection) -> None:
    """v1：把历史上「逐列补列」的零散逻辑收成第一条迁移（行为不变）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(weekly_candidate)")}
    if "human_scope" not in cols:
        conn.execute("ALTER TABLE weekly_candidate ADD COLUMN human_scope TEXT")
    if "human_at" not in cols:
        conn.execute("ALTER TABLE weekly_candidate ADD COLUMN human_at REAL")
    scols = {r[1] for r in conn.execute("PRAGMA table_info(subject)")}
    if "grp" not in scols:                       # 2026-11-06 自定义分组
        conn.execute("ALTER TABLE subject ADD COLUMN grp TEXT NOT NULL DEFAULT ''")
    if "push_live" not in scols:                 # 2026-11-07 分号推送开关
        conn.execute("ALTER TABLE subject ADD COLUMN push_live INTEGER NOT NULL DEFAULT 1")
    if "push_dynamic" not in scols:
        conn.execute("ALTER TABLE subject ADD COLUMN push_dynamic INTEGER NOT NULL DEFAULT 1")
    if "weekly_on" not in scols:                 # 2026-11-07 分号日程表（周表）监控开关
        conn.execute("ALTER TABLE subject ADD COLUMN weekly_on INTEGER NOT NULL DEFAULT 1")
    wcols = {r[1] for r in conn.execute("PRAGMA table_info(weekly_schedule)")}
    if "ocr_signal" not in wcols:                # 2026-11-06 OCR 第二意见
        conn.execute("ALTER TABLE weekly_schedule ADD COLUMN ocr_signal TEXT NOT NULL DEFAULT ''")
    if "ocr_text" not in wcols:
        conn.execute("ALTER TABLE weekly_schedule ADD COLUMN ocr_text TEXT NOT NULL DEFAULT ''")
    secols = {r[1] for r in conn.execute("PRAGMA table_info(weekly_session)")}
    if "cancel_kind" not in secols:              # 2026-11-06 预约取消标记
        conn.execute("ALTER TABLE weekly_session ADD COLUMN cancel_kind TEXT NOT NULL DEFAULT ''")
    if "dyn_text" not in secols:                 # 2026-11-06 动态正文留痕
        conn.execute("ALTER TABLE weekly_session ADD COLUMN dyn_text TEXT NOT NULL DEFAULT ''")
    mcols = {r[1] for r in conn.execute("PRAGMA table_info(weekly_manual)")}
    if "collab_json" not in mcols:
        conn.execute("ALTER TABLE weekly_manual ADD COLUMN collab_json TEXT NOT NULL DEFAULT '[]'")


def _migrate_002_claim_and_indexes(conn: sqlite3.Connection) -> None:
    """v2：两阶段领取（claimed_at）、去重时间窗（pub_ts）、M5 索引。

    · delivery.claimed_at：H2 僵尸回收用；老库补列后历史行 claimed_at=NULL，
      它们本来就是 pending，reclaim 只看 status='sending'，不会误伤。
    · seen_dynamic.pub_ts：H1 时间窗。老库里 pub_ts=0 的行无法从 id 反推发布时间，
      用「升级时刻」兜底（一次性），否则第一次 prune 会把它们全部删掉。
    · 索引：event(subject_uid) / delivery(event_id) / seen_dynamic(uid, pub_ts)。
    """
    dcols = {r[1] for r in conn.execute("PRAGMA table_info(delivery)")}
    if "claimed_at" not in dcols:
        conn.execute("ALTER TABLE delivery ADD COLUMN claimed_at REAL")
    scol = {r[1] for r in conn.execute("PRAGMA table_info(seen_dynamic)")}
    if "pub_ts" not in scol:                     # 更老的库连列都没有
        conn.execute("ALTER TABLE seen_dynamic ADD COLUMN pub_ts INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE seen_dynamic SET pub_ts=? "
                 "WHERE pub_ts IS NULL OR pub_ts<=0", (int(time.time()),))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_subject ON event(subject_uid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_event ON delivery(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_due ON delivery(status, next_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_uid_pub ON seen_dynamic(uid, pub_ts)")


def _migrate_003_delivery_raw(conn: sqlite3.Connection) -> None:
    """v3：delivery.raw —— 渲染失败时的输入原文，死信可复盘（M11）。

    老库补列后历史行 raw=NULL，查询侧 has_raw=False，既有死信列表语义不变。
    """
    dcols = {r[1] for r in conn.execute("PRAGMA table_info(delivery)")}
    if "raw" not in dcols:
        conn.execute("ALTER TABLE delivery ADD COLUMN raw TEXT")


def _migrate_004_foreign_keys(conn: sqlite3.Connection) -> None:
    """v4：M5 物理外键 —— 重建 3 张表，先清孤儿行，再挂 REFERENCES。

    · delivery.event_id   -> event(event_id)   ON DELETE CASCADE
    · delivery.target_id  -> target(target_id) ON DELETE CASCADE
    · subject_state.uid / live_session.uid -> subject(uid) ON DELETE CASCADE

    刻意不建的两条：
    · event.subject_uid -> subject(uid)：remove_subject 明确承诺「历史事件保留」，
      加外键要么阻止移除主播、要么级联删历史，两种都不可接受；
    · seen_dynamic.uid -> subject(uid)：已见动态是去重水位，移除后重新加入
      不能补推历史，必须一起保留。
    这两张表允许 subject_uid 指向已移除的主播，是有意的历史归档语义，不是孤儿。

    老库可能已有真孤儿（投递引用了不存在的事件/目标、状态引用了不存在的主播），
    外键会拒绝迁移，所以先按「引用不存在」删一遍并记日志（用户已批准删孤儿）。
    SQLite 补外键必须整表重建：建新表 → 全量拷贝 → 删旧表 → 改名 → 重建索引。
    整条迁移在事务内，失败整体回滚并保留原表（重建过程对旧表只读）。
    """
    counts = {
        "delivery": conn.execute(
            "DELETE FROM delivery WHERE event_id NOT IN (SELECT event_id FROM event) "
            "OR target_id NOT IN (SELECT target_id FROM target)").rowcount,
        "subject_state": conn.execute(
            "DELETE FROM subject_state WHERE uid NOT IN (SELECT uid FROM subject)").rowcount,
        "live_session": conn.execute(
            "DELETE FROM live_session WHERE uid NOT IN (SELECT uid FROM subject)").rowcount,
    }
    if any(counts.values()):
        log.warning("M5 schema v4：外键重建前删除孤儿行 %s", counts)

    # 1) delivery（子表，引用 event / target）
    conn.execute(
        "CREATE TABLE delivery_v4("
        " delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " event_id TEXT NOT NULL REFERENCES event(event_id) ON DELETE CASCADE,"
        " target_id INTEGER NOT NULL REFERENCES target(target_id) ON DELETE CASCADE,"
        " attempts INTEGER NOT NULL DEFAULT 0,"
        " next_at REAL NOT NULL DEFAULT 0,"
        " claimed_at REAL,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " last_error TEXT,"
        " raw TEXT,"
        " UNIQUE(event_id, target_id))")
    conn.execute(
        "INSERT INTO delivery_v4(delivery_id,event_id,target_id,attempts,next_at,claimed_at,"
        "status,last_error,raw)"
        " SELECT delivery_id,event_id,target_id,attempts,next_at,claimed_at,status,last_error,raw"
        " FROM delivery")
    conn.execute("DROP TABLE delivery")
    conn.execute("ALTER TABLE delivery_v4 RENAME TO delivery")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_due ON delivery(status, next_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_event ON delivery(event_id)")

    # 2) subject_state / live_session（以 subject.uid 为父；事件与已见动态不动）
    conn.execute(
        "CREATE TABLE subject_state_v4("
        " uid INTEGER PRIMARY KEY REFERENCES subject(uid) ON DELETE CASCADE,"
        " room_id INTEGER,"
        " live_status INTEGER NOT NULL DEFAULT 0,"
        " last_live_start_ts INTEGER,"
        " last_dynamic_id TEXT,"
        " last_poll_ts INTEGER)")
    conn.execute(
        "INSERT INTO subject_state_v4 SELECT uid,room_id,live_status,last_live_start_ts,"
        "last_dynamic_id,last_poll_ts FROM subject_state")
    conn.execute("DROP TABLE subject_state")
    conn.execute("ALTER TABLE subject_state_v4 RENAME TO subject_state")

    conn.execute(
        "CREATE TABLE live_session_v4("
        " uid INTEGER PRIMARY KEY REFERENCES subject(uid) ON DELETE CASCADE,"
        " start_ts INTEGER NOT NULL DEFAULT 0,"
        " online_max INTEGER NOT NULL DEFAULT 0,"
        " title TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO live_session_v4 SELECT uid,start_ts,online_max,title FROM live_session")
    conn.execute("DROP TABLE live_session")
    conn.execute("ALTER TABLE live_session_v4 RENAME TO live_session")

    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise RuntimeError(f"M5 schema v4：外键校验失败，仍有 {len(bad)} 处引用不成立：{bad[:5]}")


def _migrate_005_enable_all(conn: sqlite3.Connection) -> None:
    """v5：管理台取消「启用 / 停用」开关，历史上被停用的号全部恢复轮询（2026-11-10）。

    只把 enabled 抬回 1，不删数据：水位、推送开关、直播会话都留着。
    watcher._disable_unlisted 的「名单外号自动停用」对账仍然有效，下次启动按名单重算。
    """
    conn.execute("UPDATE subject SET enabled=1 WHERE enabled<>1")


# 顺序迁移函数表：(版本号, 说明, 迁移函数)，版本号必须从 1 连续递增
MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "旧库补列", _migrate_001_columns),
    (2, "两阶段领取 + 去重时间窗 + 索引", _migrate_002_claim_and_indexes),
    (3, "渲染失败原文 raw 列", _migrate_003_delivery_raw),
    (4, "M5 物理外键（重建 delivery/subject_state/live_session，先清孤儿行）", _migrate_004_foreign_keys),
    (5, "取消停用开关：名单里所有主播恢复轮询", _migrate_005_enable_all),
)


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    if not row or row[0] in (None, ""):
        return 0
    return int(row[0])


def migrate(conn: sqlite3.Connection, *, path: str = "") -> None:
    """按 meta('schema_version') 顺序执行迁移；库比代码新就拒绝启动。"""
    current = _schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"数据库 schema_version={current} 高于本代码支持的 {SCHEMA_VERSION}"
            f"（{path or 'db'}）：请升级程序，或先备份并换回旧库文件；"
            "用旧代码写新库可能造成数据错乱，已拒绝启动。")
    for version, name, fn in MIGRATIONS:
        if version <= current:
            continue
        # 每条迁移单独一个事务：中途失败就整体回滚，版本号只在成功后推进，
        # 下次启动会从同一个版本重跑（迁移函数本身写成幂等的）。
        conn.execute("BEGIN IMMEDIATE")
        try:
            fn(conn)
            conn.execute("INSERT INTO meta(k,v) VALUES('schema_version',?) "
                         "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(version),))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        current = version


def _row_to_event(r: sqlite3.Row) -> Event:
    return Event(
        event_id=r["event_id"], subject_uid=r["subject_uid"], type=EventType(r["type"]),
        occurred_at=r["occurred_at"], dedup_key=r["dedup_key"], title=r["title"],
        url=r["url"], cover_url=r["cover_url"], summary=r["summary"],
        extra=json.loads(r["extra"] or "{}"),
    )


class SQLiteStore:
    def __init__(self, path: str | Path = "data/bot.db", retry_delays=(2.0, 8.0, 30.0),
                 max_attempts: int = 3, now=time.time):
        self.path = str(path)
        # H3：重试策略的唯一来源是 Dispatcher（它把 next_at / attempts 算好交给 finish）。
        # 这两个参数只为兼容老调用方保留：脚本里直接 finish(RETRY) 不带计划时才兜底，
        # 生产路径永远不会走到 Store 自己判定重试。
        self.retry_delays = tuple(retry_delays)
        self.max_attempts = max_attempts
        self._now = now
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        # M6：构造时就建连接并跑迁移。库版本高于代码版本时立刻抛 SchemaTooNewError，
        # 启动即得到清晰错误，而不是等到第一次轮询才炸；_db() 的懒加载保留给重连兜底。
        self._connect()

    # ---------- 连接 ----------
    def _connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        # M5：外键开关必须在迁移之后打开 —— v4 重建表（DROP + RENAME）要求
        # PRAGMA foreign_keys=OFF，而该 PRAGMA 在事务内不生效，所以迁移期间保持默认关闭，
        # 全部迁移跑完再打开。之后每条连接（本 Store、管理台）各设一次。
        conn.executescript(SCHEMA)
        migrate(conn, path=self.path)
        conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._connect()
        return self._conn

    async def open(self) -> "SQLiteStore":
        await asyncio.to_thread(self._db)
        return self

    def _run(self, fn, *args):
        def job():
            # 管理台/其他进程偶尔开写事务，WAL 下最多等 busy_timeout；
            # 真撞上「database is locked」不应该让整个进程崩，重试几次再抛。
            delays = (0.2, 0.5, 1.5)
            for i in range(len(delays) + 1):
                try:
                    with self._lock:
                        return fn(self._db(), *args)
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if i >= len(delays) or ("locked" not in msg and "busy" not in msg):
                        raise
                    time.sleep(delays[i])
        return asyncio.to_thread(job)

    # ---------- subject ----------
    def _list_subjects(self, conn, enabled_only: bool) -> list[Subject]:
        sql = "SELECT uid,name,room_id,enabled,push_live,push_dynamic,weekly_on FROM subject"
        if enabled_only:
            sql += " WHERE enabled=1"
        return [Subject(uid=r["uid"], name=r["name"], room_id=r["room_id"], enabled=bool(r["enabled"]),
                        push_live=bool(r["push_live"]), push_dynamic=bool(r["push_dynamic"]),
                        weekly_on=bool(r["weekly_on"]))
                for r in conn.execute(sql + " ORDER BY uid")]

    async def list_subjects(self, enabled_only: bool = True) -> list[Subject]:
        return await self._run(self._list_subjects, enabled_only)

    def _upsert_subject(self, conn, s: Subject) -> None:
        # 推送/日程开关不跟 config 覆盖：只有管理台能改（INSERT 给默认 1，冲突时不动）
        conn.execute(
            "INSERT INTO subject(uid,name,room_id,enabled,push_live,push_dynamic,weekly_on) VALUES(?,?,?,?,1,1,1) "
            "ON CONFLICT(uid) DO UPDATE SET name=excluded.name, room_id=excluded.room_id",
            (s.uid, s.name, s.room_id, 1 if s.enabled else 0))

    async def upsert_subject(self, s: Subject) -> None:
        await self._run(self._upsert_subject, s)

    def _set_subject_enabled(self, conn, uid: int, enabled: bool) -> bool:
        cur = conn.execute("UPDATE subject SET enabled=? WHERE uid=?", (1 if enabled else 0, uid))
        return cur.rowcount > 0

    async def set_subject_enabled(self, uid: int, enabled: bool) -> bool:
        """停用/启用已有 subject；False 表示库里没有这个 uid。"""
        return await self._run(self._set_subject_enabled, uid, enabled)

    def _get_subject(self, conn, uid: int) -> Subject | None:
        r = conn.execute(
            "SELECT uid,name,room_id,enabled,push_live,push_dynamic,weekly_on FROM subject WHERE uid=?", (uid,)).fetchone()
        return (Subject(uid=r["uid"], name=r["name"], room_id=r["room_id"], enabled=bool(r["enabled"]),
                        push_live=bool(r["push_live"]), push_dynamic=bool(r["push_dynamic"]),
                        weekly_on=bool(r["weekly_on"]))
                if r else None)

    async def get_subject(self, uid: int) -> Subject | None:
        return await self._run(self._get_subject, uid)

    def _save_state(self, conn, st: SubjectState) -> None:
        conn.execute(
            "INSERT INTO subject_state(uid,room_id,live_status,last_live_start_ts,last_dynamic_id,last_poll_ts) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET room_id=excluded.room_id, "
            "live_status=excluded.live_status, last_live_start_ts=excluded.last_live_start_ts, "
            "last_dynamic_id=excluded.last_dynamic_id, last_poll_ts=excluded.last_poll_ts",
            (st.uid, st.room_id, st.live_status, st.last_live_start_ts, st.last_dynamic_id, st.last_poll_ts))

    async def save_subject_state(self, state: SubjectState) -> None:
        await self._run(self._save_state, state)

    def _get_state(self, conn, uid: int) -> SubjectState | None:
        r = conn.execute(
            "SELECT uid,room_id,live_status,last_live_start_ts,last_dynamic_id,last_poll_ts "
            "FROM subject_state WHERE uid=?", (uid,)).fetchone()
        if not r:
            return None
        return SubjectState(uid=r["uid"], room_id=r["room_id"], live_status=r["live_status"],
                            last_live_start_ts=r["last_live_start_ts"],
                            last_dynamic_id=r["last_dynamic_id"], last_poll_ts=r["last_poll_ts"])

    async def get_subject_state(self, uid: int) -> SubjectState | None:
        return await self._run(self._get_state, uid)

    def _all_states(self, conn) -> dict[int, SubjectState]:
        out = {}
        for r in conn.execute("SELECT uid,room_id,live_status,last_live_start_ts,last_dynamic_id,last_poll_ts FROM subject_state"):
            out[r["uid"]] = SubjectState(uid=r["uid"], room_id=r["room_id"], live_status=r["live_status"],
                                         last_live_start_ts=r["last_live_start_ts"],
                                         last_dynamic_id=r["last_dynamic_id"], last_poll_ts=r["last_poll_ts"])
        return out

    async def all_states(self) -> dict[int, SubjectState]:
        return await self._run(self._all_states)

    def _commit_poll(self, conn, states, events, push, seen, live_sessions) -> int:
        conn.execute("BEGIN IMMEDIATE")
        try:
            targets = self._enabled_target_ids(conn) if push else []
            queued = 0
            for event in events:
                if self._insert_event(conn, event) and targets:
                    self._enqueue(conn, event.event_id, targets)
                    queued += 1
            for state in states:
                current = self._get_state(conn, state.uid)
                if current and live_sessions is None:
                    state = replace(state, live_status=current.live_status,
                                    last_live_start_ts=current.last_live_start_ts)
                elif current:
                    state = replace(state, last_dynamic_id=current.last_dynamic_id,
                                    last_poll_ts=current.last_poll_ts)
                self._save_state(conn, state)
            for uid, session in (live_sessions or {}).items():
                if session is None:
                    self._clear_session(conn, uid)
                else:
                    self._upsert_session(conn, uid, *session)
            for uid, items in seen.items():
                self._mark_seen(conn, uid, items)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return queued

    async def commit_poll(self, states, events, *, push, seen, live_sessions=None) -> int:
        """原子保存采集状态、事件、投递和对应的已见标记。"""
        return await self._run(self._commit_poll, states, events, push, seen, live_sessions)

    # ---------- event ----------
    def _insert_event(self, conn, ev: Event) -> bool:
        try:
            conn.execute(
                "INSERT INTO event(event_id,subject_uid,type,occurred_at,dedup_key,title,url,"
                "cover_url,summary,extra,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ev.event_id, ev.subject_uid, ev.type.value, ev.occurred_at, ev.dedup_key,
                 ev.title, ev.url, ev.cover_url, ev.summary, json.dumps(ev.extra, ensure_ascii=False),
                 self._now()))
            return True
        except sqlite3.IntegrityError as e:
            # M7：只有 UNIQUE/主键冲突才算「重复事件」。
            # NOT NULL / CHECK 失败是代码 bug，以前一并吞成 False，坏数据看起来像幂等命中。
            if "unique" in str(e).lower():
                return False
            raise

    async def insert_event(self, ev: Event) -> bool:
        return await self._run(self._insert_event, ev)

    def _get_event(self, conn, event_id: str) -> Event | None:
        r = conn.execute("SELECT * FROM event WHERE event_id=?", (event_id,)).fetchone()
        return _row_to_event(r) if r else None

    async def get_event(self, event_id: str) -> Event | None:
        return await self._run(self._get_event, event_id)

    def _get_events(self, conn, event_ids: list[str]) -> dict[str, Event]:
        """M4：一次 WHERE IN 批量取事件，替代逐条 get_event 的 N+1。"""
        if not event_ids:
            return {}
        # 占位符按参数个数动态拼（L3：显式列表拼，不再用 ','.join('?' * n) 这种隐晦写法）
        marks = ",".join(["?"] * len(event_ids))
        rows = conn.execute(f"SELECT * FROM event WHERE event_id IN ({marks})", tuple(event_ids))
        return {r["event_id"]: _row_to_event(r) for r in rows}

    async def get_events(self, event_ids: list[str]) -> dict[str, Event]:
        return await self._run(self._get_events, event_ids)

    # ---------- target ----------
    def _add_target(self, conn, t: Target) -> None:
        conn.execute(
            "INSERT INTO target(target_id,channel_type,kind,external_id,display_name,enabled) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(target_id) DO UPDATE SET external_id=excluded.external_id, "
            "display_name=excluded.display_name, enabled=excluded.enabled",
            (t.target_id, t.channel_type.value, t.kind.value, t.external_id, t.display_name,
             1 if t.enabled else 0))

    async def add_target(self, t: Target) -> None:
        await self._run(self._add_target, t)

    def _get_target(self, conn, target_id: int) -> Target | None:
        r = conn.execute("SELECT * FROM target WHERE target_id=?", (target_id,)).fetchone()
        if not r:
            return None
        return Target(target_id=r["target_id"], channel_type=ChannelType(r["channel_type"]),
                      kind=TargetKind(r["kind"]), external_id=r["external_id"],
                      display_name=r["display_name"], enabled=bool(r["enabled"]))

    async def get_target(self, target_id: int) -> Target | None:
        return await self._run(self._get_target, target_id)

    def _get_targets(self, conn, target_ids: list[int]) -> dict[int, Target]:
        """M4：一次 WHERE IN 批量取投递目标。"""
        if not target_ids:
            return {}
        marks = ",".join(["?"] * len(target_ids))
        out: dict[int, Target] = {}
        for r in conn.execute(f"SELECT * FROM target WHERE target_id IN ({marks})", tuple(target_ids)):
            out[r["target_id"]] = Target(
                target_id=r["target_id"], channel_type=ChannelType(r["channel_type"]),
                kind=TargetKind(r["kind"]), external_id=r["external_id"],
                display_name=r["display_name"], enabled=bool(r["enabled"]))
        return out

    async def get_targets(self, target_ids: list[int]) -> dict[int, Target]:
        return await self._run(self._get_targets, target_ids)

    def _enabled_target_ids(self, conn) -> list[int]:
        return [r["target_id"] for r in conn.execute("SELECT target_id FROM target WHERE enabled=1")]

    async def enabled_target_ids(self) -> list[int]:
        return await self._run(self._enabled_target_ids)

    def _register_qq_target(self, conn, openid: str, app_id: str) -> Target:
        conn.execute("BEGIN IMMEDIATE")
        try:
            binding = self._get_meta(conn, "qq_delivery_binding", None)
            previous_app, previous_id = json.loads(binding) if binding is not None else ("", 1)
            previous = self._get_target(conn, previous_id)
            if previous and (previous.channel_type, previous.kind) != (
                ChannelType.OFFICIAL_QQ, TargetKind.PRIVATE
            ):
                previous = None
            if previous and previous.external_id.upper() == openid.upper() and (
                not previous_app or previous_app == app_id
            ):
                target = previous
            else:
                if previous:
                    conn.execute("UPDATE target SET enabled=0 WHERE target_id=?", (previous.target_id,))
                    conn.execute(
                        "UPDATE delivery SET status='dismissed', claimed_at=NULL, "
                        "last_error='qq_recipient_changed' WHERE target_id=? AND status IN ('pending','sending')",
                        (previous.target_id,),
                    )
                target_id = conn.execute("SELECT COALESCE(MAX(target_id), 0) + 1 FROM target").fetchone()[0]
                target = Target(target_id, ChannelType.OFFICIAL_QQ, TargetKind.PRIVATE, openid, "我的 QQ")
                self._add_target(conn, target)
            self._set_meta(conn, "qq_delivery_binding", json.dumps([app_id, target.target_id]))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return target

    async def register_qq_target(self, openid: str, app_id: str) -> Target:
        return await self._run(self._register_qq_target, openid, app_id)

    # ---------- delivery ----------
    def _enqueue(self, conn, event_id: str, target_ids: list[int]) -> int:
        n = 0
        for tid in target_ids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO delivery(event_id,target_id,attempts,next_at,status) "
                "VALUES(?,?,0,0,'pending')", (event_id, tid))
            n += cur.rowcount
        return n

    async def enqueue(self, event_id: str, target_ids: list[int]) -> int:
        return await self._run(self._enqueue, event_id, target_ids)

    def _claim_due(self, conn, limit: int) -> list[Delivery]:
        """H2 两阶段领取：pending -> sending（带 claimed_at），发送结果由 finish() 落终态。

        以前的做法是把 next_at 推到 now+3600 当占位：进程一崩，这条投递就被藏了一小时，
        既不会重发也不会进死信。现在崩在发送中的记录留在 sending，由 reclaim_stale_claims()
        超时后捞回 pending。
        """
        now = self._now()
        rows = conn.execute(
            "SELECT delivery_id,event_id,target_id,attempts,next_at,status,last_error FROM delivery "
            "WHERE status='pending' AND next_at<=? "
            "AND target_id IN (SELECT target_id FROM target WHERE enabled=1) "
            "ORDER BY next_at, delivery_id LIMIT ?",
            (now, limit)).fetchall()
        claimed: list[Delivery] = []
        for r in rows:
            # 带 status='pending' 条件更新：并发领取时只有一个进程能把 rowcount 拿到 1
            cur = conn.execute(
                "UPDATE delivery SET status='sending', claimed_at=? "
                "WHERE delivery_id=? AND status='pending'", (now, r["delivery_id"]))
            if cur.rowcount != 1:                  # 被别人抢先领走，本轮跳过
                continue
            claimed.append(Delivery(
                delivery_id=r["delivery_id"], event_id=r["event_id"], target_id=r["target_id"],
                attempts=r["attempts"], next_at=r["next_at"], status="sending",
                last_error=r["last_error"], claim_at=now))
        return claimed

    async def claim_due(self, limit: int = 20) -> list[Delivery]:
        return await self._run(self._claim_due, limit)

    def _finish(self, conn, delivery_id: int, status: SendStatus, error: str | None,
                attempts: int | None, next_at: float | None,
                raw: str | None = None, claim_at: float | None = None) -> bool:
        """H3：只执行 Dispatcher 算好的结果，Store 不再持有重试策略。

        attempts/next_at 由调用方给：dispatch 路径一定显式传；老脚本（selftest_store 直接
        finish(RETRY, "429")）不传时走 _legacy_next_at 兜底，行为与改动前一致。

        M11（2026-11-07）：raw 是渲染失败的输入原文，只有调用方显式给了才写；
        用 COALESCE 保留第一次写入的值 —— 重复 finish / 重试不会覆盖已有原文。

        H4（2026-11-14）：fencing 用「领取令牌」claim_at，而不是只看 status='sending'。
        发送可能超过 reclaim_stale_claims() 的 300 秒窗口：那时这条投递已被打回 pending、
        又被另一个进程领走（status 重新是 sending）。迟到的结果如果只按 status 判断，仍然
        会落库，把别人正在发的这一轮改掉。所以按 claimed_at 比对：只有「仍由这一次领取
        持有」的记录才能落结果，返回 False 表示本次结果被丢弃（调用方记一行日志，不要重试）。
        不传 claim_at 的老调用（脚本、给历史死信补写原文）仍按 delivery_id 直接更新。
        """
        raw_set, raw_args = ("", ())
        if raw is not None:
            raw_set, raw_args = ", raw=COALESCE(raw, ?)", (raw,)
        where, where_args = " WHERE delivery_id=?", (delivery_id,)
        if claim_at is not None:
            where += " AND status='sending' AND claimed_at=?"
            where_args = (delivery_id, claim_at)
        if status is SendStatus.OK:
            cur = conn.execute("UPDATE delivery SET status='sent', claimed_at=NULL, "
                               "attempts=COALESCE(?, attempts), last_error=NULL" + raw_set +
                               where, (attempts,) + raw_args + where_args)
            return cur.rowcount > 0
        if status is SendStatus.RETRY and next_at is not None:
            cur = conn.execute("UPDATE delivery SET status='pending', claimed_at=NULL, "
                               "attempts=COALESCE(?, attempts), next_at=?, last_error=?" + raw_set +
                               where, (attempts, next_at, error) + raw_args + where_args)
            return cur.rowcount > 0
        if status is SendStatus.RETRY:
            # 兜底路径：调用方没算重试计划，按本类旧策略补一个（唯一保留策略的地方）
            r = conn.execute("SELECT attempts FROM delivery WHERE delivery_id=?", (delivery_id,)).fetchone()
            after = attempts if attempts is not None else (r["attempts"] if r else 0) + 1
            retry_at = self._legacy_next_at(after)
            if retry_at is None:
                cur = conn.execute("UPDATE delivery SET status='failed', claimed_at=NULL, attempts=?, "
                                   "last_error=?" + raw_set + where,
                                   (after, error) + raw_args + where_args)
            else:
                cur = conn.execute("UPDATE delivery SET status='pending', claimed_at=NULL, attempts=?, "
                                   "next_at=?, last_error=?" + raw_set + where,
                                   (after, retry_at, error) + raw_args + where_args)
            return cur.rowcount > 0
        # DROP：直接进死信，attempts 保持调用方给的值
        cur = conn.execute("UPDATE delivery SET status='failed', claimed_at=NULL, "
                           "attempts=COALESCE(?, attempts), last_error=?" + raw_set + where,
                           (attempts, error) + raw_args + where_args)
        return cur.rowcount > 0

    def _legacy_next_at(self, attempts_after: int) -> float | None:
        """老策略兜底：返回下次重试的绝对时间；None 表示重试次数已耗尽（进死信）。"""
        if attempts_after >= self.max_attempts:
            return None
        idx = min(attempts_after - 1, len(self.retry_delays) - 1)
        return self._now() + self.retry_delays[idx]

    async def finish(self, delivery_id: int, status: SendStatus, error: str | None,
                     *, attempts: int | None = None, next_at: float | None = None,
                     raw: str | None = None,
                     claim_at: float | None = None) -> bool:
        """H4：claim_at 是本次领取的令牌；返回 False 表示迟到的结果已被丢弃。"""
        return bool(await self._run(self._finish, delivery_id, status, error, attempts,
                                    next_at, raw, claim_at))

    def _reclaim_stale_claims(self, conn, older_than_s: int) -> int:
        """H2：回收「领了但没发完」的僵尸（进程崩溃/被 kill 留下的 sending）。"""
        now = self._now()
        cutoff = now - max(0, int(older_than_s))
        cur = conn.execute(
            "UPDATE delivery SET status='pending', next_at=?, claimed_at=NULL "
            "WHERE status='sending' AND (claimed_at IS NULL OR claimed_at<?)", (now, cutoff))
        return int(cur.rowcount or 0)

    async def reclaim_stale_claims(self, older_than_s: int = 300) -> int:
        return await self._run(self._reclaim_stale_claims, older_than_s)

    def _prune(self, conn, seen_days: int, event_days: int) -> dict[str, int]:
        """M12：清理过期数据。事务内、可重复调用，pending/sending 的投递永远不动。

        H7（2026-11-14）：过期事件若还挂着 pending/sending 投递就跳过，等投递落终态
        后的下一轮再删 —— 否则 ON DELETE CASCADE 会把它们连事件一起删掉。
        """
        now = self._now()
        seen_cutoff = int(now - max(0, seen_days) * 86400)
        event_cutoff = int(now - max(0, event_days) * 86400)
        # H7（2026-11-14）：v4 起 delivery.event_id 带 ON DELETE CASCADE，
        # 「只删已终结投递」之后再删设备事件，会把仍挂着 pending/sending 的投递
        # 一起级联删掉 —— 上面那句 docstring 承诺的「永远不动」并不成立。
        # 现在过期事件只有在「没有未终结投递」时才删，剩下的下一轮再删。
        keep_event_sql = ("event_id NOT IN (SELECT event_id FROM delivery "
                          "WHERE status IN ('pending','sending'))")
        out = {"seen_dynamic": 0, "delivery": 0, "event": 0}
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute("DELETE FROM seen_dynamic WHERE pub_ts<?", (seen_cutoff,))
            out["seen_dynamic"] = int(cur.rowcount or 0)
            # 只删已终结（sent/failed）且所属事件已过期/已不存在的投递：
            # 排期中的投递哪怕事件过期也留着，先让 dispatcher 自己判终态。
            # H7：dismissed 是用户确认「不发了」的终态，与 sent/failed 一样随事件过期清掉
            cur = conn.execute(
                "DELETE FROM delivery WHERE status IN ('sent','failed','dismissed') "
                "AND event_id NOT IN (SELECT event_id FROM event WHERE occurred_at>=?)",
                (event_cutoff,))
            out["delivery"] = int(cur.rowcount or 0)
            cur = conn.execute(
                "DELETE FROM event WHERE occurred_at<? AND " + keep_event_sql,
                (event_cutoff,))
            out["event"] = int(cur.rowcount or 0)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return out

    async def prune(self, *, seen_days: int = 90, event_days: int = 180) -> dict[str, int]:
        return await self._run(self._prune, seen_days, event_days)

    def _open_deliveries(self, conn, event_id: str) -> int:
        # 两阶段领取后「没到终态」有两态：pending 排队、sending 正在发
        row = conn.execute(
            "SELECT COUNT(*) FROM delivery WHERE event_id=? AND status IN ('pending','sending')",
            (event_id,)).fetchone()
        return int(row[0] if row else 0)

    async def open_deliveries(self, event_id: str) -> int:
        return await self._run(self._open_deliveries, event_id)

    def _stats(self, conn) -> dict:
        stats = {"pending": 0, "sent": 0, "failed": 0}
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM delivery GROUP BY status"):
            stats[r["status"]] = r["n"]
        return {"stats": stats, "deliveries": sum(stats.values()),
                "events": conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]}

    async def stats(self) -> dict:
        return await self._run(self._stats)

    def _dead_letters(self, conn, limit: int) -> list[dict]:
        rows = conn.execute("SELECT delivery_id,event_id,target_id,attempts,last_error,raw FROM delivery "
                            "WHERE status='failed' ORDER BY delivery_id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            # 既有字段名与顺序不变；raw 预览 + has_raw 加在既有字段之后（M11）
            item = dict(r)
            raw = item["raw"]
            item["has_raw"] = bool(raw)
            if raw and len(raw) > RAW_PREVIEW_CHARS:
                item["raw"] = raw[:RAW_PREVIEW_CHARS]
            out.append(item)
        return out

    async def dead_letters(self, limit: int = 50) -> list[dict]:
        return await self._run(self._dead_letters, limit)

    # ---------- 动态水位 ----------
    def _seen_ids(self, conn, uid: int, since_ts: int | None) -> set[str]:
        # H1：since_ts 给定时只看时间窗内的记录（走 idx_seen_uid_pub），None 保持旧行为（全量）
        if since_ts is None:
            sql, args = "SELECT id_str FROM seen_dynamic WHERE uid=?", (uid,)
        else:
            sql, args = ("SELECT id_str FROM seen_dynamic WHERE uid=? AND pub_ts>=?",
                         (uid, int(since_ts)))
        return {r["id_str"] for r in conn.execute(sql, args)}

    async def seen_ids(self, uid: int, since_ts: int | None = None) -> set[str]:
        return await self._run(self._seen_ids, uid, since_ts)

    def _mark_seen(self, conn, uid: int, items: list[tuple[str, int]]) -> None:
        conn.executemany("INSERT OR IGNORE INTO seen_dynamic(uid,id_str,pub_ts) VALUES(?,?,?)",
                         [(uid, i, t) for i, t in items])

    async def mark_seen(self, uid: int, items: list[tuple[str, int]]) -> None:
        if items:
            await self._run(self._mark_seen, uid, items)

    # ---------- 直播场次 ----------
    def _upsert_session(self, conn, uid: int, start_ts: int, online: int, title: str) -> None:
        conn.execute(
            "INSERT INTO live_session(uid,start_ts,online_max,title) VALUES(?,?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET start_ts=CASE WHEN live_session.start_ts=0 "
            "THEN excluded.start_ts ELSE live_session.start_ts END, "
            "online_max=MAX(live_session.online_max, excluded.online_max), title=excluded.title",
            (uid, start_ts, online, title))

    async def upsert_live_session(self, uid: int, start_ts: int, online: int, title: str = "") -> None:
        await self._run(self._upsert_session, uid, start_ts, online, title)

    def _get_session(self, conn, uid: int) -> dict | None:
        r = conn.execute("SELECT start_ts,online_max,title FROM live_session WHERE uid=?", (uid,)).fetchone()
        return dict(r) if r else None

    async def get_live_session(self, uid: int) -> dict | None:
        return await self._run(self._get_session, uid)

    def _clear_session(self, conn, uid: int) -> None:
        conn.execute("DELETE FROM live_session WHERE uid=?", (uid,))

    async def clear_live_session(self, uid: int) -> None:
        await self._run(self._clear_session, uid)

    # ---------- 心跳（守护进程用） ----------
    def _set_meta(self, conn, k: str, v: str) -> None:
        conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    async def set_meta(self, k: str, v: str) -> None:
        await self._run(self._set_meta, k, v)

    def _get_meta(self, conn, k: str, default: str | None) -> str | None:
        r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    async def get_meta(self, k: str, default: str | None = None) -> str | None:
        return await self._run(self._get_meta, k, default)

    # ---------- 收尾 ----------
    def _close(self, conn) -> None:
        conn.close()

    async def close(self) -> None:
        if self._conn is not None:
            await self._run(self._close)
            self._conn = None
