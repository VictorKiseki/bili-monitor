"""主循环：轮询 → 状态机 → 产出事件 → 入库 → 投递

设计要点全部来自 docs/05 第 9 节（28 个号实跑挖出来的坑）：
  1. 置顶动态无视时间排首条 → 判定「新动态」= id_str 没见过 且 pub_ts 高于水位
  2. 动态接口随机静默返回空 → 本轮为空绝不动水位，直接跳过等下一轮
  3. live_status 只认 1（2=轮播按未开播）
  4. room_id=0 的号跳过直播轮询
另外：新号第一次接入先做基线快照（last_poll_ts=0 作为标记），只记水位不推历史。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from collector.bili import _to_ts
from core import feed_collect as feedmod
from core import tasks as _tasks
from collector.enrich import enrich_dynamic, enrich_live, live_gaps
from collector.events import event_from_dynamic, event_from_live
from shared.contracts import Event, EventType, Subject, SubjectState, make_event_id

# 动态去重只回看最近 90 天（H1，2026-11-07）。
# seen 表以前按 uid 全量捞，跑上几个月就是几十万 id、每轮 29 次无界查询；
# 90 天以外的动态早过了推送价值，收窄后每次查询结果集有界、能走 (uid, pub_ts) 索引。
# Store 的 prune(seen_days=...) 用同一口径，两边别各写一个数字。
SEEN_WINDOW_DAYS = 90
_SEEN_WINDOW_S = SEEN_WINDOW_DAYS * 86400
# H8（2026-11-14）：上游偶尔给出未来时间戳（预约动态 / 服务端时钟异常），
# 水位取 max(pub_ts) 会被永久抬到未来，之后所有正常动态都被当成「置顶旧动态」
# 跳过、且不报错。允许 5 分钟时钟偏移，超出就封顶并在日志里说一次。
_WATERMARK_FUTURE_TOLERANCE_S = 300

# 心跳最小写入间隔（秒，B 项）。守护进程判断「主循环卡死」是分钟级，10s 粒度足够；
# 以前每轮首尾各写两个 key、共 4 次 UPSERT，写放大明显。
HEARTBEAT_MIN_INTERVAL_S = 10.0

# 每日剪枝失败的告警节流：失败后每个 tick 都会重试，别让日志跟着刷屏
PRUNE_WARN_INTERVAL_S = 3600.0

# 心跳两个 key 合成一条 UPSERT：一次线程往返、一次事务写两行
_HEARTBEAT_SQL = (
    "INSERT INTO meta(k, v) VALUES('last_tick', ?), ('last_stage', ?)"
    " ON CONFLICT(k) DO UPDATE SET v=excluded.v"
)

#: tick_once() 结果里给程序看的键，翻成人话给日志看。
_MODE_NAMES = {"full": "全开", "collect": "只采集", "off": "全停"}
_SOURCE_NAMES = {"feed": "关注流", "per_user": "逐人轮询"}


def round_text(res: dict) -> str:
    """把一轮的统计翻成一行中文。

    以前是 f"...轮 {res}"，日志里直接出现 {'live': 0, 'dynamic': 1, ...} 这种
    dict 原文 —— 那是调试输出，不是给人看的。管理台日志页会把它当正文展示，
    结构、引号、英文键名都很扎眼；这里翻成「直播 0 条 · 动态 1 条（关注流）· 已发 0 条 · 全开」。
    """
    if res.get("error"):
        return "出错，跳过本轮：%s" % (res["error"],)
    text = "直播 %d 条 · 动态 %d 条" % (int(res.get("live") or 0), int(res.get("dynamic") or 0))
    src = _SOURCE_NAMES.get(str(res.get("dynamic_source") or ""))
    if src:
        text += "（%s）" % src
    text += " · 已发 %d 条" % int(res.get("sent") or 0)
    mode = _MODE_NAMES.get(str(res.get("mode") or ""))
    if mode:
        text += " · " + mode
    return text


@dataclass
class PollConfig:
    live_interval_s: float = 45.0
    dynamic_interval_s: float = 240.0
    dyn_limit: int = 10
    empty_retries: int = 2
    # ---- 动态采集方式（2026-11-08 起）----
    # mode: auto（有 bili_jct 就读关注流）/ feed / per_user（旧的逐人轮询）
    mode: str = "auto"
    feed_pages: int = 3               # 一轮最多翻几页（1 页固定 20 条）
    feed_burst_threshold: int = 15    # 单页条目数到这个值视为突发，放宽页数上限
    feed_burst_pages: int = 3
    sweep_hours: float = 6.0          # 逐人补漏间隔；0 = 关掉（纯关注流）
    # 2026-11-11：补漏补到的「遗漏动态」也推送（之前只记已见，等于白补一次）。
    # sweep_max_age_min 因此改成「补漏回看窗口」：早于窗口的条目只记已见，
    # 防止停机重启 / 新加号时把很久以前的历史一次推出来。默认 12 小时，覆盖 6 小时补漏间隔。
    sweep_push_late: bool = True
    sweep_max_age_min: float = 720.0  # 补漏回看窗口（分钟）
    sweep_late_max: int = 30          # 一次补漏最多推多少条，超出只记已见（防刷屏）
    follow_sync: bool = True          # 把名单里没关注的号补关注（只关注不取关）
    follow_sync_hours: float = 6.0
    # 空闲睡眠拉到 5s（B 项）：一轮没有活时每秒醒来一次纯属空转；
    # 直播 45s / 动态 240s 的轮询粒度完全不受影响，停止请求最多晚 5s 响应。
    idle_sleep_s: float = 5.0
    # 信息补全（2026-11-08）：取到的动态/直播缺关键字段时定向重取几次再产事件。
    # 0 = 关掉重取；max_items 是一轮最多补几条（风控抖动时一批可能十几条都缺字段）。
    enrich_retries: int = 2
    enrich_max_items: int = 5


class Watcher:
    def __init__(self, store, client, cfg: PollConfig | None = None,
                 dispatcher=None, log: Callable[[str], None] = print, now=time.time,
                 board=None):
        self.store = store
        # 任务开关板（core/tasks.py）：直播/动态/派发/周表/GC 各自可单独暂停
        self.board = board or _tasks.BOARD
        self._off_logged: set[str] = set()
        self.client = client
        self.cfg = cfg or PollConfig()
        self.dispatcher = dispatcher
        self.log = log
        self._now = now
        self._last_live = 0.0
        self._last_dyn = 0.0
        # 卡片/图源缓存的兜底清理（正式删卡在投递到终态时，这里只捡漏）
        self._last_gc = 0.0
        self.gc_interval_s = 1800.0
        self._last_mode = None
        self.stop_check: Callable[[], bool] | None = None
        # 登录态看门狗：登录态失效是「静默退化」（空响应变多、变慢），必须主动告警
        self.auth_check_interval_s = 1800.0
        self._auth_checked_at = 0.0
        self._auth_retry = False
        self._auth_bad = False
        self._auth_warned_at = 0.0
        # 心跳节流状态（B 项）：上次真正写库的时间与阶段
        self._heartbeat_at = 0.0
        self._heartbeat_stage: str | None = None
        # 每日剪枝：按本地日期判重，失败不记住日期（下一个 tick 会重试）
        self._last_prune_day: str | None = None
        self._prune_warned_at = 0.0
        # 动态并发（D 项）：29 个号串行太慢，5 路足够把等待压下去又不至于把 B 站打疼；
        # 节流不交给客户端的 _throttle（它共享 _last_call 且没有锁），这里自己预约时隙。
        self.dynamic_concurrency = 5
        self._fetch_gate = asyncio.Lock()
        self._fetch_next_at = 0.0
        # 关注流 / 逐人轮询 / 补漏都在「查已读 → 标已读 → 产事件」，必须互斥，
        # 否则同一条动态会被两条路径各产一次事件（用户侧看到重复推送）。
        self._dyn_lock = asyncio.Lock()
        self._feed_warned = False
        self._feed_auth_warned = False
        self._bg_tasks: dict[str, asyncio.Task] = {}
        self._last_sweep = 0.0
        self._last_follow_sync = 0.0

    def _stopping(self) -> bool:
        """守护进程通过停止文件要求收尾；这里在每个可能拖很久的循环里轮询。"""
        return self.stop_check is not None and bool(self.stop_check())

    # ---------- 初始化 ----------
    async def init_subjects(self, subjects: list[Subject]) -> None:
        for s in subjects:
            await self.store.upsert_subject(s)
        await self._reconcile_subjects(subjects)

    async def _reconcile_subjects(self, subjects: list[Subject]) -> list[int]:
        """让库里的启用集合与监控名单一致：名单外停用、名单内启用。

        2026-11-10：管理台已移除手动启用 / 停用（所有号一律轮询），`enabled` 只剩
        「是否在名单里」一个含义。关注流按 uid 归并后只处理库里的 subject，但「关注了谁」
        是 B 站账号的状态、和名单是两回事——绕过管理台直接进库的历史行（花礼就是这样）
        会让名单外的人照样产事件、照样推送。这里每次启动对账一次：停用、不删除，
        水位和推送开关都留着，重新加回名单，下一次启动就恢复。
        """
        listed = {s.uid for s in subjects}
        off: list[Subject] = []
        on: list[Subject] = []
        for s in await self.store.list_subjects(enabled_only=False):
            want = s.uid in listed
            if bool(s.enabled) == want:
                continue
            await self.store.set_subject_enabled(s.uid, want)
            (on if want else off).append(s)
        if off:
            self.log("[名单] 停用 %d 个不在监控名单里的号：%s"
                     % (len(off), "、".join("%s(%d)" % (s.name or "未知", s.uid) for s in off)))
        if on:
            self.log("[名单] 恢复 %d 个重新加入名单的号：%s"
                     % (len(on), "、".join("%s(%d)" % (s.name or "未知", s.uid) for s in on)))
        return [s.uid for s in off]

    async def bootstrap(self) -> int:
        """对没有水位记录的新号做基线快照：只记，不推。返回处理数。"""
        subs = await self.store.list_subjects()
        states = await self.store.all_states()
        todo = [s for s in subs if s.uid not in states]
        if not todo:
            return 0
        live_map: dict = {}
        uids = [s.uid for s in todo if s.room_id]
        if uids:
            live_map = await asyncio.to_thread(self.client.fetch_live_batch, uids)
        for s in todo:
            if self._stopping():
                self.log("[停止] 基线阶段收到停止请求，收尾退出")
                return 0
            cur = live_map.get(s.uid)
            st = SubjectState(
                uid=s.uid, room_id=s.room_id,
                live_status=int(getattr(cur, "live_status", 0) or 0),
                last_live_start_ts=_to_ts(getattr(cur, "live_time", 0)) or None,
                last_poll_ts=0,        # 0 = 基线未完成，下一轮动态才真正对齐
            )
            await self.store.save_subject_state(st)
            try:
                items, _, _ = await self._fetch_dynamics(s.uid)
            except Exception as e:                      # noqa: BLE001
                self.log(f"[基线] {s.name} 取动态失败：{e}（下轮重试）")
                continue
            if items:
                newest = max(items, key=lambda i: i.pub_ts)
                baseline = replace(st, last_dynamic_id=newest.id_str, last_poll_ts=newest.pub_ts)
                await self._complete_round([], [baseline], "collect",
                                           seen={s.uid: [(i.id_str, i.pub_ts) for i in items]})
                self.log(f"[基线] {s.name} 记录 {len(items)} 条历史，水位 {max(i.pub_ts for i in items)}，不推送")
            else:
                self.log(f"[基线] {s.name} 本轮动态为空，等下一轮补基线（不推历史）")
        return len(todo)

    # ---------- 直播 ----------
    async def round_live(self, *, mode: str | None = None) -> list[Event] | tuple[int, int]:
        subs = [s for s in await self.store.list_subjects() if s.room_id]
        if not subs:
            return [] if mode is None else (0, 0)
        states = await self.store.all_states()
        try:
            live = await asyncio.to_thread(self.client.fetch_live_batch, [s.uid for s in subs])
        except Exception as e:                          # noqa: BLE001
            self.log(f"[直播] 批量接口失败：{e}")
            return [] if mode is None else (0, 0)
        events: list[Event] = []
        pending: list[SubjectState] = []
        sessions: dict[int, tuple[int, int, str] | None] = {}
        for s in subs:
            # 单个号出问题（写库锁/字段异常）不能带崩整轮直播状态更新
            try:
                cur = live.get(s.uid)
                if cur is None:
                    continue
                st = states.get(s.uid) or SubjectState(uid=s.uid, room_id=s.room_id,
                                                       live_status=cur.live_status, last_poll_ts=0)
                # 批量接口偶尔漏 uname/face（2026-11-08 实测一次：卡片头像空白、名字显示成 UID）：
                # 只在「这一轮真要产开播/下播事件」且字段确实缺时补，平常一个请求都不多发
                if st.live_status != cur.live_status and live_gaps(cur):
                    cur, _left = await asyncio.to_thread(
                        enrich_live, self.client, cur,
                        retries=self.cfg.enrich_retries, log=self.log, name=s.name)
                    if not (cur.uname or "").strip() and s.name:
                        cur = replace(cur, uname=s.name)   # 再不行也别让卡片显示成 UID
                ev = event_from_live(st.live_status, cur, now=self._now())
                start_ts = _to_ts(cur.live_time)
                if cur.live_status == 1:
                    sessions[s.uid] = (start_ts, cur.online, cur.title)
                if ev is not None:
                    if ev.type is EventType.LIVE_END:
                        sess = await self.store.get_live_session(s.uid) or {}
                        start = int(sess.get("start_ts") or st.last_live_start_ts or 0)
                        dedup = str(start) if start else ev.dedup_key
                        ev = replace(ev, dedup_key=dedup,
                                     event_id=make_event_id(s.uid, EventType.LIVE_END, dedup),
                                     extra={**ev.extra, "start_ts": start,
                                            "online_max": int(sess.get("online_max") or 0)})
                        sessions[s.uid] = None
                    if not s.push_live:
                        # 关了这个号的直播通知：状态照常更新，只是不产事件（重新打开不会补推）
                        self.log(f"[直播] {cur.uname or s.name} {st.live_status}→{cur.live_status}"
                                 f" 产出 {ev.type.value}，但该主播关闭了直播通知，不入库")
                    else:
                        events.append(ev)
                        self.log(f"[直播] {cur.uname or s.name} {st.live_status}→{cur.live_status}"
                                 f" 产出 {ev.type.value} dedup={ev.dedup_key}")
                pending.append(SubjectState(
                    uid=s.uid, room_id=cur.room_id or s.room_id, live_status=cur.live_status,
                    last_live_start_ts=(start_ts if cur.live_status == 1 else st.last_live_start_ts),
                    last_dynamic_id=st.last_dynamic_id, last_poll_ts=st.last_poll_ts))
            except Exception as e:                      # noqa: BLE001
                self.log(f"[直播] {s.name} 处理失败（已跳过该号）：{type(e).__name__}: {e}")
        return await self._complete_round(events, pending, mode, live_sessions=sessions)

    # ---------- 动态 ----------
    async def round_dynamic(self, *, mode: str | None = None) -> list[Event] | tuple[int, int]:
        async with self._dyn_lock:
            return await self._round_dynamic(mode)

    async def _round_dynamic(self, mode: str | None) -> list[Event] | tuple[int, int]:
        """一轮动态：并发取数（Semaphore 限流 + 预约节流），状态收尾一次批量落盘。

        以前是 29 个号串行，每号至少 1.2s 节流、外加一次 save_subject_state 线程往返，
        单轮能拖到一分钟以上（H4）；并发后总耗时由最慢的几路决定。
        """
        subs = await self.store.list_subjects()
        if not subs:
            return [] if mode is None else (0, 0)
        states = await self.store.all_states()
        sem = asyncio.Semaphore(self.dynamic_concurrency)
        halt: list[int] = []
        seen_updates: dict[int, list[tuple[str, int]]] = {}
        # return_exceptions：单号写库/解析出错只丢这个号，不能把整轮 gather 带走
        results = await asyncio.gather(
            *(self._poll_dynamic(s, states.get(s.uid), sem, halt, seen_updates=seen_updates) for s in subs),
            return_exceptions=True)
        if halt:
            self.log("[停止] 动态轮询中途收到停止请求，收尾退出")
        events: list[Event] = []
        pending: list[SubjectState] = []
        for s, res in zip(subs, results):
            if isinstance(res, BaseException):
                self.log(f"[动态] {s.name} 处理失败（已跳过该号）：{type(res).__name__}: {res}")
                continue
            _, new_state, fresh, logs = res
            for line in logs:
                self.log(line)
            if new_state is not None:
                pending.append(new_state)
            events.extend(fresh)
        return await self._complete_round(events, pending, mode, seen=seen_updates)

    async def _poll_dynamic(self, s, st: SubjectState | None, sem: asyncio.Semaphore,
                            halt: list[int] | None = None, cutoff: float | None = None,
                            source: str = "动态", ignore_watermark: bool = False,
                            *, seen_updates: dict[int, list[tuple[str, int]]] | None = None):
        """单号取数（round_dynamic / 补漏 sweep 的并发单元）。

        返回 (subject, 新状态 | None, 新事件, 日志行)。状态不在这里写库：
        由调用方汇总后一次批量落盘，省掉每号一次 to_thread 往返（D 项）。
        cutoff：早于这个时刻的「新发现」条目不推送（补漏用，防迟到刷屏）。
        ignore_watermark：补漏专用——忽略「早于水位＝置顶旧动态」这条规则。
        关注流漏掉的条目往往比水位旧（水位被同批更新的动态顶高了），不忽略就永远补不上；
        补漏的回看窗口由 cutoff 兜住，不会把历史翻出来。
        """
        logs: list[str] = []
        if self._stopping():
            if halt is not None:
                halt.append(1)
            return s, None, [], logs
        async with sem:
            try:
                items, _, retries = await self._fetch_dynamics(s.uid)
            except Exception as e:                      # noqa: BLE001
                logs.append(f"[{source}] {s.name} 取数失败：{e}")
                return s, None, [], logs
        if not items:
            # 第 9.2 条：空响应不动水位，等下一轮
            logs.append(f"[{source}] {s.name} 本轮为空（重试 {retries} 次），跳过且不动水位")
            return s, None, [], logs
        new_state, fresh, more = await self._digest_items(
            s, st, items, source=source, cutoff=cutoff, ignore_watermark=ignore_watermark,
            seen_updates=seen_updates)
        logs.extend(more)
        return s, new_state, fresh, logs

    async def _digest_items(self, s, st: SubjectState | None, items,
                            source: str = "动态", cutoff: float | None = None,
                            ignore_watermark: bool = False, *,
                            seen_updates: dict[int, list[tuple[str, int]]] | None = None):
        """把「已经取到的一批动态」合并成事件 + 新状态。

        逐人轮询（round_dynamic）、关注流（round_dynamic_feed）、补漏（sweep_dynamic）
        三条路径共用这一段，保证去重口径、水位口径、置顶旧动态规则只有一份实现。
        返回 (新状态, 新事件, 日志行)。
        """
        logs: list[str] = []
        if not items:
            return None, [], logs
        if st is None:
            st = SubjectState(uid=s.uid, room_id=s.room_id, live_status=0, last_poll_ts=0)
        newest = max(items, key=lambda i: i.pub_ts)     # L2：同一轮只算一次
        if not st.last_poll_ts:                         # 基线轮：只记不推
            await self._remember_seen(s.uid, [(i.id_str, i.pub_ts) for i in items], seen_updates)
            logs.append(f"[{source}] {s.name} 建立基线 {len(items)} 条，不推送")
            return replace(st, last_dynamic_id=newest.id_str,
                           last_poll_ts=self._clamp_watermark(
                               st.last_poll_ts, items, s.name, logs)), [], logs
        # H1：只回看最近 90 天，别再按 uid 把历史全捞出来
        seen = await self.store.seen_ids(s.uid, since_ts=int(self._now()) - _SEEN_WINDOW_S)
        fresh: list[Event] = []
        muted = 0
        late = 0
        mark = []
        todo = []                                       # 真要推的条目，先挑出来才能批量补全
        for it in items:
            if it.id_str in seen:
                continue
            if not ignore_watermark and it.pub_ts <= st.last_poll_ts:
                mark.append((it.id_str, it.pub_ts))     # 置顶的旧动态：只记已见
                continue
            if cutoff is not None and it.pub_ts < cutoff:
                late += 1                               # 补漏补到的迟到条目：只记已见
                mark.append((it.id_str, it.pub_ts))
                continue
            todo.append(it)
        # 信息补全（2026-11-08）：desktop 兜底端点给的图文动态没有正文、图也没有宽高，
        # 批量直播接口偶尔漏 uname/face。产事件之前定向重取，限次限量；补不齐也照推。
        if todo and self.cfg.enrich_max_items > 0:
            todo, rep = await asyncio.to_thread(
                enrich_dynamic, self.client, s.uid, todo,
                retries=self.cfg.enrich_retries, max_items=self.cfg.enrich_max_items,
                log=logs.append, name=s.name)
            fixed = sum(1 for r in rep if not r["after"])
            if fixed:
                logs.append(f"[{source}] {s.name} 补全 {fixed} 条缺内容的动态")
        for it in todo:
            ev = event_from_dynamic(it, s.uid, s.name or f"UID{s.uid}")
            if ev is not None and s.push_dynamic:
                fresh.append(ev)                        # 已见标记推迟到入库之后（见 publish）
                continue
            if ev is not None:
                muted += 1                              # 关了这个号的动态通知：只记已见，不产事件
            mark.append((it.id_str, it.pub_ts))         # 不产事件的条目照旧立即记已见
        if mark:
            await self._remember_seen(s.uid, mark, seen_updates)
        if fresh:
            logs.append(f"[{source}] {s.name} 新增 {len(fresh)} 条")
        elif muted:
            logs.append(f"[{source}] {s.name} 有 {muted} 条新动态，但该主播关闭了动态通知，不入库"
                        f"（水位照常前进）")
        elif late:
            logs.append(f"[{source}] {s.name} 补到 {late} 条迟到动态，只记已见不推送")
        return replace(
            st, room_id=s.room_id,
            last_dynamic_id=newest.id_str,
            last_poll_ts=self._clamp_watermark(
                st.last_poll_ts, items, s.name, logs)), fresh, logs

    def _clamp_watermark(self, prev: float, items, name: str, logs: list[str]) -> float:
        """H8：水位不能越过「现在 + 5 分钟」，否则一次异常时间戳会永久封死后续动态。"""
        now = float(self._now())
        cap = now + _WATERMARK_FUTURE_TOLERANCE_S
        high = max([float(prev or 0)] + [float(i.pub_ts or 0) for i in items])
        if high <= cap:
            return high
        future = max(float(i.pub_ts or 0) for i in items)
        logs.append("[动态] %s 返回未来时间戳 %d（比现在晚 %.0f 分钟），水位只记到 %d；"
                    "否则之后的正常动态都会被当成置顶旧动态跳过"
                    % (name, int(future), (future - now) / 60.0, int(cap)))
        return cap

    async def _fetch_dynamics(self, uid: int):
        """取一个号的动态：先预约发起时隙，再丢线程池。

        BiliClient._throttle 靠共享的 _last_call 排队，既没有锁也不跨线程安全，
        gather 并发下会互相踩、把 1.2s 节流压塌；所以并发上限之外，watcher 侧
        自己按 min_interval 预约时隙，保证任意两次请求真正发起的间隔不小于它。
        """
        gap = float(getattr(self.client, "min_interval", 0.0) or 0.0)
        if gap > 0:
            async with self._fetch_gate:
                wait = self._fetch_next_at - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._fetch_next_at = time.monotonic() + gap
        return await asyncio.to_thread(
            self.client.fetch_dynamics, uid, "", self.cfg.dyn_limit, self.cfg.empty_retries)

    # ---------- 动态：关注流（feed 模式） ----------
    def feed_enabled(self) -> bool:
        """这一轮是否用关注流取数。

        显式配置 feed 但凭据不全时退回逐人轮询，并在日志里说一次原因：
        配置写错不能变成「一条动态都收不到」。
        """
        mode = feedmod.normalize_mode(self.cfg.mode)
        if mode == feedmod.MODE_PER_USER:
            return False
        if getattr(self, "_auth_bad", False):
            # H8：登录态失效后忠实退回逐人轮询（公告里的行为），不要一直用关注流空转
            if not self._feed_auth_warned:
                self._feed_auth_warned = True
                self.log("[关注流] B 站登录态失效 → 回退逐人轮询（动态照常收，只是慢一些；"
                         "续期成功会自动切回关注流）")
            return False
        ok = hasattr(self.client, "feed_page") and bool(getattr(self.client, "bili_jct", ""))
        if ok:
            return True
        if not self._feed_warned:
            self._feed_warned = True
            try:
                why = feedmod.fallback_reason()
            except Exception:                       # noqa: BLE001  只影响提示文案
                why = ""
            self.log("[关注流] 未启用（%s）→ 仍按逐人轮询取数" % (why or "客户端不支持关注流"))
        return False

    async def round_dynamic_feed(self, *, mode: str | None = None) -> list[Event] | tuple[int, int]:
        async with self._dyn_lock:
            return await self._round_dynamic_feed(mode)

    async def _round_dynamic_feed(self, mode: str | None) -> list[Event] | tuple[int, int]:
        """一轮关注流：取 1..N 页 → 按 uid 归并 → 复用逐人的去重/水位/推送规则。

        一页 20 条，稳态 200ms 左右；名单内没见过的条目才会产事件。
        取流指标（页数/字节/耗时/risk）全写进 meta，管理台「动态采集方式」卡直接读。
        """
        subs = await self.store.list_subjects()
        if not subs:
            return [] if mode is None else (0, 0)
        states = await self.store.all_states()
        by_uid = {s.uid: s for s in subs}
        known = {st.last_dynamic_id for st in states.values() if st.last_dynamic_id}
        since_ts = max([int(st.last_poll_ts or 0) for st in states.values()] or [0])
        try:
            res = await asyncio.to_thread(
                feedmod.feed_round_once, self.client,
                max_pages=self.cfg.feed_pages,
                burst_threshold=self.cfg.feed_burst_threshold,
                burst_pages=self.cfg.feed_burst_pages,
                since_ts=since_ts, known_ids=known)
        except Exception as e:                  # noqa: BLE001  取流失败不能带走主循环
            code = int(getattr(e, "code", 0) or 0)
            if code == -101 and not self._auth_bad:
                # H8：SESSDATA 失效时 -101 不在 RETRYABLE 里，关注流会一直空转，
                # 而 docstring 承诺的「自动退回匿名」从来没实现。这里置位后
                # feed_enabled() 下一轮就回退逐人轮询，顺带由登录态检查去告警。
                self._auth_bad = True
                self.log("[关注流] 取流返回 -101（未登录 / SESSDATA 失效）→ 回退逐人轮询")
            fails = await self._meta_int("feed_errors", 0) + 1
            await self._meta_put({
                "feed_mode": "feed",
                "feed_last_at": str(int(self._now())),
                "feed_errors": str(fails),
                "feed_last_error": ("%s: %s" % (type(e).__name__, e))[:200],
            })
            self.log(f"[关注流] 取流失败（累计 {fails} 次）：{type(e).__name__}: {e}")
            return [] if mode is None else (0, 0)
        entries = res.get("entries") or []
        groups = feedmod.entries_by_uid(entries)
        matched = {uid: g for uid, g in groups.items() if uid in by_uid}
        events: list[Event] = []
        pending: list[SubjectState] = []
        seen_updates: dict[int, list[tuple[str, int]]] = {}
        for uid, group in matched.items():
            s = by_uid[uid]
            try:
                new_state, fresh, logs = await self._digest_items(
                    s, states.get(uid), [e.item for e in group], source="关注流",
                    seen_updates=seen_updates)
            except Exception as e:              # noqa: BLE001  单号失败不影响整轮
                self.log(f"[关注流] {s.name} 处理失败（已跳过）：{type(e).__name__}: {e}")
                continue
            for line in logs:
                self.log(line)
            if new_state is not None:
                pending.append(new_state)
            events.extend(fresh)
        result = await self._complete_round(events, pending, mode, seen=seen_updates)
        newest = max(await self._meta_int("feed_newest_ts", 0), int(res.get("newest_ts") or 0))
        matched_items = sum(len(g) for g in matched.values())
        ignored = max(0, len(entries) - matched_items)   # 关注了但不在监控名单里，直接丢弃
        await self._meta_put({
            "feed_mode": "feed",
            "feed_last_at": str(int(self._now())),
            "feed_rounds": str(await self._meta_int("feed_rounds", 0) + 1),
            "feed_last_ms": str(int(res.get("ms") or 0)),
            "feed_last_bytes": str(int(res.get("bytes") or 0)),
            "feed_last_risk": str(int(res.get("risk") or 0)),
            "feed_last_pages": str(int(res.get("pages") or 0)),
            "feed_last_entries": str(len(entries)),
            "feed_last_matched": str(matched_items),
            "feed_last_ignored": str(ignored),
            "feed_newest_ts": str(newest),
            "feed_known_hit": "1" if res.get("known_hit") else "0",
            "feed_last_error": "",
        })
        if events:
            self.log("[关注流] %d 页 %d 条（名单内 %d，名单外忽略 %d）→ 新增 %d 条，%d ms / %.0f KB"
                     % (int(res.get("pages") or 0), len(entries), matched_items, ignored, len(events),
                        int(res.get("ms") or 0), int(res.get("bytes") or 0) / 1024.0))
        return result

    async def sweep_dynamic(self, max_age_min: float | None = None) -> dict:
        async with self._dyn_lock:
            return await self._sweep_dynamic(max_age_min)

    async def _sweep_dynamic(self, max_age_min: float | None) -> dict:
        """低频逐人补漏：关注流会吞条目（101 个关注实测漏 16.4%），这里逐人抓一次。

        2026-11-11 起补到的遗漏动态**照常推送**：漏了却不推，补漏就只剩「标记已读」。
        两条保护：① 只看最近 sweep_max_age_min 分钟（默认 720 = 12 小时），更早的只记已见；
        ② 一次最多推 sweep_late_max 条（默认 30），超出部分只记已见，避免停机重启后刷屏。
        sweep_push_late=false 退回旧行为（比水位旧的条目一律不推）。
        用并发 gather 把 29 次请求压到十几秒。
        """
        subs = await self.store.list_subjects()
        if not subs:
            return {"checked": 0, "missing": 0, "sent": 0, "dropped": 0}
        states = await self.store.all_states()
        mins = self.cfg.sweep_max_age_min if max_age_min is None else float(max_age_min)
        cutoff = self._now() - max(0.0, float(mins)) * 60
        push_late = bool(self.cfg.sweep_push_late)
        sem = asyncio.Semaphore(self.dynamic_concurrency)
        seen_updates: dict[int, list[tuple[str, int]]] = {}
        results = await asyncio.gather(
            *(self._poll_dynamic(s, states.get(s.uid), sem, None,
                                 cutoff=cutoff, source="补漏",
                                 ignore_watermark=push_late, seen_updates=seen_updates) for s in subs),
            return_exceptions=True)
        events: list[Event] = []
        pending: list[SubjectState] = []
        for s, res in zip(subs, results):
            if isinstance(res, BaseException):
                self.log(f"[补漏] {s.name} 失败（已跳过）：{type(res).__name__}: {res}")
                continue
            _, new_state, fresh, logs = res
            for line in logs:
                self.log(line)
            if new_state is not None:
                pending.append(new_state)
            events.extend(fresh)
        events.sort(key=lambda e: e.occurred_at)   # 补推按发布时间正序，QQ 里的顺序才自然
        cap = max(0, int(self.cfg.sweep_late_max))
        skipped = events[:-cap] if cap else []
        dropped = len(skipped)
        events = events[dropped:]
        sent, _ = await self._complete_round(events, pending, await self.global_mode(),
                                             seen=seen_updates, skipped=skipped)
        await self._meta_put({
            "sweep_last_at": str(int(self._now())),
            "sweep_runs": str(await self._meta_int("sweep_runs", 0) + 1),
            "sweep_missing": str(await self._meta_int("sweep_missing", 0) + len(events)),
            "sweep_late_max": str(cap),
        })
        if dropped:
            self.log("[补漏] 补到 %d 条，超过单次上限 %d：只推最新 %d 条，其余只记已见"
                     % (len(events) + dropped, cap, cap))
        return {"checked": len(subs), "missing": len(events), "sent": sent,
                "dropped": dropped}

    async def sync_follows(self) -> dict:
        """把名单里没关注的号补关注（只关注、不取关），结果写 meta 供管理台展示。"""
        subs = await self.store.list_subjects()
        journal = None
        path = getattr(self.store, "path", None)
        if path:
            journal = Path(path).parent.parent / "logs" / "follow_sync"
        res = await asyncio.to_thread(
            feedmod.follow_sync, self.client, subs, log=self.log,
            journal_dir=journal, gap_s=2.0)
        await self._meta_put({
            "follow_sync_last_at": str(int(self._now())),
            "follow_sync_ok": "1" if res.get("ok") else "0",
            "follow_sync_following": str(int(res.get("following") or 0)),
            "follow_sync_planned": str(int(res.get("planned") or 0)),
            "follow_sync_added": str(int(res.get("followed") or 0)),
            "follow_sync_failed": str(len(res.get("failed") or [])),
            "follow_sync_reason": str(res.get("reason") or res.get("error") or ""),
        })
        return res

    async def _sweep_job(self) -> None:
        res = await self.sweep_dynamic()
        self._last_sweep = self._now()
        self.log("[补漏] 检查 %d 个号，补到 %d 条（推送 %d 条）"
                 % (res.get("checked", 0), res.get("missing", 0), res.get("sent", 0)))

    async def _follow_sync_job(self) -> None:
        try:
            await self.sync_follows()
        finally:
            self._last_follow_sync = self._now()

    def _spawn(self, name: str, coro) -> None:
        """后台跑关注同步/补漏：29 次逐人请求要十几秒，不能把直播轮询卡住。"""
        cur = self._bg_tasks.get(name)
        if cur is not None and not cur.done():
            return
        self._bg_tasks[name] = asyncio.create_task(self._wrap_bg(name, coro))

    async def _wrap_bg(self, name: str, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:                      # noqa: BLE001  后台失败只记日志
            self.log(f"[{name}] 后台任务失败：{type(e).__name__}: {e}")

    async def _cancel_bg(self) -> None:
        pending = [t for t in self._bg_tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass
        self._bg_tasks.clear()

    # ---------- 手动触发（管理台按钮 / 请求文件） ----------
    def _consume_request(self, name: str) -> bool:
        """管理台在 data/ 下放一个空请求文件，主循环下一轮消费并删掉。

        和 data/restart.request、data/bot.stop 同一套机制：不需要跨进程 IPC，
        管理台单独跑也能生效（只是要等采集进程活着）。
        """
        path = getattr(self.store, "path", None)
        if not path:
            return False
        p = Path(path).parent / f"{name}.request"
        try:
            if not p.exists():
                return False
            p.unlink()
        except OSError as e:
            self.log(f"[手动] 处理 {p.name} 失败：{e}")
            return False
        self.log(f"[手动] 收到「{name}」请求，本轮立即执行一次")
        return True

    async def _meta_get(self, key: str, default: str = "") -> str:
        getter = getattr(self.store, "get_meta", None)
        if getter is None:
            return default
        try:
            val = await getter(key, default)
        except Exception:                           # noqa: BLE001
            return default
        return default if val is None else str(val)

    async def _meta_int(self, key: str, default: int = 0) -> int:
        try:
            return int(await self._meta_get(key, str(default)) or default)
        except (TypeError, ValueError):
            return default

    async def _meta_put(self, values: dict[str, str]) -> None:
        if not values:
            return
        many = getattr(self.store, "set_meta_many", None)
        setter = getattr(self.store, "set_meta", None)
        try:
            if many is not None:
                await many(values)
                return
            if setter is None:
                return
            for k, v in values.items():
                await setter(k, str(v))
        except Exception as e:                      # noqa: BLE001  指标写不进去不影响采集
            self.log(f"[状态] 写 meta 失败：{type(e).__name__}: {e}")

    async def _save_states(self, states: list[SubjectState]) -> None:
        """一轮里所有号的状态合并成一次写库（D 项，2026-11-07）。

        以前每个号一次 save_subject_state，等于一次 asyncio.to_thread 往返；29 个号就是 29 次。
        优先用 Store 的批量接口；没有就借它的一次 _run 复用连接逐行执行；再退回逐条。
        """
        if not states:
            return
        batch = getattr(self.store, "save_subject_states", None)
        if batch is not None:
            await batch(states)
            return
        run = getattr(self.store, "_run", None)
        save_one = getattr(self.store, "_save_state", None)
        if run is not None and save_one is not None:
            def _write_all(conn):
                bad = 0
                for st in states:
                    try:
                        save_one(conn, st)
                    except Exception:                   # noqa: BLE001  单条坏数据不能带走整批
                        bad += 1
                return bad
            bad = await run(_write_all)
            if bad:
                self.log(f"[状态] 批量写库有 {bad} 条失败（已跳过）")
            return
        for st in states:
            await self.store.save_subject_state(st)

    # ---------- 入库 + 投递 ----------
    async def global_mode(self) -> str:
        """总开关（管理台可切）：full 正常 / collect 只采集不推送 / off 全停。

        存在 meta 里，管理台单独进程也能改；每轮重新读，切完最多一个轮次生效。
        """
        getter = getattr(self.store, "get_meta", None)
        if getter is None:
            return "full"
        try:
            return (await getter("global_mode", "full")) or "full"
        except Exception:
            return "full"

    _DYNAMIC_TYPES = (EventType.DYNAMIC, EventType.VIDEO, EventType.ARTICLE)

    def _published_ids(self, events: list[Event]) -> dict[int, list[tuple[str, int]]]:
        by_uid: dict[int, list[tuple[str, int]]] = {}
        for ev in events:
            if ev.type in self._DYNAMIC_TYPES:
                by_uid.setdefault(ev.subject_uid, []).append((ev.dedup_key, ev.occurred_at))
        return by_uid

    async def _remember_seen(self, uid, items, seen_updates) -> None:
        if seen_updates is None:
            await self.store.mark_seen(uid, items)
        else:
            seen_updates.setdefault(uid, []).extend(items)

    async def _complete_round(self, events, states, mode, *, live_sessions=None, seen=None, skipped=()):
        commit = getattr(self.store, "commit_poll", None)
        if mode == "off":
            return 0, 0
        if mode is not None and commit is not None:
            seen = {} if seen is None else seen
            for uid, items in self._published_ids([*events, *skipped]).items():
                seen.setdefault(uid, []).extend(items)
            queued = await commit(states, events, push=mode == "full", seen=seen,
                                  live_sessions=live_sessions)
            return queued, len(events)

        # 保持手动取样入口和轻量 Store 的接口。
        if mode is not None:
            queued = await self.publish(events, mode)
            await self._mark_published(skipped)
        for uid, session in (live_sessions or {}).items():
            if session is None:
                await self.store.clear_live_session(uid)
            else:
                await self.store.upsert_live_session(uid, *session)
        for uid, items in (seen or {}).items():
            await self.store.mark_seen(uid, items)
        await self._save_states(states)
        return events if mode is None else (queued, len(events))

    async def _mark_published(self, events: list[Event]) -> None:
        """H8（2026-11-14）：动态类事件「入库之后」才记已见。

        以前 mark_seen 在入库之前，中间崩溃/被 kill 就永久漏推；被补漏上限裁掉的
        那些条目也由调用方在裁剪时显式补记（它们本来就不该再推）。
        """
        for uid, pairs in self._published_ids(events).items():
            await self.store.mark_seen(uid, pairs)

    async def publish(self, events: list[Event], mode: str = "full") -> int:
        if not events or mode == "off":
            return 0
        commit = getattr(self.store, "commit_poll", None)
        if commit is not None:
            return await commit([], events, push=mode == "full", seen=self._published_ids(events))
        if mode != "full":
            # 只采集模式：事件照样入库（管理台能看到），但不产生投递
            for ev in events:
                if not await self.store.insert_event(ev):
                    continue
            await self._mark_published(events)
            return 0
        targets = await self.store.enabled_target_ids()
        if not targets:
            self.log("[投递] 没有启用中的推送目标，事件只入库不发送")
        n = 0
        for ev in events:
            if not await self.store.insert_event(ev):
                self.log(f"[投递] {ev.event_id} 已存在，丢弃（幂等）")
                continue
            if targets:
                await self.store.enqueue(ev.event_id, targets)
                n += 1
        await self._mark_published(events)
        return n

    async def tick_once(self, force: bool = True) -> dict:
        now = self._now()
        result: dict = {"live": 0, "dynamic": 0, "sent": 0}
        off = self._sync_tasks()           # 任务开关：暂停的循环这一轮不干活
        await self._maintenance()          # 每个 tick 开头兜一次崩溃恢复 + 每日剪枝
        await self._heartbeat("tick_start")
        mode = await self.global_mode()
        result["mode"] = mode
        if mode == "off":
            # 总开关打到「全停」：不采集也不发送，心跳照写（不然守护会以为卡死）。
            # 只在进入 off 的那一轮打日志，否则一秒钟一行会把日志刷爆。
            if getattr(self, "_last_mode", None) != "off":
                self.log("[总开关] 已暂停（不采集不推送），本轮跳过；改回「全开」后自动恢复")
            self._last_mode = "off"
            await self._heartbeat("skipped_off")
            return result
        await self._check_login_state()
        # 管理台「立即取流 / 立即补漏 / 同步关注」按钮：放一个请求文件，这里消费掉
        want_feed = self._consume_request("feed")
        want_sweep = self._consume_request("sweep")
        want_sync = self._consume_request("follow_sync")
        if "live" not in off and (force or now - self._last_live >= self.cfg.live_interval_s):
            result["live"], count = await self.round_live(mode=mode)
            self._last_live = now
            self.board.beat("live", rounds=1, events=count)
        feed_on = self.feed_enabled() if "dynamic" not in off else False
        if "dynamic" not in off and (force or want_feed
                                     or now - self._last_dyn >= self.cfg.dynamic_interval_s):
            poll = self.round_dynamic_feed if feed_on else self.round_dynamic
            result["dynamic"], count = await poll(mode=mode)
            result["dynamic_source"] = "feed" if feed_on else "per_user"
            self._last_dyn = now
            self.board.beat("dynamic", rounds=1, events=count)
        if "dynamic" not in off and feed_on:
            await self._schedule_background(now, want_sweep=want_sweep, want_sync=want_sync)
        if self._last_mode != mode:
            if mode == "full" and getattr(self, "_last_mode", None) in ("off", "collect"):
                self.log("[总开关] 已恢复「全开」，继续采集与推送")
            self._last_mode = mode
        if self.dispatcher is not None and mode == "full" and "dispatch" not in off:
            outcomes = await self.dispatcher.tick()
            result["sent"] = sum(1 for o in outcomes if o.action == "sent")
            self.board.beat("dispatch", ticks=1, sent=result["sent"])
        if (self.dispatcher is not None and "cache_gc" not in off
                and now - self._last_gc >= self.gc_interval_s):
            self._last_gc = now
            stats = await self.dispatcher.gc_media()
            self.board.beat("cache_gc", runs=1)
            if stats and (stats.get("cards") or stats.get("imgs") or stats.get("tops")):
                self.log("[缓存] 兜底清理：卡片 %d 张 %.1f MB、图源 %d 张 %.1f MB、顶部图 %d 张 %.1f MB"
                         % (stats.get("cards", 0), stats.get("cards_mb", 0.0),
                            stats.get("imgs", 0), stats.get("imgs_mb", 0.0),
                            stats.get("tops", 0), stats.get("tops_mb", 0.0)))
        await self._heartbeat("tick_end")
        return result

    async def _schedule_background(self, now: float, want_sweep: bool = False,
                                   want_sync: bool = False) -> None:
        """关注同步（默认每 6h）与逐人补漏（默认每 6h）都是慢活儿，扔后台别卡直播轮询。

        启动后跑第一轮：关注列表可能还没同步、也可能刚开机就错过了一批动态。
        """
        if self.cfg.follow_sync and (want_sync or self._last_follow_sync == 0.0
                                     or now - self._last_follow_sync
                                     >= self.cfg.follow_sync_hours * 3600):
            self._last_follow_sync = now
            self._spawn("follow_sync", self._follow_sync_job())
        if not self.cfg.sweep_hours:
            if want_sweep:
                self.log("[补漏] 已关闭（poll.sweep_hours=0），忽略这次请求")
            return
        if (want_sweep or self._last_sweep == 0.0
                or now - self._last_sweep >= self.cfg.sweep_hours * 3600):
            self._last_sweep = now
            self._spawn("sweep", self._sweep_job())

    def _sync_tasks(self) -> set[str]:
        """读一遍任务开关；只在某个任务刚被暂停 / 恢复时打一行日志。

        开关存在 meta['tasks_off']，循环侧带 1 秒缓存，管理台点完最多一两秒生效。
        """
        try:
            off = self.board.off_keys()
        except Exception:                                  # noqa: BLE001  开关读不出来就当全开
            return set()
        for key in ("live", "dynamic", "dispatch", "cache_gc"):
            is_off = key in off
            if is_off != (key in self._off_logged):
                task = _tasks.BY_KEY.get(key)
                name = task.name if task else key
                self.log("[任务] %s 已%s（管理台「进程」页可改）"
                         % (name, "暂停" if is_off else "恢复"))
        self._off_logged = set(off)
        return off

    async def _check_login_state(self) -> None:
        """定期确认登录态。临时网络错误保留上次状态，稍后重试。"""
        cli = self.client
        if not getattr(cli, "logged_in", False) or not hasattr(cli, "check_login"):
            return                                   # 匿名模式或测试替身，无需检查
        now = self._now()
        interval = min(30.0, self.auth_check_interval_s) if self._auth_retry else self.auth_check_interval_s
        if now - self._auth_checked_at < interval:
            return
        self._auth_checked_at = now
        try:
            info = await asyncio.to_thread(cli.check_login)
        except Exception as e:                       # noqa: BLE001  网络抖动不算失效
            info = {"message": f"{type(e).__name__}: {e}"}
        self._auth_retry = not (info.get("ok") or info.get("state") == "expired")
        if self._auth_retry:
            self.log(f"[B站] 登录态暂未确认，稍后重试：{info.get('message') or '接口未返回登录状态'}")
            return
        if info.get("ok"):
            if self._auth_bad:
                self.log("[B站] 登录态已恢复：账号 %s mid=%s" % (info.get("uname"), info.get("mid")))
                self._auth_bad = False
                self._auth_warned_at = 0.0
                self._feed_auth_warned = False
            return
        self._auth_bad = True
        if now - self._auth_warned_at < 4 * 3600:    # 失效期间每 4 小时提醒一次，别刷屏
            return
        self._auth_warned_at = now
        self.log("[B站] 登录态已失效（%s）→ 现按匿名取数，空响应会变多；"
                 "到管理台「B 站登录态」点「扫码登录」续期，或运行 "
                 "python scripts/bili_qr_login.py" % (info.get("message") or "未知原因"))

    async def _maintenance(self, startup: bool = False) -> None:
        """崩溃恢复 + 每日数据剪枝（C 项，2026-11-07）。

        · 单进程模式负责回收自己的超时投递。拆分模式由独立推送进程负责回收。
        · seen / event 会无限长，按本地日期每天剪一次；失败每小时才再告警一次，别刷屏。
        """
        reclaim = getattr(self.store, "reclaim_stale_claims", None) if self.dispatcher else None
        if reclaim is not None:
            try:
                n = await reclaim()
                if n:
                    self.log(f"[恢复] 回收超时未完成的投递占位 {n} 条")
            except Exception as e:                      # noqa: BLE001
                self.log(f"[恢复] 回收投递占位失败：{type(e).__name__}: {e}")
        if startup:
            return                                      # 启动只做恢复，剪枝留给第一轮 tick
        day = dt.date.fromtimestamp(self._now()).isoformat()
        if day == self._last_prune_day:
            return
        prune = getattr(self.store, "prune", None)
        if prune is None:
            return
        try:
            stats = await prune()
        except Exception as e:                          # noqa: BLE001
            if self._now() - self._prune_warned_at >= PRUNE_WARN_INTERVAL_S:
                self._prune_warned_at = self._now()
                self.log(f"[清理] 数据剪枝失败（下轮重试）：{type(e).__name__}: {e}")
            return
        self._last_prune_day = day
        self.log(f"[清理] 每日剪枝：{stats}")

    async def _heartbeat(self, stage: str, force: bool = False) -> None:
        """守护进程（scripts/supervisor.py）靠 last_tick 判断主循环是不是卡住了。

        写放大治理（B 项）：同一阶段且距上次写入不足 10s 直接跳过；阶段变化必须落一次
        （tick_start → tick_end 能看出卡在哪一段）。两个 key 合成一条 UPSERT，
        一次线程往返写两行；Store 没有 set_meta（老实现/测试替身）时静默跳过。
        """
        now = self._now()
        if (not force and stage == self._heartbeat_stage
                and now - self._heartbeat_at < HEARTBEAT_MIN_INTERVAL_S):
            return
        setter = getattr(self.store, "set_meta", None)
        if setter is None:
            return
        ts = str(int(now))
        try:
            many = getattr(self.store, "set_meta_many", None)
            run = getattr(self.store, "_run", None)
            if many is not None:
                await many({"last_tick": ts, "last_stage": stage})
            elif run is not None:
                # 借 Store 的一次 _run：锁、重试、连接管理都由它负责
                await run(lambda conn: conn.execute(_HEARTBEAT_SQL, (ts, stage)))
            else:
                await setter("last_tick", ts)
                await setter("last_stage", stage)
            self._heartbeat_at = now
            self._heartbeat_stage = stage
        except Exception as e:      # 心跳失败绝不能拖垮采集
            self.log(f"[心跳] 写入失败：{e}")

    async def run(self, cycles: int | None = None, minutes: float | None = None,
                  stop_check=None) -> None:
        if stop_check is not None:
            self.stop_check = stop_check
        await self._maintenance(startup=True)   # 先收尾上次崩溃遗留的「发送中」占位
        await self.bootstrap()
        deadline = self._now() + minutes * 60 if minutes else None
        try:
            await self._loop(cycles, deadline, stop_check)
        finally:
            await self._cancel_bg()   # 收掉后台的关注同步 / 补漏，避免退出时挂住

    async def _loop(self, cycles: int | None, deadline: float | None,
                    stop_check) -> None:
        n = 0
        prev_mode = None
        while True:
            if stop_check is not None and stop_check():
                self.log("[停止] 收到停止请求（停止文件），收尾退出")
                return
            n += 1
            try:
                res = await self.tick_once(force=(n == 1))
            except Exception as e:                  # noqa: BLE001
                # 任何单轮异常都不许把进程带走：打日志、写心跳、下一轮继续
                self.log(f"[错误] 第 {n} 轮异常，跳过本轮：{type(e).__name__}: {e}")
                await self._heartbeat("tick_error")
                res = {"error": f"{type(e).__name__}: {e}"}
            busy = (res.get("live") or 0) + (res.get("dynamic") or 0) + (res.get("sent") or 0)
            mode = res.get("mode")
            if busy or mode != prev_mode or n == 1 or n % 120 == 0:
                # 静默轮次不刷日志：暂停/采集态下每秒一轮，全打会把日志刷成几千行
                self.log(f"[主循环轮次] 第 {n} 轮 {round_text(res)}")
            prev_mode = mode
            if cycles is not None and n >= cycles:
                return
            if deadline is not None and self._now() >= deadline:
                return
            await asyncio.sleep(self.cfg.idle_sleep_s)
