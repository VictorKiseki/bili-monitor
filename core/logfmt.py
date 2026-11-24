"""日志解析与筛选（Web 管理台「日志」页用）

日志行格式（2026-10-28 起）：
    2026-10-28 02:47:52 [动态] 残Tony 本轮为空（重试 2 次），跳过且不动水位
历史行（旧格式，没有日期）：
    02:47:52 [动态] ...

三个筛选维度：
    date  日期（日志按天分文件；三进程拆分后，同一天有 logs/collector-* 与 logs/pusher-* 两份）
    tag   类型（方括号里的标签：直播 / 动态 / 主循环轮次 / 统计 / 总开关 / 错误 …）
    level 严重程度（error 错误 / warn 警告 / info 信息）
"""
from __future__ import annotations

import re
from pathlib import Path

# 行首时间戳：带日期或只有时分秒
_TS_FULL = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s+(.*)$")
_TS_TIME = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+(.*)$")
_TAG = re.compile(r"^\[([^\]]{1,12})\]\s*(.*)$")
# 「错误 0 / 失败 0」是统计里的零值，不是错误；先剔掉再判级别
_ZERO_COUNT = re.compile(
    r"(?:错误|失败|异常|error|errors|重试/死信)\s*[:：=]?\s*0(?!\d)", re.I)

# 按日日志的三个家族：bot 是 2026-11-11 拆分前的单进程主循环，collector / pusher 是拆分后的两个进程
LOG_FAMILIES = {"bot": "主循环", "collector": "采集", "pusher": "推送"}
LOG_NAME = "bot-%s.log"          # 兼容旧引用：拆分前只有 bot-YYYY-MM-DD.log
LEGACY_NAME = "bot.log"
_DAILY_LOG = re.compile(r"^(bot|collector|pusher)-(\d{4}-\d{2}-\d{2})\.log$")

# UI 下拉框的顺序（也是「常见标签」）
KNOWN_TAGS = [
    "启动", "通道", "目标", "管理台", "直播", "动态", "主循环轮次", "统计",
    "总开关", "错误", "停止", "心跳", "基线", "死信", "退出", "完成", "FAIL",
    "补全",
]

ERROR_WORDS = ("失败", "异常", "错误", "崩溃", "Traceback", "Error", "error",
               "locked", "拒绝", "超时", "不可用")
WARN_WORDS = ("为空", "重试", "暂停", "停用", "丢弃", "退避", "重启",
              # 2026-11-08 取消「跳过」：预约汇总里的「跳过 N 条」是正常未入表，不是警告
              "没有找到", "缺少", "未开播", "轮播",
              # 2026-11-08 信息补全：重取过还缺字段——不是错误，但要能在日志页筛出来
              "仍缺", "不完整")


def parse_line(raw: str) -> dict:
    """一行 → {raw, date, time, tag, level, msg}。认不出来的行原样保留、算 info。"""
    raw = raw.rstrip("\n")
    date = ""
    tm = ""
    rest = raw
    m = _TS_FULL.match(raw)
    if m:
        date, tm, rest = m.group(1), m.group(2), m.group(3)
    else:
        m = _TS_TIME.match(raw)
        if m:
            tm, rest = m.group(1), m.group(2)
    tag = ""
    msg = rest
    mt = _TAG.match(rest)
    if mt:
        tag, msg = mt.group(1), mt.group(2)
        # 旧日志把轮次写成「轮次 419」，归一后类型筛选只出现一项
        if tag == "轮次" or tag.startswith("轮次 "):
            tag = "主循环轮次"
    return {"raw": raw, "date": date, "time": tm, "tag": tag,
            "level": classify(tag, msg, raw), "msg": msg}


def classify(tag: str, msg: str, raw: str = "") -> str:
    """error / warn / info。先看标签，再看关键字（标签里没有级别信息时）。"""
    text = _ZERO_COUNT.sub("", f"{tag} {msg} {raw}")
    if tag in ("错误",):
        return "error"
    if any(w in text for w in ERROR_WORDS):
        return "error"
    if tag in ("总开关", "停止", "死信"):
        return "warn"
    if any(w in text for w in WARN_WORDS):
        return "warn"
    return "info"


def list_logs(log_dir: Path) -> list[dict]:
    """列出现有日志文件，最新的在前。日期从文件名取，旧文件日期留空。

    一条记录 = 一个按日文件；三进程拆分后同一天会有多条（采集 + 推送），
    每条带 kind（bot / collector / pusher），由调用方决定怎么合并。
    """
    log_dir = Path(log_dir)
    out: list[dict] = []
    if not log_dir.is_dir():
        return out
    try:
        entries = sorted(log_dir.iterdir())
    except OSError:                       # 目录读不了（权限 / 被占用）时当没有日志，别让页面 500
        return out
    for p in entries:
        if p.name == LEGACY_NAME:
            kind, date, label = "bot", "", "历史 bot.log（旧格式）"
        else:
            m = _DAILY_LOG.match(p.name)
            if not m:
                continue              # collector.console.log / bot.log.1 之类不是按日结构化日志
            kind, date = m.group(1), m.group(2)
            label = "%s %s" % (date, LOG_FAMILIES[kind])
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "date": date, "kind": kind,
                    "label": label, "bytes": st.st_size, "path": str(p)})
    out.sort(key=lambda r: (r["date"], r["kind"], r["path"]), reverse=True)
    return out


def _tail_bytes(path: Path, max_bytes: int) -> list[str]:
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        text = f.read().decode("utf-8", "replace")
    lines = text.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]          # 第一行可能被截断
    return lines


# S6（2026-11-14）：date=all 且过滤条件很窄时，以前会把每个日文件都读一遍
# （N 个文件 × 每文件 2 MiB），跑一年就是几百 MB 的同步 IO，还把页面卡住。
# 现在按「最近 90 个日文件 + 总 32 MiB」封顶，超出会在返回里标记 limited。
MAX_SCAN_FILES = 90
MAX_SCAN_BYTES = 32 * 1024 * 1024


def read_logs(log_dir: Path, date: str = "", tag: str = "", level: str = "",
              q: str = "", limit: int = 200, max_bytes: int = 2 * 1024 * 1024,
              max_files: int = MAX_SCAN_FILES,
              max_total_bytes: int = MAX_SCAN_BYTES) -> dict:
    """按日期/类型/级别/关键字过滤，返回最近的 limit 行 + 统计。

    默认看「最新那一天」：拆分后同一天有采集与推送两份日志，两份都读，
    合并后按行首时间重排 —— 只读一份会漏掉另一半（2026-11-14 日志页读空的教训）。
    """
    files = list_logs(log_dir)
    limited = False
    if date in ("all", "*"):
        picked = files[:max(1, int(max_files))]
        limited = len(files) > len(picked)
    elif date:
        picked = [f for f in files if f["date"] == date]
    else:
        newest = files[0]["date"] if files else ""       # 默认最新一天
        picked = [f for f in files if f["date"] == newest]

    rows: list[dict] = []
    counts = {"error": 0, "warn": 0, "info": 0}
    tags: dict[str, int] = {}
    scanned = 0
    scan_bytes = 0
    for idx, f in enumerate(picked):
        if scan_bytes >= max_total_bytes:
            limited = True
            break
        try:
            lines = _tail_bytes(Path(f["path"]), max_bytes)
        except OSError:
            continue
        scan_bytes += min(int(f.get("bytes") or 0), max_bytes)
        parsed = [parse_line(ln) for ln in lines]
        scanned += len(parsed)
        for r in parsed:
            counts[r["level"]] = counts.get(r["level"], 0) + 1
            if r["tag"]:
                tags[r["tag"]] = tags.get(r["tag"], 0) + 1
            if not r["date"]:
                r["date"] = f["date"]
            r["src"] = f["kind"]           # 合并显示时能分清这行来自采集还是推送
            if tag and r["tag"] != tag:
                continue
            if level and r["level"] != level:
                continue
            if q and q.lower() not in r["raw"].lower():
                continue
            rows.append(r)
        last_of_date = idx == len(picked) - 1 or picked[idx + 1]["date"] != f["date"]
        if last_of_date and len(rows) >= limit:
            break          # 后面的文件都在更早的日期，切掉也进不了最后 limit 条

    # 跨文件（采集 + 推送）合并后按真实时间重排：文件是分段堆的，不能只按文件顺序切尾巴
    rows.sort(key=lambda r: (r["date"], r["time"]))
    rows = rows[-limit:]
    return {"ok": True, "files": files, "picked": [f["name"] for f in picked],
            "scanned": scanned, "limited": limited, "scanned_bytes": scan_bytes,
            "counts": counts,
            "tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])),
            "levels": {"error": "错误", "warn": "警告", "info": "信息"},
            "rows": rows}
