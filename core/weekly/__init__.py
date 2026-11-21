"""core.weekly 包：对外仍是原来那个扁平模块的 API 面（2026-11-07 机械拆分）。

拆分只按职责搬家，不改一行逻辑；子模块 parse/fetch/store/reserve/view 之间是单向依赖。
任何 from core import weekly / from core.weekly import xxx 的写法都不需要改。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .parse import (
    WEEKDAYS, WEEKDAY_CN, EXCLUDE_UIDS, _CJK_GAP, _cjk_tight, _DAY_ALIAS, _TIME_RE,
    FUZZY_TIME_MINUTES, _FUZZY_RE, _RANGE_RE, _LABEL_RE, monday_of, infer_week_start,
    parse_week_label, normalize_day, fuzzy_minutes, range_start_minutes, time_label,
    normalize_time, format_hm, is_rest, is_fuzzy_time, entry_time_label, entry_body_text,
    strip_day_words,
    NICK_STOP, streamer_aliases, mentions_alias, load_aliases,
    PROGRAM_SEED, PROGRAM_ALIAS_MIN_CJK, PROGRAM_ALIAS_MIN_ASCII, _json_arr, _PUNCT_RE,
    _flat, _lcs, same_show, EXTRA_HINTS, _join_note, _pub_times, load_programs,
    match_program, program_public, MENTION_RE, LINK_CLAUSE_RE, WITH_RE, NAME_SPLIT_RE,
    extract_names, _match_known, condense_texts, _collab_groups, merge_collabs,
    normalize_days, week_label_of, _images_of, monday_of_iso, _now_ts, _DATE_TXT_RE,
    _parse_date_text, resolve_session_date,
)

from .fetch import (
    SCHEDULE_DIR, UA, CACHE_KEEP_DAYS, _IMG_SUFFIXES, _CACHE_SKIP_DIRS,
    DOWNLOAD_MAX_BYTES, _IMG_HOSTS, _img_host_ok, _img_url_ok, _http, _download,
    _img_ext, cache_dest, _cache_keep_days, _file_mtime, _file_size, _cache_files,
    cache_stats, ensure_images, purge_cache,
)

from .store import (
    program_save, program_delete, weekly_off_uids, pending_count, scan, _STRONG, _WEAK,
    candidate_score, pending_batch, set_candidate_scope, WEEK_MIN_DAYS, classify_scope,
    _flat_entries, _diff_days, _record_change, _sessions_of, ingest, _valid_week,
    MANUAL_OPS, _subject_name, _collab_uids, manual_add, manual_update, manual_delete,
    session_delete, manual_list, week_changes, _ocr_signal,
)

from .reserve import (
    _RESERVE_TITLE_RE, _RESERVE_TIME_RE, _TIME_ONLY_RE, _WEEKDAY_TXT_RE, _REL_DAY,
    _MONTH_DAY_RE, _DOT_DATE_RE, _DASH_DATE_RE, absolute_date_hint, text_date_hint,
    parse_reserve, _TEXT_TIME_RE, _TEXT_POINT_RE, _EMOJI_TOKEN_RE, _GENERIC_TITLES,
    _CANCEL_RESERVE_RE, text_schedule_hint, _text_topic, combine_reserve,
    ingest_reserve, _local_reserve_image, refresh_reserves,
)

from .view import (
    week_view, LIVE_NEAREST_MAX_MIN, LIVE_CROSS_DAY_MIN, _entry_minutes, _by_uid,
    annotate_live, prev_next_week,
)

# _HTTP 是 fetch 里的惰性全局（第一次下载才创建 client）。用 from .fetch import _HTTP 会在导入时
# 把当时的值（None）抄一份，之后 fetch 里再赋值这个副本永远对不上；所以改成模块级 __getattr__
# 动态转发，保持和拆分前「整个模块共用一个 _HTTP」的可见行为。
def __getattr__(name: str):
    if name != "_HTTP":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(".fetch", __name__), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"_HTTP"})


# 拆分前模块级只有这一条注解（PEP 563 下存成字符串），保留下来让 dir() 与旧模块一致
__annotations__ = {"_HTTP": "httpx.Client | None"}

__all__ = [
    "WEEKDAYS", "WEEKDAY_CN", "EXCLUDE_UIDS", "_CJK_GAP", "_cjk_tight", "_DAY_ALIAS",
    "_TIME_RE", "FUZZY_TIME_MINUTES", "_FUZZY_RE", "_RANGE_RE", "_LABEL_RE",
    "monday_of", "infer_week_start", "parse_week_label", "normalize_day",
    "fuzzy_minutes", "range_start_minutes", "time_label", "normalize_time",
    "format_hm", "is_rest", "is_fuzzy_time", "entry_time_label", "entry_body_text",
    "strip_day_words",
    "NICK_STOP", "streamer_aliases", "mentions_alias", "load_aliases", "PROGRAM_SEED",
    "PROGRAM_ALIAS_MIN_CJK", "PROGRAM_ALIAS_MIN_ASCII", "_json_arr", "_PUNCT_RE",
    "_flat", "_lcs", "same_show", "EXTRA_HINTS", "_join_note", "_pub_times",
    "load_programs", "match_program", "program_public", "MENTION_RE", "LINK_CLAUSE_RE",
    "WITH_RE", "NAME_SPLIT_RE", "extract_names", "_match_known", "condense_texts",
    "_collab_groups", "merge_collabs", "normalize_days", "week_label_of", "_images_of",
    "monday_of_iso", "_now_ts", "_DATE_TXT_RE", "_parse_date_text",
    "resolve_session_date", "SCHEDULE_DIR", "UA", "CACHE_KEEP_DAYS", "_IMG_SUFFIXES",
    "_CACHE_SKIP_DIRS", "DOWNLOAD_MAX_BYTES", "_IMG_HOSTS", "_img_host_ok",
    "_img_url_ok", "_HTTP", "_http", "_download", "_img_ext", "cache_dest",
    "_cache_keep_days", "_file_mtime", "_file_size", "_cache_files", "cache_stats",
    "ensure_images", "purge_cache", "program_save", "program_delete",
    "weekly_off_uids", "pending_count", "scan", "_STRONG", "_WEAK", "candidate_score",
    "pending_batch", "set_candidate_scope", "WEEK_MIN_DAYS", "classify_scope",
    "_flat_entries", "_diff_days", "_record_change", "_sessions_of", "ingest",
    "_valid_week", "MANUAL_OPS", "_subject_name", "_collab_uids", "manual_add",
    "manual_update", "manual_delete", "session_delete", "manual_list", "week_changes",
    "_ocr_signal", "_RESERVE_TITLE_RE", "_RESERVE_TIME_RE", "_TIME_ONLY_RE",
    "_WEEKDAY_TXT_RE", "_REL_DAY", "_MONTH_DAY_RE", "_DOT_DATE_RE", "_DASH_DATE_RE",
    "absolute_date_hint", "text_date_hint", "parse_reserve", "_TEXT_TIME_RE",
    "_TEXT_POINT_RE", "_EMOJI_TOKEN_RE", "_GENERIC_TITLES", "_CANCEL_RESERVE_RE",
    "text_schedule_hint", "_text_topic", "combine_reserve", "ingest_reserve",
    "_local_reserve_image", "refresh_reserves", "week_view", "LIVE_NEAREST_MAX_MIN",
    "LIVE_CROSS_DAY_MIN", "_entry_minutes", "_by_uid", "annotate_live",
    "prev_next_week",
]
