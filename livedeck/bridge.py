"""Edge CDP 弹幕桥。

为什么必须这样
--------------
2026 年的 B 站弹幕 WebSocket 会按 TLS 指纹（JA3）拒绝非浏览器客户端：即便把
Origin / Cookie / 认证字段全部照抄，websocket-client 与 websockets 也会在
发出认证包后立刻收到 1006。真实 Chromium 页面里同样的字段却能通过。

于是这里起一个 headless Edge，打开 https://live.bilibili.com/robots.txt
（保证 Origin 是 live.bilibili.com，且页面几乎不耗资源），通过 CDP：
  Runtime.addBinding("ldSend")  →  页面把事件回传给 Python
  Runtime.evaluate              →  注入 JS：按房间 fetch 弹幕 token、建 WS、
                                    认证、心跳，并把原始二进制帧 base64 回传

Python 侧只负责解帧（brotli）与分发，不碰 WS 握手。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

import websocket

from shared.proc import no_window_flags
from shared.tmpdir import ensure as _ensure_tempdir

BRIDGE_PAGE = "https://live.bilibili.com/robots.txt"
BINDING = "ldSend"

FLAGS = [
    "--headless=new",
    "--disable-gpu",
    # 管理台/守护进程以管理员身份启动时，Edge 157 的渲染器沙箱会让页面 target
    # 完全无响应（browser WS 能连，但 Page.enable / Runtime.evaluate 一律超时）。
    # 这个临时实例只访问 B 站，关掉沙箱换取可用性。
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--incognito",
    "--disable-sync",
    "--mute-audio",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-features=msEdgeSyncPromo,EdgeSyncPromo,msEdgeFirstRunExperience,"
    "EdgeFirstRunExperience,Translate,OptimizationHints",
]

STALE_MARK = "livedeck-edge-"


SEP = "|@@|"
FLD = "|~|"


def _edge_procs(needle: str) -> list[tuple[int, str]]:
    """列出命令行里带 needle 的 msedge 进程 -> [(pid, 命令行)]。

    Edge 的启动器把浏览器甩给另一个进程后自己就退出（exit 0），所以 Popen 拿到的
    PID 往往不是真正在跑的浏览器，只能靠 profile 路径反查。顺手把命令行带回来，
    清理时才能判断哪个进程属于当前实例。
    """
    if not needle:
        return []
    # 注意：PowerShell 里 "$($_.ProcessId) + 'x'" 会尝试把字符串加进 UInt32 直接报错，
    # 必须用字符串插值拼出一整个字符串。
    script = (
        "$p=@(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -like '*" + needle.replace("'", "''") + "*' } | "
        "ForEach-Object { \"$($_.ProcessId)" + FLD + "$($_.CommandLine)\" }); "
        "($p -join '" + SEP + "')"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=30,
                             creationflags=no_window_flags()).stdout
    except Exception:  # noqa: BLE001
        return []
    rows: list[tuple[int, str]] = []
    for chunk in out.split(SEP):
        chunk = chunk.strip()
        if not chunk:
            continue
        pid_s, _, cmd = chunk.partition(FLD)
        if pid_s.strip().isdigit():
            rows.append((int(pid_s), cmd))
    return rows


def _edge_pids(needle: str) -> list[int]:
    return [pid for pid, _ in _edge_procs(needle)]


def _kill_pids(pids, log=None) -> int:
    killed = 0
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8,
                           creationflags=no_window_flags())
            killed += 1
        except Exception:  # noqa: BLE001
            pass
    if log and killed:
        log("弹幕桥清理残留浏览器：%d 个进程" % killed)
    return killed


# 页面侧逻辑：保持无外部依赖，兼容 Edge/Chrome。
JS_BOOT = r"""
(function () {
  if (window.__ldReady) return "already";
  window.__ldReady = true;
  window.__ldRooms = {};
  window.__ldSeq = 1;
  window.__ldEmit = function (obj) {
    try { window.ldSend(JSON.stringify(obj)); } catch (e) { }
  };
  window.__ldPack = function (op, ver, text) {
    var body = new TextEncoder().encode(text || "");
    var buf = new ArrayBuffer(16 + body.length);
    var dv = new DataView(buf);
    dv.setUint32(0, 16 + body.length);
    dv.setUint16(4, 16);
    dv.setUint16(6, ver || 1);
    dv.setUint32(8, op);
    dv.setUint32(12, window.__ldSeq++);
    new Uint8Array(buf).set(body, 16);
    return buf;
  };
  window.__ldB64 = function (bytes) {
    var s = "", CH = 0x8000;
    for (var i = 0; i < bytes.length; i += CH) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    }
    return btoa(s);
  };
  window.__ldClose = function (room, why) {
    var st = window.__ldRooms[room];
    if (!st) return;
    st.closedByUs = true;
    if (st.hb) { clearInterval(st.hb); st.hb = null; }
    if (st.retry) { clearTimeout(st.retry); st.retry = null; }
    try { if (st.ws) st.ws.close(); } catch (e) { }
    delete window.__ldRooms[room];
    window.__ldEmit({ type: "closed", room: room, why: why || "drop" });
  };
  window.__ldConnect = function (it) {
    var room = it.room;
    if (window.__ldRooms[room]) window.__ldClose(room, "replace");
    var st = { it: it, ws: null, hb: null, retry: null, tries: 0, closedByUs: false, state: "fetch" };
    if (!it.wss_port) it.wss_port = 443;
    window.__ldRooms[room] = st;
    // token 由 Python 侧用 WBI 签名取好后下发；页面只负责建立 WS（浏览器 TLS 指纹才过风控）
    var link = function (token, host, wssPort) {
      var ws = new WebSocket("wss://" + host + ":" + wssPort + "/sub");
      st.ws = ws;
      ws.binaryType = "arraybuffer";
      ws.onopen = function () {
        st.state = "open";
        st.tries = 0;
        var auth = JSON.stringify({
          uid: it.uid || 0,
          roomid: it.real || room,
          protover: it.protover || 3,
          buvid: it.buvid,
          support_ack: true,
          queue_uuid: "ld" + room + "x" + Math.floor(Math.random() * 1e6),
          scene: "room",
          platform: "web",
          type: 2,
          key: token || ""
        });
        try { ws.send(window.__ldPack(7, 1, auth)); } catch (e) { }
        st.hb = setInterval(function () {
          try { ws.send(window.__ldPack(2, 1, "")); } catch (e) { }
        }, 20000);
        window.__ldEmit({ type: "open", room: room });
      };
      ws.onmessage = function (ev) {
        if (st.closedByUs || st.ws !== ws) return;
        if (typeof ev.data === "string") return;
        var u8 = new Uint8Array(ev.data);
        window.__ldEmit({ type: "frame", room: room, data: window.__ldB64(u8) });
      };
      ws.onerror = function () { };
      ws.onclose = function (ev) {
        // 旧连接被替换后仍会补发 onclose，不能让它覆盖新连接的状态
        if (st.closedByUs) { try { ws.onmessage = null; } catch (e) { } return; }
        if (st.ws !== ws) { try { ws.onmessage = null; } catch (e) { } return; }
        if (st.hb) { clearInterval(st.hb); st.hb = null; }
        st.state = "closed";
        window.__ldEmit({ type: "close", room: room, code: ev.code || 0, reason: ev.reason || "" });
        if (!st.closedByUs) {
          st.ws = null;
          window.__ldRetry(st, Math.min(30000, 3000 * Math.pow(1.6, st.tries)));
        }
      };
    };
    var onInfo = function (info) {
      if (st.closedByUs) return;
      if (!info || info.code !== 0 || !info.data || !info.data.host_list || !info.data.host_list.length) {
        window.__ldEmit({ type: "denied", room: room, code: info && info.code, msg: (info && info.message) || "" });
        return window.__ldRetry(st, 8000);
      }
      var d = info.data;
      var host = d.host_list[0];
      link(d.token, host.host, host.wss_port);
    };
    var go = function () {
      if (st.closedByUs) return;
      st.state = "fetch";
      window.__ldEmit({ type: "connecting", room: room, tries: st.tries });
      if (it.token && it.host) {
        onInfo({ code: 0, data: { token: it.token, host_list: [{ host: it.host, wss_port: it.wss_port || 443 }] } });
        return;
      }
      fetch(it.url, { credentials: "include", cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(onInfo)
        .catch(function (e) {
          window.__ldEmit({ type: "error", room: room, msg: String((e && e.message) || e) });
          window.__ldRetry(st, 8000);
        });
    };
    st.go = go;
    go();
  };
  window.__ldRetry = function (st, delay) {
    if (st.closedByUs) return;
    st.tries += 1;
    if (st.retry) clearTimeout(st.retry);
    st.retry = setTimeout(function () { st.retry = null; st.go(); }, delay);
  };
  window.__ldSetRooms = function (list) {
    var want = {}, i;
    for (i = 0; i < list.length; i++) want[list[i].room] = list[i];
    for (var room in window.__ldRooms) {
      if (!want[room]) window.__ldClose(room, "removed");
    }
    for (i = 0; i < list.length; i++) {
      var it = list[i];
      var st = window.__ldRooms[it.room];
      if (st) {
        if (st.it.url !== it.url || st.it.buvid !== it.buvid || st.it.token !== it.token ||
            st.it.protover !== it.protover) {
          window.__ldClose(it.room, "refresh");
          window.__ldConnect(it);
        } else {
          st.it = it;
        }
      } else {
        window.__ldConnect(it);
      }
    }
    return Object.keys(window.__ldRooms).length;
  };
  window.__ldStat = function () {
    var out = {};
    for (var k in window.__ldRooms) out[k] = window.__ldRooms[k].state;
    return JSON.stringify(out);
  };
  return "booted";
})();
"""


class BridgeError(RuntimeError):
    pass


def find_browser() -> str | None:
    configured = os.environ.get("LIVEDECK_BROWSER")
    if configured and Path(configured).is_file():
        return configured
    for suffix in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
        for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(name)
            if base:
                path = Path(base) / suffix
                if path.is_file():
                    return str(path)
        path = shutil.which(Path(suffix).name)
        if path:
            return path
    return None


class EdgeBridge:
    """常驻 headless 浏览器 + 一条 CDP 连接，负责把各房间弹幕帧送回 Python。"""

    def __init__(self, on_event: Callable[[dict], None], log: Callable[[str], None] | None = None,
                 engine=None):
        self.on_event = on_event
        self.log = log or (lambda msg: None)
        # 引擎日志（livedeck/engine_log.py）：None 时只走 log 回调。离线现场要能事后查，
        # 所以浏览器启动 / CDP / 注入 / 读线程退出的关键节点都往这里写一份。
        self.engine = engine
        self.last_recv_error = ""
        self._timeout_logged: dict[str, float] = {}
        self.status = "idle"
        self.last_error = ""
        self.rooms: dict[int, dict] = {}
        self._browser = ""
        self._buvid = ""
        self._cookies: dict[str, str] = {}
        self._proc: subprocess.Popen | None = None
        self._ws: websocket.WebSocket | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._mid = 0
        self._profile: str | None = None
        self._port = 0
        self._browser_ws = ""
        self._stopping = False
        self._fails = 0          # 连续启动失败次数：连续失败说明残留实例把机器拖住了

    # ---------- 引擎日志 ----------
    def _ev(self, level: str, area: str, message: str, room: int = 0, detail: str = "") -> None:
        eng = self.engine
        if eng is None:
            return
        try:
            eng.write(level, area, message, room=room, detail=detail)
        except Exception:  # noqa: BLE001
            pass

    def _edge_tail(self, limit: int = 600) -> str:
        """Edge 自己被重定向的 stderr 尾部：启动器静默退出时原因往往只在这里。"""
        try:
            p = os.path.join(self._profile or "", "edge.log")
            if not (self._profile and os.path.isfile(p)):
                return ""
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - limit))
                return f.read().decode("utf-8", "replace").replace("\n", " ").strip()
        except OSError:
            return ""

    # ---------- 生命周期 ----------
    def mark_starting(self, timeout: float = 0.5) -> None:
        """先把状态标成 starting：浏览器还在起的那几秒，页面就能显示「引擎启动中」。

        原来 idle/stopped/error 会一直挂到 start() 成功为止，这段时间前端只能显示
        「引擎离线」，看起来像压根没在连。

        timeout：start() 会一直持有 _lock 到本轮启动结束（最坏几十秒）。看门狗补启动时
        不能跟着阻塞，否则「补一次」本身也卡住；拿不到锁就跳过，反正状态本来就是
        starting / error，页面显示不受影响。
        """
        got = self._lock.acquire(timeout=timeout)
        try:
            if got and self.status != "online":
                self.status = "starting"
        finally:
            if got:
                self._lock.release()

    def start(self) -> bool:
        with self._lock:
            if self.status == "online":
                return True
            # stop() 把 _stopping 置 True 只是让旧读线程别再标 stale，但它会一直挂着，
            # 之后显式 start() 会被这里直接挡掉（空舞台 → 加房间起不来就是这么来的）。
            self._stopping = False
            self.status = "starting"
            # 一次启动最多两轮：某些时候（残留实例抢 CPU、profile 锁没释放）第一轮会卡在
            # Page.enable，清干净再来一次通常就正常，比等下一轮 reconcile（8 秒）快。
            for attempt in (1, 2):
                try:
                    self._launch()
                    self._connect_cdp()
                    self._install()
                    self.status = "online"
                    self.last_error = ""
                    self._fails = 0
                    self.log("弹幕桥就绪（%s）" % Path(self._browser).name)
                    self._ev("info", "bridge", "弹幕桥就绪：%s port=%s profile=%s" % (
                        Path(self._browser).name, self._port, os.path.basename(self._profile or "")),
                        detail="第 %d 轮启动成功" % attempt)
                    return True
                except Exception as e:  # noqa: BLE001
                    self.status = "error"
                    self.last_error = "%s: %s" % (e.__class__.__name__, e)
                    self.log("弹幕桥启动失败%s：%s" % (
                        "" if attempt == 1 else "（第 2 次尝试）", self.last_error))
                    self._ev("error", "bridge", "弹幕桥启动失败%s：%s" % (
                        "" if attempt == 1 else "（第 2 次尝试）", self.last_error),
                        detail=self._edge_tail())
                    self._teardown()
                    self._fails += 1
                    if self._fails >= 3:
                        # 连续失败：残留实例/端口状态已经乱了，整批清掉歇一秒再重来
                        self.log("弹幕桥连续失败 %d 次，清理残留实例后重试" % self._fails)
                        _kill_pids(_edge_pids(STALE_MARK), self.log)
                        self._fails = 0
                        time.sleep(1.5)
                    else:
                        time.sleep(0.8)
            return False

    def _launch(self) -> None:
        browser = find_browser()
        if not browser:
            raise BridgeError("找不到 Edge/Chrome 可执行文件，可用 LIVEDECK_BROWSER 指定")
        self._browser = browser
        self._port = _free_port()
        self._profile = _make_profile_dir()
        args = [browser] + FLAGS + [
            "--remote-debugging-port=%d" % self._port,
            "--user-data-dir=%s" % self._profile,
            "about:blank",
        ]
        errfile = None
        try:
            errfile = open(os.path.join(self._profile, "edge.log"), "wb")
        except OSError:
            errfile = subprocess.DEVNULL
        # 服务被强杀时 Edge 收不到 Browser.close，会一直堆在后台；启动前先清掉带本工具
        # profile 标记的残留实例（单实例工具，profile 前缀只属于本程序）。
        stale = _edge_pids(STALE_MARK)
        if stale:
            _kill_pids(stale, self.log)
        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=errfile,
            creationflags=no_window_flags(),
        )
        self.log("弹幕桥启动浏览器：pid=%s port=%s" % (self._proc.pid, self._port))
        self._ev("info", "launch", "启动浏览器：%s pid=%s port=%s profile=%s" % (
            Path(browser).name, self._proc.pid, self._port, os.path.basename(self._profile)),
            detail="参数：%s" % " ".join(FLAGS[:4]))
        # Edge 的启动器把浏览器进程甩出去后自己就退出（code=0），不能据此判失败，
        # 只能等 DevTools 端口真正应答。
        started = time.time()
        deadline = time.time() + 45
        last = ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % self._port, timeout=2) as r:
                    info = json.loads(r.read())
                self._browser_ws = info.get("webSocketDebuggerUrl") or ""
                self._ev("info", "launch", "DevTools 就绪，用时 %.1f 秒" % (time.time() - started))
                return
            except Exception as e:  # noqa: BLE001
                last = str(e)
                time.sleep(0.35)
        code = self._proc.poll() if self._proc is not None else None
        tail = ""
        try:
            lp = os.path.join(self._profile or "", "edge.log")
            if os.path.isfile(lp):
                with open(lp, "rb") as f:
                    tail = f.read()[-400:].decode("utf-8", "replace").replace("\n", " ")
        except OSError:
            tail = ""
        tail_all = tail or self._edge_tail(1200)
        msg = "DevTools 未就绪：%s（退出码 %s）%s" % (last, code, ("｜" + tail_all) if tail_all else "")
        self._ev("error", "launch", "DevTools 端口 %.0f 秒无应答（退出码 %s）" % (
            time.time() - started, code), detail=tail_all or "edge.log 为空，多为 profile 权限/残留实例锁")
        raise BridgeError(msg)

    def _connect_cdp(self) -> None:
        page = None
        for _ in range(20):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json" % self._port, timeout=2) as r:
                    targets = json.loads(r.read())
                pages = [t for t in targets if t.get("type") == "page"]
                if pages:
                    page = pages[0]
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
        if not page:
            self._ev("error", "cdp", "6 秒内没有可用的页面 target",
                     detail="/json 列表里没有 type=page：浏览器起来了但渲染进程没建出页面")
            raise BridgeError("没有可用的页面 target")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30,
                                         enable_multithread=True, suppress_origin=True)
        self._ws = ws
        self._ev("info", "cdp", "已连接 CDP：%s" % str(page.get("url") or "")[:80])
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()

    def _install(self) -> None:
        steps = (("Page.enable", None), ("Runtime.enable", None), ("Network.enable", None),
                 ("Page.navigate", {"url": BRIDGE_PAGE}))
        for name, params in steps:
            t0 = time.time()
            try:
                self._cmd(name, params)
            except Exception as e:  # noqa: BLE001
                self._ev("error", "page", "CDP 步骤失败：%s（%.1f 秒）" % (name, time.time() - t0),
                         detail="%s: %s" % (e.__class__.__name__, e))
                raise
            cost = time.time() - t0
            if cost > 3:
                self._ev("warn", "page", "CDP 步骤偏慢：%s 用了 %.1f 秒" % (name, cost),
                         detail="页面/渲染进程被拖住；常见于残留 msedge 抢 CPU 或 profile 锁未释放")
        self._wait_page("live.bilibili.com", 20)
        self._apply_cookies()
        self._cmd("Runtime.addBinding", {"name": BINDING})
        last = ""
        for _ in range(10):
            res = self._cmd("Runtime.evaluate", {
                "expression": JS_BOOT, "returnByValue": True, "awaitPromise": False,
            })
            if res.get("exceptionDetails"):
                last = json.dumps(res["exceptionDetails"], ensure_ascii=False)[:200]
                time.sleep(0.4)
                continue
            check = self._cmd("Runtime.evaluate", {
                "expression": "typeof window.__ldSetRooms === 'function'", "returnByValue": True,
            })
            if (check.get("result") or {}).get("value"):
                return
            time.sleep(0.4)
        self._ev("error", "page", "页面脚本注入失败", detail=(last or "页面上下文未就绪")[:600])
        raise BridgeError("注入失败：%s" % (last or "页面上下文未就绪"))

    def _wait_page(self, needle: str, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                res = self._cmd("Runtime.evaluate", {
                    "expression": "location.href", "returnByValue": True,
                })
                href = str((res.get("result") or {}).get("value") or "")
                if needle in href:
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        raise BridgeError("页面没有打开：%s" % BRIDGE_PAGE)

    def _apply_cookies(self) -> None:
        jars = dict(self._cookies)
        if self._buvid:
            jars.setdefault("buvid3", self._buvid)
        for name, value in jars.items():
            if not value:
                continue
            self._cmd("Network.setCookie", {
                "name": name, "value": value,
                "domain": ".bilibili.com", "path": "/",
                "secure": True,
            })

    def set_cookies(self, cookies: dict[str, str] | None) -> bool:
        """更新登录 Cookie；返回是否需要重启页面上下文才能生效。"""
        new = {k: v for k, v in (cookies or {}).items() if v}
        changed = new != self._cookies
        self._cookies = new
        if changed and self.status == "online":
            try:
                self._apply_cookies()
            except Exception:  # noqa: BLE001
                return True
        return False

    def set_buvid(self, buvid: str) -> None:
        self._buvid = buvid

    def cleanup_stale(self) -> int:
        """清掉上一次运行遗留的无主 Edge（服务被强杀时它们收不到 Browser.close）。"""
        stale = _edge_pids(STALE_MARK)
        return _kill_pids(stale, self.log) if stale else 0

    def janitor(self) -> dict:
        """周期性清理：杀掉带本工具 profile 标记、但不是当前实例的残留浏览器，
        再删掉已经没人用的 livedeck-edge-* 临时目录。

        服务被强杀 / CDP 卡死重建时，上一份 Edge 进程树可能没有收到 Browser.close，
        一直挂在后台吃内存；只靠启动时清一次不够（长跑几小时后会堆起来）。
        当前实例的 profile 路径一定跳过，绝不误杀正在用的浏览器。
        """
        with self._lock:
            keep = self._profile or ""
            online = self.status == "online"
        if online and not keep:
            return {"killed": 0, "profiles": 0}
        procs = _edge_procs(STALE_MARK)
        # 子进程（GPU / renderer / crashpad）的命令行里可能是 8.3 短路径，全路径匹配会漏；
        # livedeck-edge-xxxx 这一段目录名是 mkdtemp 生成的，唯一且长短路径都一样，按它判断。
        own = os.path.basename(keep) if keep else ""
        def _mine(cmd: str) -> bool:
            return bool(own) and own in cmd
        victims = [pid for pid, cmd in procs if not _mine(cmd)]
        killed = _kill_pids(victims) if victims else 0
        removed = self._prune_profiles(keep, procs)
        if killed:
            self.log("弹幕桥自动清理：%d 个残留浏览器进程" % killed)
        if removed:
            self.log("弹幕桥自动清理：%d 个临时配置目录" % removed)
        return {"killed": killed, "profiles": removed}

    def _prune_profiles(self, keep: str, procs: list[tuple[int, str]]) -> int:
        """删除 livedeck-edge-* 临时目录；只删没有被任何 msedge 命令行引用、且 30 分钟没动的。"""
        root_dir = str(Path(__file__).resolve().parent.parent)
        live = [cmd for _, cmd in procs]
        keep_name = os.path.basename(keep) if keep else ""
        now = time.time()
        seen: set[str] = set()
        removed = 0
        for root in (tempfile.gettempdir(), os.getcwd(), root_dir):
            try:
                names = os.listdir(root)
            except OSError:
                continue
            for name in names:
                if not name.startswith(STALE_MARK):
                    continue
                path = os.path.join(root, name)
                if path in seen or (keep and os.path.normcase(path) == os.path.normcase(keep)):
                    continue
                seen.add(path)
                if not os.path.isdir(path):
                    continue
                if (keep_name and keep_name == name) or any(name in cmd for cmd in live):
                    continue
                try:
                    if now - os.path.getmtime(path) < 1800:
                        continue
                except OSError:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                if not os.path.isdir(path):
                    removed += 1
        return removed

    def stop(self) -> None:
        self._stopping = True
        self._teardown()
        self.status = "stopped"

    def _teardown(self) -> None:
        with self._lock:
            ws, self._ws = self._ws, None
            proc, self._proc = self._proc, None
            profile, self._profile = self._profile, None
            browser_ws, self._browser_ws = self._browser_ws, ""
        # 优先用 CDP 的 Browser.close 关掉整个浏览器实例（启动器早退时 PID 已经没用了）
        if browser_ws:
            try:
                b = websocket.create_connection(browser_ws, timeout=5, suppress_origin=True)
                b.send(json.dumps({"id": 1, "method": "Browser.close"}))
                time.sleep(0.35)
                b.close()
            except Exception:  # noqa: BLE001
                pass
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if proc is not None and proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8,
                               creationflags=no_window_flags())
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        # Browser.close 常常关不干净（尤其 browser_ws 拿不到时），按 profile 路径兜底清杀
        if profile:
            _kill_pids(_edge_pids(profile), self.log)
            for _ in range(3):  # 浏览器退出需要一点时间才能解锁 profile 目录
                shutil.rmtree(profile, ignore_errors=True)
                if not os.path.isdir(profile):
                    break
                time.sleep(0.3)

    def ensure(self) -> bool:
        if self.status == "online" and self._ws is not None and self._reader and self._reader.is_alive():
            return True
        if self.status in ("online", "stale"):
            self.log("弹幕桥连接不稳定，重启中")
            self._ev("warn", "bridge", "弹幕桥状态异常，重建：status=%s" % self.status,
                     detail=self.last_recv_error or self.last_error)
            self.status = "restart"
            self._teardown()
        # 空舞台时 stop() 过的桥也要能重新拉起来：_stopping 不是持久状态
        self._stopping = False
        return self.start()

    def ping(self, timeout: float = 8.0) -> bool:
        """CDP 还活着吗。

        页面卡死时读线程仍在跑、status 仍是 online，但每条命令都超时；
        只能靠一次主动 ping 发现（返回 False 由调用方标记 stale 重建）。
        """
        if self.status != "online" or self._ws is None:
            return False
        try:
            self._cmd("Runtime.evaluate", {"expression": "1+1", "returnByValue": True},
                      timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---------- CDP 收发 ----------
    def _cmd(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        ws = self._ws
        if ws is None:
            raise BridgeError("CDP 未连接")
        ev = threading.Event()
        box: dict = {"event": ev}
        with self._send_lock:
            self._mid += 1
            mid = self._mid
            self._pending[mid] = box
            payload = json.dumps({"id": mid, "method": method, "params": params or {}})
            try:
                ws.send(payload)
            except Exception as e:  # noqa: BLE001
                self._pending.pop(mid, None)
                raise BridgeError("CDP 发送失败：%s" % e) from e
        if not ev.wait(timeout):
            self._pending.pop(mid, None)
            now = time.time()
            if now - self._timeout_logged.get(method, 0.0) > 60:
                self._timeout_logged[method] = now
                self._ev("warn", "cdp", "CDP 命令超时 %.0f 秒：%s" % (timeout, method),
                         detail="页面假死（渲染进程无响应）时会这样；连续超时后由 reconcile 重建桥")
            raise BridgeError("CDP 超时：%s" % method)
        if box.get("error"):
            raise BridgeError("CDP 错误 %s：%s" % (method, json.dumps(box["error"], ensure_ascii=False)[:200]))
        return box.get("result") or {}

    def _read_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        while True:
            if ws is not self._ws:
                return
            try:
                raw = ws.recv()
            except Exception as e:  # noqa: BLE001
                self.last_recv_error = "%s: %s" % (e.__class__.__name__, e)
                break
            if not raw:
                self.last_recv_error = "CDP 返回空帧（连接被关闭）"
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            mid = msg.get("id")
            if mid is not None:
                box = self._pending.pop(mid, None)
                if box is not None:
                    if "error" in msg:
                        box["error"] = msg["error"]
                    else:
                        box["result"] = msg.get("result") or {}
                    box["event"].set()
                continue
            if msg.get("method") == "Runtime.bindingCalled":
                p = msg.get("params") or {}
                if p.get("name") != BINDING:
                    continue
                try:
                    data = json.loads(p.get("payload") or "{}")
                except ValueError:
                    continue
                try:
                    self.on_event(data)
                except Exception:  # noqa: BLE001
                    pass
        if self.status == "online" and not self._stopping:
            self.status = "stale"
            self.log("CDP 读线程退出，弹幕桥待重启")
            self._ev("warn", "cdp", "CDP 读线程退出，弹幕桥标记 stale",
                     detail=self.last_recv_error or "对端关闭了 CDP 连接")

    # ---------- 房间同步 ----------
    def sync(self, rooms: list[dict]) -> dict:
        """rooms: [{room, real, uid, buvid, url}]；token 过期时重新 sync 即可。"""
        if not self.ensure():
            return {"ok": False, "error": self.last_error, "rooms": 0}
        self.rooms = {int(r["room"]): r for r in rooms}
        expr = "window.__ldSetRooms(%s);" % json.dumps(rooms, ensure_ascii=False)
        try:
            res = self._cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            val = (res.get("result") or {}).get("value")
            return {"ok": True, "rooms": int(val or 0)}
        except Exception as e:  # noqa: BLE001
            self.status = "error"
            self.last_error = str(e)
            self._ev("error", "sync", "同步房间到页面失败：%s" % e,
                     detail="房间数 %d" % len(rooms))
            return {"ok": False, "error": self.last_error, "rooms": 0}

    def debug_state(self) -> dict:
        if self.status != "online":
            return {}
        try:
            res = self._cmd("Runtime.evaluate", {
                "expression": "window.__ldStat ? window.__ldStat() : '{}'",
                "returnByValue": True,
            })
            raw = str((res.get("result") or {}).get("value") or "{}")
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}


def _make_profile_dir() -> str:
    """建一个全新的 Edge profile 目录。

    不能用 tempfile.mkdtemp：Python 在 Windows 上按 0o700 建目录，落成的 ACL
    只有 "OWNER RIGHTS"，没有当前用户那条 ACE。Edge 的受限令牌在启动阶段
    拿不到写权限，启动器会**静默退出（退出码 0）**，DevTools 端口永远不应答，
    表现就是「弹幕引擎离线」而日志里只有一句 DevTools 超时。
    os.makedirs 用默认权限建目录，ACL 里带当前用户，Edge 才能正常起。
    """
    root = str(_ensure_tempdir())
    for _ in range(50):
        path = os.path.join(root, "livedeck-edge-" + secrets.token_hex(4))
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            continue
    raise BridgeError("无法创建 Edge profile 目录：%s" % root)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
