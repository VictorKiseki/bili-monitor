"""纯解析层：周次/日期/时间/昵称/节目单/合作名单——不碰数据库、不碰网络。"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 箱号/机构号：每天发文字排期，形态不是周表，明确排除（2026-11-03 需求）
EXCLUDE_UIDS = {454673997}

_CJK_GAP = re.compile(r"(?<=[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef])[ \t]+"
                      r"(?=[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef])")

def _cjk_tight(s: str) -> str:
    """Windows OCR 会在每个汉字之间插空格；去掉，免得备注和库里全是碎字。"""
    return _CJK_GAP.sub("", s or "")

_DAY_ALIAS = {
    "MON": 0, "MONDAY": 0, "周一": 0, "星期一": 0, "礼拜一": 0, "月": 0, "月曜": 0,
    "TUE": 1, "TUES": 1, "TUESDAY": 1, "周二": 1, "星期二": 1, "礼拜二": 1, "火": 1, "火曜": 1,
    "WED": 2, "WEDNESDAY": 2, "周三": 2, "星期三": 2, "礼拜三": 2, "水": 2, "水曜": 2,
    "THU": 3, "THUR": 3, "THURS": 3, "THURSDAY": 3, "周四": 3, "星期四": 3, "礼拜四": 3, "木": 3, "木曜": 3,
    "FRI": 4, "FRIDAY": 4, "周五": 4, "星期五": 4, "礼拜五": 4, "金": 4, "金曜": 4,
    "SAT": 5, "SATURDAY": 5, "周六": 5, "星期六": 5, "礼拜六": 5, "土": 5, "土曜": 5,
    "SUN": 6, "SUNDAY": 6, "周日": 6, "周天": 6, "星期日": 6, "星期天": 6, "礼拜日": 6, "日": 6, "日曜": 6,
}

_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:：.．]\s*(\d{2})\s*$")

# 模糊时段（2026-11-06）：动态只写「上午 / 中午 / 晚上」这类词、没有具体点数时，
# 不替主播编一个钟点：time 留空、time_text 记原词，这里给一个代表钟点参与排序。
FUZZY_TIME_MINUTES = {
    "凌晨": 4 * 60, "清晨": 6 * 60, "一早": 6 * 60, "早上": 8 * 60, "早晨": 8 * 60,
    "上午": 9 * 60, "中午": 12 * 60, "午后": 13 * 60, "下午": 15 * 60,
    "傍晚": 18 * 60, "晚上": 20 * 60, "晚间": 20 * 60, "夜里": 22 * 60,
    "夜间": 22 * 60, "深夜": 23 * 60, "半夜": 23 * 60,
}

_FUZZY_RE = re.compile("|".join(sorted(map(re.escape, FUZZY_TIME_MINUTES), key=len, reverse=True)))

_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:点|:|：)?\s*[~～\-—–至到]\s*(\d{1,2})")

_LABEL_RE = re.compile(
    r"(\d{1,2})\s*[.月/]\s*(\d{1,2})\s*[日号]?\s*[~～—\-–至到]+\s*(\d{1,2})\s*[.月/]\s*(\d{1,2})")

# ---------------- 时间与周次 ----------------
def monday_of(ts: float | int) -> dt.date:
    d = dt.date.fromtimestamp(float(ts))
    return d - dt.timedelta(days=d.weekday())

def infer_week_start(ts: float | int) -> str:
    """按发布时间猜周次：周日发的是「明天开始那周」，其余取本周周一。"""
    d = dt.date.fromtimestamp(float(ts))
    if d.weekday() == 6:                     # 周日：明天就是下周一
        return (d + dt.timedelta(days=1)).isoformat()
    return (d - dt.timedelta(days=d.weekday())).isoformat()

def parse_week_label(label: str | None, posted_ts: float | int) -> str | None:
    """从「11.2~11.8」「11月2日~11月8日」这类标签解析起始周一。"""
    if not label:
        return None
    m = _LABEL_RE.search(label)
    if not m:
        return None
    m1, d1 = int(m.group(1)), int(m.group(2))
    year = dt.date.fromtimestamp(float(posted_ts)).year
    for y in (year, year + 1, year - 1):
        try:
            start = dt.date(y, m1, d1)
        except ValueError:
            continue
        if abs((start - dt.date.fromtimestamp(float(posted_ts))).days) <= 45:
            return (start - dt.timedelta(days=start.weekday())).isoformat()
    return None

def normalize_day(name: str) -> int | None:
    key = (name or "").strip().upper().rstrip(".").strip()
    if key in _DAY_ALIAS:
        return _DAY_ALIAS[key]
    for k, v in _DAY_ALIAS.items():
        if key.startswith(k):
            return v
    return None

def fuzzy_minutes(text: str | None) -> int | None:
    """「晚上」「中午」这类模糊时段给一个代表钟点（分钟数），只为排序/定位；认不出返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    m = _FUZZY_RE.search(raw)
    return FUZZY_TIME_MINUTES[m.group(0)] if m else None

def range_start_minutes(text: str | None) -> int | None:
    """「9~12」「14-16」这种区间按开始钟点排序；认不出返回 None。"""
    m = _RANGE_RE.search(str(text or ""))
    if not m:
        return None
    try:
        h = int(m.group(1))
    except ValueError:
        return None
    return h * 60 if 0 <= h <= 23 else None

# 中文数字时间（2026-11-09）：「晚上十点」「十一点半」也要能落到具体钟点。
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_TOKEN = "[零〇一二两三四五六七八九十]{1,3}"

_COLON_RE = re.compile(r"(?<![\d:])(\d{1,2})\s*[:：]\s*(\d{2})(?![\d])")
_DIGIT_POINT_RE = re.compile(
    r"(?<![\d])(\d{1,2})\s*点\s*(?:(半|一刻|三刻)|(\d{1,2})\s*分?)?")
_CN_POINT_RE = re.compile(
    r"(" + _CN_TOKEN + r")\s*点\s*(?:(半|一刻|三刻)|(" + _CN_TOKEN + r")\s*分?)?")
_BARE_RANGE_RE = re.compile(r"(?<![-/.\d:])(\d{1,2})\s*[~～\-—–]\s*(\d{1,2})(?![\d])")
_RANGE_SEP_RE = re.compile(r"^\s*(?:点|分)?\s*(?:[~～\-—–]|至|到)\s*(?:点|分)?\s*$")
_HM_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$")
_REST_RE = re.compile(r"^(?:今天|今日|明天|明日)?\s*(?:休息|休|停播|不播|放假|歇)(?:一天|日)?\s*$")
_TBD_RE = re.compile(r"待定|未定|tbd", re.I)

# 相对日词（2026-11-10）：条目已经落到具体日期，正文再写「今天/明天/后天」是重复信息。
# 只在展示层去掉，库里的原文保留，方便回溯主播当时的说法。
_DAY_WORD_RE = re.compile(
    r"大后天|大前天|明天|明日(?!方舟|之后|香|之子)|明晚|明早|明晨|明儿|"
    r"今天(?!开始做)|今日(?!说法|头条|关注)|今晚|今早|今晨|今儿|"
    r"后天|前天|次日|隔天|当天|当日|翌日")
_DATE_ONLY_PAREN_RE = re.compile(r"[（(]([^（）()]*)[）)]")
_DATE_ONLY_REST = {"", "休息", "休", "停播", "不播", "放假", "歇", "休息中", "都休息", "也休息"}
_DUP_PUNCT_RE = re.compile(r"[、，,;；]\s*(?=[、，,;；])")
_EMPTY_PAREN_RE = re.compile(r"[（(]\s*[、，,;；]*\s*[）)]")
_EDGE_PUNCT_RE = re.compile(r"^[\s、，,;；:：\-—·/]+|[\s、，,;；:：·/]+$")

def _cn_int(text):
    """中文数字转整数：十 → 10、二十三 → 23；认不出返回 None。"""
    s = str(text or "").strip()
    if not s:
        return None
    if "十" in s:
        left, _, right = s.partition("十")
        tens = 1 if not left else _CN_DIGITS.get(left)
        ones = 0 if not right else _CN_DIGITS.get(right)
        if tens is None or ones is None:
            return None
        val = tens * 10 + ones
        return val if 0 <= val <= 99 else None
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    return None

def _point_minutes(mark, num, cn: bool = False):
    """「半/一刻/三刻」或「30分」→ 分钟数。"""
    if mark == "半":
        return 30
    if mark == "一刻":
        return 15
    if mark == "三刻":
        return 45
    if num in (None, ""):
        return 0
    return _cn_int(num) if cn else int(num)

def _time_mentions(raw: str) -> list[tuple]:
    """找出文本里所有钟点提及：[(start, end, hour, minute)]，按出现位置排序。"""
    spans = []
    for m in _COLON_RE.finditer(raw):
        spans.append((m.start(), m.end(), int(m.group(1)), int(m.group(2))))
    for m in _DIGIT_POINT_RE.finditer(raw):
        spans.append((m.start(), m.end(), int(m.group(1)),
                      _point_minutes(m.group(2), m.group(3))))
    for m in _CN_POINT_RE.finditer(raw):
        h = _cn_int(m.group(1))
        mi = _point_minutes(m.group(2), m.group(3), cn=True)
        if h is not None and mi is not None:
            spans.append((m.start(), m.end(), h, mi))
    spans.sort()
    out = []
    last_end = -1
    for sp in spans:                    # 同一段文字被两种写法重复命中时只算一次
        if sp[0] < last_end:
            continue
        out.append(sp)
        last_end = sp[1]
    return out

def _adjust_hour(h: int, raw: str) -> int:
    """12 小时制修正：「晚上8点」→ 20:00、「晚上十二点」→ 00:00。"""
    late = re.search(r"(晚|夜|下午|傍晚|中午)", raw)
    early = re.search(r"(早上|早晨|一早|上午|凌晨|清晨)", raw)
    if h == 12 and re.search(r"(晚|夜)", raw) \
            and not re.search(r"(早上|早晨|上午|中午|凌晨|清晨)", raw):
        return 0
    if h < 12 and late and not early:
        return h + 12
    return h

def _range_start(raw: str, mentions: list[tuple]):
    """「9~12」「9点到12点」「晚上9:30~12:00」这类区间取开始时间。"""
    if len(mentions) >= 2:
        a, b = mentions[0], mentions[1]
        if _RANGE_SEP_RE.match(raw[a[1]:b[0]]):
            return a[2], a[3]
    m = _BARE_RANGE_RE.search(raw)
    if m:
        h = int(m.group(1))
        if 0 <= h <= 23:
            return h, 0
    return None

def normalize_time(value) -> tuple[str | None, str]:
    """返回 (HH:MM 或 None, 原文)。

    2026-11-09 起：区间写开始时间（9~12 → 09:00）、中文数字转 24 小时制
    （晚上十点 → 22:00）；同一段里出现两个不同钟点（十点或十一点）仍返回 None，
    由展示层写「待定」并把原文放进正文。
    """
    raw = str(value or "").strip()
    if not raw:
        return None, ""
    m = _TIME_RE.match(raw)
    if m:
        h = _adjust_hour(int(m.group(1)), raw)
        mi = int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return "%02d:%02d" % (h, mi), raw
        return None, raw
    mentions = _time_mentions(raw)
    if mentions:
        rs = _range_start(raw, mentions)
        if rs is not None:
            h, mi = _adjust_hour(rs[0], raw), rs[1]
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return "%02d:%02d" % (h, mi), raw
            return None, raw
        cands = []
        for _s, _e, h, mi in mentions:
            h = _adjust_hour(h, raw)
            if 0 <= h <= 23 and 0 <= mi <= 59 and (h, mi) not in cands:
                cands.append((h, mi))
        if len(cands) == 1:
            return "%02d:%02d" % cands[0], raw
        return None, raw
    m = _BARE_RANGE_RE.search(raw)
    if m:
        h = _adjust_hour(int(m.group(1)), raw)
        if 0 <= h <= 23:
            return "%02d:00" % h, raw
    return None, raw

def format_hm(value) -> str:
    """展示用钟点：09:30 → 9:30；认不出返回空串。"""
    raw = str(value or "").strip()
    m = _HM_RE.match(raw)
    if not m:
        t, _ = normalize_time(raw)
        if not t:
            return ""
        m = _HM_RE.match(t)
        if not m:
            return ""
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return ""
    return "%d:%02d" % (h, mi)

def is_rest(*values) -> bool:
    """整条日程就是休息：「休息 / 休 / 停播 / 不播 / 放假」。"""
    for v in values:
        s = str(v or "").strip()
        if s and _REST_RE.match(s):
            return True
    return False

def is_fuzzy_time(text) -> bool:
    """写了时间但认不出具体钟点（上午/晚上、待定、十点或十一点）→ 展示为「待定」。"""
    s = str(text or "").strip()
    if not s:
        return False
    return bool(_FUZZY_RE.search(s) or _RANGE_RE.search(s) or _TBD_RE.search(s)
                or _time_mentions(s))

def time_label(value, time_text=None) -> str:
    """展示用的时间：具体钟点 → H:mm；休息 → 休息；写了模糊时间 → 待定。"""
    t = format_hm(value)
    if t:
        return t
    txt = str(time_text if time_text is not None else value or "").strip()
    if is_rest(txt):
        return "休息"
    if is_fuzzy_time(txt):
        return "待定"
    return ""

def entry_time_label(entry) -> str:
    """一条日程在表上的时间列：H:mm / 休息 / 待定 / 空串。"""
    e = entry or {}
    t = format_hm(e.get("time"))
    if t:
        return t
    tt = str(e.get("time_text") or "").strip()
    if tt:
        t = format_hm(tt)               # 旧数据：time 空、time_text 里写着 9~12
        if t:
            return t
    if is_rest(tt, e.get("text")):
        return "休息"
    if is_fuzzy_time(tt):
        return "待定"
    if str(e.get("text") or "").strip():
        return "待定"                  # 有节目却没写时间：只能是待定
    return ""

def strip_day_words(text) -> str:
    """去掉正文里的相对日词（今天/明天/后天）：条目已经落在对应日期上，不必再写「哪天」。

    只删相对日，不动「周三/星期四」这类星期词，避免把「周三见面」改成「见面」。
    像「（今天、明天休息）」这种只讲日期的括号片段整段删掉；删完只剩标点也清掉。
    """
    s = str(text or "").strip()
    if not s:
        return ""

    def _drop_date_only(m):
        inner = _DAY_WORD_RE.sub("", m.group(1))
        inner = re.sub(r"[\s、，,;；·/]+", "", inner)
        return "" if inner in _DATE_ONLY_REST else m.group(0)

    s = _DATE_ONLY_PAREN_RE.sub(_drop_date_only, s)
    s = _DAY_WORD_RE.sub("", s)
    s = _DUP_PUNCT_RE.sub("", s)
    s = _EMPTY_PAREN_RE.sub("", s)
    s = _EDGE_PUNCT_RE.sub("", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()

def entry_body_text(entry) -> str:
    """正文列：去掉相对日词；时间写「待定」时，把原来的模糊时间括在正文后面。"""
    e = entry or {}
    text = strip_day_words(e.get("text"))
    if entry_time_label(e) != "待定":
        return text
    raw = strip_day_words(e.get("time_text"))
    if not raw or _TBD_RE.search(raw) or raw in text:
        return text
    return ("%s（%s）" % (text, raw)) if text else raw

# ---------- 联动合并（2026-11-04）----------
# 文案里提到另一位主播（可能是昵称/英文名/去后缀的写法），且两人同一时间都有排期，
# 基本可以认定是同一场联动，合成一条，免得表上出现两条各说各的。
NICK_STOP = {"channel", "official", "ch", "tv", "live", "and", "with", "the",
             "官方", "频道", "直播", "字幕组", "公式"}

def streamer_aliases(name: str) -> list[str]:
    """从主播名拆出可用于文案匹配的别名：全名、去后缀、中英分段。"""
    raw = (name or "").strip()
    if not raw:
        return []
    out = {raw}
    for part in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", raw):
        out.add(part)
    for suf in ("Channel", "CHANNEL", "Official", "OFFICIAL", "官方", "频道"):
        if raw.endswith(suf) and len(raw) > len(suf):
            out.add(raw[:-len(suf)].strip(" _-"))
    return sorted({a for a in out if len(a) >= 2 and a.lower() not in NICK_STOP},
                  key=len, reverse=True)

def mentions_alias(text: str, alias: str) -> bool:
    """文案里是否点到这个名字（英文按词边界，中文按子串）。"""
    if not text or not alias:
        return False
    if re.fullmatch(r"[A-Za-z0-9]+", alias):
        return re.search(r"(?<![A-Za-z])" + re.escape(alias) + r"(?![A-Za-z])",
                         text, re.I) is not None
    return alias in text

def load_aliases(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """人工登记的主播昵称（streamer_alias 表）：{uid: [别名…]}。"""
    try:
        rows = conn.execute("SELECT uid, alias FROM streamer_alias").fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[int, list[str]] = {}
    for row in rows:
        try:
            uid, alias = int(row[0]), str(row[1] or "").strip()
        except (TypeError, ValueError):
            continue
        if alias:
            out.setdefault(uid, []).append(alias)
    return out

# ---------- 常驻联动节目（2026-11-05）----------
# 固定主持的固定节目（屋里吃饭 / Potluck 等）：动态里经常只写嘉宾、不写常驻主持。
# 按节目名/别名认出这一场，把常驻主持补进同一条，日程表才不会漏人。
PROGRAM_SEED = [
    {"name": "屋里吃饭",
     "aliases": ["屋里吃饭", "屋里说话", "Potluck", "Namae's Lovely Potluck"],
     "hosts": [29080, 3821157], "weekday": "", "time": "",
     "note": "常驻主持：无前Namae、东爱璃Lovely；动态常只写嘉宾（花礼Harei、星汐Seki 等），按节目名补主持"},
]

PROGRAM_ALIAS_MIN_CJK = 2      # 中文别名短了会误吃（「屋里」不等于「屋里吃饭」）

PROGRAM_ALIAS_MIN_ASCII = 4

def _json_arr(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return val if isinstance(val, list) else []

# ---------- 排重：同一场可能既写进周表、又有单场预告 ----------
# 2026-11-05：一场安排常常两边都有，且时间/措辞会不一样（「和鸽做出轨测试」vs
# 「和黎歌Neeko一起做出轨测试题」、「屋里吃饭 20:00」vs 单场改 19:00）。
# 这里按「同一天 + 同一主播 + 内容相近 / 同一时间」认成同一场，再按发布时间定谁为准。
_PUNCT_RE = re.compile(r"[\s\u3000·,，。.!！?？:：;；、()（）\[\]【】<>《》~～\-—_/\\|+＋*#]")

def _flat(text: str) -> str:
    """去掉标点空白、统一小写，用来比内容。"""
    return _PUNCT_RE.sub("", (text or "").lower())

def _lcs(a: str, b: str) -> int:
    """最长公共子串长度（中文短句够用，动态规划 O(n*m)）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b, 1):
            v = prev[j - 1] + 1 if ca == cb else 0
            cur.append(v)
            if v > best:
                best = v
        prev = cur
    return best

def same_show(a: str, b: str) -> bool:
    """两段文案是不是同一场节目：包含关系、二元组相似度过半，或公共子串够长。

    中文短句里二元组会被英文名/连接词稀释（「和鸽做出轨测试」vs
    「和黎歌Neeko一起做出轨测试题」只有 0.25），所以再兜一层最长公共子串。
    """
    fa, fb = _flat(a), _flat(b)
    if not fa or not fb:
        return False
    if fa == fb or fa in fb or fb in fa:
        return True
    if len(fa) < 2 or len(fb) < 2:
        return False
    ga = {fa[i:i + 2] for i in range(len(fa) - 1)}
    gb = {fb[i:i + 2] for i in range(len(fb) - 1)}
    if ga and gb and len(ga & gb) / len(ga | gb) >= 0.45:
        return True
    l = _lcs(fa, fb)
    return l >= 4 and l >= min(len(fa), len(fb)) * 0.4

# 单场文案里出现这些词，说明是「另加一场」而不是「原定的那场改期」
EXTRA_HINTS = ("加播", "加场", "追加", "再加", "第二场", "额外", "补场")

def _join_note(old: str, new: str) -> str:
    old = (old or "").strip()
    new = (new or "").strip()
    if not old:
        return new
    if not new or new in old:
        return old
    return old + " · " + new

def _pub_times(conn: sqlite3.Connection, event_ids) -> dict[str, float]:
    """event_id → 动态发布时间。用来判断周表和单场谁更新。"""
    ids = [i for i in dict.fromkeys(event_ids) if i]
    out: dict[str, float] = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        sql = ("SELECT event_id, occurred_at FROM event WHERE event_id IN (%s)"
               % ",".join("?" * len(chunk)))
        try:
            for eid, ts in conn.execute(sql, chunk):
                out[eid] = float(ts or 0)
        except sqlite3.OperationalError:
            break
    return out

def load_programs(conn: sqlite3.Connection, seed: bool = True) -> list[dict]:
    """常驻节目登记表。空表时灌入 PROGRAM_SEED，老库也能直接用。"""
    try:
        rows = conn.execute("SELECT * FROM weekly_program ORDER BY program_id").fetchall()
    except sqlite3.OperationalError:
        return []
    if not rows and seed:
        now = time.time()
        conn.row_factory = sqlite3.Row
        for p in PROGRAM_SEED:
            conn.execute("INSERT OR IGNORE INTO weekly_program(name, aliases_json, hosts_json,"
                         " weekday, time, note, enabled, created_at, updated_at)"
                         " VALUES(?,?,?,?,?,?,1,?,?)",
                         (p["name"], json.dumps(p["aliases"], ensure_ascii=False),
                          json.dumps(p["hosts"]), p.get("weekday") or "",
                          p.get("time") or "", p.get("note") or "", now, now))
        conn.commit()
        rows = conn.execute("SELECT * FROM weekly_program ORDER BY program_id").fetchall()
    out = []
    for r in rows:
        out.append({"program_id": int(r["program_id"]), "name": r["name"],
                    "aliases": [str(a) for a in _json_arr(r["aliases_json"]) if str(a).strip()],
                    "hosts": [int(u) for u in _json_arr(r["hosts_json"]) if str(u).isdigit()],
                    "weekday": r["weekday"] or "", "time": r["time"] or "",
                    "note": r["note"] or "", "enabled": bool(r["enabled"]),
                    "updated_at": r["updated_at"]})
    return out

def match_program(text: str, programs: list[dict]) -> dict | None:
    """文案里点到哪个常驻节目（取最长别名，避免短名吃掉长名）。"""
    if not text or not programs:
        return None
    best = None
    best_len = 0
    for p in programs:
        if not p.get("enabled", True):
            continue
        for alias in [p.get("name") or ""] + list(p.get("aliases") or []):
            a = (alias or "").strip()
            if not a:
                continue
            min_len = PROGRAM_ALIAS_MIN_CJK if re.search(r"[\u4e00-\u9fff]", a) \
                else PROGRAM_ALIAS_MIN_ASCII
            if len(a) < min_len:
                continue
            if mentions_alias(text, a) and len(a) > best_len:
                best, best_len = p, len(a)
    return best

def program_public(programs: list[dict], subjects: dict | None = None) -> list[dict]:
    """给管理台/周视图用的节目清单（主持补名字、带本周命中场次）。"""
    subjects = subjects or {}
    return [{"program_id": p["program_id"], "name": p["name"], "aliases": p["aliases"],
             "weekday": p.get("weekday") or "", "time": p.get("time") or "",
             "note": p.get("note") or "", "enabled": p.get("enabled", True),
             "hosts": [{"uid": u, "uname": subjects.get(u) or ("UID%s" % u)}
                       for u in p.get("hosts") or []]} for p in programs]

MENTION_RE = re.compile(r"@([0-9A-Za-z_\u4e00-\u9fff·]{2,20})")

LINK_CLAUSE_RE = re.compile(r"[（(][^（）()]{0,60}?(?:联动|联投|连麦)[^（）()]{0,10}?[）)]")

WITH_RE = re.compile(r"\bwith\b\s+[A-Za-z0-9_/\u4e00-\u9fff·]+", re.I)

NAME_SPLIT_RE = re.compile(r"[/、,，·\s]+")

def extract_names(text: str) -> list[str]:
    """从文案里抠出「另一个主播可能叫什么」：@提及 + 「与…联动」里的名字。"""
    if not text:
        return []
    raw: list[str] = [m.group(1) for m in MENTION_RE.finditer(text)]
    for m in re.finditer(r"与([^（）()]{2,60}?)(?:联动|联投|连麦)", text):
        raw.extend(p.strip() for p in NAME_SPLIT_RE.split(m.group(1)))
    out: list[str] = []
    seen: set[str] = set()
    for n in raw:
        if len(n) < 2 or n.lower() in NICK_STOP:
            continue
        k = n.lower()
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out

def _match_known(name: str, aliases: dict[int, list[str]]) -> int | None:
    """名字对到监控名单：别名相等，或互相包含（YUNA ↔ 由菜Yuna、AWAWA ↔ 安晚aWa）。"""
    low = (name or "").lower()
    if len(low) < 2:
        return None
    for uid, al in aliases.items():
        for a in al:
            la = a.lower()
            if low == la:
                return uid
            if len(la) >= 3 and (la in low or low in la):
                return uid
    return None

def condense_texts(texts: list[str]) -> str:
    """联动文案提炼：去掉重复的点名/联动说明，只留一条主文案，再补对方独有的《节目名》。"""
    clean: list[str] = []
    for t in texts:
        t = MENTION_RE.sub("", t or "")
        t = LINK_CLAUSE_RE.sub("", t)
        t = WITH_RE.sub("", t)
        t = re.sub(r"\s+", " ", t).strip(" /、,，·-")
        t = re.sub(r"\s+([）)])", r"\1", t)
        t = re.sub(r"\s+([（(])", r"\1", t)
        t = re.sub(r"([（(])\s+", r"\1", t)
        t = re.sub(r"[（(]\s*[）)]", "", t)
        if t and t not in clean:
            clean.append(t)
    if not clean:
        return ""
    body = max(clean, key=len)
    extras: list[str] = []
    for t in clean:
        if t is body:
            continue
        for m in re.findall(r"《[^》]{1,24}》", t):
            if m not in body and m not in extras:
                extras.append(m)
    return " · ".join([body] + extras)

def _collab_groups(items: list[dict], aliases: dict[int, list[str]]) -> list[list[int]]:
    """同一时间的条目里，按「文案提到对方」把主播串成组（并查集）。"""
    uids = sorted({e["uid"] for e in items})
    parent = {u: u for u in uids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in items:                              # 人工联动写死的 uids 直接并组
        crew = [int(u) for u in (e.get("uids") or []) if int(u) in parent]
        for u in crew[1:]:
            ra, rb = find(crew[0]), find(u)
            if ra != rb:
                parent[rb] = ra
    for a in uids:
        text = " ".join((e.get("text") or "") for e in items if e["uid"] == a)
        for b in uids:
            if a == b:
                continue
            if any(mentions_alias(text, al) for al in aliases.get(b, [])):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra
    groups: dict[int, list[int]] = {}
    for u in uids:
        groups.setdefault(find(u), []).append(u)
    return [g for g in groups.values() if len(g) > 1]

def merge_collabs(days: list[dict], extra_aliases: dict | None = None,
                  subjects: dict | None = None) -> dict:
    """把同一时间的联动条目合并。

    返回 {"pairs": 合并掉的对数, "externals": [文案里点到、但不在监控名单的名字…]}。
    · 文案点到的监控主播即使这一场没排期，也补一条 derived 日程（联动对象没填周表也要上表）；
    · subjects: {uid: uname}，用来给「本周没出现过」的主播认名字；
    · extra_aliases: 人工登记的昵称 {uid: [别名…]}。
    """
    unames: dict[int, str] = dict(subjects or {})
    for day in days:
        for e in day["entries"]:
            unames.setdefault(e["uid"], e.get("uname") or ("UID%s" % e["uid"]))
    aliases: dict[int, list[str]] = {}
    for uid, n in unames.items():
        al = set(streamer_aliases(n))
        al.update(a for a in (extra_aliases or {}).get(uid, []) if a)
        aliases[uid] = sorted(al, key=len, reverse=True)
    pairs = 0
    externals: dict[str, None] = {}
    for day in days:
        buckets: dict[str, list[dict]] = {}
        for e in day["entries"]:
            if e.get("cancelled") or not e.get("time"):
                continue
            buckets.setdefault(e["time"], []).append(e)
        for time_key, items in buckets.items():
            if not items:
                continue
            # 文案点到 / 人工联动指定的监控主播，这一场没排期也补一条（derived）
            present = set()
            for e in items:
                present.add(int(e["uid"]))
                present.update(int(u) for u in e.get("uids") or [] if str(u).isdigit())

            def _derive(u: int) -> None:
                present.add(u)
                items.append({"uid": u, "uname": unames.get(u) or ("UID%s" % u),
                              "time": time_key, "time_text": "", "text": "",
                              "image_url": None, "url": None, "confidence": None,
                              "needs_review": False, "week_label": None,
                              "source": "collab", "kind": "derived",
                              "derived": True, "updated_at": None})
                day["entries"].append(items[-1])

            for e in list(items):
                for name in extract_names(e.get("text") or ""):
                    uid = _match_known(name, aliases)
                    if uid and uid not in present and uid in unames:
                        _derive(uid)
            for e in list(items):                      # 人工联动：对象没排期也补
                for u in e.get("uids") or []:
                    try:
                        u = int(u)
                    except (TypeError, ValueError):
                        continue
                    if u not in present and u in unames:
                        _derive(u)
            # 不在监控名单的名字：留给 B 站搜索（console 侧解析）
            for e in items:
                for name in extract_names(e.get("text") or ""):
                    if _match_known(name, aliases) is None:
                        externals.setdefault(name, None)
            if len(items) < 2:
                continue
            for comp in _collab_groups(items, aliases):
                mine = [e for e in items if e["uid"] in comp]
                if len({e["uid"] for e in mine}) < 2:
                    continue
                # 用 sorted() 而不是就地 sort：key 里要遍历 mine，
                # 就地 sort 时列表处于重排中间态，遍历会拿到残缺数据（踩过）。
                mine = sorted(mine, key=lambda e: (0 if any(
                    mentions_alias(e.get("text") or "", al)
                    for other in mine if other["uid"] != e["uid"]
                    for al in streamer_aliases(other.get("uname"))) else 1,
                    e["uid"]))
                primary = mine[0]
                # 名字按 uid 去重：人工联动条目自己也带 uids/partners，
                # 直接拼 uname 会出现「A × B × A」这种叠加（踩过）。
                names: dict[int, str] = {}
                order: list[int] = []
                for e in mine:
                    crew = [int(u) for u in (e.get("uids") or [e["uid"]])]
                    parts = (e.get("uname") or "").split(" × ")
                    if len(parts) == len(crew):
                        npairs = list(zip(crew, parts))
                    else:
                        npairs = [(int(e["uid"]), parts[0] if parts else "")]
                        npairs += [(int(p["uid"]), (p.get("uname") or "").split(" × ")[0])
                                   for p in e.get("partners") or []]
                    for u, n in npairs:
                        if u not in names:
                            names[u] = n or unames.get(u) or ("UID%s" % u)
                            order.append(u)
                        elif not names[u] and n:
                            names[u] = n
                merged = dict(primary)
                merged["uname"] = " × ".join(names[u] for u in order)
                merged["text"] = condense_texts([e.get("text") or "" for e in mine]) \
                    or merged.get("text") or ""
                merged["uids"] = order
                merged["partners"] = [{"uid": u, "uname": names[u]} for u in order
                                      if u != primary["uid"]]
                merged["collab"] = True
                for e in mine:
                    if e in day["entries"]:
                        day["entries"].remove(e)
                pairs += len(mine) - 1
                day["entries"].append(merged)
    return {"pairs": pairs, "externals": list(externals)}

def normalize_days(days) -> list[dict]:
    """把模型给的 days 洗成 [{day, entries:[{time,time_text,text}], note}]，按周一到周日排序。"""
    out: dict[int, dict] = {}
    for item in days or []:
        if not isinstance(item, dict):
            continue
        idx = normalize_day(str(item.get("day") or item.get("weekday") or ""))
        if idx is None:
            continue
        entries = []
        for e in item.get("entries") or []:
            if isinstance(e, str):
                e = {"text": e}
            if not isinstance(e, dict):
                continue
            t, raw = normalize_time(e.get("time") or e.get("time_text") or "")
            text = str(e.get("text") or e.get("activity") or e.get("title") or "").strip()
            if not (t or text):
                continue
            entries.append({"time": t, "time_text": raw if t is None and raw else "",
                            "text": text})
        if not entries and not item.get("note"):
            continue
        out[idx] = {"day": WEEKDAYS[idx], "entries": entries,
                    "note": str(item.get("note") or "").strip()}
    return [out[i] for i in sorted(out)]

def week_label_of(week_start: str) -> str:
    d = dt.date.fromisoformat(week_start)
    end = d + dt.timedelta(days=6)
    return f"{d.month}.{d.day}~{end.month}.{end.day}"

# ---------------- 候选扫描 ----------------
def _images_of(extra: dict, cover: str | None) -> list[dict]:
    imgs = extra.get("images") or []
    out = [x for x in imgs if isinstance(x, dict) and x.get("url")]
    if not out and cover:
        out = [{"url": cover, "w": 0, "h": 0}]
    return out

def monday_of_iso(value) -> str:
    d = dt.date.fromisoformat(str(value)[:10])
    return (d - dt.timedelta(days=d.weekday())).isoformat()

_DATE_TXT_RE = re.compile(r"(\d{1,2})\s*[.月/]\s*(\d{1,2})\s*[日号]?")

def _parse_date_text(value, near_ts: float) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        pass
    m = _DATE_TXT_RE.search(s)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    base = dt.date.fromtimestamp(float(near_ts))
    for y in (base.year, base.year + 1, base.year - 1):
        try:
            d = dt.date(y, month, day)
        except ValueError:
            continue
        if abs((d - base).days) <= 14:
            return d.isoformat()
    return None

def resolve_session_date(sess: dict, posted_ts: float, week_start: str | None = None):
    """(日期, 0=图上写明 / 1=靠星期推 / 2=按发帖日猜)。

    单场预告很少有完整日期：能读到日期最好；只写「周三」就按那一周推；
    都没有就按发帖日算（「明天」「明晚」顺延一天）。
    """
    got = _parse_date_text(sess.get("date"), posted_ts)
    if got:
        return got, 0
    idx = normalize_day(str(sess.get("day") or ""))
    if idx is not None and week_start:
        return (dt.date.fromisoformat(week_start) + dt.timedelta(days=idx)).isoformat(), 1
    base = dt.date.fromtimestamp(float(posted_ts))
    text = str(sess.get("text") or "")
    if any(k in text for k in ("明天", "明晚", "明日", "次日")):
        base = base + dt.timedelta(days=1)
    return base.isoformat(), 2

def _now_ts() -> float:
    """manual_add 的参数里有 time，别让形参把 time 模块遮住。"""
    return time.time()
