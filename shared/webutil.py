"""共享的 HTTP 小工具：CSRF / 来源校验 / 请求体上限 / 查询参数解析。

管理台（web/console.py，8765，ThreadingHTTPServer）和直播台
（livedeck/server.py，8760，HTTPServer + SSE）原来各写各的守卫，规则一多就
容易一边严一边松（S1：任意网页都能 POST 打本机管理台）。这里只用标准库
（hmac / secrets / ipaddress），两边共用同一份判定逻辑。

一次「能不能改我的状态」由 validate_request 判定：
  1. TCP 对端必须是环回地址；Host 头必须是本机名（挡住 DNS rebinding 把
     evil.com 解析到 127.0.0.1）；
  2. Origin 若存在，必须精确等于本服务：解析后比较 host 与 port，绝不做
     子串判断 —— 否则 http://127.0.0.1.evil.com 也能混过去；
  3. 非只读方法必须带 X-CSRF-Token，且等于本进程启动时随机生成的 token
     （hmac.compare_digest 常量时间比较）。跨站页面想加这个头就得先过 CORS
     预检，而本服务不响应预检，所以拿不到也带不上；
  4. 带 body 的请求只收 application/json 与
     application/x-www-form-urlencoded，且大小先看 Content-Length 再决定
     读不读（超限直接 413，不会先把几 MB 读进内存）。

失败一律抛 HttpError，由各 Handler 渲染成统一的
{"ok": false, "error": "...", "code": "..."}，不静默放行。
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_MAX_BODY", "ALLOWED_CONTENT_TYPES", "LOOPBACK_HOSTS", "HttpError",
    "csrf_token", "new_token", "validate_request", "read_body", "read_json",
    "safe_int", "safe_uid", "MAX_UID",
    "inject_csrf", "bootstrap_js", "is_loopback_host", "is_loopback_ip",
]

# JSON 接口的请求体都很小；1 MiB 只是防「拿字节流把内存撑爆」的上限
DEFAULT_MAX_BODY = 1024 * 1024
ALLOWED_CONTENT_TYPES = frozenset({
    "application/json",
    "application/x-www-form-urlencoded",
})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# 非只读方法：必须带 CSRF 头（GET/HEAD/OPTIONS 只读，免 token）
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 进程启动时生成的一次性 token：重启即失效，跨站页面无从得知。
# 页面里的引导脚本由服务端注入（见 inject_csrf），值就是这里这枚。
_TOKEN = secrets.token_urlsafe(32)


def csrf_token() -> str:
    """当前进程的 CSRF token（给服务端渲染/校验用，不要下发给跨站来源）。"""
    return _TOKEN


def new_token() -> str:
    """新生成一枚 token（单测与轮换用；生产照常读 csrf_token）。"""
    return secrets.token_urlsafe(32)


class HttpError(Exception):
    """带 HTTP 状态码与错误码的统一异常，Handler 负责渲染成 JSON。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)

    @property
    def payload(self) -> dict:
        """两个 Handler 共用的错误结构，前端 api() 读 j.error 就能直接提示。"""
        return {"ok": False, "error": self.message, "code": self.code}

    @property
    def close_connection(self) -> bool:
        """413 时请求体还没读完：必须断连接，否则残余字节会被当成下一个请求。"""
        return self.status == 413


def is_loopback_ip(value: str) -> bool:
    """对端地址是不是环回（127.0.0.0/8、::1、v4-mapped 的 127.x）。"""
    text = str(value or "").strip().strip("[]").lower()
    if text.startswith("::ffff:"):
        text = text[7:]
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def is_loopback_host(value: str) -> bool:
    """主机名/字面量地址是不是「本机」：localhost、127.x、::1。"""
    text = str(value or "").strip().strip("[]").lower()
    if not text:
        return False
    return text in LOOPBACK_HOSTS or is_loopback_ip(text)


def _split_host(value: str) -> tuple[str, int | None]:
    """把 Host 头拆成 (host, port|None)：兼容 [::1]:8765 与 127.0.0.1:8765。"""
    text = str(value or "").strip().lower()
    if not text:
        return "", None
    if text.startswith("["):                       # [::1]:8765
        end = text.find("]")
        if end < 0:
            return text, None
        host, rest = text[1:end], text[end + 1:]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, None
    if text.count(":") == 1:                       # host:port
        host, _, port = text.partition(":")
        return host, (int(port) if port.isdigit() else None)
    return text, None                              # 裸 IPv6 或不带端口的主机名


def _server_port(handler) -> int | None:
    """本机实际监听的端口，拿不到就 None（只用于 Origin 端口比较）。"""
    addr = getattr(getattr(handler, "server", None), "server_address", None)
    try:
        return int(addr[1])
    except (TypeError, IndexError, ValueError):
        return None


def _origin_parts(value: str) -> tuple[str, str, int]:
    """Origin → (scheme, host, port)；'null' 或乱七八糟的值直接 403。"""
    text = str(value or "").strip()
    if not text or text.lower() == "null":
        raise HttpError(403, "bad_origin", "Origin 为空或 null，拒绝跨站请求")
    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        raise HttpError(403, "bad_origin", "Origin 格式不合法，拒绝跨站请求")
    try:
        port = parts.port
    except ValueError:
        raise HttpError(403, "bad_origin", "Origin 的端口不合法，拒绝跨站请求") from None
    return scheme, host, (port if port is not None else (443 if scheme == "https" else 80))


def _content_length(headers, max_bytes: int) -> int:
    """解析 Content-Length；非法 400，超限 413（注意：此时一个字节都没读）。"""
    raw = headers.get("Content-Length")
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        raise HttpError(400, "bad_content_length", "Content-Length 不是整数") from None
    if n < 0:
        raise HttpError(400, "bad_content_length", "Content-Length 不能为负数")
    if n > max_bytes:
        raise HttpError(413, "body_too_large",
                        "请求体太大：上限 %d 字节，声明了 %d 字节" % (max_bytes, n))
    return n


def _check_body_meta(method: str, headers, max_bytes: int) -> None:
    """请求体的大小与类型；分块传输直接拒绝（本服务只认 Content-Length）。"""
    te = str(headers.get("Transfer-Encoding") or "").strip().lower()
    if te and te != "identity":
        raise HttpError(400, "bad_encoding", "不支持分块传输：请显式给出 Content-Length")
    n = _content_length(headers, max_bytes)
    if not n or method in ("GET", "HEAD", "OPTIONS"):
        return
    ctype = str(headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype not in ALLOWED_CONTENT_TYPES:
        raise HttpError(415, "bad_content_type",
                        "Content-Type 只接受 application/json 或 "
                        "application/x-www-form-urlencoded")


def validate_request(handler, *, token: str | None = None,
                     max_body: int = DEFAULT_MAX_BODY,
                     require_token_for_mutation: bool = True,
                     require_loopback: bool = True) -> None:
    """统一的请求守卫（来源 + Origin + CSRF 头 + Content-Type / 体积）。

    handler: BaseHTTPRequestHandler 实例（用到 command / headers /
    client_address / server.server_address）。失败抛 HttpError。
    require_loopback=False 只用于直播台显式 --host 绑到局域网地址的场景：
    这时改按 Host 精确匹配 Origin，自证同源。
    """
    method = str(getattr(handler, "command", "GET") or "GET").upper()
    headers = handler.headers

    # 1) 对端地址：默认只允许本机
    addr = getattr(handler, "client_address", None) or ("",)
    ip = str(addr[0] or "")
    if require_loopback and not is_loopback_ip(ip):
        raise HttpError(403, "forbidden_client", "只允许本机访问")

    # 2) Host 头：必须是本机名/回环字面量（防 DNS rebinding）
    host, host_port = _split_host(headers.get("Host"))
    if not host:
        raise HttpError(400, "bad_host", "缺少 Host 头")
    if require_loopback and not is_loopback_host(host):
        raise HttpError(403, "bad_host", "Host 头不是环回地址，拒绝")
    port = host_port or _server_port(handler)

    # 3) Origin：浏览器发起的跨站写请求一定带；解析后精确比 host + port
    origin = headers.get("Origin")
    if origin:
        scheme, o_host, o_port = _origin_parts(origin)
        if scheme != "http":
            raise HttpError(403, "bad_origin", "Origin 的协议不是 http，拒绝跨站请求")
        if require_loopback:
            if not is_loopback_host(o_host):
                raise HttpError(403, "bad_origin", "跨站请求被拒绝：Origin 不是本机地址")
        elif o_host != host:
            raise HttpError(403, "bad_origin", "跨站请求被拒绝：Origin 与 Host 不一致")
        if port and o_port != int(port):
            raise HttpError(403, "bad_origin", "跨站请求被拒绝：Origin 的端口不是本服务端口")

    # 4) 浏览器给的免费提示：明确写了 cross-site 就别客气了
    site = str(headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site == "cross-site":
        raise HttpError(403, "cross_site", "跨站请求被拒绝（Sec-Fetch-Site: cross-site）")

    # 5) 改状态必须带自定义头；跨站页面加了头会触发预检，本服务不响应预检
    if require_token_for_mutation and method in MUTATING_METHODS:
        got = str(headers.get("X-CSRF-Token") or "")
        want = token if token else _TOKEN
        if not got or not hmac.compare_digest(got.encode("utf-8", "replace"),
                                              want.encode("utf-8", "replace")):
            raise HttpError(403, "csrf_failed",
                            "CSRF 校验失败：缺少或过期的 X-CSRF-Token，请刷新页面后重试")

    # 6) 请求体大小与类型（放最后：前面的判定更便宜）
    _check_body_meta(method, headers, max_body)


def read_body(handler, max_bytes: int = DEFAULT_MAX_BODY) -> bytes:
    """按 Content-Length 读请求体；超限在读取之前就 413，短读 400。"""
    n = _content_length(handler.headers, max_bytes)
    if n <= 0:
        return b""
    raw = handler.rfile.read(n)
    if len(raw) != n:
        raise HttpError(400, "body_truncated",
                        "请求体不完整：声明 %d 字节，实际收到 %d 字节" % (n, len(raw)))
    return raw


def read_json(handler, max_bytes: int = DEFAULT_MAX_BODY, *,
              allow_empty: bool = True) -> dict:
    """读 body 并解析成 JSON 对象；不合法一律 400（不再静默当成 {}）。"""
    raw = read_body(handler, max_bytes)
    if not raw:
        if allow_empty:
            return {}
        raise HttpError(400, "body_required", "这个接口需要 JSON 请求体")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HttpError(400, "bad_body",
                        "请求体不是 UTF-8（浏览器一般不会这样，注意别用 GBK 终端拼请求）") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise HttpError(400, "bad_json", "请求体不是合法 JSON")
    if not isinstance(data, dict):
        raise HttpError(400, "bad_json", "请求体必须是 JSON 对象")
    return data


def _parse_int_text(text: str) -> int | None:
    """只认 ASCII 十进制（含正负号）；全角数字、上下限交给调用方。"""
    body = text
    sign = 1
    if body[:1] in ("+", "-"):
        sign = -1 if body[0] == "-" else 1
        body = body[1:]
    if not body or len(body) > 18 or not body.isascii() or not body.isdigit():
        return None
    return sign * int(body)


def safe_int(value, default: int | None = None, lo: int | None = None,
             hi: int | None = None, *, name: str = "参数") -> int | None:
    """宽松解析 query / body 里的整数：非法值 400（结构化 JSON，不回显栈）。

    None / 空串 → default；再按 lo/hi 夹取（与原实现 min(limit, 500) 等价）。
    """
    if value is None:
        n = default
    elif isinstance(value, bool):
        n = int(value)
    elif isinstance(value, int):
        n = value
    else:
        text = str(value).strip()
        if not text:
            n = default
        else:
            n = _parse_int_text(text)
            if n is None:
                raise HttpError(400, "bad_param", "%s 不是合法整数" % name)
    if n is None:
        return None
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


# B 站 UID 上限：2026-11 起监控名单里就有 16 位 UID（如 3461582034045213）。
# 不要用「看起来够大」的 10**12 去夹 UID —— 16 位 UID 会被静默夹成 1e12，
# 之后按 UID 查库必然查不到，页面上所有操作都会报「没有这个 UID」（2026-11-09）。
MAX_UID = 2 ** 63 - 1


def safe_uid(value, default: int | None = None, *, name: str = "uid") -> int | None:
    """解析 UID（路径 / query / body）：只接受 [1, MAX_UID]，越界按非法值 400。

    与 safe_int 的区别是**不夹取**：UID 被夹到别的值不会报错，只会变成
    「没有这个 UID」，排查成本极高；宁可 400 说「不是合法 UID」。
    """
    n = safe_int(value, default, None, None, name=name)
    if n is None:
        return None
    if not 1 <= n <= MAX_UID:
        raise HttpError(400, "bad_param", "%s 不是合法 UID" % name)
    return n


# 页面引导脚本：注入到 <head> 之后、页面自己的脚本之前执行。
# 只做一件事 —— 把 X-CSRF-Token 自动加到同源的写请求上。
# 之所以要 patch fetch 而不是改页面：web/index.html 与 livedeck/static/ 下的
# 前端文件不在本次允许改动的清单里，服务端注入是唯一不改 UI 的加法。
_BOOTSTRAP_JS = r"""
(function () {
  "use strict";
  var T = "__TOKEN__";
  window.__CSRF_TOKEN__ = T;
  function isMut(m) { return m !== "GET" && m !== "HEAD" && m !== "OPTIONS"; }
  function sameOrigin(u) {
    try { return new URL(String(u), location.href).origin === location.origin; }
    catch (e) { return false; }
  }
  function looksJson(b) {
    if (typeof b !== "string") { return false; }
    var s = b.replace(/^\s+/, "");
    if (!s) { return false; }
    var c = s.charAt(0);
    return c === "{" || c === "[";
  }
  try {
    if (window.fetch && !window.__csrfFetchPatched) {
      window.__csrfFetchPatched = true;
      var origFetch = window.fetch;
      window.fetch = function (input, init) {
        try {
          var url = (typeof input === "string") ? input : ((input && input.url) || String(input));
          var method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
          if (isMut(method) && sameOrigin(url)) {
            var opt = {};
            for (var k in (init || {})) {
              if (Object.prototype.hasOwnProperty.call(init, k)) { opt[k] = init[k]; }
            }
            var h = new Headers(opt.headers || undefined);
            if (!h.has("X-CSRF-Token")) { h.set("X-CSRF-Token", T); }
            // 页面里有几处 POST 带了 JSON body 却没设 Content-Type，
            // fetch 会按 text/plain 发；这里补成 application/json，否则会被 415 挡掉。
            if (opt.body != null && !h.has("Content-Type") && looksJson(opt.body)) {
              h.set("Content-Type", "application/json");
            }
            opt.headers = h;
            return origFetch.call(this, input, opt);
          }
        } catch (e) { /* 补丁失败就按原样发，服务端会给出结构化错误 */ }
        return origFetch.apply(this, arguments);
      };
    }
  } catch (e) {}
  // 兜底：页面或第三方库（如 mpegts.js）若走 XMLHttpRequest 写请求，同样补头。
  // 注意必须在 send() 里设置，open() 之后 setRequestHeader 才合法。
  try {
    var XP = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
    if (XP && !XP.__csrfPatched) {
      XP.__csrfPatched = true;
      var xOpen = XP.open;
      var xSend = XP.send;
      XP.open = function (m, u) {
        this.__csrfMethod = String(m || "GET").toUpperCase();
        this.__csrfUrl = u;
        return xOpen.apply(this, arguments);
      };
      XP.send = function () {
        try {
          if (isMut(this.__csrfMethod) && sameOrigin(this.__csrfUrl)) {
            this.setRequestHeader("X-CSRF-Token", T);
          }
        } catch (e) {}
        return xSend.apply(this, arguments);
      };
    }
  } catch (e) {}
})();
"""


def bootstrap_js(token: str | None = None) -> str:
    """渲染引导脚本（token 用随机字母数字，直接替换占位符是安全的）。"""
    return _BOOTSTRAP_JS.replace("__TOKEN__", token or _TOKEN)


def inject_csrf(html: str, token: str | None = None) -> str:
    """把 token 与 fetch/XHR 补丁注入页面 HTML（服务端渲染，先于页面脚本执行）。

    幂等：已注入过（含 __csrfFetchPatched 标记）就不再插第二遍。
    """
    if not html or "__csrfFetchPatched" in html:
        return html
    tag = "<script data-csrf>" + bootstrap_js(token) + "</scr" + "ipt>"
    low = html.lower()
    idx = low.find("<head")
    if idx >= 0:
        end = low.find(">", idx)
        if end >= 0:
            return html[:end + 1] + tag + html[end + 1:]
    idx = low.find("<script")
    if idx >= 0:
        return html[:idx] + tag + html[idx:]
    return tag + html
