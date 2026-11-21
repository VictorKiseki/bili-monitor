"""T1 → T2 的桥：把采集层结构体转成 Event（契约 v0）

两条规则（docs/05 第 1、9 节）：
  · LIVE_RCMD（平台自动开播提醒）丢弃 —— fetch_dynamics 已过滤，这里再兜一次
  · 置顶动态会混在首条，不能「取第一条当最新」，去重靠 id_str 水位，不靠顺序
"""
from __future__ import annotations

import time

from collector.bili import DynItem, LiveState, _to_ts
from shared.contracts import Event, EventType, make_event_id

_SUBTYPE_TO_TYPE = {
    "WORD": EventType.DYNAMIC,
    "DRAW": EventType.DYNAMIC,
    "FORWARD": EventType.DYNAMIC,
    "AV": EventType.VIDEO,
    "ARTICLE": EventType.ARTICLE,
}


def event_from_live(prev_status: int, cur: LiveState, now: float | None = None) -> Event | None:
    """状态变化才产出事件；2 是轮播，按未开播处理。

    occurred_at：下播时接口的 live_time 已经是 0，不能直接拿来当发生时间，
    否则事件时间会变成 1970（2026-10-28 实测踩到），下播统一用「检测到的那一刻」。
    """
    now_living = cur.live_status == 1
    was_living = prev_status == 1
    if now_living == was_living:
        return None
    room_id = cur.room_id or 0
    start_ts = _to_ts(cur.live_time)
    dedup = str(start_ts or int(cur.live_status))
    t = EventType.LIVE_START if now_living else EventType.LIVE_END
    return Event(
        event_id=make_event_id(cur.uid, t, dedup),
        subject_uid=cur.uid,
        type=t,
        occurred_at=start_ts or int(now if now is not None else time.time()),
        dedup_key=dedup,
        title=cur.title or "",
        url=f"https://live.bilibili.com/{room_id}" if room_id else "",
        cover_url=cur.cover or None,
        summary=None,
        extra={"uname": cur.uname, "room_id": room_id, "area_name": cur.area_name,
               "start_ts": start_ts, "online": cur.online,
               "face": cur.face, "cover": cur.cover},
    )


def event_from_dynamic(item: DynItem, uid: int, uname: str) -> Event | None:
    """动态/视频/专栏统一从动态流派生。未知类型返回 None（不猜）。"""
    if item.type == "DYNAMIC_TYPE_LIVE_RCMD" or item.subtype == "LIVE_RCMD":
        return None
    t = _SUBTYPE_TO_TYPE.get(item.subtype)
    if t is None:
        return None
    # 昵称优先用动态接口自己返回的作者名（名单里的名字可能和 B 站现昵称不一致）
    uname = item.uname or uname
    # 2026-10-28 v6：卡片要用完整正文（含换行）、内联表情 token、转发原文、视频资料。
    # text 与 summary 分工：summary 给纯文字消息（压成一句），text 给卡片（保留换行）。
    extra = {"uname": uname, "subtype": item.subtype, "image_count": item.image_count,
             "face": item.face, "text": item.text, "rich": item.rich,
             "fwd": item.fwd, "video": item.video,
             # 周表识别要用：全部图片 + 宽高（老库里没有这个键，读取方要会兜底）
             "images": item.images,
             # 2026-11-05 直播预约模块（没挂预约就是 None）
             "reserve": item.reserve or None}
    if item.subtype == "FORWARD":
        extra["forward_from"] = item.title.replace("转发了 @", "", 1)
    if item.subtype == "AV":
        extra["bvid"] = item.url.rsplit("/", 1)[-1] if item.url else ""
    return Event(
        event_id=make_event_id(uid, t, item.id_str),
        subject_uid=uid,
        type=t,
        occurred_at=int(item.pub_ts or 0),
        dedup_key=item.id_str,
        title=item.title or "",
        url=item.url or "",
        cover_url=item.cover or None,
        summary=item.summary or None,
        extra=extra,
    )
