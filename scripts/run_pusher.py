"""推送进程：只负责把库里排队的投递发出去（QQ 官方机器人）。

为什么单独一个进程（2026-11-11）：
  · 采集要连 B 站、推送要连 QQ，任何一边卡住都不该拖住另一边；
  · 管理台是第三个进程，页面永远要能打开；
  · 用户还没配机器人时这个进程根本不该起——由监控台主进程按绑定状态拉起。

职责：claim_due → 渲染 → 限流 → 发送 → 重试/死信（逻辑仍在 notifications/dispatcher.py），
外加 QQ 私聊收件（清理确认命令）与清理通知。

用法（一般不手动跑，由 web/stack.py 托管）：
    python scripts/run_pusher.py --db data/bot.db --stop-file data/pusher.stop
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from shared import proxy as proxy_cfg                   # noqa: E402
proxy_cfg.apply_to_env()                                # 推送进程：建 client 前套用代理配置

import run_bot as bot                                   # noqa: E402  复用日志/环境/清理通知
from channels.official_qq import OfficialQQChannel       # noqa: E402
from core import tasks as T                             # noqa: E402
from notifications.dispatcher import Dispatcher          # noqa: E402
from shared import readiness                                # noqa: E402
from shared.contracts import RenderedMessage, Target  # noqa: E402
from store.sqlite import SQLiteStore                     # noqa: E402

RECLAIM_INTERVAL_S = 30.0
TICK_S = 1.0            # 库里没东西时也每秒看一眼：新事件入库就能马上发
GC_S = 1800.0           # 缓存兜底清理（卡图/图源/顶部图）


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "bot.db"))
    ap.add_argument("--env", default=str(ROOT / "BBOT.env"))
    ap.add_argument("--ids", default=str(ROOT / ".p0_ids.json"))
    ap.add_argument("--openid", default="")
    ap.add_argument("--log-file", default=str(ROOT / "logs" / "pusher.log"))
    ap.add_argument("--stop-file", default=str(ROOT / "data" / "pusher.stop"))
    ap.add_argument("--interval", type=float, default=TICK_S)
    ap.add_argument("--no-inbox", action="store_true", help="不监听 QQ 私聊")
    ap.add_argument("--no-cards", action="store_true", help="只发文字，不画卡片图")
    ap.add_argument("--once", action="store_true", help="清一轮队列就退出（排错用）")
    return ap.parse_args()


def _install_sigbreak_handler() -> None:
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, signal.default_int_handler)
        except Exception:                               # noqa: BLE001
            pass


async def _start_inbox(args, ch, target, app_id, secret, log) -> tuple:
    """QQ 私聊收件 + 清理通知：只在真正配好机器人和目标时才起。"""
    from channels.qq_inbox import QQInbox
    import cleanup_review as review

    async def _send_text(text: str):
        return await ch.send(target, RenderedMessage(text=text))

    inbox = QQInbox(app_id, secret, target.external_id,
                    lambda text: review.handle_qq_command(text),
                    _send_text, log,
                    paused=lambda: T.BOARD.paused("qq_inbox"),
                    on_beat=lambda: T.BOARD.beat("qq_inbox"))
    T.BOARD.start("qq_inbox")
    inbox_task = asyncio.create_task(inbox.run())
    log("[QQ收件] 已启动：私聊回复「保留 1 3」可保护待清理录像")
    T.BOARD.start("cleanup_notify")
    notify_task = asyncio.create_task(bot.cleanup_notify_loop(ch, target, log, T.BOARD))
    log("[清理通知] 已启动：待确认计划会通过本 Bot 推送")
    return inbox, inbox_task, notify_task


async def run() -> int:
    args = _parse_args()
    _install_sigbreak_handler()
    sink, log = bot._make_logger(args)
    T.BOARD.bind(args.db)

    env = bot.load_env_file(args.env)
    app_id = env.get("QQBOT_APP_ID", "")
    secret = env.get("QQBOT_APP_SECRET", "")
    openid = args.openid or bot.read_openid(args.ids)
    # 占位值（*example* 里的「填…」/「<openid>」）与空值同样算没配：
    # 否则会拿着占位凭据去连 QQ，在收件循环里反复重连，用户只看到满屏报错
    if readiness.is_placeholder(app_id) or readiness.is_placeholder(secret):
        log("[FAIL] 还没有 QQ 机器人凭据：到管理台「机器人」页填 AppID / AppSecret")
        return 2
    if readiness.is_placeholder(openid) or len(openid) < readiness.MIN_OPENID:
        log("[FAIL] 还没有推送目标：先在 QQ 里私聊机器人一句话，再到管理台「机器人」页抓取")
        return 2

    store = await SQLiteStore(args.db).open()
    target = await store.register_qq_target(openid, app_id)
    log("[目标] 私聊 target_id=%d openid=%s****%s" % (target.target_id, openid[:6], openid[-4:]))

    ch = OfficialQQChannel(app_id, secret)
    log("[通道] 官方 QQ，AppID %s****%s，token 正常：%s"
        % (app_id[:4], app_id[-2:], await ch.health()))

    cards = None
    if not args.no_cards:
        from notifications.cards import CardMaker
        cards = CardMaker(out_dir=str(ROOT / "data" / "cache" / "cards"),
                          img_dir=str(ROOT / "data" / "cache" / "img"), enabled=True)
        log("[卡片] 开播/动态以图片推送，缓存 data/cache/cards")

    dispatcher = Dispatcher(store, ch, now=time.time, cardmaker=cards, log=log)

    inbox = inbox_task = notify_task = None
    if not args.once and not args.no_inbox:
        inbox, inbox_task, notify_task = await _start_inbox(args, ch, target, app_id, secret, log)

    stop_file = Path(args.stop_file)
    started = time.time()
    sent_total = 0
    last_gc = time.time()
    reclaim_at = time.monotonic()
    log("[推送] 开始派发，间隔 %.1fs，停止文件 %s" % (max(0.2, args.interval), stop_file))
    try:
        while True:
            if not args.once and stop_file.exists():
                log("[退出] 收到停止请求（%s）" % stop_file.name)
                break
            now = time.monotonic()
            if now >= reclaim_at:
                reclaimed = await store.reclaim_stale_claims()
                if reclaimed:
                    log(f"[恢复] 回收超时未完成的投递 {reclaimed} 条")
                reclaim_at = now + RECLAIM_INTERVAL_S
            if not T.BOARD.paused("dispatch") and await store.get_meta("global_mode", "full") == "full":
                outcomes = await dispatcher.tick()
                sent = sum(1 for o in outcomes if o.action == "sent")
                sent_total += sent
                if outcomes:
                    failed = sum(1 for o in outcomes if o.action != "sent")
                    log("[派发] %d 条：成功 %d，重试/死信 %d" % (len(outcomes), sent, failed))
            if T.BOARD.paused("cache_gc") is False and time.time() - last_gc >= GC_S:
                last_gc = time.time()
                stats = await dispatcher.gc_media()
                T.BOARD.beat("cache_gc", runs=1)
                if stats:
                    log("[缓存] 兜底清理：卡片 %d 张、图源 %d 张、顶部图 %d 张"
                        % (stats.get("cards", 0), stats.get("imgs", 0), stats.get("tops", 0)))
            if args.once:
                break
            await asyncio.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        log("[退出] 收到 Ctrl+C")
    finally:
        if inbox is not None:
            try:
                await inbox.stop()
            except Exception:                           # noqa: BLE001
                pass
        for task in (inbox_task, notify_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except BaseException:                       # noqa: BLE001
                pass
        if cards is not None:
            await cards.aclose()
        st = await store.stats()
        log("[统计] 推送进程运行 %.0fs，本次发出 %d 条  事件 %d  投递 %d"
            % (time.time() - started, sent_total, st["events"], st["deliveries"]))
        await ch.close()
        await store.close()
        if sink:
            sink.close()
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
