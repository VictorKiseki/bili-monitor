"""常驻入口：把采集 → 归一化 → 入库 → 投递 串起来跑

用法：
  python scripts/run_bot.py --once                  # 跑一轮就退出（联调用）
  python scripts/run_bot.py --minutes 30            # 跑 30 分钟
  python scripts/run_bot.py                          # 一直跑（Ctrl+C 停）
  python scripts/run_bot.py --channel qq --once      # 真发私聊（需要 BBOT.env + .p0_ids.json）
  python scripts/run_bot.py --channel qq --web      # 同时开 Web 管理台 http://127.0.0.1:8765

默认 --channel echo：只打印不发送，事件照样入库。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

# ---------- H10：依赖自检 ----------
# 必须在导入项目模块之前执行：这些模块会 import httpx 等第三方包，缺包时
# 默认抛裸 ImportError 刷一屏栈，用户看不懂。这里提前用 find_spec 探测，
# 给出可执行的修复提示；函数本身不退出，方便其它入口 import 复用。
REQUIRED_DEPENDENCIES = ("httpx", "PIL", "websockets", "websocket", "segno")
# 模块名 → requirements.txt 里的包名（提示用）
REQUIREMENT_NAMES = {
    "PIL": "Pillow",
    "websocket": "websocket-client",
}


def check_dependencies(required=REQUIRED_DEPENDENCIES) -> list[str]:
    """返回缺失的依赖模块名（保持传入顺序，去重）。只探测，不 import、不退出。"""
    from importlib.util import find_spec

    missing: list[str] = []
    for name in required:
        if name in missing:
            continue
        try:
            found = find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(name)
    return missing


def ensure_dependencies(required=REQUIRED_DEPENDENCIES, exit_code: int = 2) -> None:
    """依赖齐全则返回；缺失则打印中文提示并 exit(exit_code)，不抛裸 ImportError。"""
    missing = check_dependencies(required)
    if not missing:
        return
    names = " ".join(REQUIREMENT_NAMES.get(m, m) for m in missing)
    print(f"缺少必需依赖: {', '.join(missing)}", file=sys.stderr)
    print(f"请执行 pip install -r requirements.txt 缺少: {names}", file=sys.stderr)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    ensure_dependencies()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import proxy as proxy_cfg                       # noqa: E402
proxy_cfg.apply_to_env()                                    # 采集进程：建 client 前套用代理配置

from channels.echo import EchoChannel                       # noqa: E402
from collector.bili import BiliClient                       # noqa: E402
from collector.follow_feed import FollowFeedClient          # noqa: E402
from core import feed_collect as feedmod                    # noqa: E402
from core import tasks as T                                # noqa: E402
from core.watcher import PollConfig, Watcher, round_text   # noqa: E402
from core.weekly.push import push_once                       # noqa: E402
from notifications.dispatcher import Dispatcher             # noqa: E402
from shared.contracts import RenderedMessage, SendStatus, Subject, Target  # noqa: E402
from store.sqlite import SQLiteStore                        # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import cleanup_review as review  # noqa: E402


class DailySink:
    """按天落盘：logs/bot-2026-10-28.log，跨零点自动换文件。

    日志页要按日期筛选，所以文件名带日期，行首也带日期。
    """

    def __init__(self, base: Path):
        self.base = Path(base)
        self._date = ""
        self._fh = None

    def path_for(self, date: str) -> Path:
        return self.base.with_name("%s-%s%s" % (self.base.stem, date, self.base.suffix or ".log"))

    def write(self, date: str, line: str) -> None:
        if date != self._date:
            self.close()
            p = self.path_for(date)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._fh = p.open("a", encoding="utf-8")
            self._date = date
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None


def load_env_file(path: str | Path) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_subjects(path: str | Path) -> tuple[list[Subject], dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    subs = [Subject(uid=int(s["uid"]), name=s.get("name", ""), room_id=int(s.get("room_id") or 0))
            for s in data.get("subjects", [])]
    return subs, data.get("poll", {})


def read_openid(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    return json.loads(p.read_text(encoding="utf-8")).get("user_openid", "")


async def cleanup_notify_loop(ch, target: Target, log, board=None) -> None:
    """发现待确认计划后，用监控台自己的 Bot 发通知；只有发送成功才写 sent。

    清理守护看到 sent 才开始 1 小时倒计时；这里失败/没跑，绝不会发生删除。
    管理台「进程」页可以暂停这个循环（暂停期间不会发通知，也就不会开始倒计时）。
    """
    while True:
        try:
            if board is not None and board.paused("cleanup_notify"):
                await asyncio.sleep(5)
                continue
            plan = review.load_plan()
            if not plan or plan.get("status") != "awaiting_notify":
                await asyncio.sleep(5)
                continue
            ns = review.load_notify()
            if ns.get("plan_id") == plan.get("plan_id") and ns.get("status") == "sent":
                await asyncio.sleep(5)
                continue
            now = time.time()
            if (ns.get("plan_id") == plan.get("plan_id")
                    and ns.get("status") == "failed"
                    and now < float(ns.get("next_retry_at") or 0)):
                await asyncio.sleep(5)
                continue
            messages = review.notification_messages(plan)
            error = ""
            last_raw = {}
            for msg in messages:
                res = await ch.send(target, RenderedMessage(text=msg))
                if res.status is not SendStatus.OK:
                    error = (res.error_msg or str(res.status))
                    break
                last_raw = res.raw or {}
                await asyncio.sleep(0.4)
            if not error:
                review.save_notify({
                    "plan_id": plan["plan_id"], "status": "sent",
                    "sent_at": time.time(), "parts": len(messages),
                    "message_id": last_raw.get("id") or "",
                })
                log("[清理通知] 已发送计划 %s（%d 条），等待用户确认"
                    % (plan["plan_id"], len(messages)))
            else:
                retry = 300 if "openid" in error or "权限" in error else 60
                review.save_notify({
                    "plan_id": plan["plan_id"], "status": "failed",
                    "last_error": error, "next_retry_at": time.time() + retry,
                    "attempts": int(ns.get("attempts") or 0) + 1,
                })
                log("[清理通知] 发送失败：%s；%d 秒后重试（不会删除）" % (error, retry))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log("[清理通知] 循环异常：%s: %s" % (type(e).__name__, e))
        await asyncio.sleep(5)


async def main() -> int:
    args = _parse_args()

    # Windows：把 CTRL_BREAK 映射成 KeyboardInterrupt，好让 finally 里的收尾逻辑跑到
    _install_sigbreak_handler()

    sink, log = _make_logger(args)

    if args.push_now:
        return await _run_push_now(args, log)

    if args.collector:
        return await _run_collector(args, log)

    T.BOARD.bind(args.db)          # 任务开关：暂停的循环这一轮不干活（管理台「进程」页）
    subs, poll = load_subjects(args.subjects)
    mode = _resolve_poll_mode(args, poll, log)
    log(f"[启动] 主体 {len(subs)} 个，通道 {args.channel}，库 {args.db}，取数 {mode}")

    ch, app_id, secret = await _init_channel(args, log)
    if ch is None:
        return 2
    store, client = await _init_store_client(args, log, mode)
    target, openid, ok = await _init_target(args, store, app_id, log)
    if not ok:
        return 2
    (inbox, inbox_task, notify_task) = await _start_background(
        args, ch, target, openid, app_id, secret, log)

    cfg = _poll_config(args, poll, mode)
    w, cards = _build_watcher(args, cfg, store, ch, client, log)
    await w.init_subjects(subs)
    _start_web(args, log, mode)

    started = time.time()
    try:
        await _run_cycles(w, args, log)
    except KeyboardInterrupt:
        log("[退出] 收到 Ctrl+C")
    finally:
        await _shutdown(store, sink, inbox, inbox_task, notify_task, cards, started, log)
    return 0

async def _run_collector(args: argparse.Namespace, log) -> int:
    """采集进程：只做「取数 → 归一化 → 入库」。

    不建通道、不配目标、不起收件/通知/卡片/管理台；Watcher 拿不到 dispatcher，
    所以 tick 里那一段派发与缓存清理根本不会执行——队列由推送进程消费。
    好处：B 站慢/被风控时只影响采集，QQ 发送和管理台照常。
    """
    T.BOARD.bind(args.db)
    subs, poll = load_subjects(args.subjects)
    mode = _resolve_poll_mode(args, poll, log)
    log(f"[采集] 主体 {len(subs)} 个，取数 {mode}，库 {args.db}（只入库，不推送）")

    store, client = await _init_store_client(args, log, mode)
    cfg = _poll_config(args, poll, mode)
    w = Watcher(store, client, cfg=cfg, dispatcher=None, log=log)
    await w.init_subjects(subs)

    started = time.time()
    try:
        await _run_cycles(w, args, log)
    except KeyboardInterrupt:
        log("[退出] 收到 Ctrl+C")
    finally:
        await _shutdown(store, sink=None, inbox=None, inbox_task=None, notify_task=None,
                        cards=None, started=started, log=log)
    return 0

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subjects", default=str(ROOT / "config" / "subjects.json"))
    ap.add_argument("--db", default=str(ROOT / "data" / "bot.db"))
    ap.add_argument("--channel", choices=["echo", "qq"], default="echo")
    ap.add_argument("--env", default=str(ROOT / "BBOT.env"))
    ap.add_argument("--ids", default=str(ROOT / ".p0_ids.json"))
    ap.add_argument("--openid", default="")
    ap.add_argument("--no-target", action="store_true", help="只入库，不配置推送目标")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--minutes", type=float, default=None)
    ap.add_argument("--live-interval", type=float, default=None)
    ap.add_argument("--dynamic-interval", type=float, default=None)
    ap.add_argument("--poll-mode", choices=["auto", "feed", "per_user"], default=None,
                    help="动态取数方式：auto=有关注流凭据就用关注流（默认），feed=强制关注流，"
                         "per_user=逐人轮询")
    ap.add_argument("--log-file", default="")
    ap.add_argument("--web", action="store_true", help="同时启动 Web 管理台（只监听本机）")
    ap.add_argument("--web-host", default="127.0.0.1")
    ap.add_argument("--web-port", type=int, default=8765)
    ap.add_argument("--no-cards", action="store_true", help="关掉卡片图，只发文字")
    ap.add_argument("--no-inbox", action="store_true", help="不监听 QQ 私聊（清理确认命令会不可用）")
    ap.add_argument("--collector", action="store_true",
                    help="采集进程：只采集入库，不推送、不起管理台（由监控台主进程托管）")
    ap.add_argument("--push-now", action="store_true",
                    help="立刻跑一次日程推送就退出（不启动采集）")
    ap.add_argument("--push-no-send", action="store_true",
                    help="配合 --push-now：只出图出文案，不发 QQ")
    ap.add_argument("--stop-file", default=str(ROOT / "data" / "bot.stop"),
                    help="出现这个文件就优雅退出（守护进程用）")
    return ap.parse_args()

def _install_sigbreak_handler() -> None:
    # Windows：把 CTRL_BREAK 映射成 KeyboardInterrupt，好让 finally 里的收尾逻辑跑到
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, signal.default_int_handler)
        except Exception:
            pass

def _make_logger(args: argparse.Namespace) -> tuple:
    sink = DailySink(Path(args.log_file)) if args.log_file else None

    def log(msg: str) -> None:
        now = time.localtime()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", now)
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
        if sink:
            sink.write(time.strftime("%Y-%m-%d", now), f"{stamp} {msg}")
    return sink, log

async def _run_push_now(args: argparse.Namespace, log) -> int:
    ok, summary, _detail = await asyncio.to_thread(
        push_once, ("--no-send",) if args.push_no_send else ())
    log(f"[日程推送] 手动触发{'（只出图不发送）' if args.push_no_send else ''}：{summary}")
    return 0 if ok else 3

async def _init_channel(args: argparse.Namespace, log) -> tuple:
    app_id = secret = ""
    ch = EchoChannel()
    if args.channel == "qq":
        from channels.official_qq import OfficialQQChannel
        env = load_env_file(args.env)
        app_id, secret = env.get("QQBOT_APP_ID", ""), env.get("QQBOT_APP_SECRET", "")
        if not app_id or not secret:
            log(f"[FAIL] {args.env} 里没有 QQBOT_APP_ID / QQBOT_APP_SECRET")
            return None, "", ""
        ch = OfficialQQChannel(app_id, secret)
        log(f"[通道] 官方 QQ，AppID {app_id[:4]}****{app_id[-2:]}，token 正常：{await ch.health()}")
    return ch, app_id, secret

def _new_client(mode: str = "per_user"):
    """按取数方式造客户端：关注流用 FollowFeedClient（BiliClient 子类，多 bili_jct/feed_page）。"""
    if mode == "feed":
        return FollowFeedClient()
    return BiliClient()

def _feed_capable() -> tuple[bool, str]:
    """关注流需要两样东西：登录态（读流）+ bili_jct（同步关注）。"""
    try:
        cookies = feedmod.load_follow_cookies()
    except Exception as e:                              # noqa: BLE001
        return False, f"读取关注凭据失败：{type(e).__name__}: {e}"
    return bool(cookies.can_read and cookies.can_follow), feedmod.fallback_reason(cookies)

def _resolve_poll_mode(args: argparse.Namespace, poll: dict, log) -> str:
    """定下这一轮跑什么取数方式，并把「为什么退回逐人」讲清楚。

    auto：有关注流凭据就走关注流，没有就逐人（老用户不配 config/bili_follow.env 也不报错）。
    feed：用户明确要关注流，凭据不全时临时退回逐人并告警，绝不让采集停摆。
    """
    configured = args.poll_mode or poll.get("mode") or "auto"
    mode = feedmod.normalize_mode(configured)
    if mode == "per_user":
        log("[取数] 动态采集：逐人轮询（poll.mode=per_user）")
        return "per_user"
    ok, why = _feed_capable()
    if ok:
        log("[取数] 动态采集：关注流（一次请求拿到全部关注的新动态，名单外自动忽略）")
        return "feed"
    if mode == "feed":
        log(f"[取数] poll.mode=feed，但关注流凭据不全（{why}）→ 本轮按逐人轮询跑；"
            "补上 config/bili_follow.env 后自动切回关注流")
    else:
        log(f"[取数] 动态采集：逐人轮询（未接入关注流：{why}）")
    return "per_user"

async def _init_store_client(args: argparse.Namespace, log, mode: str = "per_user") -> tuple:
    store = await SQLiteStore(args.db).open()
    client = _new_client(mode)
    if client.logged_in:
        _li = client.check_login()
        if _li["ok"]:
            log("[B站] 动态取数登录态：已启用（有效，账号 %s mid=%s）" % (_li["uname"], _li["mid"]))
        elif _li.get("state") == "expired":
            log("[B站] 动态取数登录态已失效（%s），请到管理台扫码续期"
                % (_li["message"] or "未登录"))
        else:
            log("[B站] 动态取数登录态暂未确认（%s），采集器稍后重试"
                % (_li["message"] or "接口未返回登录状态"))
    else:
        log("[B站] 动态取数登录态：未配置（匿名模式，撞 -352 时靠 desktop 兜底）；"
            "可到管理台点「扫码登录」或运行 python scripts/bili_qr_login.py")
    return store, client

async def _init_target(args: argparse.Namespace, store, app_id, log) -> tuple:
    target: Target | None = None
    openid = ""
    if args.channel == "qq" and not args.no_target:
        openid = args.openid or read_openid(args.ids)
        if not openid:
            log(f"[FAIL] 没有 user_openid：先跑 scripts/p0_check.py listen 让机器人收到你的私聊，或在 {args.ids} 里填")
            return None, "", False
        target = await store.register_qq_target(openid, app_id)
        log(f"[目标] 私聊 target_id={target.target_id} openid={openid[:6]}****{openid[-4:]}")
    return target, openid, True

async def _start_background(args: argparse.Namespace, ch, target, openid, app_id,
                            secret, log) -> tuple:
    inbox_task = notify_task = None
    inbox = None
    if target is not None and not args.once:
        async def _send_text(text: str):
            return await ch.send(target, RenderedMessage(text=text))

        if not args.no_inbox:
            from channels.qq_inbox import QQInbox
            inbox = QQInbox(app_id, secret, openid,
                            lambda text: review.handle_qq_command(text),
                            _send_text, log,
                            paused=lambda: T.BOARD.paused("qq_inbox"),
                            on_beat=lambda: T.BOARD.beat("qq_inbox"))
            T.BOARD.start("qq_inbox")
            inbox_task = asyncio.create_task(inbox.run())
            log("[QQ收件] 已启动：私聊回复「保留 1 3」可保护待清理录像")
        else:
            log("[QQ收件] 已禁用（--no-inbox），清理确认命令不可用")
        T.BOARD.start("cleanup_notify")
        notify_task = asyncio.create_task(cleanup_notify_loop(ch, target, log, T.BOARD))
        log("[清理通知] 已启动：待确认计划会通过本 Bot 推送")

    # 周表扫描 / 预约刷新 / 日程交接 / 每日推送已拆到 scripts/schedule_worker.py，
    # 由守护进程当第二个子进程拉起；这里不再起这四条循环。
    return inbox, inbox_task, notify_task

def _poll_config(args: argparse.Namespace, poll: dict, mode: str = "per_user") -> PollConfig:
    cfg = PollConfig()
    cfg.mode = mode
    if poll.get("live_interval_s"):
        cfg.live_interval_s = float(poll["live_interval_s"])
    if poll.get("dynamic_interval_s"):
        cfg.dynamic_interval_s = float(poll["dynamic_interval_s"])
    # 关注流一个请求就覆盖全部关注，轮询间隔可以短得多（默认 60s）
    if mode == "feed" and poll.get("feed_interval_s"):
        cfg.dynamic_interval_s = float(poll["feed_interval_s"])
    for key, attr, cast in (("feed_pages", "feed_pages", int),
                            ("feed_burst_threshold", "feed_burst_threshold", int),
                            ("feed_burst_pages", "feed_burst_pages", int),
                            ("sweep_hours", "sweep_hours", float),
                            ("sweep_max_age_min", "sweep_max_age_min", float),
                            ("sweep_late_max", "sweep_late_max", int),
                            ("follow_sync_hours", "follow_sync_hours", float),
                            ("enrich_retries", "enrich_retries", int),
                            ("enrich_max_items", "enrich_max_items", int)):
        if poll.get(key) is not None:
            try:
                setattr(cfg, attr, cast(poll[key]))
            except (TypeError, ValueError):
                pass                                   # 配置写错就用默认值，别让进程起不来
    if "follow_sync" in poll:
        cfg.follow_sync = bool(poll["follow_sync"])
    # 补漏补到的遗漏动态是否推送（2026-11-11）；false 退回「只记已见」的旧口径
    if "sweep_push_late" in poll:
        cfg.sweep_push_late = bool(poll["sweep_push_late"])
    if args.live_interval:
        cfg.live_interval_s = args.live_interval
    if args.dynamic_interval:
        cfg.dynamic_interval_s = args.dynamic_interval
    return cfg

def _build_watcher(args: argparse.Namespace, cfg: PollConfig, store, ch, client, log) -> tuple:
    cards = None
    if args.channel == "qq" and not args.no_cards:
        from notifications.cards import CardMaker
        cards = CardMaker(out_dir=str(ROOT / "data" / "cache" / "cards"),
                          img_dir=str(ROOT / "data" / "cache" / "img"), enabled=True)
        log("[卡片] 开播/动态以图片推送（文字与卡片图合并在一条富媒体消息里），缓存 data/cache/cards")

    dispatcher = Dispatcher(store, ch, now=time.time, cardmaker=cards, log=log)
    w = Watcher(store, client, cfg=cfg, dispatcher=dispatcher, log=log)
    return w, cards

def _start_web(args: argparse.Namespace, log, mode: str = "per_user") -> None:
    if args.web:
        from web.console import Console, serve_in_thread
        console = Console(args.db, args.subjects, args.log_file or None,
                          client_factory=lambda: _new_client(mode))
        serve_in_thread(console, args.web_host, args.web_port)
        log(f"[管理台] http://{args.web_host}:{args.web_port}  （只监听本机）")

async def _run_cycles(w, args: argparse.Namespace, log) -> None:
    if args.once:
        await w.bootstrap()
        res = await w.tick_once(force=True)
        log(f"[完成] {round_text(res)}")
    else:
        stop_file = Path(args.stop_file)

        def _stop_requested() -> bool:
            return stop_file.exists()

        await w.run(cycles=None, minutes=args.minutes, stop_check=_stop_requested)

async def _shutdown(store, sink, inbox, inbox_task, notify_task, cards,
                    started: float, log) -> None:
    if inbox is not None:
        try:
            await inbox.stop()
        except Exception:  # noqa: BLE001
            pass
    if inbox_task is not None:
        try:
            await asyncio.wait_for(inbox_task, timeout=3.0)
        except Exception:  # noqa: BLE001
            inbox_task.cancel()
    if notify_task is not None:
        notify_task.cancel()
        try:
            await notify_task
        except Exception:  # noqa: BLE001
            pass
    if cards is not None:
        await cards.aclose()
    st = await store.stats()
    log(f"[统计] 运行 {time.time() - started:.0f}s  事件 {st['events']}  投递 {st['deliveries']}  {st['stats']}")
    dead = await store.dead_letters(5)
    if dead:
        item = dict(dead[0])
        raw = item.pop("raw", None)
        log(f"[死信] {len(dead)} 条，最近：{item}")
        if raw:
            # 原文可能上万字（最多 4000 字预览）：日志只留开头够定位的部分，
            # 完整原文在管理台「投递」页看，别让一行日志把整个日志文件撑大。
            log(f"[死信] 原文（前 300 字，完整见管理台）：{raw[:300]}")
    await store.close()
    if sink:
        sink.close()

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
