#!/usr/bin/env python
"""录播姬录制目录自清理：超过上限时，从最旧的录像开始删。

设计约束（和 docs/20 对应）：
  1. 只管理 root 下匹配 managed_dir_pattern 的「房间目录」（默认 2001-示例主播 这种），
     不碰 切片/片段/素材 等剪辑目录。
  2. 只删 .flv，以及同名弹幕 .xml；文件最近 keep_recent_minutes 分钟内被写过就跳过，
     防止删到正在录制的文件。
  3. 两个触发条件，先到先删：
       - 受管录像总大小 > max_gb
       - 所在盘剩余空间 < min_free_gb（防止剪辑素材把盘吃满）
  4. 删除顺序按 mtime 从旧到新；删 .flv 成功后才删同名 .xml。
  5. 单实例锁 logs/recorder_cleanup.lock：自启循环和手动 --once 不会同时删。
  6. 默认 dry_run=false；手动排查时用 --dry-run 先看清单。

用法：
  python scripts/recorder_cleanup.py --once              # 跑一次（自启/手动）
  python scripts/recorder_cleanup.py --once --dry-run    # 只看会删什么
  python scripts/recorder_cleanup.py --show              # 看当前占用/上限
  python scripts/recorder_cleanup.py --set-max-gb 120    # 改上限
  python scripts/recorder_cleanup.py --loop              # 常驻，按 interval_days + run_time 调度
  python scripts/recorder_cleanup.py --stop              # 给常驻循环写停止标记

管理台（web/console.py）通过 logs/recorder_cleanup.stop 与
logs/recorder_cleanup_status.json 控制/观察本进程，不靠强杀 PID。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "recorder_cleanup.json"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import cleanup_review as review  # noqa: E402

DEFAULTS = {
    "root": "",
    "max_gb": 150,
    "min_free_gb": 20,
    "keep_recent_minutes": 30,
    "interval_days": 1,
    "run_time": "03:00",
    "review_wait_minutes": 60,
    "protected_dir": "",
    "enabled": True,
    "managed_dir_pattern": r"^\d+-",
    "sidecar_suffixes": [".xml"],
    "log_file": "logs/recorder_cleanup.log",
    "dry_run": False,
}

GIB = 1024 ** 3
STATUS_FILE = ROOT / "logs" / "recorder_cleanup_status.json"
STOP_FILE = ROOT / "logs" / "recorder_cleanup.stop"
LOCK_FILE = ROOT / "logs" / "recorder_cleanup.lock"
REVIEW_PLAN_FILE = review.PLAN_FILE
REVIEW_DECISIONS_FILE = review.DECISIONS_FILE
REVIEW_NOTIFY_FILE = review.NOTIFY_FILE
REVIEW_SCHEDULE_FILE = review.SCHEDULE_FILE
REVIEW_TRIGGER_FILE = review.TRIGGER_FILE


@dataclass
class Sidecar:
    path: Path
    size: int
    mtime: float


@dataclass
class Candidate:
    flv: Path
    size: int
    mtime: float
    sidecars: list[Sidecar] = field(default_factory=list)


@dataclass
class CleanupResult:
    total_before: int
    total_after: int
    free_before: int
    free_after: int
    deleted: list[str]
    bytes_freed: int
    skipped_recent: int
    errors: list[str]
    dry_run: bool
    candidates: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


class Cleaner:
    def __init__(
        self,
        root: str | Path,
        max_bytes: int,
        min_free_bytes: int = 0,
        keep_recent_s: float = 1800,
        managed_dir_pattern: str = r"^\d+-",
        sidecar_suffixes: tuple[str, ...] = (".xml",),
        now=time.time,
        disk_usage=shutil.disk_usage,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.keep_recent_s = max(0.0, float(keep_recent_s))
        self.dir_re = re.compile(managed_dir_pattern)
        self.sidecar_suffixes = tuple(sidecar_suffixes)
        self._now = now
        self._disk_usage = disk_usage
        self.log = logger or logging.getLogger("recorder_cleanup")
        self.dry_run = dry_run

    # ---------- 扫描 ----------
    def _managed_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for entry in os.scandir(self.root):
            if entry.is_dir(follow_symlinks=False) and self.dir_re.match(entry.name):
                dirs.append(Path(entry.path))
        return dirs

    def scan(self) -> tuple[int, list[Candidate]]:
        total = 0
        candidates: list[Candidate] = []
        for room_dir in self._managed_dirs():
            for dirpath, _dirnames, filenames in os.walk(room_dir, followlinks=False):
                for name in filenames:
                    p = Path(dirpath) / name
                    try:
                        st = p.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += st.st_size
                    if p.suffix.lower() != ".flv":
                        continue
                    sidecars: list[Sidecar] = []
                    for suffix in self.sidecar_suffixes:
                        sp = p.with_suffix(suffix)
                        try:
                            sst = sp.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        sidecars.append(Sidecar(sp, sst.st_size, sst.st_mtime))
                    candidates.append(Candidate(p, st.st_size, st.st_mtime, sidecars))
        # 旧文件在前
        candidates.sort(key=lambda c: (c.mtime, str(c.flv)))
        return total, candidates

    def plan(self) -> dict:
        """只选候选，不删文件；供「先通知、1 小时后再删」的评审流程使用。"""
        if not self.root.is_dir():
            raise FileNotFoundError(f"录制根目录不存在：{self.root}")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes 必须大于 0")

        total, candidates = self.scan()
        free = self._disk_usage(self.root).free
        total_before, free_before = total, free
        selected: list[Candidate] = []
        skipped_recent = 0
        for c in candidates:
            if total <= self.max_bytes and free >= self.min_free_bytes:
                break
            if self._too_recent(c.mtime):
                skipped_recent += 1
                continue
            if any(self._too_recent(s.mtime) for s in c.sidecars):
                skipped_recent += 1
                continue
            if self._looks_open(c.flv):
                skipped_recent += 1
                continue
            cost = c.size + sum(s.size for s in c.sidecars)
            selected.append(c)
            total -= cost
            free += cost

        reasons: list[str] = []
        if total_before > self.max_bytes:
            reasons.append("受管录像 %.2f GiB 超过上限 %.2f GiB"
                           % (total_before / GIB, self.max_bytes / GIB))
        if free_before < self.min_free_bytes:
            reasons.append("磁盘剩余 %.2f GiB 低于保底 %.2f GiB"
                           % (free_before / GIB, self.min_free_bytes / GIB))
        return {
            "total_before": total_before,
            "free_before": free_before,
            "selected": selected,
            "skipped_recent": skipped_recent,
            "candidates": len(candidates),
            "reason": "；".join(reasons),
        }

    # ---------- 安全判断 ----------
    @staticmethod
    def _looks_open(path: Path) -> bool:
        """能拿到独占写句柄 = 没人占用；拿不到 = 可能正在写/读，跳过。"""
        try:
            fd = os.open(str(path), os.O_RDWR)
        except OSError:
            return True
        else:
            try:
                os.close(fd)
            except OSError:
                pass
            return False

    def _too_recent(self, mtime: float) -> bool:
        return self._now() - mtime < self.keep_recent_s

    # ---------- 执行 ----------
    def run(self) -> CleanupResult:
        if not self.root.is_dir():
            raise FileNotFoundError(f"录制根目录不存在：{self.root}")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes 必须大于 0")

        total, candidates = self.scan()
        free = self._disk_usage(self.root).free
        total_before, free_before = total, free
        deleted: list[str] = []
        bytes_freed = 0
        skipped_recent = 0
        errors: list[str] = []

        self.log.info(
            "扫描完成：受管录像 %.2f GiB / 上限 %.2f GiB，剩余 %.2f GiB / 保底 %.2f GiB，候选 %d 个",
            total / GIB, self.max_bytes / GIB, free / GIB, self.min_free_bytes / GIB, len(candidates),
        )

        for c in candidates:
            if total <= self.max_bytes and free >= self.min_free_bytes:
                break
            if self._too_recent(c.mtime):
                skipped_recent += 1
                continue
            if any(self._too_recent(s.mtime) for s in c.sidecars):
                # 弹幕还在写，说明这个场次可能还在录；不拆散文件
                skipped_recent += 1
                continue
            if self._looks_open(c.flv):
                skipped_recent += 1
                continue

            if self.dry_run:
                self.log.info("[dry-run] 会删除 %s（%.2f GiB）", c.flv, c.size / GIB)
                deleted.append(str(c.flv))
                bytes_freed += c.size
                total -= c.size
                free += c.size
                continue

            try:
                c.flv.unlink()
            except OSError as e:
                errors.append(f"{c.flv}: {e}")
                self.log.warning("删除失败，跳过：%s（%s）", c.flv, e)
                continue

            freed = c.size
            for sc in c.sidecars:
                try:
                    sc.path.unlink()
                    freed += sc.size
                except OSError as e:
                    self.log.warning("弹幕删除失败（录像已删）：%s（%s）", sc.path, e)

            total -= freed
            bytes_freed += freed
            deleted.append(str(c.flv))
            self.log.info("已删除 %s（%.2f GiB，含弹幕 %.2f MiB）", c.flv, c.size / GIB, (freed - c.size) / (1024 ** 2))
            free = self._disk_usage(self.root).free

        total_after = total
        free_after = self._disk_usage(self.root).free
        self.log.info(
            "结束：受管录像 %.2f → %.2f GiB，剩余 %.2f → %.2f GiB，删除 %d 个 / %.2f GiB，跳过最近 %d 个",
            total_before / GIB, total_after / GIB, free_before / GIB, free_after / GIB,
            len(deleted), bytes_freed / GIB, skipped_recent,
        )
        if not self.dry_run and (total_after > self.max_bytes or free_after < self.min_free_bytes):
            self.log.warning(
                "仍未达标：受管 %.2f GiB（上限 %.2f），剩余 %.2f GiB（保底 %.2f）——可能都被占用/太新",
                total_after / GIB, self.max_bytes / GIB, free_after / GIB, self.min_free_bytes / GIB,
            )
        return CleanupResult(
            total_before=total_before,
            total_after=total_after,
            free_before=free_before,
            free_after=free_after,
            deleted=deleted,
            bytes_freed=bytes_freed,
            skipped_recent=skipped_recent,
            errors=errors,
            dry_run=self.dry_run,
            candidates=len(candidates),
        )


# ---------- 配置 / 日志 ----------
def load_config(path: Path) -> dict:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(DEFAULTS)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return data
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    cfg = dict(DEFAULTS)
    cfg.update(data)
    return cfg


def save_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子替换：常驻循环可能同一时刻在读配置，避免读到半截 JSON
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------- 状态文件（给管理台看） ----------
def _cfg_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _status_config(cfg: dict) -> dict:
    return {
        "root": str(cfg.get("root", "")),
        "enabled": _cfg_bool(cfg.get("enabled", True), True),
        "max_gb": float(cfg.get("max_gb", 0) or 0),
        "min_free_gb": float(cfg.get("min_free_gb", 0) or 0),
        "keep_recent_minutes": float(cfg.get("keep_recent_minutes", 0) or 0),
        "interval_days": int(cfg.get("interval_days", 1) or 1),
        "run_time": str(cfg.get("run_time", "03:00") or "03:00"),
        "review_wait_minutes": int(cfg.get("review_wait_minutes", 60) or 60),
        "protected_dir": str(review.protected_dir_for(cfg)),
        "dry_run": _cfg_bool(cfg.get("dry_run")),
    }


def read_status(path: Path = STATUS_FILE) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_status(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def mark_daemon_status(cfg: dict, phase: str, started_at: float | None = None,
                       error: str | None = None, path: Path = STATUS_FILE) -> None:
    st = read_status(path)
    now = int(time.time())
    st.update({"pid": os.getpid(), "updated_at": now, "phase": phase,
               "config": _status_config(cfg)})
    if started_at is not None:
        st["started_at"] = int(started_at)
    if error:
        st["last_error"] = str(error)
    try:
        write_status(path, st)
    except OSError:
        pass


def record_run_status(cfg: dict, res: CleanupResult, path: Path = STATUS_FILE) -> None:
    now = int(time.time())
    last = {
        "ts": now,
        "ok": not res.errors,
        "dry_run": res.dry_run,
        "candidates": res.candidates,
        "deleted_count": len(res.deleted),
        "deleted": res.deleted[:80],
        "bytes_freed": res.bytes_freed,
        "total_before": res.total_before,
        "total_after": res.total_after,
        "free_before": res.free_before,
        "free_after": res.free_after,
        "skipped_recent": res.skipped_recent,
        "errors": res.errors[:20],
        "max_gb": float(cfg.get("max_gb", 0) or 0),
        "min_free_gb": float(cfg.get("min_free_gb", 0) or 0),
    }
    st = read_status(path)
    st.update({"pid": os.getpid(), "updated_at": now, "phase": "idle",
               "config": _status_config(cfg), "last_run": last})
    st.pop("last_error", None)
    try:
        write_status(path, st)
    except OSError:
        pass


def run_once_recorded(cfg: dict, logger: logging.Logger,
                      dry_run_override: bool | None = None,
                      path: Path = STATUS_FILE) -> CleanupResult:
    """跑一次并把结果写进状态文件；异常也会留下 phase=error，供管理台显示。"""
    effective_dry = _cfg_bool(cfg.get("dry_run")) if dry_run_override is None else bool(dry_run_override)
    try:
        cleaner = _cleaner_from_config(cfg, logger, dry_run=effective_dry)
        res = cleaner.run()
    except Exception as e:  # noqa: BLE001
        now = int(time.time())
        st = read_status(path)
        st.update({"pid": os.getpid(), "updated_at": now, "phase": "error",
                   "config": _status_config(cfg), "last_error": "%s: %s" % (type(e).__name__, e)})
        try:
            write_status(path, st)
        except OSError:
            pass
        raise
    record_run_status(cfg, res, path)
    return res



# ---------- 待确认清理（通知 → 1 小时 → 删除/保护） ----------
def _review_entries(selected: list[Candidate]) -> list[dict]:
    return [{
        "flv": c.flv,
        "size": c.size,
        "mtime": c.mtime,
        "room": c.flv.parent.name,
        "name": c.flv.name,
        "sidecars": [(s.path, s.size) for s in c.sidecars],
    } for c in selected]


def review_summary(plan: dict | None) -> dict:
    if not plan:
        return {}
    cands = plan.get("candidates") or []
    counts: dict[str, int] = {}
    for c in cands:
        s = str(c.get("status") or "pending")
        counts[s] = counts.get(s, 0) + 1
    deleted_bytes = sum(int(c.get("size") or 0) for c in cands if c.get("status") == "deleted")
    return {
        "plan_id": plan.get("plan_id"),
        "status": plan.get("status"),
        "created_at": int(plan.get("created_at") or 0),
        "notified_at": int(plan.get("notified_at") or 0),
        "deadline_at": int(plan.get("deadline_at") or 0),
        "wait_minutes": int(plan.get("wait_minutes") or 60),
        "reason": plan.get("reason") or "",
        "candidates": len(cands),
        "pending": counts.get("pending", 0),
        "moved": counts.get("moved", 0),
        "deleted": counts.get("deleted", 0),
        "missing": counts.get("missing", 0),
        "skipped": counts.get("skipped", 0),
        "errors": list(plan.get("errors") or [])[-20:],
        "bytes_total": sum(int(c.get("size") or 0) for c in cands),
        "bytes_deleted": deleted_bytes,
    }


def record_review_status(plan: dict | None, path: Path = STATUS_FILE) -> None:
    st = read_status(path)
    now = int(time.time())
    summary = review_summary(plan)
    st.update({"pid": os.getpid(), "updated_at": now,
               "phase": "review_wait" if review.is_active(plan) else "idle",
               "review": summary})
    if plan and plan.get("status") == "done":
        st["last_review"] = summary
    try:
        write_status(path, st)
    except OSError:
        pass


_LOG_GATES: dict[tuple[str, str], int] = {}


def _log_gate(plan: dict, key: str, seconds: int, logger: logging.Logger, msg: str, *args) -> None:
    # 计划每轮都会重新从磁盘读取，节流状态必须放在进程内存里，不能写回 plan。
    gate_key = (str(plan.get("plan_id") or ""), key)
    now = int(time.time())
    if now - int(_LOG_GATES.get(gate_key) or 0) < seconds:
        return
    _LOG_GATES[gate_key] = now
    logger.info(msg, *args)


def create_review_plan(cfg: dict, logger: logging.Logger,
                       plan_path: Path = REVIEW_PLAN_FILE,
                       now: float | None = None,
                       status_path: Path = STATUS_FILE) -> dict:
    """扫描并写待确认计划；已有活动计划时不重复创建。"""
    existing = review.load_plan(plan_path)
    if review.is_active(existing):
        logger.info("已有待确认计划 %s（%s），不重复创建", existing.get("plan_id"), existing.get("status"))
        return {"created": False, "plan": existing}
    cleaner = _cleaner_from_config(cfg, logger, dry_run=False)
    sel = cleaner.plan()
    if not sel["selected"]:
        logger.info("检查完成：受管 %s / 上限 %s，剩余 %s / 保底 %s；无需清理（候选 %d，跳过最近/占用 %d）",
                    _fmt_gib(sel["total_before"]), _fmt_gib(cleaner.max_bytes),
                    _fmt_gib(sel["free_before"]), _fmt_gib(cleaner.min_free_bytes),
                    sel["candidates"], sel["skipped_recent"])
        return {"created": False, "plan": None, "selection": sel}

    wait = max(1, int(cfg.get("review_wait_minutes", 60) or 60))
    plan = review.make_plan(
        _review_entries(sel["selected"]),
        reason=sel["reason"] or "超过上限/磁盘不足",
        total_before=sel["total_before"], free_before=sel["free_before"],
        max_bytes=cleaner.max_bytes, min_free_bytes=cleaner.min_free_bytes,
        wait_minutes=wait, now=now,
    )
    review.save_plan(plan, plan_path)
    logger.info("创建待确认计划 %s：%d 个 / %s；原因：%s；等待 Bot 通知后 %d 分钟执行",
                plan["plan_id"], len(plan["candidates"]),
                _fmt_gib(sum(c["size"] for c in plan["candidates"])), plan["reason"], wait)
    record_review_status(plan, status_path)
    return {"created": True, "plan": plan}


def _move_kept(plan: dict, cfg: dict, logger: logging.Logger, now: int,
               plan_path: Path, decisions_path: Path) -> bool:
    """把用户保留的候选移到保护目录；返回是否还有移动失败。"""
    protected = review.protected_dir_for(cfg)
    keep, keep_all = review.keep_set(plan, decisions_path)
    if keep_all:
        keep = set(review.candidate_ids(plan))
    blocked = False
    for c in plan.get("candidates") or []:
        if int(c.get("id") or 0) not in keep:
            continue
        if c.get("status") in ("moved", "deleted", "missing", "skipped"):
            continue
        last = int(c.get("last_attempt_at") or 0)
        if last and now - last < 30:
            blocked = True
            continue
        c["last_attempt_at"] = now
        res = review.move_to_protected(c, protected)
        if res.get("ok"):
            c["status"] = "moved"
            c["protected_flv"] = res.get("flv") or ""
            c["protected_sidecars"] = res.get("sidecars") or []
            c["moved_at"] = now
            c.pop("error", None)
            for e in res.get("errors") or []:
                plan.setdefault("errors", []).append(e)
                logger.warning("保护移动部分失败：%s", e)
            logger.info("已移入保护目录：%s -> %s", c.get("flv"), c.get("protected_flv"))
        else:
            c["status"] = "move_error"
            msg = "；".join(res.get("errors") or ["未知错误"])
            c["error"] = msg
            plan.setdefault("errors", []).append(msg)
            logger.warning("保留文件移动失败，暂不删除：%s（%s）", c.get("flv"), msg)
            blocked = True
        review.save_plan(plan, plan_path)
    return blocked


# ---------- S2：删除前的路径安全校验 ----------
def _config_root(cfg: dict) -> Path:
    """配置里的 root 统一收敛成绝对路径（相对路径按项目根解析）。"""
    raw = str(cfg.get("root") or "").strip()
    if not raw:
        raise ValueError("请先在录播清理设置中填写录像目录")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        root = (ROOT / root).resolve()
    return root


def _normalize_plan_path(root: Path, raw: str | Path) -> tuple[Path | None, str]:
    """把 plan 里的路径收敛成 root 下的绝对候选（不 resolve、不要求存在）。

    plan 里可能存绝对路径（旧版本）或相对路径（新版本）：
    - 绝对路径：先按原样接收，之后由 safe_delete_target 校验是否落在 root 内；
    - 相对路径：禁止盘符 / 绝对 / ..，统一拼到 root 下。
    """
    text = str(raw or "").strip()
    if not text:
        return None, "空路径"
    try:
        p = Path(text)
    except (OSError, ValueError):
        return None, "路径无法解析"
    if p.drive and not p.is_absolute():
        return None, "相对路径带盘符"
    if p.is_absolute():
        return p, ""
    if any(part == ".." for part in p.parts):
        return None, "相对路径含 .."
    return Path(root) / p, ""


def safe_delete_target(root: str | Path, candidate: str | Path) -> tuple[Path | None, str]:
    """S2：校验候选确实在 root 内、且 resolve 后仍是普通文件；不满足返回 (None, 原因)。

    顺序：root resolve → 候选 resolve(strict) → is_relative_to → 非链接 → 普通文件。
    任何一步失败都只返回原因，调用方必须跳过，不得删除。返回的是 resolve 后的
    真实路径，删除时也用它，避免校验完再被替换成链接/目录。
    """
    try:
        root_r = Path(root).resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "root 无法解析"
    p = Path(candidate)
    if p.drive and not p.is_absolute():
        return None, "盘符相对路径"
    if not p.is_absolute():
        # 相对路径一律按 root 解释，绝不能按进程 CWD 解释
        p = root_r / p
    try:
        real = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "文件不存在或无法解析"
    try:
        if not real.is_relative_to(root_r):
            return None, "解析后不在 root 内"
    except ValueError:
        return None, "路径与 root 不同盘"
    if real == root_r:
        return None, "路径就是 root 目录"
    try:
        st = real.lstat()
    except OSError:
        return None, "无法读取文件属性"
    if stat.S_ISLNK(st.st_mode):
        return None, "是符号链接/目录联接"
    if not stat.S_ISREG(st.st_mode):
        return None, "不是普通文件"
    # Windows 的目录联接会被 resolve 成目标，这里再查原始路径本身，双保险
    try:
        if p.is_symlink():
            return None, "是符号链接/目录联接"
    except OSError:
        return None, "无法读取文件属性"
    return real, ""


def _skip_unsafe(c: dict, logger: logging.Logger, raw: str, why: str, now: int) -> None:
    """安全校验失败：标记 skipped 并记日志，绝不删除。"""
    c["status"] = "skipped"
    c["finished_at"] = now
    c["error"] = "路径安全校验未通过：%s" % why
    logger.warning("跳过不安全候选（不删除）：%s（%s）", raw or "<空>", why)


def _delete_candidate(c: dict, logger: logging.Logger, now: int, root: str | Path) -> None:
    raw = str(c.get("flv") or "")
    cand, why = _normalize_plan_path(Path(root), raw)
    if cand is None:
        _skip_unsafe(c, logger, raw, why, now)
        return
    if not cand.exists():
        c["status"] = "missing"
        c["finished_at"] = now
        return
    flv, why = safe_delete_target(root, cand)
    if flv is None:
        _skip_unsafe(c, logger, raw, why, now)
        return
    try:
        fd = os.open(str(flv), os.O_RDWR)
    except OSError as e:
        c["status"] = "delete_error"
        c["attempts"] = int(c.get("attempts") or 0) + 1
        c["error"] = "文件被占用或不可写：%s" % e
        logger.warning("跳过被占用文件：%s（%s）", flv, e)
        return
    else:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        flv.unlink()
    except OSError as e:
        c["status"] = "delete_error"
        c["attempts"] = int(c.get("attempts") or 0) + 1
        c["error"] = "删除失败：%s" % e
        logger.warning("删除失败：%s（%s）", flv, e)
        return

    freed = int(c.get("size") or 0)
    for sc in c.get("sidecars") or []:
        raw_sc = str(sc.get("path") or "")
        sc_cand, why = _normalize_plan_path(Path(root), raw_sc)
        if sc_cand is None:
            logger.warning("跳过不安全的弹幕路径（不删除）：%s（%s）", raw_sc or "<空>", why)
            continue
        if not sc_cand.exists():
            continue
        sp, why = safe_delete_target(root, sc_cand)
        if sp is None:
            logger.warning("跳过不安全的弹幕路径（不删除）：%s（%s）", raw_sc, why)
            continue
        try:
            st = sp.stat()
            if time.time() - st.st_mtime < 1800:
                logger.warning("弹幕仍在写入，保留：%s", sp)
                continue
            sp.unlink()
            freed += int(sc.get("size") or 0)
        except OSError as e:
            logger.warning("弹幕删除失败（录像已删）：%s（%s）", sp, e)
    c["status"] = "deleted"
    c["deleted_at"] = now
    c["freed"] = freed
    logger.info("已删除 %s（%.2f GiB）", flv, freed / GIB)


def process_review(cfg: dict, logger: logging.Logger,
                   plan_path: Path = REVIEW_PLAN_FILE,
                   decisions_path: Path = REVIEW_DECISIONS_FILE,
                   notify_path: Path = REVIEW_NOTIFY_FILE,
                   now: float | None = None,
                   status_path: Path = STATUS_FILE,
                   archive_dir: Path = review.ARCHIVE_DIR) -> dict | None:
    """推进当前计划：接收通知结果、移动保留文件、到点删除。"""
    now_i = int(now if now is not None else time.time())
    root = _config_root(cfg)
    plan = review.load_plan(plan_path)
    if not plan or not review.is_active(plan):
        return plan
    keep, keep_all = review.keep_set(plan, decisions_path)
    if keep_all:
        keep = set(review.candidate_ids(plan))

    _move_kept(plan, cfg, logger, now_i, plan_path, decisions_path)

    if plan.get("status") == "awaiting_notify":
        ns = review.load_notify(notify_path)
        if ns.get("plan_id") == plan.get("plan_id") and ns.get("status") == "sent":
            sent_at = int(ns.get("sent_at") or now_i)
            wait = max(1, int(plan.get("wait_minutes") or 60))
            plan["status"] = "notified"
            plan["notified_at"] = sent_at
            plan["deadline_at"] = sent_at + wait * 60
            plan["notify_message_id"] = ns.get("message_id") or ""
            plan.pop("last_notify_error", None)
            review.save_plan(plan, plan_path)
            logger.info("Bot 已发送清理通知（%s）；%d 分钟后执行，截止 %s",
                        plan.get("notify_message_id") or "无 id", wait,
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(plan["deadline_at"])))
            record_review_status(plan, status_path)
        elif ns.get("plan_id") == plan.get("plan_id") and ns.get("status") == "failed":
            err = str(ns.get("last_error") or "未知错误")
            if plan.get("last_notify_error") != err:
                plan["last_notify_error"] = err
                review.save_plan(plan, plan_path)
            _log_gate(plan, "notify_failed", 120, logger,
                      "等待 Bot 通知失败：%s；不会开始倒计时，也不会删除", err)
        else:
            _log_gate(plan, "notify_wait", 120, logger,
                      "计划 %s 待 Bot 发送通知；未确认发送前不会删除", plan.get("plan_id"))

    if plan.get("status") in ("notified", "executing") and now_i >= int(plan.get("deadline_at") or 0):
        plan["status"] = "executing"
        review.save_plan(plan, plan_path)
        logger.info("确认时间到，开始执行计划 %s", plan.get("plan_id"))
        for c in plan.get("candidates") or []:
            if c.get("status") not in ("pending", "delete_error"):
                continue
            if int(c.get("id") or 0) in keep:
                # 保留但还没移动成功：绝不删除，留给下一轮重试
                continue
            if int(c.get("attempts") or 0) >= 3:
                c["status"] = "skipped"
                c["finished_at"] = now_i
                review.save_plan(plan, plan_path)
                continue
            _delete_candidate(c, logger, now_i, root)
            review.save_plan(plan, plan_path)

    # 收尾判断：保留项移动失败时保持 executing，绝不把待保护文件留在可删状态
    pending = [c for c in plan.get("candidates") or []
               if c.get("status") in ("pending", "delete_error")]
    move_errors = [c for c in plan.get("candidates") or []
                   if c.get("status") == "move_error"
                   and int(c.get("id") or 0) in keep]
    if plan.get("status") in ("notified", "executing") and not pending and not move_errors:
        plan["status"] = "done"
        plan["completed_at"] = now_i
        review.save_plan(plan, plan_path)
        review.archive_plan(plan, archive_dir)
        s = review_summary(plan)
        logger.info("计划 %s 完成：删除 %d 个 / 保护 %d 个，释放 %.2f GiB",
                    plan.get("plan_id"), s["deleted"], s["moved"], s["bytes_deleted"] / GIB)
        record_review_status(plan, status_path)
    elif plan.get("status") == "executing":
        review.save_plan(plan, plan_path)
        record_review_status(plan, status_path)
    return plan


# ---------- M13：停止标记有效期 ----------
# 旧逻辑只看文件是否存在：上次异常退出（或管理台连点两次）留下的旧标记会让
# 新循环启动即退出。现在标记里带写入时间，读取时 mtime + 内容双重校验，
# 超过 TTL 一律视为过期并清理。
STOP_FILE_TTL_S = 30 * 60


def _parse_stop_ts(text: str) -> float | None:
    """解析停止标记里的时间：兼容 JSON、epoch 数字、旧版本地时间字符串。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict):
        for key in ("ts", "time", "at_ts"):
            try:
                return float(obj[key])
            except (KeyError, TypeError, ValueError):
                continue
        text = str(obj.get("at") or "")
    try:
        v = float(text)
        return v if v > 0 else None
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    return None


def write_stop_request(path: Path = STOP_FILE, ttl_s: int = STOP_FILE_TTL_S) -> None:
    """写停止标记：带写入时间与 TTL，读取端据此判断是否过期。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": int(time.time()),
        "ttl_s": int(ttl_s),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _stop_age(path: Path) -> float | None:
    """停止标记已存在的时长（秒）；读不到返回 None。"""
    try:
        st = path.stat()
    except OSError:
        return None
    age = max(0.0, time.time() - st.st_mtime)
    try:
        ts = _parse_stop_ts(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        ts = None
    if ts:
        age = max(age, time.time() - ts)          # 防止 mtime 被外部 touch 刷新
    return age


def stop_requested(stop_file: Path = STOP_FILE, logger: logging.Logger | None = None,
                   ttl_s: int = STOP_FILE_TTL_S) -> bool:
    """M13：是否存在「未过期」的停止请求；过期文件顺手清理并返回 False。"""
    path = Path(stop_file)
    age = _stop_age(path)
    if age is None:
        return False
    if age > ttl_s:
        try:
            path.unlink()
        except OSError:
            pass
        if logger is not None:
            logger.warning("忽略过期停止标记（%.0f 分钟前写入，TTL %d 分钟）：%s",
                           age / 60.0, ttl_s // 60, path)
        return False
    return True


def clear_stop_request(stop_file: Path = STOP_FILE) -> None:
    try:
        Path(stop_file).unlink()
    except OSError:
        pass


def _wait_interval(seconds: float, logger: logging.Logger,
                   stop_file: Path = STOP_FILE) -> bool:
    """可被 stop 文件打断的等待；每秒看一下，管理台点停止能很快生效。"""
    end = time.monotonic() + max(0.0, float(seconds))
    while True:
        if stop_requested(stop_file, logger):
            clear_stop_request(stop_file)
            logger.info("收到停止请求，退出循环")
            return False
        remaining = end - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(1.0, remaining))


def build_logger(log_file: Path, verbose: bool = False) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("recorder_cleanup")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.DEBUG if verbose else logging.INFO)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


# ---------- 单实例锁 ----------
def lock_info_path(path: Path) -> Path:
    """锁文件本体只做互斥；PID/角色写到旁边的 .info，避免 Windows 字节锁挡住读取。"""
    return Path(str(path) + ".info")


# ---------- M13：PID 复用防护 ----------
# 本脚本的所有停止动作都只写标记、不 kill；下面这些函数只回答「能否确认是目标
# 进程」，任何拿不准的情况都返回 False，调用方只记日志。
def process_start_time(pid: int) -> float | None:
    """进程创建时间（epoch 秒）；psutil 优先，Windows 用 ctypes，拿不到返回 None。"""
    if not pid or pid <= 0:
        return None
    try:
        import psutil  # 可选依赖：requirements 里是注释项，缺失自动降级
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return float(psutil.Process(pid).create_time())
        except Exception:                        # noqa: BLE001 进程不存在/无权限
            return None
    if os.name != "nt":
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
                fields = f.read().split()
            start_ticks = float(fields[21])
            with open("/proc/uptime", "r", encoding="utf-8") as f:
                uptime = float(f.read().split()[0])
            return time.time() - uptime + start_ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError, AttributeError):
            return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_t = wintypes.FILETIME()
        kernel_t = wintypes.FILETIME()
        user_t = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                        ctypes.byref(kernel_t), ctypes.byref(user_t)):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        # FILETIME 是 1601-01-01 起的 100ns 计数
        return ticks / 10_000_000.0 - 11644473600.0
    finally:
        kernel32.CloseHandle(handle)


def process_cmdline(pid: int) -> str | None:
    """进程命令行；只有 psutil 在时才查。拿不到返回 None（调用方按「不确定」处理）。"""
    if not pid or pid <= 0:
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        return " ".join(psutil.Process(pid).cmdline())
    except Exception:                            # noqa: BLE001
        return None


def is_recorder_cleanup_process(pid: int, expected_start: float | None = None,
                                marker: str = "recorder_cleanup") -> bool:
    """确认 pid 就是本清理进程；无法确认一律返回 False（调用方只记日志，不 kill）。"""
    if not pid or pid <= 0:
        return False
    start = process_start_time(pid)
    if start is None:
        return False                              # 创建时间都拿不到：保守否决
    start_ok = False
    if expected_start:
        start_ok = abs(start - float(expected_start)) <= 5.0
        if not start_ok:
            return False                          # 创建时间对不上：PID 已被复用
    cmd = process_cmdline(pid)
    if cmd is None:
        # 命令行不可得时，只有「创建时间对得上」才认；没记录创建时间则否决
        return start_ok
    return marker in cmd or "recorder_cleanup.py" in cmd


def read_lock_info(path: Path = LOCK_FILE) -> dict:
    """读锁旁 .info（优先）或锁文件里的 "pid role [start]"；读不到返回 {}。"""
    for candidate in (lock_info_path(path), Path(path)):
        try:
            text = candidate.read_text(encoding="ascii", errors="ignore").strip()
        except OSError:
            continue
        if not text:
            continue
        parts = text.split()
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        info = {"pid": pid, "role": parts[1] if len(parts) > 1 else "",
                "start_time": None, "source": str(candidate)}
        if len(parts) > 2:
            try:
                info["start_time"] = float(parts[2])
            except ValueError:
                pass
        return info
    return {}


def acquire_lock(path: Path, role: str = "daemon") -> int | None:
    """跨进程互斥：自启循环和手动 --once 不会同时删。拿不到返回 None。

    锁文件里同时写 PID 和角色（daemon / once / manual），管理台据此区分
    「守护在跑」和「控制台里刚跑完一次手动清理」，避免把控制台自己当成守护。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pid = os.getpid()
        start = process_start_time(pid)
        # 第三段是进程创建时间，供管理台/--stop 识别 PID 复用；拿不到就不写
        line = f"{pid} {role} {start:.3f}\n" if start else f"{pid} {role}\n"
        os.ftruncate(fd, 0)
        os.write(fd, line.encode("ascii"))
        # Windows 的字节锁会让别的进程读不了锁文件本身，PID/角色单独写 info
        info = lock_info_path(path)
        tmp = info.with_name(info.name + ".tmp")
        try:
            tmp.write_text(line, encoding="ascii")
            os.replace(tmp, info)
        except OSError:
            pass
        return fd
    except OSError:
        os.close(fd)
        return None


def release_lock(fd: int, info_path: Path | None = None) -> None:
    # 清掉锁文件里的旧 PID，避免停止后管理台把残留内容当成 stale lock
    try:
        os.ftruncate(fd, 0)
    except OSError:
        pass
    if info_path is not None:
        try:
            Path(info_path).unlink()
        except OSError:
            pass
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _cleaner_from_config(cfg: dict, logger: logging.Logger, dry_run: bool) -> Cleaner:
    root = _config_root(cfg)
    suffixes = tuple(str(x) for x in cfg.get("sidecar_suffixes") or [".xml"])
    return Cleaner(
        root=root,
        max_bytes=int(float(cfg["max_gb"]) * GIB),
        min_free_bytes=int(float(cfg.get("min_free_gb", 0)) * GIB),
        keep_recent_s=float(cfg.get("keep_recent_minutes", 30)) * 60,
        managed_dir_pattern=str(cfg.get("managed_dir_pattern", r"^\d+-")),
        sidecar_suffixes=suffixes,
        logger=logger,
        dry_run=dry_run,
    )


def _fmt_gib(n: int) -> str:
    return f"{n / GIB:.2f} GiB"


def _configure_stdio() -> None:
    # Windows 管道默认用 GBK，统一成 UTF-8，日志和外部采集都不乱码
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="录播姬录制目录自清理")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--once", action="store_true", help="检查一次；非 dry-run 时只创建待确认计划，不直接删")
    parser.add_argument("--loop", action="store_true", help="常驻循环（按天+时间调度）")
    parser.add_argument("--stop", action="store_true", help="给常驻循环写停止标记后退出")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不创建计划、不删除")
    parser.add_argument("--max-gb", type=float, help="临时覆盖上限，不写回配置")
    parser.add_argument("--set-max-gb", type=float, help="把上限写回配置文件后退出")
    parser.add_argument("--show", action="store_true", help="只显示当前占用/上限")
    parser.add_argument("--verbose", action="store_true")
    return parser


def _handle_stop() -> int:
    """--stop：写停止标记；只有确认是本清理进程时才提示它会在跑完后退出。"""
    write_stop_request(STOP_FILE)
    print(f"已写入停止标记：{STOP_FILE}")
    info = read_lock_info(LOCK_FILE)
    if info.get("pid"):
        confirmed = is_recorder_cleanup_process(info["pid"], info.get("start_time"))
        if confirmed:
            print(f"检测到清理进程 PID {info['pid']}（角色 {info.get('role') or '未知'}），"
                  "等待其自行退出")
        else:
            print(f"锁文件记录的 PID {info['pid']} 无法确认为本清理进程（可能已被复用），"
                  "不发送任何终止信号，仅写停止标记")
    return 0


def _resolve_config_path(args: argparse.Namespace) -> Path:
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (ROOT / config_path).resolve()
    return config_path


def _load_config_or_none(config_path: Path) -> dict | None:
    try:
        return load_config(config_path)
    except Exception as e:  # noqa: BLE001
        print(f"读取配置失败：{e}", file=sys.stderr)
        return None


def _apply_set_max_gb(args: argparse.Namespace, cfg: dict,
                      config_path: Path) -> int | None:
    """--set-max-gb：写回配置并返回退出码；没带这个参数时返回 None。"""
    if args.set_max_gb is None:
        return None
    if args.set_max_gb <= 0:
        print("--set-max-gb 必须大于 0", file=sys.stderr)
        return 1
    cfg["max_gb"] = float(args.set_max_gb)
    save_config(config_path, cfg)
    print(f"已写入 {config_path}：max_gb={cfg['max_gb']}")
    return 0


def _build_logger(cfg: dict, args: argparse.Namespace) -> logging.Logger:
    log_path = Path(str(cfg.get("log_file") or DEFAULTS["log_file"]))
    if not log_path.is_absolute():
        log_path = (ROOT / log_path).resolve()
    return build_logger(log_path, verbose=args.verbose)


def _show_status(cfg: dict, logger: logging.Logger) -> int:
    """--show：打印占用/上限/调度；扫描失败返回 1。"""
    try:
        c = _cleaner_from_config(cfg, logger, dry_run=True)
        total, candidates = c.scan()
        free = shutil.disk_usage(c.root).free
        days = max(1, int(cfg.get("interval_days", 1) or 1))
        run_time = review.parse_run_time(cfg.get("run_time", "03:00"))
        st = review.ensure_schedule(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"扫描失败：{e}", file=sys.stderr)
        return 1
    print(f"录制根目录：{c.root}")
    print(f"受管录像：{_fmt_gib(total)}（{len(candidates)} 个 .flv）")
    print(f"上限：{_fmt_gib(c.max_bytes)}；磁盘剩余：{_fmt_gib(free)}；保底：{_fmt_gib(c.min_free_bytes)}")
    print(f"自动清理：{'启用' if _cfg_bool(cfg.get('enabled', True), True) else '停用'}；"
          f"每 {days} 天 {run_time}；下次 {time.strftime('%Y-%m-%d %H:%M', time.localtime(int(st['next_run_at'])))}")
    protected = review.protected_dir_for(cfg) if cfg.get("root") else "尚未设置录像目录"
    print(f"保护目录：{protected}；确认等待 {int(cfg.get('review_wait_minutes', 60) or 60)} 分钟")
    return 0


def _run_once_cli(config_path: Path, args: argparse.Namespace,
                  logger: logging.Logger) -> int:
    """--once：跑一轮检查；dry-run 只统计，否则创建待确认计划。"""
    try:
        # 每轮重读配置，管理台改完不用重启循环
        current = load_config(config_path)
        if args.max_gb is not None:
            current["max_gb"] = args.max_gb
        effective_dry = bool(args.dry_run or _cfg_bool(current.get("dry_run")))
        if effective_dry:
            res = run_once_recorded(current, logger, dry_run_override=True)
            logger.info("[dry-run] 共会删除 %d 个 / %s", len(res.deleted), _fmt_gib(res.bytes_freed))
            return 0
        out = create_review_plan(current, logger)
        if out.get("created"):
            logger.info("待确认计划已创建；请确认 Bot 通知已发出，1 小时内可回复保留")
        elif out.get("plan"):
            logger.info("已有待确认计划，未重复创建")
        else:
            logger.info("无需清理")
    except Exception as e:  # noqa: BLE001
        logger.exception("清理检查失败：%s", e)
        return 1
    return 0


def _run_due_check(current: dict, logger: logging.Logger, dry: bool,
                   due: bool, sched: dict) -> None:
    """到点（或管理台手动触发）时创建计划 / dry-run 统计；成功才推进调度。"""
    handled = False
    if dry:
        try:
            res = run_once_recorded(current, logger, dry_run_override=True)
            logger.info("[dry-run] 共会删除 %d 个 / %s；未创建计划、未通知",
                        len(res.deleted), _fmt_gib(res.bytes_freed))
            handled = True
        except Exception as e:  # noqa: BLE001
            logger.exception("dry-run 检查失败：%s", e)
    else:
        try:
            create_review_plan(current, logger)
            handled = True
        except Exception as e:  # noqa: BLE001
            logger.exception("创建待确认计划失败：%s", e)
    # 检查失败不推进调度，30 秒后会重试，避免整轮被跳过
    if due and handled:
        try:
            review.advance_schedule(sched)
            new_st = review.load_schedule()
            logger.info("下次检查：%s",
                        time.strftime("%Y-%m-%d %H:%M",
                                      time.localtime(int(new_st.get("next_run_at") or 0))))
        except Exception as e:  # noqa: BLE001
            logger.error("推进调度失败：%s", e)


def _run_loop(args: argparse.Namespace, cfg: dict, logger: logging.Logger,
              config_path: Path) -> int:
    """--loop：常驻循环（按天+时间调度），退出时写回状态。"""
    # 上一轮停止留下的标记不应拦住这次新启动（过期/残留一并清掉）
    clear_stop_request(STOP_FILE)
    started_at = time.time()
    mark_daemon_status(cfg, "running", started_at=started_at)
    if not _cfg_bool(cfg.get("enabled", True), True):
        logger.info("自动清理已停用（enabled=false），不进入循环")
        mark_daemon_status(cfg, "stopped")
        return 0
    try:
        st = review.ensure_schedule(cfg)
        days = max(1, int(st.get("interval_days") or 1))
        run_time = str(st.get("run_time") or "03:00")
    except Exception as e:  # noqa: BLE001
        logger.error("调度配置无效：%s", e)
        mark_daemon_status(cfg, "error", error=str(e))
        return 1
    logger.info("进入循环模式：每 %d 天 %s 检查一次；下次 %s；保护目录 %s",
                days, run_time,
                time.strftime("%Y-%m-%d %H:%M", time.localtime(int(st["next_run_at"]))),
                review.protected_dir_for(cfg))

    try:
        while True:
            if stop_requested(STOP_FILE, logger):
                clear_stop_request(STOP_FILE)
                logger.info("收到停止请求，退出循环")
                break
            try:
                current = load_config(config_path)
            except Exception as e:  # noqa: BLE001
                logger.error("读取配置失败：%s", e)
                if not _wait_interval(30, logger):
                    break
                continue
            if not str(current.get("root") or "").strip() or not _cfg_bool(current.get("enabled", True), True):
                logger.info("配置已停用自动清理，退出循环")
                break

            dry = bool(args.dry_run or _cfg_bool(current.get("dry_run")))
            plan = review.load_plan()
            if review.is_active(plan) and dry:
                _log_gate(plan, "dry_run_wait", 120, logger,
                          "dry-run：计划 %s 暂停推进，不会移动或删除", plan.get("plan_id"))
            elif review.is_active(plan):
                try:
                    process_review(current, logger)
                except Exception as e:  # noqa: BLE001
                    logger.exception("推进待确认计划失败：%s", e)
                    record_review_status(plan)
            plan = review.load_plan()

            manual = review.trigger_requested()
            if manual:
                review.clear_trigger()
                logger.info("收到管理台「立即检查」请求")
            try:
                due, sched = review.schedule_due(current)
            except Exception as e:  # noqa: BLE001
                logger.error("调度计算失败：%s", e)
                due, sched = False, {}
            if (due or manual) and not review.is_active(plan):
                _run_due_check(current, logger, dry, due, sched)

            plan = review.load_plan()
            wait = 1.0 if review.is_active(plan) else 30.0
            if not _wait_interval(wait, logger):
                break
    finally:
        try:
            end_cfg = load_config(config_path)
        except Exception:  # noqa: BLE001
            end_cfg = cfg
        mark_daemon_status(end_cfg, "stopped")
        record_review_status(review.load_plan())
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = _build_parser().parse_args(argv)
    if args.stop:
        return _handle_stop()
    config_path = _resolve_config_path(args)
    cfg = _load_config_or_none(config_path)
    if cfg is None:
        return 1
    rc = _apply_set_max_gb(args, cfg, config_path)
    if rc is not None:
        return rc
    logger = _build_logger(cfg, args)
    if args.show:
        return _show_status(cfg, logger)
    if not str(cfg.get("root") or "").strip():
        logger.error("请先在录播清理设置中填写录像目录")
        return 1
    lock_fd = acquire_lock(LOCK_FILE, "daemon" if args.loop else "once")
    if lock_fd is None:
        logger.warning("另一个清理实例正在运行，跳过本次")
        return 0
    try:
        if args.loop:
            return _run_loop(args, cfg, logger, config_path)
        return _run_once_cli(config_path, args, logger)
    finally:
        release_lock(lock_fd, lock_info_path(LOCK_FILE))



if __name__ == "__main__":
    raise SystemExit(main())
