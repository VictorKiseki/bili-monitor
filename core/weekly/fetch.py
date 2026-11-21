"""取图与图片缓存：下载（白名单+20MB 上限）、落盘路径、缓存统计与清理。"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse
import httpx
from shared import proxy as proxy_cfg
SCHEDULE_DIR = Path("data/schedules")

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}

# 候选图缓存清理（2026-11-07）：判定完的图没人再看，留着会把 data/schedules 撑到几百 MB。
# 规则——待判定的永远留；已判定（done/skipped/error）满 keep_days 天删；单场日程还指着的
# 图留到那一场结束；没有任何行引用的孤儿按文件时间删。keep_days 默认 0（判定完下一轮就清），
# 想给人工改判留缓冲就在 meta 里写 weekly_cache_keep_days=3。
CACHE_KEEP_DAYS = 0.0

_IMG_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")

_CACHE_SKIP_DIRS = {"inbox"}

# 候选图下载的安全边界（S3，2026-11-07）：这些 url 来自 B 站动态 JSON，等于半个不可信输入，
# 不能让一段脏数据把本机变成任意 URL 下载器。只放行 B 站图床/主站的 http(s) 地址，
# 并按「精确域或子域」判定（host.endswith("." + 域)），绝不做 substring 匹配——
# 否则 evil-hdslb.com 这种域名能混过去。
DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024      # 硬上限 20MB，超了直接判失败

_IMG_HOSTS = ("hdslb.com", "bilibili.com", "hdslb.cn")

def _img_host_ok(host: str) -> bool:
    """域名白名单：精确等于白名单域，或是它的子域。"""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    return any(h == d or h.endswith("." + d) for d in _IMG_HOSTS)

def _img_url_ok(url: str) -> bool:
    """只认 http(s) + 白名单域的图片地址；解析失败一律不放行。"""
    try:
        u = urlparse(str(url or ""))
    except ValueError:
        return False
    if u.username or u.password:          # user@host 会把真实主机藏在视觉噪声后面
        return False
    return u.scheme.lower() in ("http", "https") and _img_host_ok(u.hostname or "")

_HTTP: httpx.Client | None = None
_HTTP_REV = None

def _http() -> httpx.Client:
    """复用一个连接池：每张图新建 Client 会把 TLS 握手成本乘上图数。

    2026-11-20：代理配置改了（config/proxy.json 指纹变）就重建连接池，
    免得这个模块级单例一直带着启动时的旧代理（代理软件崩了没起来时会连死端口）。
    """
    global _HTTP, _HTTP_REV
    rev = proxy_cfg.revision()
    if _HTTP is not None and rev != _HTTP_REV:
        try:
            _HTTP.close()
        except Exception:                    # noqa: BLE001
            pass
        _HTTP = None
    if _HTTP is None:
        _HTTP = httpx.Client(timeout=20.0, follow_redirects=True, headers=UA)
        _HTTP_REV = rev
    return _HTTP

def _download(url: str, dest: Path) -> bool:
    """按 url 把候选图落盘；返回是否成功（签名与返回类型保持不变）。

    安全边界（S3）：scheme/host 白名单、流式读取 + 20MB 硬上限、Content-Type 必须以
    image/ 开头；跳转后的最终地址还要再校验一次，防止合法域名 302 到别处。
    协议层面的问题（非图片、超限、域名不对）不重试，网络抖动才重试一次。
    """
    if dest.exists() and dest.stat().st_size > 0:
        return True
    if not _img_url_ok(url):
        return False                        # 不认识的地址：宁可不落盘，也不按脏 JSON 取数据
    dest.parent.mkdir(parents=True, exist_ok=True)
    ext = Path(url.split("?", 1)[0]).suffix.lower()
    if ext not in _IMG_SUFFIXES:
        ext = ".jpg"
    tmp = dest.with_suffix(ext + ".part")
    for attempt in range(2):
        wrote = 0
        try:
            with _http().stream("GET", url) as r:
                r.raise_for_status()
                if not _img_url_ok(str(r.url)):
                    raise ValueError("跳转后的地址不在白名单内")
                ctype = (r.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                if not ctype.startswith("image/"):
                    raise ValueError("响应不是图片：%s" % (ctype or "空 Content-Type"))
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(65536):
                        wrote += len(chunk)
                        if wrote > DOWNLOAD_MAX_BYTES:
                            raise ValueError("图片超过 %dMB 上限" % (DOWNLOAD_MAX_BYTES // 1048576))
                        f.write(chunk)
            if not wrote:
                raise ValueError("空响应")
            tmp.replace(dest.with_suffix(ext))
            return True
        except ValueError:
            # 协议/安全层面的失败：重试也没用，清掉半截文件直接放弃
            try:
                tmp.unlink()
            except OSError:
                pass
            return False
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            if attempt == 1:
                return False
            time.sleep(0.5)
    return False

# ---------------- 候选图缓存清理（2026-11-07）----------------
def _img_ext(url: str) -> str:
    ext = Path(str(url or "").split("?", 1)[0]).suffix.lower()
    return ext if ext in _IMG_SUFFIXES else ".jpg"

def cache_dest(uid: int, dyn_id: str, i: int, url: str) -> Path:
    """候选图落盘位置：扫描和「判定后重下」共用同一套命名，别各写一份。"""
    return SCHEDULE_DIR / str(uid) / ("%s_%d%s" % (dyn_id, i, _img_ext(url)))

def _cache_keep_days(conn: sqlite3.Connection) -> float:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k='weekly_cache_keep_days'").fetchone()
        if row and str(row[0] or "").strip():
            return max(0.0, min(float(row[0]), 90.0))
    except (sqlite3.Error, TypeError, ValueError):
        pass
    return CACHE_KEEP_DAYS

def _file_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0

def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0

def _cache_files(base: Path, cutoff: float | None = None,
                 refs: set[str] | None = None) -> list[Path]:
    """列出缓存里的候选图。

    cutoff 给定时只收「mtime 早于 cutoff」的文件（refs 里点名的照收）：比 cutoff 新的图
    这一轮本来就会按「近期保留」跳过，没必要为它们遍历整棵树（M3，2026-11-07）。
    返回结构不变（list[Path]）；cache_stats 不传 cutoff，仍是全量统计。
    """
    if not base.is_dir():
        return []
    out: list[Path] = []
    stack = [base]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in _CACHE_SKIP_DIRS:
                        continue
                    stack.append(Path(e.path))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                if Path(e.name).suffix.lower() not in _IMG_SUFFIXES:
                    continue
                p = Path(e.path)
                if cutoff is not None:
                    if refs and os.path.abspath(str(p)) in refs:
                        out.append(p)
                        continue
                    if e.stat(follow_symlinks=False).st_mtime >= cutoff:
                        continue
                out.append(p)
            except OSError:
                continue
    return out

def cache_stats(root=None) -> dict:
    """本地候选图缓存有多大（给管理台和自检看）。"""
    base = Path(root) if root else SCHEDULE_DIR
    files = _cache_files(base)
    size = sum(_file_size(p) for p in files)
    return {"files": len(files), "bytes": size, "mb": round(size / 1048576.0, 1)}

def ensure_images(conn: sqlite3.Connection, event_id: str, download: bool = True) -> list[dict]:
    """候选图还在不在？不在就按 url 重下一遍（清理之后人工改判用）。"""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT uid, dyn_id, images_json FROM weekly_candidate"
                       " WHERE event_id=?", (event_id,)).fetchone()
    if not row:
        return []
    images = json.loads(row["images_json"] or "[]")
    changed = False
    for i, im in enumerate(images):
        if not isinstance(im, dict):
            continue
        url = str(im.get("url") or "")
        if not url:
            continue
        path = im.get("path")
        if path and Path(str(path)).is_file():
            continue
        dest = cache_dest(int(row["uid"]), row["dyn_id"] or "", i, url)
        if download and _download(url, dest):
            im["path"] = str(dest)
            changed = True
        elif path is not None:
            im["path"] = None
            changed = True
    if changed:
        conn.execute("UPDATE weekly_candidate SET images_json=? WHERE event_id=?",
                     (json.dumps(images, ensure_ascii=False), event_id))
        conn.commit()
    return images

def _cache_candidate_paths(conn: sqlite3.Connection) -> dict[str, dict]:
    """库里每个候选图：绝对路径 -> {event_id, status, ts}。读不到表就当没有。"""
    out: dict[str, dict] = {}
    try:
        rows = conn.execute("SELECT event_id, status, pub_ts, decided_at, images_json"
                            " FROM weekly_candidate WHERE images_json <> '[]'").fetchall()
    except sqlite3.Error:
        rows = []
    for r in rows:
        ts = float(r["decided_at"] or r["pub_ts"] or 0)
        for im in json.loads(r["images_json"] or "[]"):
            p = (im or {}).get("path") if isinstance(im, dict) else None
            if p:
                out[os.path.abspath(str(p))] = {
                    "event_id": r["event_id"], "status": r["status"], "ts": ts}
    return out


def _cache_session_refs(conn: sqlite3.Connection, today, keep: float) -> set[str]:
    """单场日程还指着的图（那一场还没过去），绝对路径集合。"""
    refs: set[str] = set()
    try:
        for r in conn.execute("SELECT session_id, date, image_path FROM weekly_session"
                              " WHERE image_path IS NOT NULL AND image_path <> ''").fetchall():
            try:
                day = dt.date.fromisoformat(str(r["date"] or ""))
            except ValueError:
                day = None
            if day is None or day >= today - dt.timedelta(days=int(keep)):
                refs.add(os.path.abspath(str(r["image_path"])))
    except sqlite3.Error:
        pass
    return refs


def _purge_files(base: Path, *, cutoff, cand_paths, sess_refs, keep_s: float,
                 now: float, dry_run: bool, stat: dict) -> set[str]:
    """扫待清理范围、按规则删图，返回有图被删掉的 event_id 集合。"""
    touched: set[str] = set()
    refs = set(cand_paths) | sess_refs
    for p in sorted(_cache_files(base, cutoff=cutoff, refs=refs)):
        stat["scanned"] += 1
        key = os.path.abspath(str(p))
        ref = cand_paths.get(key)
        if ref and ref["status"] == "pending":
            stat["kept_pending"] += 1
            continue
        if key in sess_refs:
            stat["kept_ref"] += 1
            continue
        when = ref["ts"] if ref else _file_mtime(p)
        if keep_s > 0 and when and (now - when) < keep_s:
            stat["kept_recent"] += 1
            continue
        size = _file_size(p)
        if dry_run:
            stat["removed"] += 1
            stat["bytes"] += size
            continue
        try:
            p.unlink()
        except OSError:
            stat["failed"] += 1
            continue
        stat["removed"] += 1
        stat["bytes"] += size
        if ref:
            touched.add(ref["event_id"])
    return touched


def _purge_clear_refs(conn: sqlite3.Connection, touched: set[str], stat: dict) -> None:
    """把已删图的 path 置空（url 保留），并清掉指向不存在文件的日程封面。"""
    for eid in sorted(touched):
        row = conn.execute("SELECT images_json FROM weekly_candidate WHERE event_id=?",
                           (eid,)).fetchone()
        images = json.loads((row["images_json"] if row else "[]") or "[]")
        hit = False
        for im in images:
            path = (im or {}).get("path") if isinstance(im, dict) else None
            if path and not Path(str(path)).is_file():
                im["path"] = None
                hit = True
        if hit:
            conn.execute("UPDATE weekly_candidate SET images_json=? WHERE event_id=?",
                         (json.dumps(images, ensure_ascii=False), eid))
            stat["cleared_candidates"] += 1
    if touched:
        conn.commit()
    for r in conn.execute("SELECT session_id, image_path FROM weekly_session"
                          " WHERE image_path IS NOT NULL AND image_path <> ''").fetchall():
        if not Path(str(r["image_path"])).is_file():
            conn.execute("UPDATE weekly_session SET image_path=NULL WHERE session_id=?",
                         (r["session_id"],))
            stat["cleared_sessions"] += 1
    if stat["cleared_sessions"]:
        conn.commit()


def _purge_dirs(base: Path, stat: dict) -> None:
    """删完图后顺手移除空日期目录（inbox 等特殊目录不碰）。"""
    for d in sorted(base.iterdir()):
        if d.is_dir() and d.name not in _CACHE_SKIP_DIRS:
            try:
                if not any(d.iterdir()):
                    d.rmdir()
                    stat["dirs_removed"] += 1
            except OSError:
                pass


def purge_cache(conn: sqlite3.Connection, root=None, keep_days: float | None = None,
                now: float | None = None, dry_run: bool = False) -> dict:
    """删掉判定完的候选图，只留待判定和还在用的。

    · 待判定（pending）的图永远保留 —— 视觉回合要读；
    · 已判定（done / skipped / error）从 decided_at 起算，满 keep_days 天删；
    · 单场日程（weekly_session.image_path）还指着的图，留到那一场过去；
    · 没有任何候选 / 日程引用的孤儿（半截下载、早期残留）按文件时间删；
    · 只动 data/schedules 下的图片，inbox/ 放的是 AI 批次 JSON，不碰。
    删掉的图在库里把 path 置空（url 保留），以后人工改判时 ensure_images 会按 url 重下。
    """
    base = Path(root) if root else SCHEDULE_DIR
    now = time.time() if now is None else float(now)
    conn.row_factory = sqlite3.Row
    keep = _cache_keep_days(conn) if keep_days is None else max(0.0, float(keep_days))
    keep_s = keep * 86400.0
    today = dt.date.fromtimestamp(now)
    stat = {"scanned": 0, "removed": 0, "bytes": 0, "kept_pending": 0, "kept_recent": 0,
            "kept_ref": 0, "failed": 0, "keep_days": keep, "cleared_candidates": 0,
            "cleared_sessions": 0, "dirs_removed": 0, "dry_run": bool(dry_run)}
    if not base.is_dir():
        return stat

    cand_paths = _cache_candidate_paths(conn)
    sess_refs = _cache_session_refs(conn, today, keep)
    # M3：只扫「待清理范围」——没有库引用的新图一律按近期保留跳过，不必白走一遍全树
    cutoff = (now - keep_s) if keep_s > 0 else None
    touched = _purge_files(base, cutoff=cutoff, cand_paths=cand_paths, sess_refs=sess_refs,
                           keep_s=keep_s, now=now, dry_run=dry_run, stat=stat)
    if not dry_run:
        _purge_clear_refs(conn, touched, stat)
        _purge_dirs(base, stat)
    return stat
