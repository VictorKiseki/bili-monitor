"""每日日程推送：时间计算、标记文件、失败告警与推送循环（2026-11-08 移出 run_bot）。

独立进程 scripts/schedule_worker.py 调用；scripts/run_bot.py 的 --push-now 也复用
push_once()，保证「手动补推」和「每天定时」走同一条路径。

推送时间优先读 meta['daily_push_at']（管理台「日程表」页可改），没设置时回落到
环境变量 BBOT_PUSH_AT，再回落到 08:10。改完不用重启进程，循环每 60 秒读一次库。

不调用任何模型，只负责按时间拉起 scripts/daily_push.py 子进程。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from shared.proc import no_window_flags

ROOT = Path(__file__).resolve().parents[2]

PUSH_AT_ENV = "BBOT_PUSH_AT"       # 默认 08:10；设成 off/空 就不跑推送循环
PUSH_AT_KEY = "daily_push_at"      # 管理台保存的时间；优先于环境变量
DEFAULT_PUSH_AT = "08:10"
PUSH_POLL_S = 60.0                 # 轮询配置的间隔；改完 1 分钟内生效
PUSH_MARKER = ROOT / "data" / "daily_push.last"   # daily_push.py 推送成功后写入当天日期
PUSH_ALERT = ROOT / "data" / "push_alert.json"     # 推送最终失败时写；巡检会话靠它决定要不要介入

_PUSH_AT_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$")
_OFF_VALUES = ("", "off", "0", "none")


def normalize_push_at(value) -> str:
    """把 8:10 / 08：10 归一成 HH:MM；空或 off 返回空串；非法值抛 ValueError。"""
    raw = "" if value is None else str(value).strip()
    if raw.lower() in _OFF_VALUES:
        return ""
    m = _PUSH_AT_RE.match(raw)
    if not m:
        raise ValueError("推送时间要写成 H:mm，例如 8:10 或 20:30")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise ValueError("推送时间超出范围（00:00–23:59）")
    return f"{hour:02d}:{minute:02d}"


def push_at_from_conn(conn: sqlite3.Connection) -> str | None:
    """读库里的推送时间；没有这个键或值不合法返回 None，off 返回空串。"""
    try:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (PUSH_AT_KEY,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        return normalize_push_at(row[0])
    except ValueError:
        return None


def push_at_from_db(db_path: str | Path) -> str | None:
    """只读打开数据库读推送时间；库不存在 / 打不开按「没设置」处理。"""
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            return push_at_from_conn(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def get_push_at(conn: sqlite3.Connection | None = None,
                db_path: str | Path | None = None,
                env: str | None = None) -> str:
    """推送时间的唯一读口：库 > BBOT_PUSH_AT > 08:10。

    库里明确写了 off 时返回空串（关闭）；库里没写或写坏了才看环境变量。
    """
    stored = push_at_from_conn(conn) if conn is not None else (
        push_at_from_db(db_path) if db_path is not None else None)
    if stored is not None:
        return stored
    raw = os.environ.get(PUSH_AT_ENV) if env is None else env
    if raw is None:
        return DEFAULT_PUSH_AT
    try:
        return normalize_push_at(raw) or ""
    except ValueError:
        return DEFAULT_PUSH_AT


def set_push_at(conn: sqlite3.Connection, value) -> str:
    """保存推送时间（HH:MM），返回归一后的值；空值/非法值抛 ValueError。"""
    at = normalize_push_at(value)
    if not at:
        raise ValueError("推送时间不能为空；要停推送请关闭日程功能或暂停 daily_push")
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (PUSH_AT_KEY, at))
    conn.commit()
    return at


def pushed_on(day: str, marker: Path | None = None) -> bool:
    """当天是否已经推过（标记文件里是不是这个日期）。"""
    p = Path(marker) if marker is not None else PUSH_MARKER
    try:
        return p.read_text(encoding="utf-8").strip() == day
    except Exception:                          # noqa: BLE001
        return False


def write_alert(day: str, summary: str, detail: str, path: Path | None = None) -> None:
    """落一份告警文件，等 DSH 巡检会话（08:40）来读；本进程不做模型调用。"""
    target = Path(path) if path is not None else PUSH_ALERT
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "day": day,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary,
            "detail": (detail or "")[-2000:],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:                          # noqa: BLE001
        pass


def clear_alert(path: Path | None = None) -> None:
    target = Path(path) if path is not None else PUSH_ALERT
    try:
        target.unlink(missing_ok=True)
    except Exception:                          # noqa: BLE001
        pass


def _today_due(at: str, now: float) -> float:
    """今天 at 点的时间戳（可能已经过去）。"""
    hour, minute = (int(x) for x in at.split(":"))
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))


def today_due(at: str, now: float) -> float:
    """今天 at 点的时间戳（可能已经过去）；供推送循环和巡检脚本共用。"""
    return _today_due(at, now)


def next_push_at(hhmm: str, now: float | None = None) -> float:
    """下一个 hh:mm（本机时区）的时间戳；今天已过就顺延到明天。"""
    cur = time.time() if now is None else now
    nxt = _today_due(hhmm, cur)
    return nxt if nxt > cur else nxt + 86400


def push_once(extra_args=(), timeout: float = 300.0) -> tuple[bool, str, str]:
    """跑一次 scripts/daily_push.py（子进程），返回 (是否成功, 摘要, 末几行输出)。"""
    cmd = [sys.executable, str(ROOT / "scripts" / "daily_push.py"), *extra_args]
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=no_window_flags())
    except Exception as e:                       # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", ""
    out = (p.stdout or "").strip().splitlines()
    err = (p.stderr or "").strip().splitlines()
    summary = (out[-1] if out else "") or (err[-1] if err else "")
    return p.returncode == 0, f"rc={p.returncode} {summary}".strip(), "\n".join((out + err)[-8:])


async def attempt_push(log) -> bool:
    """推一次，失败 60 秒后重试一次；都不行就写告警文件，交给巡检会话处理。"""
    day = time.strftime("%Y-%m-%d")
    summary = detail = ""
    for attempt in (1, 2):
        ok, summary, detail = await asyncio.to_thread(push_once)
        if ok:
            log(f"[日程推送] 完成：{summary}")
            clear_alert()
            return True
        log(f"[日程推送] 第 {attempt} 次失败：{summary}")
        if attempt == 1:
            await asyncio.sleep(60)
    write_alert(day, summary, detail)
    log("[日程推送] 两次都失败：已写 data/push_alert.json，等巡检会话接手")
    return False


def _read_at(db_path: str | Path | None) -> str:
    return get_push_at(db_path=db_path) if db_path is not None else get_push_at()


async def push_loop(log, at: str | None = None, board=None,
                    db_path: str | Path | None = None) -> None:
    """每天按设置的时间把当天日程推到 QQ，配置改了不用重启。

    at 显式传入时固定用它（联调/测试）；否则每 PUSH_POLL_S 秒读一次
    meta['daily_push_at']，库里没设置时回落到 BBOT_PUSH_AT / 08:10。
    纯 Python 子进程，不经过模型、不开 DSH 会话；失败重试一次（60 秒后）。
    """
    fixed: str | None = None
    if at is not None:
        try:
            fixed = normalize_push_at(at)
        except ValueError as e:
            log(f"[日程推送] 时间设置不合法（{at}）：{e}，改用 {DEFAULT_PUSH_AT}")
            fixed = DEFAULT_PUSH_AT
        if not fixed:
            log("[日程推送] 已关闭（BBOT_PUSH_AT=off）")
            return

    current = fixed if fixed is not None else _read_at(db_path)
    if not current:
        log("[日程推送] 已关闭（推送时间为空）")
        return
    log(f"[日程推送] 已启动：每天 {current} 把当天日程推到 QQ（脚本化，不依赖 DSH 会话）")

    # 开机补推：今天的时间点已经过了、但标记文件不是今天 → 说明到点时进程没在跑
    now, today = time.time(), time.strftime("%Y-%m-%d")
    skipped_day = failed_day = ""
    if now >= _today_due(current, now) and not pushed_on(today):
        if board is not None and board.paused("daily_push"):
            log("[日程推送] 已暂停（管理台「进程」页），today 的补推跳过")
            skipped_day = today
        else:
            log("[日程推送] 今天还没推过，先补一次")
            if not await attempt_push(log):
                failed_day = today
            if board is not None:
                board.beat("daily_push", runs=1 if not failed_day else 0,
                           failures=1 if failed_day else 0)

    target: float | None = None
    while True:
        now = time.time()
        if fixed is None:
            latest = _read_at(db_path)
            if latest != current:
                old, current = current, latest
                target = None
                skipped_day = failed_day = ""
                if current:
                    log(f"[日程推送] 推送时间已更新：{old or '关闭'} → {current}（1 分钟内生效）")
                else:
                    log("[日程推送] 推送时间已关闭，等管理台重新设置")
        if not current:
            target = None
            await asyncio.sleep(PUSH_POLL_S)
            continue

        # 目标点一旦算出来就保留到触发为止：到点那一轮若重算，next_push_at 会
        # 把「已经到点」当成「今天过了」顺延到明天，当天的推送就被跳过。
        if target is None:
            due = _today_due(current, now)
            target = due if due > now else due + 86400
            log("[日程推送] 下次 %s" % time.strftime("%m-%d %H:%M", time.localtime(target)))

        delay = target - now
        if delay > 0:
            await asyncio.sleep(min(PUSH_POLL_S, delay))
            continue

        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if pushed_on(today) or failed_day == today:
            target = None
            await asyncio.sleep(PUSH_POLL_S)
            continue
        if board is not None and board.paused("daily_push"):
            if skipped_day != today:
                board.beat("daily_push", skipped=1)
                log("[日程推送] 已暂停（管理台「进程」页），本次跳过；恢复后等下一个时间点")
                skipped_day = today
            target = None
            await asyncio.sleep(PUSH_POLL_S)
            continue

        ok = await attempt_push(log)
        failed_day = "" if ok else today
        if board is not None:
            board.beat("daily_push", runs=1 if ok else 0, failures=0 if ok else 1)
        target = None
