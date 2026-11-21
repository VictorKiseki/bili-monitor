"""日程功能开关（2026-11-08）：默认关闭，按需开启。

首次配置的项目不该在后台跑周表扫描、预约刷新、日程交接和每日推送：
这些能力统一由 meta['weekly_feature_enabled'] 控制，默认「没有这个键 = 关闭」。
管理台在「监控名单」的添加框输入 weekly_open / weekly_close 时调用
ConsoleWeekly.weekly_feature_update()，这里只负责读写这一个事实来源。

守护进程 scripts/supervisor.py 与日程进程组 scripts/schedule_worker.py 都直接
读这个键：关闭时不拉起 / 自行退出，不依赖管理台进程常驻。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

FEATURE_KEY = "weekly_feature_enabled"
TASKS_OFF_KEY = "tasks_off"
# 这四个循环归日程进程组；开关关闭时一并标成暂停，重新开启时再清掉。
FEATURE_TASKS = ("weekly_scan", "reserve", "weekly_agent", "daily_push")
_TRUE = ("1", "true", "yes", "on")


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in _TRUE


def enabled(conn: sqlite3.Connection) -> bool:
    """读开关；没有 meta 表 / 没有这个键都按关闭处理。"""
    try:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (FEATURE_KEY,)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and _truthy(row[0])


def enabled_db(db_path: str | Path) -> bool:
    """只读打开数据库判断开关；守护进程和日程进程组用这个（不建表、不写入）。"""
    path = Path(db_path)
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            return enabled(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def set_enabled(conn: sqlite3.Connection, on: bool) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (FEATURE_KEY, "1" if on else "0"))
    conn.commit()


def _off_keys(conn: sqlite3.Connection) -> list[str]:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (TASKS_OFF_KEY,)).fetchone()
        data = json.loads(row[0]) if row and row[0] else []
    except (sqlite3.Error, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def set_tasks_off(conn: sqlite3.Connection, keys, off: bool) -> list[str]:
    """只改这四个任务的暂停标记，保留用户在别处暂停的其他循环。"""
    current = _off_keys(conn)
    target = set(current)
    if off:
        target.update(keys)
    else:
        target.difference_update(keys)
    wanted = sorted(target)
    if wanted != sorted(current):
        conn.execute(
            "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (TASKS_OFF_KEY, json.dumps(wanted, ensure_ascii=False)))
        conn.commit()
    return wanted


def apply_enabled(conn: sqlite3.Connection, on: bool) -> None:
    """写开关并同步四个任务的暂停标记（开启时清掉，关闭时补上）。"""
    set_enabled(conn, on)
    set_tasks_off(conn, FEATURE_TASKS, off=not on)


def status(conn: sqlite3.Connection) -> dict:
    return {"ok": True, "enabled": enabled(conn), "tasks": list(FEATURE_TASKS)}
