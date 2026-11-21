#!/usr/bin/env python
"""日程进程组：周表扫描 / 日程识别交接 / 每日推送 / 直播预约刷新（2026-11-08）

从 scripts/run_bot.py 拆出来的独立进程，由 scripts/supervisor.py 看护：
崩了自动重启，主进程重启时一起重启，停止服务时一起收。

用法：
  python scripts/schedule_worker.py                  # 常驻，Ctrl+C 停
  python scripts/schedule_worker.py --once           # 各跑一轮就退出（联调）
  python scripts/schedule_worker.py --once --push-now  # 额外立即推一次当天日程

状态写 meta['worker_state']（pid / last_beat / 每个任务的最近心跳与计数），
管理台「进程」页据此判活；暂停开关仍然读 meta['tasks_off']，和主进程共用。

日程功能总开关是 meta['weekly_feature_enabled']（默认关闭）：关闭时本进程启动即退出，
常驻期间每 5 秒查一次，关掉就收掉全部循环，由守护进程收起进程组。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.bili import BiliClient                  # noqa: E402
from core import tasks as T                            # noqa: E402
from core import weekly as _wk                         # noqa: E402
from core.weekly import agent as _agent                # noqa: E402
from core.weekly import feature as _feature            # noqa: E402
from core.weekly import push as _push                  # noqa: E402
from shared import proxy as proxy_cfg                  # noqa: E402
from store.sqlite import SCHEMA                        # noqa: E402

# 2026-11-20：本进程也会联网（周表抓图、直播预约刷新），启动时同样套用代理配置，
# 否则「不使用代理 / 自定义代理」对它不生效。
proxy_cfg.apply_to_env()

AGENT_TICK_S = 300.0
SCAN_TICK_S = 600.0
RESERVE_TICK_S = 1800.0
HEARTBEAT_S = 15.0

_STATE_LOCK = threading.Lock()


class FileLog:
    """按行追加到 --log-file；supervisor 会在重启前轮转它。"""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = "%s [日程进程组] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        print(line, flush=True)
        if self.path is None:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:                       # noqa: BLE001
            pass


def _connect(db_path: str | Path, schema: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if schema:
        conn.executescript(SCHEMA)
    return conn


def _read_state(conn: sqlite3.Connection) -> dict:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (T.WORKER_STATE_KEY,)).fetchone()
        data = json.loads(row[0]) if row and row[0] else {}
        return data if isinstance(data, dict) else {}
    except Exception:                           # noqa: BLE001
        return {}


def _write_state(conn: sqlite3.Connection, state: dict) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (T.WORKER_STATE_KEY, json.dumps(state, ensure_ascii=False)))
    conn.commit()


def _update_state(db_path: str | Path, key: str | None = None, counters: dict | None = None,
                  touch: bool = False) -> dict:
    """合并写 worker_state。touch=True 只刷新 last_beat（心跳）。"""
    now = time.time()
    with _STATE_LOCK:
        conn = _connect(db_path)
        try:
            state = _read_state(conn)
            state["pid"] = os.getpid()
            state["last_beat"] = now
            state.setdefault("started_at", int(now))
            state.setdefault("tasks", {})
            if key:
                row = state["tasks"].setdefault(key, {"counters": {}})
                row["beat_at"] = now
                if counters:
                    c = row.setdefault("counters", {})
                    for k, v in counters.items():
                        try:
                            c[k] = c.get(k, 0) + v
                        except Exception:       # noqa: BLE001
                            c[k] = v
            _write_state(conn, state)
            return state
        finally:
            conn.close()


def worker_state(db_path: str | Path, pid: int | None = None) -> dict:
    """管理台/测试用：读当前状态（不写）。"""
    conn = _connect(db_path)
    try:
        return _read_state(conn)
    finally:
        conn.close()


def scan_once(db_path: str, log) -> dict:
    """扫最近 3 天的图文动态进候选池，并顺手清理已判定的缓存图。"""
    conn = _connect(db_path)
    try:
        batch = _wk.scan(conn, since=int(time.time() - 3 * 86400), limit=200, download=True)
        pending = _wk.pending_batch(conn, limit=500)
        purged = 0
        try:
            stat = _wk.purge_cache(conn)
            purged = int(stat.get("removed") or 0)
        except Exception as e:                   # noqa: BLE001
            log("[周表] 缓存清理失败：%s: %s" % (type(e).__name__, e))
        log("[周表] 候选扫描：新增 %d，待判定 %d，清理 %d 张"
            % (len(batch), len(pending), purged))
        return {"added": len(batch), "pending": len(pending), "purged": purged}
    finally:
        conn.close()


def reserve_once(db_path: str, log) -> dict:
    """把动态里挂的直播预约落成单场日程。"""
    conn = _connect(db_path, schema=True)
    try:
        res = _wk.refresh_reserves(BiliClient(), conn, days=3)
    finally:
        conn.close()
    if res.get("saved") or res.get("errors"):
        text = "[直播预约] 新增/更新 %d 条" % res.get("saved", 0)
        if res.get("skipped"):
            text += "，跳过 %d" % res["skipped"]
        if res.get("errors"):
            text += "，错误 %d" % res["errors"]
        log(text)
    return res


def agent_once(db_path: str, log) -> dict:
    """先回收结果，再按闸门决定是否导出任务包；不调用任何模型。"""
    conn = _connect(db_path)
    try:
        ing = _agent.harness_ingest(conn, log=None)
        gate = _agent.gate(conn)
        if not gate.get("go"):
            return {"ingest": ing, "gate": gate}
        if not gate.get("manual") and not _agent.slot_due(conn).get("due"):
            return {"ingest": ing, "gate": gate, "slot": "wait"}
        exp = _agent.harness_export(conn, limit=gate.get("limit") or 12)
        return {"ingest": ing, "gate": gate, "export": exp}
    finally:
        conn.close()


async def heartbeat_loop(db_path: str) -> None:
    while True:
        await asyncio.to_thread(_update_state, db_path, None, None, True)
        await asyncio.sleep(HEARTBEAT_S)


async def weekly_scan_loop(db_path: str, log, board=None) -> None:
    await asyncio.sleep(45)          # 让主进程先落一轮动态，候选池才有东西
    while True:
        if board is not None and board.paused("weekly_scan"):
            await asyncio.sleep(60)
            continue
        try:
            res = await asyncio.to_thread(scan_once, db_path, log)
            await asyncio.to_thread(_update_state, db_path, "weekly_scan", {
                "rounds": 1, "saved": res.get("added", 0),
                "pending": res.get("pending", 0), "purged": res.get("purged", 0)})
        except Exception as e:                   # noqa: BLE001
            log("[周表] 本轮失败：%s: %s" % (type(e).__name__, e))
        await asyncio.sleep(SCAN_TICK_S)


async def reserve_loop(db_path: str, log, board=None) -> None:
    await asyncio.sleep(20)
    while True:
        if board is not None and board.paused("reserve"):
            await asyncio.sleep(60)
            continue
        try:
            res = await asyncio.to_thread(reserve_once, db_path, log)
            await asyncio.to_thread(_update_state, db_path, "reserve",
                                    {"rounds": 1, "saved": res.get("saved", 0)})
        except Exception as e:                   # noqa: BLE001
            log("[直播预约] 本轮失败：%s: %s" % (type(e).__name__, e))
        await asyncio.sleep(RESERVE_TICK_S)


async def weekly_agent_loop(db_path: str, log, board=None) -> None:
    await asyncio.sleep(60)
    while True:
        if board is not None and board.paused("weekly_agent"):
            await asyncio.sleep(60)
            continue
        try:
            res = await asyncio.to_thread(agent_once, db_path, log)
            ing = res.get("ingest") or {}
            saved = (ing.get("ingest") or {}).get("saved", 0)
            await asyncio.to_thread(_update_state, db_path, "weekly_agent",
                                    {"rounds": 1, "saved": saved})
            if ing.get("count"):
                log("[日程交接] 回收结果 %d 条 / 入库 %d 条"
                    % (ing.get("count"), saved))
            exp = res.get("export") or {}
            if exp.get("exported"):
                log("[日程交接] 已导出任务 %s（%d 条），等 DSH 会话读图"
                    % (exp.get("task_path"), exp.get("exported")))
        except Exception as e:                   # noqa: BLE001
            log("[日程交接] 本轮失败：%s: %s" % (type(e).__name__, e))
        await asyncio.sleep(AGENT_TICK_S)


async def feature_watch(db_path: str, log) -> str:
    """总开关关掉后让整个进程组退出（守护进程看到已关闭就不会重启）。"""
    while True:
        await asyncio.sleep(5.0)
        if not await asyncio.to_thread(_feature.enabled_db, db_path):
            log("日程功能已关闭，进程组退出")
            return "disabled"


async def watch_stop(path: Path | None) -> str:
    while True:
        if path is not None and path.exists():
            return "stop"
        await asyncio.sleep(2)


async def run_all(db_path: str, log, stop_file: Path | None, board=None) -> None:
    tasks = [
        asyncio.create_task(heartbeat_loop(db_path)),
        asyncio.create_task(weekly_scan_loop(db_path, log, board)),
        asyncio.create_task(reserve_loop(db_path, log, board)),
        asyncio.create_task(weekly_agent_loop(db_path, log, board)),
        asyncio.create_task(_push.push_loop(log, board=board, db_path=db_path)),
    ]
    stopper = asyncio.create_task(watch_stop(stop_file))
    gate = asyncio.create_task(feature_watch(db_path, log))
    done, _pending = await asyncio.wait(tasks + [stopper, gate],
                                        return_when=asyncio.FIRST_COMPLETED)
    for t in tasks + [stopper, gate]:
        if not t.done():
            t.cancel()
    await asyncio.gather(*(tasks + [stopper, gate]), return_exceptions=True)
    for t in done:
        exc = t.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            log("循环异常退出：%s: %s" % (type(exc).__name__, exc))


def _install_sigbreak_handler() -> None:
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)


def main() -> int:
    ap = argparse.ArgumentParser(description="日程进程组（周表扫描 / 日程交接 / 每日推送 / 预约刷新）")
    ap.add_argument("--db", default=str(ROOT / "data" / "bot.db"))
    ap.add_argument("--log-file", default=str(ROOT / "logs" / "schedule_worker.log"))
    ap.add_argument("--stop-file", default=str(ROOT / "data" / "schedule_worker.stop"))
    ap.add_argument("--once", action="store_true", help="各跑一轮就退出（联调用）")
    ap.add_argument("--push-now", action="store_true", help="与 --once 一起用：立即推一次")
    args = ap.parse_args()

    log = FileLog(args.log_file)
    stop_file = Path(args.stop_file)
    _install_sigbreak_handler()
    if not _feature.enabled_db(args.db):
        log("日程功能未启用（meta.weekly_feature_enabled 不是 1），本进程退出")
        return 0
    T.BOARD.bind(args.db)
    _update_state(args.db)
    log("启动 pid=%d，库 %s" % (os.getpid(), args.db))

    if args.once:
        try:
            if not T.BOARD.paused("weekly_scan"):
                res = scan_once(args.db, log)
                _update_state(args.db, "weekly_scan", {"rounds": 1, "saved": res.get("added", 0)})
            if not T.BOARD.paused("reserve"):
                res = reserve_once(args.db, log)
                _update_state(args.db, "reserve", {"rounds": 1, "saved": res.get("saved", 0)})
            if not T.BOARD.paused("weekly_agent"):
                res = agent_once(args.db, log)
                log("[日程交接] gate=%s" % json.dumps(res.get("gate") or {}, ensure_ascii=False))
            if args.push_now and not T.BOARD.paused("daily_push"):
                ok = asyncio.run(_push.attempt_push(log))
                _update_state(args.db, "daily_push", {"runs": 1 if ok else 0})
        except Exception as e:                   # noqa: BLE001
            log("单轮执行失败：%s: %s" % (type(e).__name__, e))
            return 1
        log("单轮完成，退出")
        return 0

    try:
        asyncio.run(run_all(args.db, log, stop_file, T.BOARD))
    except KeyboardInterrupt:
        log("收到停止信号")
    log("退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
