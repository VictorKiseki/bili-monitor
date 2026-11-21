# -*- coding: utf-8 -*-
"""S4 凭据脱敏（唯一实现，2026-11-14 从 collector/bili.py 抽出）。

原来这套规则只写在 collector/bili.py 里，而 web/ 与 livedeck/ 的异常日志走
traceback.print_exc() 直出 stderr，等于完全绕过它 —— 规则存在、却没有任何调用点。
抽到 shared/ 之后，采集日志（collector.bili.scrub_text 是这里的别名）和
「未捕获异常」日志用的是同一份规则，不再各写一套。

约定：
  · 敏感键按「前 4 后 4」留痕（短值全遮），Cookie/Authorization 整条抹掉；
  · 纯函数，不依赖客户端实例，测试可直接断言。
"""
from __future__ import annotations

import re
from typing import Any

# 只 mask SESSDATA 不够：BILI_JCT 是可写 CSRF token，bili_ticket / access_key
# 也能冒用登录态；Cookie / Authorization 整条进日志等于把凭据明文抄进文件。
# 这里维护一张「敏感键」表，dict 值、查询串、header、异常文本都走同一套规则。
SENSITIVE_KEY_TOKENS = (
    "sessdata", "bili_jct", "bili_ticket", "csrf", "access_key",
    "refresh_token", "app_secret", "password", "passwd", "authorization",
    "cookie", "token", "secret",
)
# 这几类不做「前 4 后 4」，整条抹掉（Cookie 里往往还套着多个 key=value）
FULL_REDACT_KEY_TOKENS = ("cookie", "authorization", "proxy_authorization")
FULL_REDACT = "****（整条已脱敏）"


def _norm_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def is_sensitive_key(key: Any) -> bool:
    k = _norm_key(key)
    return any(tok in k for tok in SENSITIVE_KEY_TOKENS)


def is_full_redact_key(key: Any) -> bool:
    k = _norm_key(key)
    return any(tok in k for tok in FULL_REDACT_KEY_TOKENS)


def mask_secret(value: Any, *, full: bool = False) -> str:
    """把一个敏感值压成可进日志的摘要；空值返回空串。"""
    s = str(value)
    if not s:
        return ""
    if full:
        return FULL_REDACT
    # 短值直接全遮；长值留前 4 后 4 方便人工比对（都是随机串，前 4 位不足以还原）
    if len(s) <= 10:
        return "****"
    return "%s****%s（%d 位）" % (s[:4], s[-4:], len(s))


# 表里的下划线在现实 header 里可能写成连字符（BILI-JCT / access-key），一并兼容
_KEY_ALT = "|".join(re.escape(t).replace("_", "[_-]?") for t in SENSITIVE_KEY_TOKENS)
# 覆盖 key=value、key: value、"key": "value"；key 允许前后缀（如 bili_jct / BILI_JCT_NEW）
_KV_RE = re.compile(
    r"""(?i)(?P<key>(?P<qk>["']?)\b[A-Za-z0-9_.\-]*(?:"""
    + _KEY_ALT
    + r""")[A-Za-z0-9_.\-]*(?P=qk))(?P<sep>\s*[=:]\s*)"""
    r"""(?P<val>"[^"]*"|'[^']*'|[^\s&;,"'}<>]+)"""
)
# 整条 Cookie / Authorization 头：冒号后全部抹掉（可能还套着多个 key=value）
_HEADER_RE = re.compile(
    r"""(?im)^(?P<head>[ \t]*(?:cookie|set-cookie|authorization|proxy-authorization)"""
    r"""[ \t]*:[ \t]*).*$"""
)


def _mask_kv(m: re.Match) -> str:
    key, sep, val = m.group("key"), m.group("sep"), m.group("val")
    quote = val[0] if val[:1] in ("\"", "'") else ""
    if quote:
        val = val[1:-1]
    return "%s%s%s%s%s" % (key, sep, quote,
                           mask_secret(val, full=is_full_redact_key(key)), quote)


def scrub_text(text: Any) -> str:
    """把任意字符串里的敏感键值、Cookie/Authorization 头脱敏（纯函数）。"""
    s = _HEADER_RE.sub(lambda m: m.group("head") + FULL_REDACT, str(text))
    return _KV_RE.sub(_mask_kv, s)


def scrubbed(obj: Any) -> Any:
    """通用脱敏：dict 按敏感键处理，list/tuple 递归，字符串扫 key=value 与头。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if is_sensitive_key(k) and not isinstance(v, (dict, list, tuple)):
                out[k] = mask_secret(v, full=is_full_redact_key(k))
            else:
                out[k] = scrubbed(v)
        return out
    if isinstance(obj, (list, tuple)):
        return type(obj)(scrubbed(x) for x in obj)
    if isinstance(obj, bytes):
        return scrub_text(obj.decode("utf-8", "replace"))
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj


def mask_sessdata(s: str | None) -> str:
    if not s:
        return "未配置"
    return mask_secret(s)
