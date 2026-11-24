"""监控台与监控室的个人配置迁移。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
from urllib.parse import urlsplit

from core import tasks
from core.weekly.feature import FEATURE_KEY
from core.weekly.push import PUSH_AT_KEY
from scripts.recorder_cleanup import DEFAULTS as CLEANUP_DEFAULTS
from shared import proxy, webutil
from web.console_core import ApiError

FORMAT = "bili-monitor-profile"
VERSION = 1
MAX_BYTES = 8 * 1024 * 1024
FILES = ("config/proxy.json", "config/recorder_cleanup.json", "data/livedeck_state.json")
PRIVATE_FILES = ("BBOT.env", "BILI.env", ".p0_ids.json", "config/bili.env", "config/bili_follow.env",
                 "config/livedeck.env", "config/ai.env")
META_KEYS = ("group_names", "group_colors", "global_mode", tasks.META_KEY, tasks.AUTOSTART_KEY,
             FEATURE_KEY, PUSH_AT_KEY, "weekly_ai_enabled", "weekly_ai_limit", "weekly_ai_slots",
             "weekly_cache_keep_days")
SUBJECT_COLUMNS = ("uid", "name", "room_id", "grp", "push_live", "push_dynamic", "weekly_on")
TABLE_COLUMNS = {
    "streamer_alias": ("uid", "alias", "note", "created_at"),
    "weekly_program": ("name", "aliases_json", "hosts_json", "weekday", "time", "note",
                       "enabled", "created_at", "updated_at"),
}


def _invalid(section: str) -> None:
    raise ApiError("迁移包中的%s格式无效" % section)


def _json(data) -> bytes:
    return (json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def _uid(value, minimum=1) -> bool:
    return type(value) is int and minimum <= value <= webutil.MAX_UID


def _flag(value) -> bool:
    return type(value) is int and value in (0, 1)


def _validate_state(state) -> None:
    if (type(state.get("grid", 0)) is not int
            or any(not isinstance(state.get(k, []), list)
                   for k in ("active", "hidden", "listening", "group_order", "combos", "temp"))
            or not isinstance(state.get("colors", {}), dict)):
        _invalid("监控室布局")
    for room in state.get("temp", []):
        if (not isinstance(room, dict) or not _uid(room.get("room_id"))
                or not _uid(room.get("uid", 0), 0)
                or any(not isinstance(room.get(k, ""), str) for k in ("name", "grp"))):
            _invalid("监控室临时房间")
    for combo in state.get("combos", []):
        if (not isinstance(combo, dict) or not _uid(combo.get("id", 1))
                or not isinstance(combo.get("name", ""), str)
                or not isinstance(combo.get("rooms"), list)
                or any(not _uid(room) for room in combo["rooms"])):
            _invalid("监控室弹幕组合")


def _validate_files(files: dict, cleanup) -> dict[str, bytes]:
    out = {}
    for name, value in files.items():
        if name not in FILES + PRIVATE_FILES or not isinstance(value, str) or "\0" in value:
            _invalid("配置文件")
        if name.endswith(".json"):
            data = json.loads(value)
            if not isinstance(data, dict):
                _invalid("配置文件")
            if name == "config/proxy.json":
                proxy.validate(data)
            if name == "config/recorder_cleanup.json":
                cleanup._validated({**CLEANUP_DEFAULTS, **data})
            if name == "data/livedeck_state.json":
                _validate_state(data)
        out[name] = value.encode("utf-8")
    return out


def _validate_tables(tables: dict) -> None:
    if tables.keys() != TABLE_COLUMNS.keys():
        _invalid("日程设置")
    for table, columns in TABLE_COLUMNS.items():
        rows = tables[table]
        if not isinstance(rows, list):
            _invalid("日程设置")
        for row in rows:
            if not isinstance(row, dict) or row.keys() != set(columns):
                _invalid("日程设置")
            for key, value in row.items():
                if key == "uid":
                    valid = _uid(value)
                elif key == "enabled":
                    valid = _flag(value)
                elif key in ("created_at", "updated_at"):
                    valid = value is None or type(value) in (int, float)
                elif key in ("aliases_json", "hosts_json"):
                    valid = isinstance(value, str) and isinstance(json.loads(value), list)
                else:
                    valid = isinstance(value, str) or (key == "note" and value is None)
                if not valid:
                    _invalid("日程设置")


def _validate(bundle: dict, cleanup) -> dict[str, bytes]:
    if (bundle.get("format") != FORMAT or type(bundle.get("version")) is not int
            or bundle["version"] != VERSION):
        raise ApiError("无法识别迁移包或迁移包版本不受支持")
    fields = ("files", "subject_config", "meta", "tables")
    if any(not isinstance(bundle.get(key), dict) for key in fields):
        _invalid("配置")
    config = bundle["subject_config"]
    subjects = config.get("subjects")
    if not isinstance(subjects, list) or not isinstance(config.get("poll", {}), dict):
        _invalid("监控名单")
    seen = set()
    for subject in subjects:
        if (not isinstance(subject, dict) or not _uid(subject.get("uid"))
                or not _uid(subject.get("room_id", 0), 0)
                or any(not isinstance(subject.get(k, ""), str) for k in ("name", "group"))
                or subject["uid"] in seen):
            _invalid("监控名单")
        seen.add(subject["uid"])
    flags = bundle.get("subject_flags")
    if (not isinstance(flags, dict) or flags.keys() != {str(uid) for uid in seen}
            or any(not isinstance(v, list) or len(v) != 3 or not all(map(_flag, v))
                   for v in flags.values())):
        _invalid("主播开关")
    if any(k not in META_KEYS or not isinstance(v, str) for k, v in bundle["meta"].items()):
        _invalid("监控台选项")
    meta = bundle["meta"]
    for key, kind in (("group_names", list), ("group_colors", dict), (tasks.META_KEY, list),
                      (tasks.AUTOSTART_KEY, dict)):
        if key in meta and not isinstance(json.loads(meta[key]), kind):
            _invalid("监控台选项")
    if "weekly_ai_limit" in meta and not 1 <= int(meta["weekly_ai_limit"] or 12) <= 200:
        _invalid("日程识别批次上限")
    _validate_tables(bundle["tables"])
    return _validate_files(bundle["files"], cleanup)


def _replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            staged = Path(stream.name)
            stream.write(data)
        os.replace(staged, path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


class ConsoleProfile:
    def _profile_paths(self) -> dict[str, Path]:
        root = self._root()
        return {name: root / name for name in FILES + PRIVATE_FILES}

    def export_profile(self, include_private: bool) -> bytes:
        with self._lock:
            conn = self._db()
            conn.execute("BEGIN")
            try:
                config = self._read_config_locked()
                subjects = {int(row["uid"]): dict(row) for row in config.get("subjects", [])}
                for uid, subject in subjects.items():
                    subject.update(uid=uid, room_id=int(subject.get("room_id") or 0))
                flags = {}
                for row in conn.execute("SELECT * FROM subject ORDER BY uid"):
                    uid = row["uid"]
                    if not row["enabled"]:
                        subjects.pop(uid, None)
                        continue
                    subject = subjects.setdefault(uid, {"uid": uid})
                    subject.update(name=row["name"], room_id=row["room_id"] or 0)
                    subject.pop("_disabled", None)
                    if row["grp"]:
                        subject["group"] = row["grp"]
                    else:
                        subject.pop("group", None)
                    if row["room_id"]:
                        subject.pop("_no_room", None)
                    else:
                        subject["_no_room"] = True
                    flags[str(uid)] = [row[k] for k in SUBJECT_COLUMNS[-3:]]
                config["subjects"] = [subjects[uid] for uid in sorted(subjects)]
                meta = dict(conn.execute("SELECT k,v FROM meta WHERE k IN (%s)"
                                         % ",".join("?" for _ in META_KEYS), META_KEYS))
                tables = {table: [dict(row) for row in conn.execute(
                    "SELECT %s FROM %s" % (",".join(columns), table))]
                    for table, columns in TABLE_COLUMNS.items()}
            finally:
                conn.rollback()

            files = {}
            for name, path in self._profile_paths().items():
                if (include_private or name in FILES) and path.is_file():
                    with path.open("rb") as stream:
                        raw = stream.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise ApiError("个人配置超过 8 MiB", 413)
                    try:
                        value = raw.decode("utf-8")
                        if name == "config/proxy.json" and not include_private:
                            settings = proxy.validate(json.loads(value))
                            if urlsplit(settings["url"]).username is not None:
                                continue
                            value = _json(settings).decode("utf-8")
                    except (ValueError, AttributeError, RecursionError):
                        raise ApiError("本机配置格式无效：%s" % name) from None
                    files[name] = value
            bundle = {"format": FORMAT, "version": VERSION, "files": files, "meta": meta,
                      "subject_config": config, "tables": tables,
                      "subject_flags": {str(uid): flags.get(str(uid), [1, 1, 1]) for uid in subjects}}
            try:
                _validate(bundle, self.cleanup)
                raw = _json(bundle)
            except (ValueError, OverflowError, RecursionError):
                raise ApiError("个人配置格式无效") from None
            if len(raw) > MAX_BYTES:
                raise ApiError("个人配置超过 8 MiB", 413)
            return raw

    def import_profile(self, bundle: dict) -> dict:
        try:
            files = _validate(bundle, self.cleanup)
            config = bundle["subject_config"]
            paths = self._profile_paths()
            changes = {paths[name]: value for name, value in files.items()}
            changes[self.subjects_path] = _json(config)
        except (ValueError, OverflowError, RecursionError):
            raise ApiError("迁移包格式无效") from None

        with self._lock:
            originals = {path: path.read_bytes() if path.exists() else None for path in changes}
            conn = self._db()
            written = []
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 保留目标机器的水位、历史和主键，仅将名单外的主播停用。
                conn.execute("UPDATE subject SET enabled=0 WHERE enabled=1")
                conn.executemany(
                    "INSERT INTO subject(uid,name,room_id,grp,push_live,push_dynamic,weekly_on,enabled)"
                    " VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(uid) DO UPDATE SET name=excluded.name,"
                    " room_id=excluded.room_id,grp=excluded.grp,push_live=excluded.push_live,"
                    " push_dynamic=excluded.push_dynamic,weekly_on=excluded.weekly_on,enabled=1",
                    [(row["uid"], row.get("name", ""), row.get("room_id") or None,
                      row.get("group", ""), *bundle["subject_flags"][str(row["uid"])])
                     for row in config["subjects"]])
                conn.executemany("DELETE FROM meta WHERE k=?", [(key,) for key in META_KEYS])
                conn.executemany("INSERT INTO meta(k,v) VALUES(?,?)", bundle["meta"].items())
                for table, columns in TABLE_COLUMNS.items():
                    conn.execute("DELETE FROM " + table)
                    conn.executemany("INSERT INTO %s(%s) VALUES(%s)" % (
                        table, ",".join(columns), ",".join("?" for _ in columns)),
                        [tuple(row[key] for key in columns) for row in bundle["tables"][table]])
                for path, data in changes.items():
                    _replace(path, data)
                    written.append(path)
                conn.commit()
            except Exception as err:
                conn.rollback()
                failed = False
                for path in reversed(written):
                    try:
                        if originals[path] is None:
                            path.unlink(missing_ok=True)
                        else:
                            _replace(path, originals[path])
                    except OSError:
                        failed = True
                if failed:
                    raise ApiError("导入失败，部分配置文件无法还原", 500) from None
                if isinstance(err, (sqlite3.IntegrityError, OverflowError)):
                    raise ApiError("迁移包包含重复或无效的配置项") from None
                if isinstance(err, OSError):
                    raise ApiError("无法写入个人配置，已还原本次修改", 500) from None
                raise
            self._client = None
            self._auth_cache = None
        return {"ok": True, "subjects": len(config["subjects"]), "files": len(changes),
                "note": "配置已导入。请重启监控台和监控室，使全部设置生效。"}
