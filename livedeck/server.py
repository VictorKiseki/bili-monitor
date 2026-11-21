"""本地 HTTP 服务：静态页 + JSON API + SSE 弹幕流。"""
from __future__ import annotations

import json
import logging
import mimetypes
import queue
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from shared import webutil          # S1 / H8：环回 + Origin + CSRF + 请求体上限
from shared.redact import scrub_text  # S4：异常日志统一脱敏（H9）
from shared.version import read_version  # 关于弹窗的版本号（唯一来源 VERSION）
from livedeck.app import DeckError, LiveDeck
from livedeck.engine_log import EngineLog
from livedeck.stream_ws import StreamWS
from livedeck import config

log = logging.getLogger("livedeck.server")

# M9：静态根锚在源码目录（不随启动目录漂移），后面一律 resolve 后再判是否仍在根内
STATIC_ROOT = (Path(__file__).resolve().parent / "static").resolve()
STATIC_DIR = str(STATIC_ROOT)


class Handler(BaseHTTPRequestHandler):
    deck: LiveDeck = None  # type: ignore[assignment]
    server_version = "DDWorkshop/" + read_version()
    protocol_version = "HTTP/1.1"
    # S1：进程启动时生成的一次性 CSRF token；页面渲染时注入，写请求必须带回
    csrf_token: str = ""
    max_body: int = webutil.DEFAULT_MAX_BODY
    # 绑定环回时严格只认本机；绑 0.0.0.0 时退化为 Origin 与 Host 精确相等
    require_loopback: bool = True

    def log_message(self, fmt, *args):  # 静音默认逐请求日志
        pass

    # ---------- helpers ----------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200, extra: dict | None = None) -> None:
        self._send(code, _dumps(obj), "application/json; charset=utf-8", extra)

    def _err(self, message: str, code: int = 400) -> None:
        self._json({"ok": False, "error": message}, code)

    def _body(self) -> dict:
        # H8：长度先校验再看，超上限 413（不会把 2 MiB 全收下来再判断）；
        # 非法 JSON / 非 UTF-8 一律 400 结构化返回，不再静默当成 {}
        return webutil.read_json(self, self.max_body)

    def _guard(self) -> None:
        """统一守卫：对端环回 / Host / Origin / CSRF / Content-Type，失败抛 webutil.HttpError。"""
        webutil.validate_request(
            self,
            token=self.csrf_token,
            max_body=self.max_body,
            require_token_for_mutation=True,
            require_loopback=self.require_loopback,
        )

    def _fail(self, e: webutil.HttpError) -> None:
        """安全校验失败统一出口：{"ok": false, "error": ..., "code": ...}。"""
        extra = {"Connection": "close"} if e.close_connection else None
        if e.close_connection:
            self.close_connection = True
        self._json(e.payload, e.status, extra=extra)

    # ---------- routing ----------
    def do_GET(self):
        try:
            self._guard()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path
            if path == "/api/health":
                return self._json({"ok": True, "ts": time.time()})
            if path == "/api/version":
                # 关于弹窗读这里：版本号只从 VERSION 拿，前端不写死（AGENTS R5）
                return self._json({"ok": True, "version": read_version()})
            if path == "/api/update":
                # 「设置 → 更新检查」：远端只读 VERSION 比对，不下载、不升级
                from shared.update import check as check_update

                return self._json(check_update())
            if path == "/api/proxy":
                # 「设置 → 网络代理」：监控室与监控台共用 config/proxy.json，这里只读
                from shared import proxy as proxy_cfg

                return self._json(proxy_cfg.status())
            if path == "/api/diag":
                return self._json(self.deck.diag())
            if path == "/api/engine":
                # 引擎日志（离线 / 零帧 / 解码失败）：不用再去翻 stdout 被重定向到哪了
                n = _int((q.get("tail") or ["120"])[0], 120, 1, 600)
                return self._json({"ok": True, "engine": self.deck.engine.snapshot(n)})
            if path == "/api/resolve":
                # 手填输入自动识别：房间号 / 主播 UID / 直播间链接都往这里丢
                return self._json(self.deck.resolve_room((q.get("q") or [""])[0]))
            if path == "/api/snapshot":
                return self._json(self.deck.snapshot())
            if path == "/api/history":
                rooms = _ids(q.get("rooms", []))
                if not rooms:
                    rooms = self.deck._watched_rooms()
                limit = _int((q.get("limit") or ["120"])[0], 120, 1, 400)
                return self._json({"messages": self.deck.hub.history(rooms, limit)})
            if path == "/api/net":
                net = self.deck.stream_ws.stats() if self.deck.stream_ws is not None else {}
                return self._json(net or {"total_bps": 0.0, "rooms": {}, "window": 4.0})
            if path == "/api/emoticons":
                room = _int((q.get("room") or ["0"])[0], 0, 1, 10 ** 12)
                if not room:
                    return self._err("缺少 room")
                force = (q.get("force") or ["0"])[0] in ("1", "true")
                return self._json(self.deck.emoticons(room, force=force))
            if path == "/api/qualities":
                room = _int((q.get("room") or ["0"])[0], 0, 1, 10 ** 12)
                if not room:
                    return self._err("缺少 room")
                return self._json(self.deck.qualities(room))
            if path == "/api/flv":
                return self._flv(q)
            if path == "/api/stream":
                return self._stream(q)
            return self._static(path)
        except webutil.HttpError as e:
            return self._fail(e)
        except DeckError as e:
            return self._err(str(e), e.code)
        except BrokenPipeError:
            return
        except Exception:  # noqa: BLE001
            # H9（2026-11-14）：traceback.print_exc() 直出 stderr，既进不了日志文件，
            # 也绕过 shared/redact 的脱敏；统一走日志 + scrubbed 文本。
            log.error("未捕获异常 %s %s\n%s", self.command, self.path,
                      scrub_text(traceback.format_exc()))
            try:
                self._err("服务器内部错误", 500)
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self):
        try:
            self._guard()
            u = urlparse(self.path)
            path = u.path
            body = self._body()
            if path == "/api/active":
                rooms = body.get("rooms")
                if not isinstance(rooms, list):
                    return self._err("rooms 必须是数组")
                return self._json(self.deck.set_active(rooms))
            if path == "/api/listen":
                rooms = body.get("rooms")
                if not isinstance(rooms, list):
                    return self._err("rooms 必须是数组")
                return self._json(self.deck.set_listening(rooms))
            if path == "/api/grid":
                # 前端把排布编码成 列*10+行（如 3x3 → 33），0 表示 AUTO
                g = _int(str(body.get("grid") or 0), 0, 0, 44)
                self.deck.grid = g
                # 别直接写 state 文件：会把 hidden/temp/combos 一起冲掉
                self.deck._save_state()
                return self._json({"ok": True, "grid": g})
            if path == "/api/combos":
                return self._json(self.deck.set_combos(body.get("combos") or []))
            if path == "/api/reload":
                self.deck.load_rooms()
                return self._json({"ok": True, "rooms": len(self.deck.rooms)})
            if path == "/api/group":
                room = _int(str(body.get("room") or 0), 0, 1, 10 ** 12)
                if not room:
                    return self._err("缺少 room")
                return self._json(self.deck.set_group(room, str(body.get("grp") or "")))
            if path == "/api/groups/order":
                # 左栏分组顺序：只存分组名，房间不在里面；临时分组由前端固定在最后
                return self._json(self.deck.set_group_order(body.get("order") or []))
            if path == "/api/room/color":
                room = _int(str(body.get("room") or 0), 0, 1, 10 ** 12)
                if not room:
                    return self._err("缺少 room")
                return self._json(self.deck.set_room_color(
                    room, str(body.get("main") or ""), str(body.get("second") or ""),
                    bool(body.get("on", True)), bool(body.get("clear"))))
            if path == "/api/room/remove":
                room = _int(str(body.get("room") or 0), 0, 1, 10 ** 12)
                if not room:
                    return self._err("缺少 room")
                return self._json(self.deck.remove_room(room, str(body.get("mode") or "hide")))
            if path == "/api/room/add":
                room = _int(str(body.get("room") or 0), 0, 1, 10 ** 12)
                if not room:
                    return self._err("缺少 room")
                return self._json(self.deck.add_room_db(room))
            if path == "/api/room/unhide":
                return self._json(self.deck.unhide_rooms())
            if path == "/api/login/start":
                return self._json(self.deck.login_start())
            if path == "/api/login/poll":
                key = str(body.get("key") or "")
                if not key:
                    return self._err("缺少 key")
                return self._json(self.deck.login_poll(key))
            if path == "/api/login/paste":
                return self._json(self.deck.login_paste(
                    str(body.get("sessdata") or ""), str(body.get("bili_jct") or ""),
                    str(body.get("dedeuserid") or "")))
            if path == "/api/login/logout":
                return self._json(self.deck.logout())
            if path == "/api/send":
                room = _int(str(body.get("room") or 0), 0, 0, 10 ** 12)
                if not room:
                    return self._err("缺少 room")
                emo = str(body.get("emoticon") or "")
                if emo:
                    return self._json(self.deck.send_emoticon(room, emo))
                return self._json(self.deck.send_danmaku(room, str(body.get("text") or "")))
            if path == "/api/proxy":
                # 「设置 → 网络代理」：保存后立刻写本进程环境变量（新请求即生效）
                from shared import proxy as proxy_cfg

                try:
                    cfg = proxy_cfg.save(body or {})
                except (TypeError, ValueError) as exc:
                    return self._err(str(exc))
                st = proxy_cfg.apply_to_env(cfg)
                # 2026-11-20：保存后立刻换掉本进程的三个连接池（不重启监控室，
                # 页面和舞台都不动）；采集 / 推送由监控台那边的栈自动重启。
                try:
                    self.deck.apply_proxy(force=True)
                    st["note"] = "监控室立即换用新连接；采集 / 推送会在几秒内自动重启"
                except Exception as exc:        # noqa: BLE001
                    st["note"] = "已保存；监控室换连接失败（%s），重启监控室即可生效" % exc
                return self._json(st)
            if path == "/api/proxy/test":
                from shared import proxy as proxy_cfg

                saved = proxy_cfg.load()
                mode = str((body or {}).get("mode") or saved["mode"]).strip().lower()
                url = str((body or {}).get("url") or saved["url"]).strip()
                if mode == "custom":
                    return self._json(proxy_cfg.probe(url))
                if mode == "system":
                    return self._json(proxy_cfg.probe(use_system=True))
                return self._json({"ok": True, "passed": False, "status": 0, "ms": 0,
                                   "target": "", "error": "当前是「不使用代理」，不用测试"})
            return self._err("未知接口 %s" % path, 404)
        except webutil.HttpError as e:
            return self._fail(e)
        except DeckError as e:
            return self._err(str(e), e.code)
        except BrokenPipeError:
            return
        except Exception:  # noqa: BLE001
            # H9（2026-11-14）：traceback.print_exc() 直出 stderr，既进不了日志文件，
            # 也绕过 shared/redact 的脱敏；统一走日志 + scrubbed 文本。
            log.error("未捕获异常 %s %s\n%s", self.command, self.path,
                      scrub_text(traceback.format_exc()))
            try:
                self._err("服务器内部错误", 500)
            except Exception:  # noqa: BLE001
                pass

    # ---------- SSE ----------
    def _flv(self, q: dict) -> None:
        """把 B 站的 HTTP-FLV 流转成同源字节流，浏览器侧由 mpegts.js 解码。"""
        room = _int((q.get("room") or ["0"])[0], 0, 1, 10 ** 12)
        qn = _int((q.get("qn") or ["10000"])[0], 10000, 1, 100000)
        if not room:
            return self._err("缺少 room")
        resp = None
        for attempt in (0, 1):
            try:
                url = self.deck.flv_url(room, qn=qn, force=attempt == 1)
                resp = self.deck.stream_http.send(
                    self.deck.stream_http.build_request("GET", url), stream=True)
            except DeckError as e:
                if attempt:
                    return self._err(str(e), e.code)
                continue
            except Exception as e:  # noqa: BLE001
                if attempt:
                    return self._err("拉流失败：%s" % e, 502)
                continue
            if resp.status_code == 200:
                break
            resp.close()
            resp = None
        if resp is None:
            return self._err("直播流不可用", 502)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/x-flv")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in resp.iter_bytes(64 * 1024):
                # 房间下屏后别再往里写，否则没人看的 FLV 会一直占着上游连接
                if room not in self.deck.active:
                    break
                if chunk:
                    self.wfile.write(chunk)
                    if self.deck.stream_ws is not None:
                        self.deck.stream_ws.record(room, len(chunk))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def _stream(self, q: dict) -> None:
        rooms = _ids(q.get("rooms", []))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        sub = self.deck.hub.subscribe()
        try:
            hello = {"type": "snapshot", "ts": time.time(), "data": self.deck.snapshot()}
            self._event(hello)
            last_ping = time.time()
            while True:
                try:
                    ev = sub.get(timeout=5.0)
                except queue.Empty:
                    now = time.time()
                    if now - last_ping > 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_ping = now
                    continue
                if ev is None:
                    break
                last_ping = time.time()
                if ev.get("type") in ("msg", "status", "online", "room_live", "viewers", "watched") and ev.get("room") is not None:
                    # 没显式指定房间时，跟随当前舞台 + 只听弹幕；移出后立即停止进入弹幕流
                    allowed = set(rooms) if rooms else set(self.deck._watched_rooms())
                    if ev["room"] not in allowed:
                        continue
                self._event(ev)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            self.deck.hub.unsubscribe(sub)

    def _event(self, obj) -> None:
        self.wfile.write(b"data: " + _dumps(obj) + b"\n\n")
        self.wfile.flush()

    # ---------- 静态文件 ----------
    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        rel = unquote(path.lstrip("/"))
        if rel.startswith("static/"):
            rel = rel[7:]
        # M9：先 resolve 再判包含关系。字符串 startswith 会被同级 static-evil/
        # 或 ..%2f 之类的编码绕过，is_relative_to 按路径分段比较不受影响
        try:
            full = (STATIC_ROOT / rel).resolve()
        except (OSError, ValueError):
            return self._err("not found", 404)
        if not full.is_relative_to(STATIC_ROOT) or not full.is_file():
            return self._err("not found", 404)
        ctype = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        body = full.read_bytes()
        extra = {}
        if ctype.startswith("text/html"):
            # S1：HTML 出网前注入 CSRF 引导脚本；页面文件本身不改，
            # 补丁挂在 window.fetch / XHR 上，给同源写请求自动补 X-CSRF-Token
            body = webutil.inject_csrf(body.decode("utf-8", "replace"), self.csrf_token).encode("utf-8")
        self._send(200, body, ctype, extra)


def _dumps(obj) -> bytes:
    """JSON 编码：B 站昵称里偶尔有非法代理字符，用 replace 兜住，避免整条响应 500。"""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8", "replace")


def _ids(values) -> list[int]:
    out = []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
    return out


def _int(value: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def serve(host: str = "127.0.0.1", port: int = 8760, open_browser: bool = True) -> None:
    log = lambda m: print("[deck]", m, flush=True)
    # S1（2026-11-14）：只允许绑环回。以前 --host 可以绑局域网，靠「Origin 必须等于 Host」
    # 挡跨站，但两个值都取自请求头，DNS rebinding 下它们天然相等 —— 等于自证同源。
    # docs/12 §、docs/23、AGENTS §4 本来就禁止对外暴露，这里直接拒绝启动，别留一条
    # 「只有读文档才知道不能开」的口子。
    if not webutil.is_loopback_host(host):
        raise SystemExit("[deck] 拒绝启动：--host %s 不是环回地址。监控室只服务本机，"
                         "要远程看请用 SSH 隧道转发 127.0.0.1:%d。" % (host, port))
    engine = EngineLog(echo=log)
    deck = LiveDeck(log=log, engine=engine)
    Handler.deck = deck
    # S1：token 每进程一份；require_loopback 跟绑定地址走，绑 0.0.0.0 时
    # 改用「Origin 必须与 Host 完全一致」来挡跨站，别把局域网用法搞坏
    Handler.csrf_token = webutil.csrf_token()
    Handler.require_loopback = webutil.is_loopback_host(host)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    deck.start()
    stream = StreamWS(deck, log=lambda m: print("[deck]", m, flush=True))
    try:
        deck.stream_port = stream.start(host)
        deck.stream_ws = stream
        print("直播流通道（WebSocket-FLV）：ws://%s:%d/flv" % (host, deck.stream_port), flush=True)
    except Exception as e:  # noqa: BLE001
        stream = None
        print("[deck] 直播流 WebSocket 通道启动失败，回退 HTTP 代理：%s" % e, flush=True)
    url = "http://%s:%d/" % (host, port)
    print("DD全能工作间·监控室 已启动：%s" % url, flush=True)
    print("[deck] 引擎日志：%s（也可访问 %sapi/engine）" % (engine.path, url), flush=True)
    print("按 Ctrl+C 停止", flush=True)
    if open_browser:
        try:
            import webbrowser
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在退出…", flush=True)
    finally:
        if stream is not None:
            stream.stop()
        deck.stop()
        httpd.shutdown()
        httpd.server_close()


def main(argv: list[str]) -> int:
    host, port, open_browser = "127.0.0.1", 8760, True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2; continue
        if a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]; i += 2; continue
        if a in ("--no-open", "-n"):
            open_browser = False; i += 1; continue
        if a in ("-h", "--help"):
            print("用法：python run_livedeck.py [--port 8760] [--host 127.0.0.1] [--no-open]")
            print("      --host 只能是环回地址（127.0.0.1 / localhost / ::1），不对局域网开放")
            return 0
        i += 1
    serve(host, port, open_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
