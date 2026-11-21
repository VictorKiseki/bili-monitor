"""取到的动态/直播信息不完整时，定向重取几次再放行（2026-11-08）。

为什么要有这个模块（2026-11-08 用户发来两张缺内容的卡片）：

  · 开播卡：头像空白、名字显示成 UID1041474702 —— 批量直播接口那一轮把 uname/face 漏了，
    事件 extra 里就是空的（库里有原样记录：extra.uname=""、extra.face=""）；
  · 动态卡：只有图没有正文，图片也没有宽高 —— 那条动态走了 desktop 兜底端点，
    它只给 dyn_draw（没有 major/desc），图文动态的正文取不到，图也只有 src。

三条采集路径（逐人轮询 / 关注流 / 补漏）最后都进 core/watcher.py 的 _digest_items，
所以补全放在那里：产事件之前看缺口，有就定向重取，最多 retries 次。

刻意**不**在这里决定「推不推」：漏一条动态的代价远大于卡片少一行正文 ——
用户至少能点链接进去看。所以重取仍不完整时原样放行，只记一条 [补全] 警告日志。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from collector.bili import DynItem, LiveState

# 一轮最多补几个条目：风控抖动时一批里可能有十几条都缺字段，全补会把请求量放大
MAX_ITEMS = 5
# 默认额外重取次数（不含第一次拿到的那份）
RETRIES = 2


def dynamic_gaps(it: DynItem) -> list[str]:
    """这条动态缺了什么（空 list = 完整）。顺序固定，日志里好读。"""
    gaps: list[str] = []
    subtype = it.subtype or ""
    if subtype in ("DRAW", "WORD") and not (it.text or "").strip():
        gaps.append("正文")
    if subtype in ("AV", "ARTICLE") and not (it.title or "").strip():
        gaps.append("标题")
    if not it.face:
        gaps.append("头像")
    imgs = [p for p in (it.images or []) if isinstance(p, dict)]
    if it.image_count and not imgs:
        gaps.append("配图")
    elif any(not (p.get("w") and p.get("h")) for p in imgs[:4]):
        gaps.append("图片尺寸")
    return gaps


def live_gaps(st: LiveState) -> list[str]:
    """这次直播状态缺了什么（空 list = 完整）。"""
    gaps: list[str] = []
    if not (st.uname or "").strip():
        gaps.append("主播名")
    if not st.face:
        gaps.append("头像")
    if not (st.title or "").strip():
        gaps.append("标题")
    if not st.cover:
        gaps.append("封面")
    return gaps


def _first(a: str, b: str) -> str:
    return a if (a or "").strip() else (b or "")


def _better_images(a: list[dict], b: list[dict]) -> list[dict]:
    """两边的图列表挑一份：带宽高的优先（desktop 端点给的没有宽高）。"""
    if not b:
        return a or []
    if not a:
        return b
    a_ok = all(p.get("w") and p.get("h") for p in a if isinstance(p, dict))
    b_ok = all(p.get("w") and p.get("h") for p in b if isinstance(p, dict))
    if b_ok and not a_ok:
        return b
    return a


def fill_dynamic(base: DynItem, better: DynItem | None) -> DynItem:
    """把 better 里非空的字段补进 base（base 已有的绝不覆盖）。

    正文/富文本成对取：只换正文不换 rich，卡片里的表情 token 就和正文对不上了。
    """
    if better is None:
        return base
    text = _first(base.text, better.text)
    rich = base.rich if (base.text or "").strip() or not better.rich else better.rich
    return replace(
        base,
        text=text,
        rich=rich,
        title=_first(base.title, better.title),
        summary=_first(base.summary, better.summary),
        url=_first(base.url, better.url),
        cover=_first(base.cover, better.cover),
        face=_first(base.face, better.face),
        uname=_first(base.uname, better.uname),
        images=_better_images(base.images or [], better.images or []),
        image_count=max(int(base.image_count or 0), int(better.image_count or 0)),
        fwd=base.fwd or better.fwd,
        video=base.video or better.video,
        reserve=base.reserve or better.reserve,
    )


def _refetch_dynamic(client: Any, uid: int, id_str: str) -> DynItem | None:
    """先问详情接口，失败再翻一次 space 首页找它（详情接口带着 opus 风格参数才有正文）。"""
    try:
        got = client.fetch_dynamic_detail(id_str)
        if got is not None and got.id_str == id_str:
            return got
    except Exception:                                   # noqa: BLE001
        pass
    try:
        items, _, _ = client.fetch_dynamics(uid, "", 10, 0)
    except Exception:                                   # noqa: BLE001
        return None
    for x in items or []:
        if x.id_str == id_str:
            return x
    return None


def enrich_dynamic(client: Any, uid: int, items: list[DynItem], *,
                   retries: int = RETRIES, max_items: int = MAX_ITEMS,
                   log: Callable[[str], None] | None = None,
                   name: str = "") -> tuple[list[DynItem], list[dict]]:
    """把缺字段的动态补一补。返回 (新列表, 补全报告)。

    只处理前 max_items 条有缺口的：多数轮次没有缺口，这个函数就是一次空跑。
    """
    out = list(items or [])
    report: list[dict] = []
    picks: list[tuple[int, DynItem, list[str]]] = []
    for i, it in enumerate(out):
        if len(picks) >= max(0, int(max_items or 0)):
            break
        g = dynamic_gaps(it)
        if g:
            picks.append((i, it, g))
    for i, it, before in picks:
        best = it
        tries = 0
        for _ in range(max(0, int(retries or 0))):
            tries += 1
            best = fill_dynamic(best, _refetch_dynamic(client, uid, it.id_str))
            if not dynamic_gaps(best):
                break
        out[i] = best
        after = dynamic_gaps(best)
        report.append({"id": it.id_str, "uid": uid, "before": before,
                       "after": after, "tries": tries})
        if after and log is not None:
            who = name or ("UID%s" % uid)
            log("[补全] %s 的动态 %s 重取 %d 次仍缺 %s，按现有内容推送"
                % (who, it.id_str, tries, "/".join(after)))
    return out, report


def enrich_live(client: Any, st: LiveState, *, retries: int = RETRIES,
                log: Callable[[str], None] | None = None,
                name: str = "") -> tuple[LiveState, list[str]]:
    """直播状态缺 uname/face/title/cover 时，用单房间接口再问几次。

    批量接口一次取 30 个号，偶尔会漏掉某个号的 uname/face（2026-11-08 实测到一次）；
    单房间的 room/v1/Room/get_info 字段是全的，拿它补。
    """
    before = live_gaps(st)
    if not before:
        return st, []
    best = st
    for _ in range(max(0, int(retries or 0))):
        try:
            fresh = client.fetch_live(st.room_id)
        except Exception:                               # noqa: BLE001
            continue
        best = replace(
            best,
            uname=_first(best.uname, fresh.uname),
            face=_first(best.face, fresh.face),
            title=_first(best.title, fresh.title),
            cover=_first(best.cover, fresh.cover),
            area_name=_first(best.area_name, fresh.area_name),
            online=int(best.online or 0) or int(fresh.online or 0),
        )
        if not live_gaps(best):
            break
    after = live_gaps(best)
    if after and log is not None:
        who = name or ("UID%s" % getattr(st, "uid", 0))
        log("[补全] %s 的直播状态重取后仍缺 %s，按现有内容推送"
            % (who, "/".join(after)))
    return best, after
