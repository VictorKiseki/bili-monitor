"""HTTP Handler / make_server / serve_in_thread（由 web/console.py 拆出）。"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent

import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import webutil  # noqa: E402
from shared.redact import scrub_text  # noqa: E402 - 异常日志统一脱敏（S4/H9）
from store.sqlite import SCHEMA, migrate  # noqa: E402
from web.cleanup import CleanupError, CleanupManager  # noqa: E402

from web.console_core import ApiError  # noqa: E402

log = logging.getLogger("console.http")
# 这个模块的日志以前没配 handler：log.error 靠 logging 的 lastResort 打到 stderr，
# log.info 直接丢掉。写操作审计（C1）要看得到，就在这里兜一个 stdout handler ——
# 监控台的 stdout 由守护进程重定向进 out/console.log，所以审计行也落在那份文件里。
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

_UNHANDLED = object()


def _weekly_write(h, method: str, path: str, c):
    """周表写接口的唯一实现（C3，2026-11-14）。

    同一套 programs / manual / feature / ai 的写法以前在 _post_weekly、
    _handle_patch、_handle_delete 里各抄了一份，三份里还夹着按「别的 method」
    判断的分支（_dispatch 严格按 method 分派，那些分支永远不会命中）。
    现在收成一处：路由和 method 都不匹配就返回 _UNHANDLED，让调用方接着试
    下一个接口。测试里直接跨 method 调用这些 handler 的语义保持不变。

    刻意写成模块级函数而不是 Handler 方法：测试里的 FakeHandler 只镜像
    Handler._arg_int/_body/_send 这几个口子，用不着一层层再抄一份私有 helper。
    """
    arg_int = h._arg_int
    m = re.fullmatch(r"/api/weekly/programs/(\d+)", path)
    if m:
        pid = arg_int(m.group(1), 0, 1, 10**12, name="program_id")
        if method == "DELETE":
            return h._send(c.weekly_program_delete(pid))
        if method in ("POST", "PATCH"):
            body = h._body()
            body["program_id"] = pid
            return h._send(c.weekly_program_save(body))
        return _UNHANDLED
    m = re.fullmatch(r"/api/weekly/manual/(\d+)", path)
    if m:
        mid = arg_int(m.group(1), 0, 1, 10**12, name="manual_id")
        if method == "DELETE":
            return h._send(c.weekly_manual_delete(mid))
        if method in ("POST", "PATCH"):
            return h._send(c.weekly_manual_update(mid, h._body()))
        return _UNHANDLED
    if path == "/api/weekly/feature" and method in ("POST", "PATCH"):
        return h._send(c.weekly_feature_update(h._body()))
    if path == "/api/weekly/ai" and method in ("POST", "PATCH"):
        return h._send(c.weekly_ai_update(h._body()))
    return _UNHANDLED


class Handler(BaseHTTPRequestHandler):
    server_version = "BiliConsole/1.0"
    console: Console = None          # 由 serve() 注入
    # 前端改动不用重启后端：index.html 按 mtime 缓存，改完刷新浏览器即可
    _index_cache: dict = {"mtime": None, "html": ""}
    csrf_token = ""                  # 由 make_server() 注入（进程内一次性）
    max_body = webutil.DEFAULT_MAX_BODY
    _last_code = 0                   # 最近一次响应的状态码（写操作审计用，见 _audit）
    def log_message(self, fmt, *args):     # 静音，避免刷屏
        pass
    def _send(self, obj, code: int = 200, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._last_code = code
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _index_page(self, index: Path | None = None) -> str:
        """按 mtime 缓存的页面：改了 index.html 刷新浏览器就能看到，不必重启进程。"""
        index = index or (Path(__file__).resolve().parent / "index.html")
        mtime = index.stat().st_mtime
        cache = self._index_cache
        if cache.get("mtime") != mtime:
            cache["html"] = webutil.inject_csrf(index.read_text("utf-8"), self.csrf_token)
            cache["mtime"] = mtime
        return cache["html"]

    def _body(self) -> dict:
        """读 JSON body：上限 / 编码 / JSON 合法性统一交给 shared.webutil（H8）。"""
        return webutil.read_json(self, self.max_body)
    def _guard(self) -> None:
        """S1：来源 + Origin + CSRF 头 + Content-Type 全部走 shared/webutil。

        旧版只看 TCP 来源和 Host，任意网页一个 form/fetch 就能改名单、重启服务。
        """
        webutil.validate_request(self, token=self.csrf_token, max_body=self.max_body)
    def _fail(self, e: webutil.HttpError) -> None:
        """统一的失败出口：{"ok": false, "error": ..., "code": ...}。

        413 时 body 还没读完，必须断连接，否则残余字节会被当成下一个请求解析。
        """
        extra = {"Connection": "close"} if e.close_connection else None
        if e.close_connection:
            self.close_connection = True
        try:
            self._send(e.payload, e.status, extra=extra)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def _arg_int(self, value, default=None, lo=None, hi=None, name: str = "参数"):
        """query / 路径里的整数统一收口：非法值 400 结构化 JSON，不再 500（H8）。"""
        return webutil.safe_int(value, default, lo, hi, name=name)
    def _uid(self, value, name: str = "uid"):
        """UID 单独收口：不能再按 10**12 夹取。

        16 位 UID（如 3461582034045213）被夹成 1e12 后查库必然空，页面会把
        所有操作都显示成「没有这个 UID」。safe_uid 只校验范围、不改写原值。
        """
        return webutil.safe_uid(value, name=name)

    def _audit(self, method: str, path: str, t0: float) -> None:
        """C1（2026-11-14）：写操作成功后留一行日志。

        以前 POST/PATCH/DELETE 没有任何记录：误点了「停用推送目标」「暂停任务」
        或「全停」，事后翻日志查不到谁在什么时候动过什么。这里只记
        method + 路径 + 状态码 + 耗时，**不记 body** —— body 里有 SESSDATA、
        openid、代理密码，落日志违反 AGENTS §4。
        """
        if method == "GET" or self._last_code >= 400:
            return
        log.info("[写操作] %s %s → %s（%d ms）", method, path, self._last_code,
                 int((time.time() - t0) * 1000))

    def _dispatch(self, method: str) -> None:
        t0 = time.time()
        path = ""
        try:
            self._guard()
            url = urlparse(self.path)
            path = url.path
            q = parse_qs(url.query)
            c = self.console
            if method != "GET" and getattr(c, "_online_update_trial", False):
                raise ApiError("正在检查新版本，暂时无法写入", 503)
            if method == "GET":
                self._handle_get(method, path, q, c)
            elif method == "POST":
                self._handle_post(method, path, q, c)
            elif method == "PATCH":
                self._handle_patch(method, path, q, c)
            elif method == "DELETE":
                self._handle_delete(method, path, q, c)
            else:
                raise ApiError("没有这个接口：%s %s" % (method, path), 404)
            self._audit(method, path, t0)
        except CleanupError as e:
            self._send({"ok": False, "error": e.message, "code": "cleanup_error"}, e.code)
        except ApiError as e:
            self._send({"ok": False, "error": e.message, "code": "api_error"}, e.code)
        except webutil.HttpError as e:
            self._fail(e)                    # 守卫 / body / safe_int 的结构化失败
        except Exception:                                   # noqa: BLE001
            # H8：内部异常只进日志，不再把异常类型和原文回给页面。
            # H9（2026-11-14）：以前是 traceback.print_exc() 直出 stderr —— 不走日志文件、
            # 也不过脱敏。现改成统一日志 + scrubbed 后的 traceback 文本（URL 里的 token 一并遮住）。
            log.error("未捕获异常 %s %s\n%s", self.command, self.path,
                      scrub_text(traceback.format_exc()))
            self._send({"ok": False, "error": "服务器内部错误，详情见 logs/", "code": "internal"}, 500)

    def _handle_get(self, method: str, path: str, q: dict, c) -> None:
        arg_int = self._arg_int
        if method == "GET" and path in ("/", "/index.html"):
            return self._send(self._index_page().encode("utf-8"), 200, "text/html; charset=utf-8")

        if method == "GET" and path == "/api/csrf":
            # 只读备用通道：页面已由服务端注入 token，这里给脚本/调试用。
            # 跨站页面读不到响应体（没有 CORS 头），拿到了也带不进写请求。
            return self._send({"ok": True, "token": self.csrf_token})

        if method == "GET" and path == "/api/version":
            # 关于弹窗读这里：版本号只从 VERSION 拿，前端不写死（AGENTS R5）
            return self._send(c.version())

        if method == "GET" and path == "/api/update":
            return self._send(c.update())

        if method == "GET" and path == "/api/update/status":
            return self._send(c.online_update_status())

        if method == "GET" and path == "/api/proxy":
            # 「设置 → 网络代理」：当前配置 + 本进程环境变量是否已生效
            return self._send(c.proxy())

        if method == "GET" and path == "/api/overview":
            return self._send(c.overview())

        if method == "GET" and path == "/api/stats":
            return self._send(c.stats())

        if method == "GET" and path == "/api/weekly/feature":
            return self._send(c.weekly_feature_status())

        if method == "GET" and path == "/api/weekly":
            return self._send(c.weekly(
                week=(q.get("week") or [""])[0],
                limit=arg_int((q.get("limit") or [None])[0], 24, 1, 500, name="limit")))

        if method == "GET" and path == "/api/weekly/image":
            body, ctype = c.weekly_image((q.get("event") or [""])[0])
            return self._send(body, 200, ctype)

        if method == "GET" and path == "/api/weekly/programs":
            return self._send(c.weekly_programs())

        if method == "GET" and path == "/api/weekly/agent":
            return self._send(c.weekly_agent_status())
        if method == "GET" and path == "/api/weekly/ai":
            return self._send(c.weekly_ai_status())

        if method == "GET" and path == "/api/subjects":
            return self._send({"ok": True, "subjects": c.subjects(), "groups": c.groups()})

        if method == "GET" and path == "/api/search":
            kw = (q.get("q") or [""])[0].strip()
            if not kw:
                raise ApiError("缺少 q")
            return self._send({"ok": True, "results": c.search(kw)})

        if method == "GET" and path == "/api/events":
            limit = arg_int((q.get("limit") or [None])[0], 50, 1, 500, name="limit")
            uid = self._uid((q.get("uid") or [None])[0]) or None
            # H10：total 是库里的总数（翻页/上限之外也准），前端用它写「共 N 条」
            return self._send({"ok": True, "events": c.events(limit=limit, uid=uid),
                               "total": c.events_total(uid=uid)})

        if method == "GET" and path == "/api/deliveries":
            status = (q.get("status") or [""])[0]
            limit = arg_int((q.get("limit") or [None])[0], 50, 1, 500, name="limit")
            return self._send({"ok": True,
                               "deliveries": c.deliveries(status=status, limit=limit)})

        if method == "GET" and path == "/api/tasks":
            return self._send(c.tasks())
        if method == "GET" and path == "/api/global":
            return self._send(c.global_state())

        if method == "GET" and path == "/api/feed/status":
            return self._send(c.feed_status())

        if method == "GET" and path == "/api/stack":
            return self._send(c.stack_status())

        if method == "GET" and path == "/api/qq/status":
            return self._send(c.qq_status())

        if method == "GET" and path == "/api/qq/listen":
            return self._send(c.qq_listen_status())

        if method == "GET" and path == "/api/bili/auth":
            force = (q.get("force") or [""])[0] in ("1", "true", "yes")
            return self._send(c.bili_auth_status(force=force))

        if method == "GET" and path == "/api/bili/qr/poll":
            return self._send(c.bili_qr_poll((q.get("key") or [""])[0]))

        if method == "GET" and path == "/api/targets":
            return self._send({"ok": True, "targets": c.targets()})

        if method == "GET" and path == "/api/logs":
            def pick(name, default=""):
                return (q.get(name) or [default])[0]
            return self._send(c.logs(
                lines=arg_int(pick("lines"), 200, 1, 2000, name="lines"),
                date=pick("date"), tag=pick("tag"),
                level=pick("level"), q=pick("q")))

        if method == "GET" and path == "/api/cleanup/status":
            return self._send(c.cleanup.status())

        if method == "GET" and path == "/api/cleanup/config":
            return self._send({"ok": True, "config": c.cleanup.public_config()})

        if method == "GET" and path == "/api/cleanup/logs":
            def pick2(name, default=""):
                return (q.get(name) or [default])[0]
            return self._send(c.cleanup.logs(
                lines=arg_int(pick2("lines"), 300, 1, 2000, name="lines"),
                level=pick2("level"), q=pick2("q")))

        raise ApiError("没有这个接口：%s %s" % (method, path), 404)

    def _handle_post(self, method: str, path: str, q: dict, c) -> None:
        for part in (self._post_weekly, self._post_subjects, self._post_qq,
                     self._post_proxy, self._post_admin):
            result = part(method, path, q, c)
            if result is not _UNHANDLED:
                return result
        raise ApiError("没有这个接口：%s %s" % (method, path), 404)

    def _post_weekly(self, method: str, path: str, q: dict, c):
        arg_int = self._arg_int
        result = _weekly_write(self, method, path, c)
        if result is not _UNHANDLED:
            return result
        if method == "POST" and path == "/api/weekly/candidates/verdict":
            return self._send(c.weekly_candidate_verdict(self._body()))
        m = re.fullmatch(r"/api/weekly/candidates/([^/]+)/skip", path)
        if m and method == "POST":
            return self._send(c.weekly_skip(unquote(m.group(1))))
        if method == "POST" and path == "/api/weekly/cache/clean":
            b = self._body()
            return self._send(c.weekly_cache_clean(bool(b.get("force"))))
        if method == "POST" and path == "/api/weekly/manual":
            return self._send(c.weekly_manual_save(self._body()))
        if method == "POST" and path == "/api/weekly/programs":
            return self._send(c.weekly_program_save(self._body()))
        if method == "POST" and path == "/api/weekly/ai/run":
            return self._send(c.weekly_ai_request())
        if method == "POST" and path == "/api/weekly/agent/export":
            b = self._body()
            limit = arg_int(b.get("limit"), 0, 0, 60, name="limit") or None
            eids = b.get("event_ids")
            if isinstance(eids, str):
                eids = [x for x in eids.split(",") if x.strip()]
            return self._send(c.weekly_agent_export(limit=limit, event_ids=eids))
        if method == "POST" and path == "/api/weekly/agent/import":
            return self._send(c.weekly_agent_import(self._body()))
        return _UNHANDLED

    def _post_subjects(self, method: str, path: str, q: dict, c):
        arg_int = self._arg_int
        if method == "POST" and path == "/api/subjects":
            b = self._body()
            uid = self._uid(b.get("uid"))
            return self._send(c.add_subject(uid=uid, keyword=(b.get("keyword") or "").strip() or None,
                                            group=str(b.get("group") or "")))
        if method == "POST" and path == "/api/subjects/flags":
            b = self._body()
            return self._send(c.set_subject_flags(
                self._uid(b.get("uid")),
                live=b.get("live"), dynamic=b.get("dynamic"), weekly=b.get("weekly")))
        if method == "POST" and path == "/api/subjects/batch":
            b = self._body()
            return self._send(c.batch_subjects(b.get("uids"), str(b.get("action") or ""),
                                               str(b.get("group") or "")))
        if method == "POST" and path == "/api/groups/create":
            b = self._body()
            return self._send(c.create_group(str(b.get("name") or ""),
                                             str(b.get("color") or "")))
        if method == "POST" and path == "/api/groups/order":
            b = self._body()
            return self._send(c.set_group_order(b.get("names")))
        if method == "POST" and path == "/api/groups/color":
            b = self._body()
            return self._send(c.set_group_color(str(b.get("name") or ""),
                                                str(b.get("color") or "")))
        if method == "POST" and path == "/api/groups/rename":
            b = self._body()
            return self._send(c.rename_group(str(b.get("from") or ""), str(b.get("to") or "")))
        if method == "POST" and path == "/api/groups/delete":
            b = self._body()
            return self._send(c.delete_group(str(b.get("name") or "")))
        return _UNHANDLED

    def _post_qq(self, method: str, path: str, q: dict, c):
        if method == "POST" and path == "/api/qq/credentials":
            return self._send(c.qq_save_credentials(self._body()))
        if method == "POST" and path == "/api/qq/verify":
            return self._send(c.qq_verify(self._body()))
        if method == "POST" and path == "/api/qq/listen":
            return self._send(c.qq_listen_start(self._body()))
        if method == "POST" and path == "/api/qq/listen/stop":
            return self._send(c.qq_listen_stop())
        if method == "POST" and path == "/api/qq/test":
            return self._send(c.qq_test_send(self._body()))
        if method == "POST" and path == "/api/qq/ids":
            return self._send(c.qq_save_ids(self._body()))
        return _UNHANDLED

    def _post_proxy(self, method: str, path: str, q: dict, c):
        if method == "POST" and path == "/api/proxy":
            return self._send(c.save_proxy(self._body()))
        if method == "POST" and path == "/api/proxy/test":
            return self._send(c.test_proxy(self._body()))
        return _UNHANDLED

    def _post_admin(self, method: str, path: str, q: dict, c):
        arg_int = self._arg_int
        m = re.fullmatch(r"/api/deliveries/(\d+)/retry", path)
        if m and method == "POST":
            return self._send(c.retry_delivery(
                arg_int(m.group(1), 0, 1, 10**12, name="id")))
        m = re.fullmatch(r"/api/deliveries/(\d+)/dismiss", path)
        if m and method == "POST":
            return self._send(c.dismiss_delivery(arg_int(m.group(1), 0, 1, 10**12, name="id")))
        if method == "POST" and path == "/api/deliveries/flush":
            return self._send(c.flush_pending())
        if method == "POST" and path == "/api/update/apply":
            return self._send(c.queue_online_update(str(self._body().get("target") or "")), 202)
        if method == "POST" and path == "/api/bili/auth":
            b = self._body()
            return self._send(c.set_bili_auth(str(b.get("sessdata") or "")))
        if method == "POST" and path == "/api/bili/qr/new":
            return self._send(c.bili_qr_new())
        if method == "POST" and path == "/api/stack/action":
            b = self._body()
            return self._send(c.stack_action(b.get("role"), b.get("action")))
        if method == "POST" and path == "/api/feed/action":
            b = self._body()
            return self._send(c.feed_action(str(b.get("action") or "")))
        if method == "POST" and path == "/api/feed/mode":
            b = self._body()
            return self._send(c.feed_set_mode(str(b.get("mode") or "")))
        if method == "POST" and path == "/api/tasks/toggle":
            b = self._body()
            return self._send(c.set_task(str(b.get("key") or ""), bool(b.get("off"))))
        if method == "POST" and path == "/api/tasks/process":
            b = self._body()
            return self._send(c.task_process(str(b.get("key") or ""), str(b.get("action") or "")))
        if method == "POST" and path == "/api/tasks/main":
            b = self._body()
            return self._send(c.main_process(str(b.get("action") or ""), bool(b.get("confirm"))))
        if method == "POST" and path == "/api/tasks/autostart":
            b = self._body()
            if not isinstance(b.get("on"), bool):
                raise ApiError("on 必须是布尔值")
            return self._send(c.set_process_autostart(str(b.get("key") or ""), b["on"]))
        if method == "POST" and path == "/api/service/restart":
            return self._send(c.restart_service())
        if method == "POST" and path == "/api/cleanup/enable":
            b = self._body()
            return self._send(c.cleanup.set_enabled(bool(b.get("enabled"))))
        if method == "POST" and path == "/api/cleanup/start":
            return self._send(c.cleanup.start())
        if method == "POST" and path == "/api/cleanup/stop":
            return self._send(c.cleanup.stop())
        if method == "POST" and path == "/api/cleanup/restart":
            return self._send(c.cleanup.restart())
        if method == "POST" and path == "/api/cleanup/scan":
            return self._send(c.cleanup.scan())
        if method == "POST" and path == "/api/cleanup/run":
            return self._send(c.cleanup.run_once())
        if method == "POST" and path == "/api/cleanup/autostart":
            b = self._body()
            return self._send(c.cleanup.set_autostart(bool(b.get("enabled"))))
        return _UNHANDLED

    def _handle_patch(self, method: str, path: str, q: dict, c) -> None:
        arg_int = self._arg_int
        result = _weekly_write(self, method, path, c)
        if result is not _UNHANDLED:
            return result

        if method == "PATCH" and path == "/api/global":
            b = self._body()
            return self._send(c.set_global_mode(str(b.get("mode") or "").strip()))

        m = re.fullmatch(r"/api/targets/(\d+)", path)
        if m and method == "PATCH":
            b = self._body()
            return self._send(c.set_target_enabled(
                arg_int(m.group(1), 0, 1, 10**12, name="target_id"), bool(b.get("enabled"))))

        if method == "PATCH" and path == "/api/cleanup/config":
            return self._send(c.cleanup.update_config(self._body()))

        raise ApiError("没有这个接口：%s %s" % (method, path), 404)

    def _handle_delete(self, method: str, path: str, q: dict, c) -> None:
        result = _weekly_write(self, method, path, c)
        if result is not _UNHANDLED:
            return result

        m = re.fullmatch(r"/api/weekly/sessions/([^/]+)", path)
        if m and method == "DELETE":
            return self._send(c.weekly_session_delete(unquote(m.group(1))))

        m = re.fullmatch(r"/api/subjects/(\d+)", path)
        if m and method == "DELETE":
            return self._send(c.remove_subject(self._uid(m.group(1))))

        if method == "DELETE" and path == "/api/bili/auth":
            return self._send(c.clear_bili_auth())

        if method == "DELETE" and path == "/api/qq/ids":
            return self._send(c.qq_clear_ids())

        raise ApiError("没有这个接口：%s %s" % (method, path), 404)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


def make_server(console: Console, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    Handler.console = console
    Handler.csrf_token = webutil.csrf_token()
    # S1：token 与 fetch 补丁在首次请求时渲染进页面（先于页面自己的脚本执行），
    # 前端一行都不用改，也不存在「先异步取 token 才敢发写请求」的窗口期
    Handler._index_cache["mtime"] = None
    Handler._index_cache["html"] = ""
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    # 进程页「主进程」那行显示真实端口，「停止」「重启」要靠它判断独立运行时的动作
    console._web_server = srv
    console._web_port = int(srv.server_address[1])
    return srv


def serve_in_thread(console: Console, host: str = "127.0.0.1", port: int = 8765):
    srv = make_server(console, host, port)
    t = threading.Thread(target=srv.serve_forever, name="console", daemon=True)
    t.start()
    return srv, t
