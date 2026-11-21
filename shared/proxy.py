"""网络代理设置：config/proxy.json + 进程环境变量注入（2026-11-12）。

三种模式（页面「设置 → 网络代理」里三选一）：
  · none    不使用代理：主动删掉 HTTP(S)_PROXY 等变量，系统里就算设了也不走；
  · system  使用系统代理：完全不动这些变量，沿用系统 / 启动脚本里已有的代理；
  · custom  自定义代理：把 HTTP(S)_PROXY 等变量写成下面的 url。

为什么用环境变量而不是逐个改 httpx.Client：
  · 项目里所有 HTTP 出口（collector/、livedeck/、core/weekly/、shared/update.py）
    都用 httpx 且默认 trust_env=True，urllib.request 与 websocket-client 也会读
    HTTP_PROXY / HTTPS_PROXY / http_proxy / https_proxy，所以启动时写一次环境变量
    就能整体覆盖，不用在十几处构造点各传一次 proxy。
  · 新增出口只要用 httpx / urllib 的默认配置就自动生效，不会漏。

边界（写进文档，别让用户以为开了代理就万事大吉）：
  · 只作用于本程序进程里的 HTTP(S) 请求；
  · 浏览器里的直播画面与弹幕（走本机 Edge 的 CDP 桥）、QQ 收消息的
    websockets（库本身不支持 HTTP 代理）不受影响；
  · 保存后新的请求立即生效；已经跑着的进程由各自主人拉回来：
    监控台盯着配置文件的 revision()，一改就把采集 / 推送重启一次（新环境变量随子进程继承），
    监控室自己重建三个 HTTP 连接池（不重启整个页面）。

自定义代理地址只接受 http:// / https://：httpx 走 socks5 需要额外的 socksio 依赖，
按 AGENTS §5「不新增运行时依赖」的要求这里不装，socks5 明确拒绝并给出提示。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "config" / "proxy.json"

#: 默认不走代理的地址：本机服务（8765 / 8760 / CDP 端口）绝不能绕去代理
DEFAULT_NO_PROXY = "127.0.0.1,localhost,::1"

#: 三种模式：不使用 / 用系统的 / 自定义（顺序就是页面上的顺序）
MODES = ("none", "system", "custom")
MODE_LABELS = {"none": "不使用代理", "system": "使用系统代理", "custom": "自定义代理"}
DEFAULT_MODE = "system"

#: 代理地址在环境变量里的两套写法都要设：httpx/urllib 认大写，websocket-client 优先小写
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")

#: 本进程真正改过的环境变量原值；切回「系统代理」时按它还原，不误伤系统里已有的设置
_SAVED: dict[str, str] = {}


def _default() -> dict:
    return {"mode": DEFAULT_MODE, "url": "", "no_proxy": DEFAULT_NO_PROXY}


def _mode_of(data: dict) -> str:
    """读模式；兼容旧版只有 enabled / url 两个字段的配置。"""
    mode = str(data.get("mode") or "").strip().lower()
    if mode in MODES:
        return mode
    if "mode" not in data and "enabled" in data:
        # 0.3.5 及更早只有开关：开着且填了地址 = 自定义，其余 = 用系统代理
        on = bool(data.get("enabled"))
        url = str(data.get("url") or "").strip()
        return "custom" if (on and url) else "system"
    return DEFAULT_MODE


def load(path: Path | str | None = None) -> dict:
    """读配置；文件不存在 / 不是 JSON / 字段缺失都退回默认（系统代理），不让页面 500。"""
    cfg = _default()
    try:
        data = json.loads(Path(path or PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    cfg["mode"] = _mode_of(data)
    url = str(data.get("url") or "").strip()
    if url:
        cfg["url"] = url
    no_proxy = str(data.get("no_proxy") or "").strip()
    if no_proxy:
        cfg["no_proxy"] = no_proxy
    return cfg


def normalize(url: str) -> str:
    """校验并规范化自定义代理地址；不合法抛 ValueError（给页面直接展示）。"""
    s = str(url or "").strip()
    if not s:
        raise ValueError("选了「自定义代理」就要填地址，例如 http://127.0.0.1:7890")
    if any(c.isspace() for c in s):
        raise ValueError("代理地址里不能有空格")
    parts = urlsplit(s)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("只支持 http:// 或 https:// 代理（socks5 需要额外依赖，本程序未内置）")
    if not parts.hostname:
        raise ValueError("代理地址缺少主机名，例如 http://127.0.0.1:7890")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("代理地址只填到端口就行，后面不要带路径或参数")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("代理端口不合法（应为 1-65535）") from None
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("代理端口不合法（应为 1-65535）")
    host = parts.hostname
    if ":" in host and not host.startswith("["):     # IPv6 字面量要带方括号
        host = "[%s]" % host
    netloc = host if port is None else "%s:%d" % (host, port)
    if parts.username:
        auth = parts.username
        if parts.password:
            auth += ":" + parts.password
        netloc = auth + "@" + netloc
    return "%s://%s" % (scheme, netloc)


def merge_no_proxy(raw: str) -> str:
    """把用户填的 no_proxy 与「本机地址一律免代理」合并，而不是整串替换。

    以前是「填了就整体覆盖」：手工在 config/proxy.json 里写个 example.com，
    DEFAULT_NO_PROXY 里的 127.0.0.1 / localhost / ::1 就全没了，监控台自己
    调 8760 和 CDP 端口的请求会被送进代理绕一圈（表现为监控室页面空、
    Chromium 连不上）。本机地址是硬要求，不允许被配置抹掉。
    """
    items: list[str] = []
    for part in str(DEFAULT_NO_PROXY or "").split(","):
        item = part.strip()
        if item and item not in items:
            items.append(item)
    for part in str(raw or "").replace(";", ",").split(","):
        item = part.strip()
        if item and item not in items:
            items.append(item)
    return ",".join(items)


def validate(cfg: dict) -> dict:
    """把页面传来的字段收成一份可落盘的配置；自定义模式必须有合法地址。"""
    cfg = cfg or {}
    raw = str(cfg.get("mode") or "").strip().lower()
    if raw and raw not in MODES:
        raise ValueError("代理模式只能是 none / system / custom")
    mode = _mode_of(cfg)
    url = str(cfg.get("url") or "").strip()
    if url:
        url = normalize(url)
    elif mode == "custom":
        raise ValueError("选了「自定义代理」就要填地址，例如 http://127.0.0.1:7890")
    return {
        "mode": mode,
        "url": url,
        "no_proxy": merge_no_proxy(str(cfg.get("no_proxy") or "")),
    }


def save(cfg: dict, path: Path | str | None = None) -> dict:
    """校验后写 config/proxy.json（本机配置，config/*.json 已在 .gitignore 里）。"""
    out = validate(cfg)
    p = Path(path or PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:                                  # 顺带收紧权限；Windows 上 chmod 基本是空操作
        os.chmod(p, 0o600)
    except OSError:
        pass
    return out


def revision(path: Path | str | None = None) -> tuple[int, int] | None:
    """配置文件的指纹（mtime_ns, size）：判断「保存过没有」。

    2026-11-20 补：原来只有「保存的那个进程」会 apply_to_env，采集 / 推送 / 监控室
    还带着启动时的旧代理。系统里的代理软件崩溃后没再起来时，它们会一直往死端口上撞
    （日志里是 `[WinError 10061] 由于目标计算机积极拒绝`），而用户在页面上选的
    「不使用代理」要等手动重启才生效。各进程拿这个指纹做「配置变了就换」的判据：
    文件不存在 / stat 失败返回 None（表示没有可比较的依据）。
    """
    try:
        st = Path(path or PATH).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _snapshot() -> None:
    """改环境变量之前整份拍快照：Windows 变量名大小写不敏感，逐个 setdefault 会记错原值。"""
    if not _SAVED:
        for key in PROXY_KEYS + NO_PROXY_KEYS:
            _SAVED[key] = os.environ.get(key, "")


def apply_to_env(cfg: dict | None = None, path: Path | str | None = None) -> dict:
    """把配置注入当前进程的环境变量并返回状态。

    启动时调用（容忍坏配置：退成系统代理，不让进程起不来）；页面保存后也调用一次，
    这样「更新检查」这类每次新建 client 的请求立即走新代理。
    """
    error = ""
    if cfg is None:
        try:
            cfg = validate(load(path))
        except ValueError as exc:
            error = str(exc)
            cfg = _default()
    cfg = validate(cfg)
    if cfg["mode"] == "custom" and cfg["url"]:
        _snapshot()
        for key in PROXY_KEYS:
            os.environ[key] = cfg["url"]
        for key in NO_PROXY_KEYS:
            os.environ[key] = cfg["no_proxy"]
    elif cfg["mode"] == "none":
        _snapshot()
        for key in PROXY_KEYS:            # 主动清掉：系统代理也不许用
            os.environ.pop(key, None)
    else:                                 # system：环境变量原样不动
        _restore_env()
    out = status(cfg)
    out["error"] = error
    return out


@contextmanager
def applied_for_reload():
    """热更新失败时还原代理环境，等下一轮重试。"""
    previous = {key: os.environ.get(key) for key in PROXY_KEYS + NO_PROXY_KEYS}
    saved = _SAVED.copy()
    try:
        status_now = apply_to_env()
        if status_now.get("error"):
            raise ValueError(status_now["error"])
        yield
    except BaseException:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _SAVED.clear()
        _SAVED.update(saved)
        raise


def _restore_env() -> None:
    """撤销本进程设过的代理变量，还原成启动时看到的值（不动没碰过的变量）。"""
    for key in PROXY_KEYS + NO_PROXY_KEYS:
        if key not in _SAVED:
            continue
        old = _SAVED[key]
        if old:
            os.environ[key] = old
        else:
            os.environ.pop(key, None)
    _SAVED.clear()


def status(cfg: dict | None = None, path: Path | str | None = None) -> dict:
    """给页面/调试看的当前状态：保存了什么、现在是什么模式。

    注意 url 原样返回、不做打码：设置弹窗是「读出来填进输入框、改完整体提交」
    的表单（web/index.html 的 #proxyUrl、livedeck/static/app.js 的 #proxy-url），
    把账号密码遮成 **** 之后用户一点保存就会把 **** 当成新地址写回配置，
    代理直接坏掉。这里只在环回 + CSRF 之后可读，看到的就是自己刚填的那串。
    """
    if cfg is None:
        cfg = load(path)
    try:
        cfg = validate(cfg)
    except ValueError:                    # 手工改坏的配置别让状态接口 500
        cfg = _default()
    mode = cfg["mode"]
    return {
        "ok": True,
        "mode": mode,
        "mode_name": MODE_LABELS.get(mode, mode),
        "enabled": mode == "custom",       # 兼容旧前端：只有自定义代理才算「已开启」
        "url": cfg["url"],
        "no_proxy": cfg["no_proxy"],
        "active": mode == "custom" and bool(cfg["url"]),
    }


def probe(proxy_url: str = "", client=None, timeout: float = 8.0, use_system: bool = False) -> dict:
    """访问一次 Gitee 上的 VERSION（就是「更新检查」实际要打的地址）。

    use_system=True 时按当前环境变量里的系统代理走；否则用给定的自定义代理地址。
    返回 {"ok", "passed", "status", "ms", "target", "error"}；永远不抛异常，
    client 可注入（AGENTS §5：测试传假 client，不打真实接口）。
    """
    from shared.update import VERSION_URLS

    target = VERSION_URLS[0]
    out = {"ok": True, "passed": False, "status": 0, "ms": 0, "target": target, "error": ""}
    if use_system and client is None and not any(os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")):
        out["error"] = "系统里没有配置 HTTP(S) 代理（HTTP_PROXY / HTTPS_PROXY 都是空的）"
        return out
    started = time.perf_counter()
    own = client is None
    try:
        if own:
            import httpx

            client = httpx.Client(timeout=timeout, trust_env=use_system, follow_redirects=True) \
                if use_system else httpx.Client(proxy=normalize(proxy_url), timeout=timeout,
                                                trust_env=False, follow_redirects=True)
        res = client.get(target)
        out["status"] = int(getattr(res, "status_code", 0) or 0)
        out["passed"] = 200 <= out["status"] < 400
        if not out["passed"]:
            out["error"] = "代理连上了，但目标返回 HTTP %s" % out["status"]
    except ValueError as exc:
        out["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001  连接失败 / 超时 / TLS 都归到这里
        out["error"] = "连接失败（%s）" % type(exc).__name__
    finally:
        out["ms"] = int((time.perf_counter() - started) * 1000)
        if own and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    return out


__all__ = ["PATH", "DEFAULT_MODE", "DEFAULT_NO_PROXY", "MODES", "MODE_LABELS", "load",
           "normalize", "validate", "save", "revision", "apply_to_env", "status", "probe"]
