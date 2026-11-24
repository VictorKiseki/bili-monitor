"""配置、凭据与监控名单。

设计要点
--------
* 只读现有 data/bot.db 的 subject / subject_state，不写它 —— 名单归属监控台。
* 登录态单独存 config/livedeck.env（0600），不动 bili.env / bili_follow.env。
* 发弹幕需要 bili_jct + DedeUserID，扫码登录时一并保存。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

DB_PATH = Path(os.environ.get("LIVEDECK_DB") or (DATA_DIR / "bot.db"))
LIVEDECK_ENV = CONFIG_DIR / "livedeck.env"
FOLLOW_ENV = CONFIG_DIR / "bili_follow.env"
BILI_ENV = CONFIG_DIR / "bili.env"
STATE_PATH = DATA_DIR / "livedeck_state.json"
FACES_PATH = DATA_DIR / "livedeck_faces.json"   # 主播头像/昵称缓存，丢了只是重拉一遍
STACK_PATH = DATA_DIR / "stack.json"            # 监控台进程状态（看采集进程在不在跑）

COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")


@dataclass
class Credentials:
    sessdata: str = ""
    bili_jct: str = ""
    dedeuserid: str = ""
    uname: str = ""
    mid: int = 0
    saved_at: str = ""
    source: str = ""

    @property
    def logged_in(self) -> bool:
        return bool(self.sessdata)

    @property
    def can_send(self) -> bool:
        return bool(self.sessdata and self.bili_jct and self.dedeuserid)

    def masked(self) -> dict:
        return {
            "logged_in": self.logged_in,
            "can_send": self.can_send,
            "uname": self.uname,
            "mid": self.mid,
            "sessdata": _mask(self.sessdata),
            "csrf": _mask(self.bili_jct),
            "saved_at": self.saved_at,
            "source": self.source,
        }


def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 10:
        return "****"
    return "%s****%s" % (s[:4], s[-4:])


def _parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text("utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip().upper()
        v = v.strip().strip('"').strip("'")
        if k and v:
            out[k] = v
    return out


def load_credentials() -> Credentials:
    """优先 livedeck.env，其次 bili_follow.env（含 bili_jct），最后 bili.env。"""
    c = Credentials()
    for path, source in ((LIVEDECK_ENV, "livedeck"), (FOLLOW_ENV, "follow"), (BILI_ENV, "monitor")):
        env = _parse_env(path)
        if not env.get("SESSDATA"):
            continue
        c.sessdata = env.get("SESSDATA", "")
        c.bili_jct = env.get("BILI_JCT", "")
        c.dedeuserid = env.get("DEDEUSERID", "")
        c.uname = env.get("BILI_UNAME", "")
        try:
            c.mid = int(env.get("BILI_MID") or env.get("DEDEUSERID") or 0)
        except ValueError:
            c.mid = 0
        c.source = source
        c.saved_at = env.get("SAVED_AT", "")
        if c.sessdata and c.bili_jct and c.dedeuserid:
            break
    return c


_SAVE_HEAD = """# DD全能工作间 登录态（由工具写入，权限 600；请勿提交到仓库）
# 账号 {uname} mid={mid}；保存于 {ts}；来源 {source}
"""


def save_credentials(sessdata: str, bili_jct: str, dedeuserid: str,
                     uname: str = "", mid: int = 0, source: str = "扫码登录") -> Credentials:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    body = {
        "SESSDATA": sessdata,
        "BILI_JCT": bili_jct,
        "DEDEUSERID": dedeuserid,
        "BILI_UNAME": uname,
        "BILI_MID": str(mid or dedeuserid or ""),
        "SAVED_AT": ts,
        "LOGIN_SOURCE": source,
    }
    text = _SAVE_HEAD.format(uname=uname or "-", mid=mid or dedeuserid or 0, ts=ts, source=source)
    text += "\n".join("%s=%s" % (k, v) for k, v in body.items()) + "\n"
    tmp = LIVEDECK_ENV.with_name(LIVEDECK_ENV.name + ".tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, LIVEDECK_ENV)
    try:
        os.chmod(LIVEDECK_ENV, 0o600)
    except OSError:
        pass
    return Credentials(sessdata=sessdata, bili_jct=bili_jct, dedeuserid=dedeuserid,
                       uname=uname, mid=mid or int(dedeuserid or 0), saved_at=ts, source="livedeck")


def clear_credentials() -> None:
    try:
        LIVEDECK_ENV.unlink()
    except OSError:
        pass


@dataclass
class RoomEntry:
    uid: int
    name: str
    room_id: int
    grp: str = ""
    enabled: bool = True
    live_status: int = 0
    title: str = ""
    cover: str = ""
    face: str = ""
    online: int = 0
    # True = 来自监控台名单（data/bot.db）；临时填房间号加进来的为 False。
    # 不能用 uid 判断：临时房间 fetch_info 后会回填真实 uid。
    from_db: bool = False

    def to_dict(self) -> dict:
        return {
            "uid": self.uid, "name": self.name, "room_id": self.room_id,
            "grp": self.grp, "enabled": self.enabled, "live_status": self.live_status,
            "title": self.title, "cover": self.cover, "face": self.face, "online": self.online,
        }


def load_rail_live() -> dict[int, tuple[int, str]]:
    """读取监控台每轮采集保存的开播状态和在播场次标题。"""
    if not DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            has_sessions = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_session'"
            ).fetchone()
            title = "CASE WHEN st.live_status = 1 THEN ls.title END" if has_sessions else "NULL"
            join = "LEFT JOIN live_session ls ON ls.uid = s.uid " if has_sessions else ""
            rows = conn.execute(
                f"SELECT s.room_id, COALESCE(st.live_status, 0), {title} "
                "FROM subject s LEFT JOIN subject_state st ON st.uid = s.uid "
                f"{join}WHERE s.room_id IS NOT NULL AND s.room_id > 0"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out: dict[int, tuple[int, str]] = {}
    for r in rows:
        try:
            out[int(r[0])] = (int(r[1] or 0), r[2] or "")
        except (TypeError, ValueError):
            continue
    return out


def load_rooms() -> list[RoomEntry]:
    """从监控台数据库读名单（只读）。"""
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH.as_posix(), uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT s.uid, s.name, s.room_id, s.enabled, s.grp, "
            "       COALESCE(st.live_status, 0) AS live_status "
            "FROM subject s LEFT JOIN subject_state st ON st.uid = s.uid "
            "WHERE s.room_id IS NOT NULL AND s.room_id > 0 "
            "ORDER BY s.enabled DESC, CASE WHEN s.grp = '' THEN 1 ELSE 0 END, s.grp, s.name"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    out: list[RoomEntry] = []
    for r in rows:
        out.append(RoomEntry(
            uid=int(r["uid"]), name=(r["name"] or str(r["uid"])).strip(),
            room_id=int(r["room_id"]), grp=(r["grp"] or "").strip(),
            enabled=bool(r["enabled"]), live_status=int(r["live_status"] or 0),
            from_db=True,
        ))
    return out


# ---------- 名单写操作（分组 / 停用 / 删除） ----------
def _rw_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def list_groups() -> list[str]:
    """监控台已有的分组名，供前端下拉提示。"""
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH.as_posix(), uri=True, timeout=3)
        rows = conn.execute("SELECT DISTINCT grp FROM subject WHERE grp IS NOT NULL AND grp <> '' ORDER BY grp").fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    return [str(r[0]) for r in rows if r[0]]


def set_group(room_id: int, grp: str) -> bool:
    """写监控台的 subject.grp。这是本工具唯一修改名单归属的地方。"""
    grp = (grp or "").strip()[:40]
    if not DB_PATH.exists():
        return False
    try:
        conn = _rw_conn()
        try:
            cur = conn.execute("UPDATE subject SET grp = ? WHERE room_id = ?", (grp, int(room_id)))
            conn.commit()
        finally:
            conn.close()
        return cur.rowcount > 0
    except sqlite3.Error:
        return False


def set_enabled(room_id: int, enabled: bool) -> bool:
    """停用/恢复监控台里的某个主播（不删任何历史）。"""
    if not DB_PATH.exists():
        return False
    try:
        conn = _rw_conn()
        try:
            cur = conn.execute("UPDATE subject SET enabled = ? WHERE room_id = ?",
                               (1 if enabled else 0, int(room_id)))
            conn.commit()
        finally:
            conn.close()
        return cur.rowcount > 0
    except sqlite3.Error:
        return False


def add_subject(uid: int, name: str, room_id: int, grp: str = "") -> bool:
    """把一个临时房间写进监控台名单（subject + subject_state）。已存在就恢复启用。"""
    if not DB_PATH.exists() or not uid or not room_id:
        return False
    name = (name or "").strip()[:80] or ("房间 %d" % int(room_id))
    grp = (grp or "").strip()[:40]
    try:
        conn = _rw_conn()
        try:
            conn.execute(
                "INSERT INTO subject (uid, name, room_id, enabled, grp) VALUES (?,?,?,1,?) "
                "ON CONFLICT(uid) DO UPDATE SET name=excluded.name, room_id=excluded.room_id, enabled=1",
                (int(uid), name, int(room_id), grp))
            conn.execute(
                "INSERT OR IGNORE INTO subject_state (uid, room_id, live_status, last_poll_ts) "
                "VALUES (?,?,0,0)", (int(uid), int(room_id)))
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


def delete_subject(room_id: int) -> bool:
    """从监控台名单彻底删除（事件历史保留，仅删 subject 与 subject_state 行）。"""
    if not DB_PATH.exists():
        return False
    try:
        conn = _rw_conn()
        try:
            conn.execute("DELETE FROM subject_state WHERE room_id = ?", (int(room_id),))
            cur = conn.execute("DELETE FROM subject WHERE room_id = ?", (int(room_id),))
            conn.commit()
        finally:
            conn.close()
        return cur.rowcount > 0
    except sqlite3.Error:
        return False


# ---------- 轻量状态（上次选台、布局） ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, STATE_PATH)


# ---------- 主播头像缓存（uid -> {face, uname, ts}） ----------
def load_faces() -> dict[int, dict]:
    """读头像缓存。单独一个小文件，不写进监控台的库。"""
    try:
        raw = json.loads(FACES_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[int, dict] = {}
    for key, val in raw.items():
        try:
            uid = int(key)
        except (TypeError, ValueError):
            continue
        if uid <= 0 or not isinstance(val, dict):
            continue
        face = str(val.get("face") or "")
        uname = str(val.get("uname") or "")
        try:
            ts = float(val.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if face or uname:
            out[uid] = {"face": face, "uname": uname, "ts": ts}
    return out


def save_faces(faces: dict[int, dict]) -> None:
    """原子写头像缓存；写失败只影响下次启动要多拉一遍，不影响功能。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    body = {str(uid): {"face": v.get("face") or "", "uname": v.get("uname") or "",
                       "ts": float(v.get("ts") or 0)} for uid, v in faces.items()}
    tmp = FACES_PATH.with_name(FACES_PATH.name + ".tmp")
    try:
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=0), "utf-8")
        os.replace(tmp, FACES_PATH)
    except OSError:
        pass


# ---------- 监控台采集进程状态 ----------
def collector_alive() -> bool | None:
    """采集进程在不在跑：读 data/stack.json（StackManager 每 2 秒刷新）。

    返回 None 表示判断不了（没这个文件，或状态已经过期），调用方自己决定要不要兜底。
    last_poll_ts 不能当心跳用——它是动态流的水位，主播不发动态就一直不动。
    """
    try:
        raw = json.loads(STACK_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        age = time.time() - float(raw.get("updated_at") or 0)
    except (TypeError, ValueError):
        return None
    if age > 60:
        return None
    role = ((raw.get("roles") or {}).get("collector") or {})
    if not isinstance(role, dict) or "alive" not in role:
        return None
    return bool(role.get("alive"))
