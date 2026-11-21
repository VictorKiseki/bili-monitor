"""监控台主进程：起管理台页面，并托管采集/推送两个子进程。

用法：
    python scripts/run_console.py                     # http://127.0.0.1:8765
    python scripts/run_console.py --port 9000 --open   # 换端口并自动开浏览器
    python scripts/run_console.py --no-stack           # 只开页面，不托管采集/推送

进程模型（2026-11-11 起）：
    supervisor.py ── run_console.py（本进程，页面 + 进程栈）
                        ├─ run_bot.py --collector   （绑了 B 站账号才起）
                        └─ run_pusher.py            （配好 QQ 机器人 + 目标才起）
    管理台只读写同一个 data/bot.db（WAL），采集卡住不影响页面刷新。
"""
from __future__ import annotations

import argparse
import http.client
import json
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared import online_update
from shared import proxy as proxy_cfg                      # noqa: E402
proxy_cfg.apply_to_env()                                   # 先套用 config/proxy.json，再建任何 HTTP client

import core.tasks as T                                     # noqa: E402
from web.console import Console, serve_in_thread           # noqa: E402
from web.stack import StackManager                         # noqa: E402

HEARTBEAT_S = 10.0


def _install_sigbreak_handler(stop_event: threading.Event) -> None:
    def _on_break(_signum, _frame):
        stop_event.set()

    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, _on_break)
        except Exception:                          # noqa: BLE001
            pass


def _heartbeat_loop(path: Path, stop_event: threading.Event) -> None:
    """给守护进程看的「监控台还活着」心跳：采集全停时主循环不会写 last_tick。"""
    while not stop_event.wait(HEARTBEAT_S):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("%d\n" % int(time.time()), encoding="ascii")
        except OSError:
            pass


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
        except Exception:                                      # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "bot.db"))
    ap.add_argument("--subjects", default=str(ROOT / "config" / "subjects.json"))
    ap.add_argument("--log-file", default=str(ROOT / "out" / "console.log"))
    ap.add_argument("--log-dir", default=str(ROOT / "logs"),
                    help="按日日志目录（采集/推送写这里，日志页读这里）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--stop-file", default=str(ROOT / "data" / "console.stop"),
                    help="出现这个文件就优雅退出（守护进程用）")
    ap.add_argument("--beat-file", default=str(ROOT / "data" / "console.beat"))
    ap.add_argument("--no-stack", action="store_true", help="只开页面，不托管采集/推送进程")
    ap.add_argument("--stack-interval", type=float, default=2.0)
    args = ap.parse_args()

    stop_event = threading.Event()
    _install_sigbreak_handler(stop_event)
    stop_file = Path(args.stop_file)
    try:                                    # 上一次留下的停止文件不能误杀这一次
        stop_file.unlink(missing_ok=True)
    except OSError:
        pass

    def log(msg: str) -> None:
        print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)

    # 进程页的开关写 meta['tasks_off']：ConsoleCore.__init__ 里已经 bind 过一次，
    # 这里显式再绑一遍（幂等），免得以后换掉 Console 的实现又漏掉。
    T.BOARD.bind(args.db)
    console = Console(args.db, args.subjects, args.log_file or None, log_dir=args.log_dir)
    stack = None
    if not args.no_stack:
        stack = StackManager(root=ROOT, python=sys.executable, db=args.db,
                             subjects=args.subjects, interval=args.stack_interval,
                             log=log)
        console.stack = stack

    # 进程页「开机自启」：决定本次启动要不要顺手拉起子进程。必须赶在 stack.start()
    # 之前把开关交给栈管理器，否则绑定命中时会先起一次再被下一轮 reconcile 收掉。
    try:
        console.apply_autostart()
    except Exception as e:  # noqa: BLE001
        log("[管理台] 自启开关应用失败：%s" % e)
    trial = online_update.trial_token()
    console._online_update_trial = bool(trial)
    if stack is not None and not trial:
        stack.start()

    srv, _ = serve_in_thread(console, args.host, args.port)
    if trial:
        update = online_update.OnlineUpdate(ROOT)
        probe = http.client.HTTPConnection(args.host, srv.server_address[1], timeout=3)
        try:
            probe.request("GET", "/")
            page = probe.getresponse()
            if page.status != 200 or b'id="updModal"' not in page.read():
                raise online_update.UpdateError("管理台页面健康检查失败")
            probe.request("GET", "/api/version")
            response = probe.getresponse()
            if response.status != 200 or json.loads(response.read()).get("version") != update.current():
                raise online_update.UpdateError("管理台版本健康检查失败")
        finally:
            probe.close()
        update.mark_ready(trial)
        deadline = time.monotonic() + 50
        while not update.trial_accepted(trial):
            if stop_file.exists() or stop_event.wait(0.25) or time.monotonic() >= deadline:
                srv.shutdown()
                srv.server_close()
                return 1
        console._online_update_trial = False
        if stack is not None:
            stack.start()
    url = "http://%s:%d" % (args.host, args.port)
    log("[管理台] %s  （库 %s）" % (url, args.db))
    if stack is not None:
        state = stack.binding_state()
        log("[管理台] 绑定状态：B 站 %s / QQ %s（绑上后自动起对应进程）"
            % ("已绑定" if state["bili"]["bound"] else "未绑定",
               "已绑定" if state["qq"]["bound"] else "未绑定"))
    else:
        log("[管理台] --no-stack：只提供页面，不起采集/推送")
    log("[管理台] Ctrl+C 停止；只用本机访问，不对外网开放")

    beat_stop = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(Path(args.beat_file), beat_stop),
                     name="console-beat", daemon=True).start()
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        while not stop_event.wait(1.0):
            if stop_file.exists():
                log("[管理台] 收到停止请求（%s）" % stop_file.name)
                break
    except KeyboardInterrupt:
        log("[管理台] 收到 Ctrl+C")
    finally:
        beat_stop.set()
        if stack is not None:
            log("[管理台] 收尾：停采集/推送子进程")
            stack.request_stop()
            stack.shutdown()
        try:
            srv.shutdown()
        except Exception:                    # noqa: BLE001
            pass
        srv.server_close()
        try:
            stop_file.unlink(missing_ok=True)
        except OSError:
            pass
        log("[管理台] 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
