"""A. 消息模板：Event -> RenderedMessage（对应 docs/02 第 5.4 节 / docs/05 第 3 节）

T2 只负责渲染，不关心通道是官方还是 NapCat。
图片发不出去时降级为纯文字，由发送层按 ChannelCapability 决定，不在这里判断。
"""
from __future__ import annotations

from shared.contracts import Event, EventType, RenderedMessage

DEFAULT_MAX_LEN = 900

LIVE_URL = "https://live.bilibili.com/{room_id}"


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return ""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")


def _fmt_duration(seconds: int | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    if h:
        return f"{h}小时{m}分"
    return f"{m}分钟"


def _clip(text: str, n: int) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _big(n: int | None) -> str:
    if not n:
        return ""
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(int(n))


def render_live_start(ev: Event) -> str:
    room_id = ev.extra.get("room_id") or ""
    lines = [f"【开播】{ev.uname}"]
    if ev.title:
        lines.append(f"标题：{_clip(ev.title, 60)}")
    meta = []
    if ev.extra.get("area_name"):
        meta.append(f"分区：{ev.extra['area_name']}")
    if ev.occurred_at:
        meta.append(f"时间：{_fmt_ts(ev.occurred_at)}")
    if meta:
        lines.append("　".join(meta))
    if room_id:
        lines.append(LIVE_URL.format(room_id=room_id))
    lines.append(ev.url)
    return "\n".join(dict.fromkeys(lines))


def render_live_end(ev: Event) -> str:
    lines = [f"【下播】{ev.uname} 已下播"]
    start_ts = ev.extra.get("start_ts")
    duration = ev.extra.get("duration_s")
    if not duration and start_ts and ev.occurred_at:
        duration = int(ev.occurred_at) - int(start_ts)
    if start_ts:
        lines.append(f"开播：{_fmt_ts(int(start_ts))}")
    if duration:
        lines.append(f"本场时长：{_fmt_duration(int(duration))}")
    if ev.extra.get("online_max"):
        lines.append(f"本场最高人气：{_big(int(ev.extra['online_max']))}")
    if ev.title:
        lines.append(f"直播标题：{_clip(ev.title, 60)}")
    return "\n".join(lines)


def render_dynamic(ev: Event) -> str:
    """动态文案只保留「谁 + 怎么了 + 链接」，正文交给卡片图（2026-10-28 用户要求）。"""
    sub = ev.subtype
    if sub == "FORWARD":
        who = ev.extra.get("forward_from") or "动态"
        head = f"【动态】{ev.uname} 转发了 @{who} 的动态"
    else:
        head = f"【动态】{ev.uname} 发布了新动态"
    n = int(ev.extra.get("image_count") or 0)
    if n > 1:
        head += f"（{n} 图）"
    return "\n".join([head, ev.url])


def render_video(ev: Event) -> str:
    lines = [f"【视频】{ev.uname} 投稿了新视频"]
    title = _clip(ev.title or "", 60)
    dur = _fmt_duration(ev.extra.get("duration_s"))
    if title and dur:
        lines.append(f"{title}（{dur}）")
    elif title:
        lines.append(title)
    elif dur:
        lines.append(f"（{dur}）")
    lines.append(ev.url)
    return "\n".join(lines)


def render_article(ev: Event) -> str:
    lines = [f"【专栏】{ev.uname} 发布了新专栏"]
    if ev.title:
        lines.append(_clip(ev.title, 60))
    lines.append(ev.url)
    return "\n".join(lines)


_RENDERERS = {
    EventType.LIVE_START: render_live_start,
    EventType.LIVE_END: render_live_end,
    EventType.DYNAMIC: render_dynamic,
    EventType.VIDEO: render_video,
    EventType.ARTICLE: render_article,
}


def render(ev: Event, max_len: int = DEFAULT_MAX_LEN) -> RenderedMessage:
    """把 Event 渲染成 RenderedMessage。未知类型抛 ValueError（宁可炸，不要静默发错）。"""
    fn = _RENDERERS.get(ev.type)
    if fn is None:
        raise ValueError(f"未知事件类型：{ev.type}")
    text = fn(ev)
    if len(text) > max_len:
        # 超长时优先砍正文行（第 2 行），保住标题与链接
        rows = text.split("\n")
        if len(rows) > 3:
            rows[1] = _clip(rows[1], max(20, max_len - sum(len(r) + 1 for r in rows) + len(rows[1])))
        text = "\n".join(rows)[:max_len]
    image = ev.cover_url or None
    return RenderedMessage(
        text=text,
        image_url=image,
        link_url=ev.url or None,
        extra={"event_id": ev.event_id, "type": ev.type.value, "subtype": ev.subtype},
    )
