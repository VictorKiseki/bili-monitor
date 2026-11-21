"""录播清理评审：计划、Bot 确认、保护移动与按天调度。

设计要点：
  · 清理器只负责扫描/选择候选；本模块把候选写成“待确认计划”，不直接删。
  · 计划由清理守护写，Bot 只写 notify.json / decisions.json，避免两个进程抢写同一文件。
  · 通知发送成功（notify.json: sent）后，守护才开始 1 小时倒计时；通知没发出去绝不删。
  · 被用户保留的文件移到 protected_dir（默认 root/_protected），移动成功才不再删。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
PLAN_FILE = LOG_DIR / "recorder_cleanup_review.json"
DECISIONS_FILE = LOG_DIR / "recorder_cleanup_decisions.json"
NOTIFY_FILE = LOG_DIR / "recorder_cleanup_notify.json"
SCHEDULE_FILE = LOG_DIR / "recorder_cleanup_schedule.json"
TRIGGER_FILE = LOG_DIR / "recorder_cleanup_run.request"
ARCHIVE_DIR = LOG_DIR / "cleanup_reviews"

GIB = 1024 ** 3
ACTIVE_STATUSES = ("awaiting_notify", "notified", "executing")
KEEP_WORDS = ("保留", "保护", "不清理", "不可清理", "拒绝", "别删")
ALL_WORDS = ("全部保留", "都保留", "全部保护", "全保留", "全都保留", "全部不清理", "都不清理")


def _read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_json(path: Path, default):
    return _read_json(Path(path), default)


def write_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _now(now=None) -> int:
    return int(now if now is not None else time.time())


def fmt_ts(ts: int) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except (OSError, ValueError):
        return "—"


# ---------- 计划 ----------
def new_plan_id(now=None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(_now(now))) + "-" + secrets.token_hex(2)


def make_plan(entries: list[dict], *, reason: str, total_before: int, free_before: int,
              max_bytes: int, min_free_bytes: int, wait_minutes: int = 60,
              now=None) -> dict:
    ts = _now(now)
    candidates = []
    for i, e in enumerate(entries, 1):
        candidates.append({
            "id": i,
            "flv": str(e["flv"]),
            "name": str(e.get("name") or Path(str(e["flv"])).name),
            "room": str(e.get("room") or Path(str(e["flv"])).parent.name),
            "size": int(e.get("size") or 0),
            "mtime": int(e.get("mtime") or 0),
            "sidecars": [{"path": str(s), "size": int(sz)}
                         for s, sz in (e.get("sidecars") or [])],
            "status": "pending",
        })
    return {
        "version": 1,
        "plan_id": new_plan_id(ts),
        "created_at": ts,
        "updated_at": ts,
        "status": "awaiting_notify",
        "notified_at": 0,
        "deadline_at": 0,
        "wait_minutes": int(wait_minutes),
        "reason": str(reason),
        "total_before": int(total_before),
        "free_before": int(free_before),
        "max_bytes": int(max_bytes),
        "min_free_bytes": int(min_free_bytes),
        "candidates": candidates,
        "errors": [],
    }


def load_plan(path: Path = PLAN_FILE) -> dict | None:
    data = _read_json(Path(path), None)
    return data if isinstance(data, dict) and data.get("plan_id") else None


def save_plan(plan: dict, path: Path = PLAN_FILE) -> None:
    plan["updated_at"] = _now()
    write_json(Path(path), plan)


def is_active(plan: dict | None) -> bool:
    return bool(plan and plan.get("status") in ACTIVE_STATUSES)


def candidate_ids(plan: dict) -> list[int]:
    return [int(c["id"]) for c in plan.get("candidates", []) if "id" in c]


def archive_plan(plan: dict, archive_dir: Path = ARCHIVE_DIR) -> None:
    try:
        p = Path(archive_dir) / ("%s.json" % plan["plan_id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------- 通知状态（Bot 进程写） ----------
def load_notify(path: Path = NOTIFY_FILE) -> dict:
    data = _read_json(Path(path), {})
    return data if isinstance(data, dict) else {}


def save_notify(data: dict, path: Path = NOTIFY_FILE) -> None:
    data = dict(data)
    data["updated_at"] = _now()
    write_json(Path(path), data)


# ---------- 用户决定（Bot 进程写） ----------
def load_decisions(path: Path = DECISIONS_FILE) -> dict:
    data = _read_json(Path(path), {})
    return data if isinstance(data, dict) else {}


def keep_set(plan: dict, path: Path = DECISIONS_FILE) -> tuple[set[int], bool]:
    d = load_decisions(path)
    if not plan or d.get("plan_id") != plan.get("plan_id"):
        return set(), False
    ids = set()
    for x in d.get("keep") or []:
        try:
            ids.add(int(x))
        except (TypeError, ValueError):
            pass
    return ids, bool(d.get("keep_all"))


def mark_keep(plan: dict, ids: list[int], keep_all: bool = False,
              path: Path = DECISIONS_FILE) -> dict:
    allowed = set(candidate_ids(plan))
    ids = sorted({int(i) for i in ids if int(i) in allowed})
    old = load_decisions(path)
    if old.get("plan_id") == plan["plan_id"]:
        merged = set()
        for x in old.get("keep") or []:
            try:
                merged.add(int(x))
            except (TypeError, ValueError):
                pass
    else:
        merged = set()
    merged.update(ids)
    data = {"plan_id": plan["plan_id"], "keep": sorted(merged),
            "keep_all": bool(keep_all or (old.get("plan_id") == plan["plan_id"] and old.get("keep_all"))),
            "updated_at": _now(), "source": "qq"}
    write_json(Path(path), data)
    return data


def _parse_ids(text: str) -> list[int]:
    text = text.replace("，", ",").replace("、", ",").replace("．", ".")
    out: list[int] = []
    for token in re.split(r"[,;\s]+", text.strip()):
        if not token:
            continue
        m = re.fullmatch(r"(\d+)\s*(?:[-~至]\s*(\d+))?", token)
        if not m:
            continue
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a > b:
            a, b = b, a
        if b - a > 200:
            b = a + 200
        out.extend(range(a, b + 1))
    return out


def handle_qq_command(text: str, plan_path: Path = PLAN_FILE,
                      decisions_path: Path = DECISIONS_FILE,
                      now=None) -> str | None:
    """解析私聊命令并写 keep 决定；返回给用户的确认文字，None 表示不是命令。"""
    raw = unicodedata.normalize("NFKC", str(text or ""))
    raw = re.sub(r"<@!?\d+>", " ", raw).strip()
    if not raw:
        return None
    plan = load_plan(plan_path)
    is_keep = any(w in raw for w in KEEP_WORDS)
    if not is_keep:
        return None
    if not plan or not is_active(plan):
        return "当前没有待确认的录播清理计划。"
    if plan.get("status") == "notified" and plan.get("deadline_at") and _now(now) >= int(plan["deadline_at"]):
        return "这次清理的确认时间已经过了，计划正在执行/已结束，不能再改。"
    all_flag = any(w in raw for w in ALL_WORDS)
    ids = [] if all_flag else _parse_ids(raw)
    if not all_flag and not ids:
        return "请回复：保留 1 3（编号见待确认通知），或回复「全部保留」。"
    allowed = set(candidate_ids(plan))
    ids = [i for i in ids if i in allowed]
    if not all_flag and not ids:
        return "编号不在本次计划里，有效编号：1-%d。" % max(allowed or [1])
    mark_keep(plan, ids, keep_all=all_flag, path=decisions_path)
    if all_flag:
        return "已全部标记保留（%d 个）。清理时会把它们移到保护目录。" % len(allowed)
    return "已标记保留：%s。清理时会移到保护目录，其余文件按通知时间删除。" % "、".join(map(str, ids))


# ---------- 通知文本 ----------
def notification_messages(plan: dict, max_len: int = 850) -> list[str]:
    cands = plan.get("candidates") or []
    total = sum(int(c.get("size") or 0) for c in cands)
    head = ("【录播清理待确认】\n"
            "原因：%s\n"
            "将清理 %d 个录像，共 %.2f GiB；发出后 %d 分钟内未回复的文件会自动删除。"
            % (plan.get("reason") or "超过上限/磁盘不足", len(cands), total / GIB,
               int(plan.get("wait_minutes") or 60)))
    foot = ("回复「保留 1 3」保护指定编号，回复「全部保留」保护全部。\n"
            "管理台：http://127.0.0.1:8765")
    lines = []
    for c in cands:
        room = str(c.get("room") or "")
        name = str(c.get("name") or "")
        if len(room) > 22:
            room = room[:21] + "…"
        if len(name) > 36:
            name = name[:35] + "…"
        lines.append("%d. %s/%s  %.2f GiB  %s"
                     % (c["id"], room, name, int(c.get("size") or 0) / GIB, fmt_ts(c.get("mtime") or 0)))
    # 按长度切块，每条消息都带页眉/页脚；文件多时发多条，编号仍然完整
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_len = len(head) + len(foot)
    for line in lines:
        if cur and cur_len + len(line) + 1 > max_len:
            chunks.append(cur)
            cur, cur_len = [], len(head) + len(foot)
        cur.append(line)
        cur_len += len(line) + 1
    if cur or not chunks:
        chunks.append(cur)
    out = []
    for i, chunk in enumerate(chunks, 1):
        prefix = head if len(chunks) == 1 else ("【录播清理待确认 %d/%d】\n" % (i, len(chunks))
                                                + head[len("【录播清理待确认】\n"):])
        out.append(prefix + "\n" + "\n".join(chunk) + "\n" + foot)
    return out


# ---------- 保护移动 ----------
def resolve_root(cfg: dict) -> Path:
    root = Path(str(cfg.get("root") or "")).expanduser()
    if not root.is_absolute():
        root = (ROOT / root).resolve()
    return root


def protected_dir_for(cfg: dict) -> Path:
    raw = str(cfg.get("protected_dir") or "").strip()
    if not raw:
        return resolve_root(cfg) / "_protected"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


def _unique_dest(dest: Path, sidecar_suffixes: list[str]) -> Path:
    def taken(p: Path) -> bool:
        if p.exists():
            return True
        return any(p.with_suffix(s).exists() for s in sidecar_suffixes)
    if not taken(dest):
        return dest
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cand = dest.with_name("%s_%s%s" % (dest.stem, stamp, dest.suffix))
    n = 0
    while taken(cand):
        n += 1
        cand = dest.with_name("%s_%s_%d%s" % (dest.stem, stamp, n, dest.suffix))
    return cand


def move_to_protected(candidate: dict, protected_dir: Path) -> dict:
    src = Path(str(candidate["flv"]))
    dest_dir = Path(protected_dir) / str(candidate.get("room") or src.parent.name)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "errors": ["创建保护目录失败：%s" % e], "flv": "", "sidecars": []}
    suffixes = [Path(str(s.get("path", ""))).suffix for s in candidate.get("sidecars") or []]
    dest = _unique_dest(dest_dir / src.name, suffixes)
    try:
        shutil.move(str(src), str(dest))
    except OSError as e:
        return {"ok": False, "errors": ["移动 %s 失败：%s" % (src, e)], "flv": "", "sidecars": []}
    errors: list[str] = []
    moved: list[str] = []
    for sc in candidate.get("sidecars") or []:
        sp = Path(str(sc.get("path") or ""))
        if not sp.exists():
            continue
        sp_dest = dest.with_suffix(sp.suffix)
        try:
            shutil.move(str(sp), str(sp_dest))
            moved.append(str(sp_dest))
        except OSError as e:
            errors.append("移动弹幕 %s 失败：%s" % (sp, e))
    return {"ok": True, "flv": str(dest), "sidecars": moved, "errors": errors}


# ---------- 按天 + 指定时间调度 ----------
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_run_time(value) -> str:
    m = _TIME_RE.match(str(value or "").strip())
    if not m:
        raise ValueError("run_time 必须是 HH:MM（00:00-23:59）")
    return "%02d:%02d" % (int(m.group(1)), int(m.group(2)))


def _at_time(d: datetime, run_time: str) -> datetime:
    hh, mm = (int(x) for x in run_time.split(":"))
    return d.replace(hour=hh, minute=mm, second=0, microsecond=0)


def first_run_at(now: float, run_time: str) -> int:
    dt = datetime.fromtimestamp(now)
    cand = _at_time(dt, run_time)
    if cand <= dt:
        cand = _at_time(dt + timedelta(days=1), run_time)
    return int(cand.timestamp())


def next_after(ts: float, interval_days: int, run_time: str) -> int:
    dt = datetime.fromtimestamp(ts) + timedelta(days=max(1, int(interval_days)))
    return int(_at_time(dt, run_time).timestamp())


def schedule_fingerprint(interval_days: int, run_time: str) -> str:
    return "%d|%s" % (max(1, int(interval_days)), run_time)


def load_schedule(path: Path = SCHEDULE_FILE) -> dict:
    data = _read_json(Path(path), {})
    return data if isinstance(data, dict) else {}


def save_schedule(data: dict, path: Path = SCHEDULE_FILE) -> None:
    write_json(Path(path), data)


def ensure_schedule(cfg: dict, now=None, path: Path = SCHEDULE_FILE) -> dict:
    now = _now(now)
    days = max(1, int(cfg.get("interval_days", 1) or 1))
    run_time = parse_run_time(cfg.get("run_time", "03:00"))
    fp = schedule_fingerprint(days, run_time)
    st = load_schedule(path)
    if st.get("fingerprint") == fp and int(st.get("next_run_at") or 0) > 0:
        return st
    st = {"fingerprint": fp, "interval_days": days, "run_time": run_time,
          "next_run_at": first_run_at(now, run_time), "last_run_at": int(st.get("last_run_at") or 0)}
    save_schedule(st, path)
    return st


def advance_schedule(st: dict, now=None, path: Path = SCHEDULE_FILE) -> dict:
    now = _now(now)
    days = max(1, int(st.get("interval_days") or 1))
    run_time = parse_run_time(st.get("run_time") or "03:00")
    nxt = int(st.get("next_run_at") or first_run_at(now, run_time))
    nxt = next_after(nxt, days, run_time)
    while nxt <= now:
        nxt = next_after(nxt, days, run_time)
    st = dict(st)
    st.update({"next_run_at": nxt, "last_run_at": now, "updated_at": now})
    save_schedule(st, path)
    return st


def schedule_due(cfg: dict, now=None, path: Path = SCHEDULE_FILE) -> tuple[bool, dict]:
    st = ensure_schedule(cfg, now, path)
    return _now(now) >= int(st.get("next_run_at") or 0), st


def trigger_requested(path: Path = TRIGGER_FILE) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


def clear_trigger(path: Path = TRIGGER_FILE) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def write_trigger(path: Path = TRIGGER_FILE) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(str(_now()), encoding="ascii")
