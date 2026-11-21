"""QQ 官方机器人接入的公共实现（2026-11-08）。

管理台（web/console_qq.py）、命令行（scripts/p0_check.py、scripts/setup_wizard.py）
共用这一份逻辑，避免两套代码各写一遍 token / openid / 发送：

  · 凭据：读 / 写 BBOT.env（保留原有注释与其它键，原子替换，权限收紧）
  · 鉴权：bots.qq.com/app/getAppAccessToken，失败码翻成人话
  · 机器人信息：GET /users/@me（名字、头像），用来在页面上显示「已连上谁」
  · 收 openid：连 WebSocket 网关，等你私聊 / 群里 @ 一次，把 openid 写 .p0_ids.json
  · 主动发送：POST /v2/users/{openid}/messages 或 /v2/groups/{openid}/messages

收 openid 的等待要放在后台线程里跑（管理台不能因为「等人发消息」把请求挂住），
所以 OpenidListener 自己管线程、状态和退出条件，调用方只读 status()。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import httpx

from shared.netsafe import qq_gateway_ok

ROOT = Path(__file__).resolve().parent.parent
# 地址与 intents 由 channels/qq_api.py 统一给出（审查细节 10）；TOKEN_API 是旧名字，保留兼容。
from channels.qq_api import API, INTENTS, TOKEN_URL as TOKEN_API  # noqa: E402,F401
ENV_CANDIDATES = ("BBOT.env", ".env", "bbot.env", os.path.join("config", ".env"))
IDS_FILE_NAME = ".p0_ids.json"

_TOKEN_HINTS = {
    100016: "AppID 或 AppSecret 填错（去 q.qq.com → 开发设置里重新复制）",
    100007: "机器人已被停用或删除",
    100001: "参数不对：AppID 要是数字，AppSecret 要整串复制",
}
_SEND_HINTS = {
    403: "常见原因：① 群里没开「主动推送消息能力」；② 私聊时你不在「开发体验用户」白名单；"
         "③ 机器人未加入该群",
    404: "openid 不存在或已失效，重新抓一次（管理台「机器人」页 → 等一条消息）",
}


class QQError(RuntimeError):
    """带 code / hint 的失败：UI 直接把 message 和 hint 显示出来。"""

    def __init__(self, message: str, hint: str = "", code=None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.message, "hint": self.hint, "code": self.code}


# ---------------- 凭据文件 ----------------
def env_path(root: Path | str | None = None) -> Path:
    """凭据文件的落点：已有就沿用，没有就用根目录 BBOT.env（与 run_bot 的 --env 默认一致）。"""
    base = Path(root or ROOT)
    for name in ENV_CANDIDATES:
        p = base / name
        if p.is_file():
            return p
    return base / "BBOT.env"


def read_env(path: Path | str) -> dict:
    """读 KEY=VALUE；忽略注释与空行，去掉成对引号。"""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text("utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if not k:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out


def write_env(path: Path | str, values: dict) -> Path:
    """把 values 合并进 env 文件：同名键原位替换，新键追加，注释和其它键原样保留。

    先写 .tmp 再 os.replace，避免写一半断电留下半个文件（管理台保存凭据时会用到）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if p.is_file():
        lines = p.read_text("utf-8", errors="replace").splitlines()
    pending = dict(values)
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in pending:
                out.append("%s=%s" % (k, pending.pop(k)))
                continue
        out.append(line)
    if pending:
        if out and out[-1].strip():
            out.append("")
        for k, v in pending.items():
            out.append("%s=%s" % (k, v))
    text = "\n".join(out).rstrip("\n") + "\n"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, "utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def mask(value: str) -> str:
    s = str(value or "")
    return s[:4] + "****" + s[-2:] if len(s) > 8 else "****"


def credentials(root: Path | str | None = None) -> dict:
    """当前生效的凭据：文件优先，其次进程环境变量（与 run_bot 的读取顺序一致）。"""
    p = env_path(root)
    data = read_env(p)
    app_id = (data.get("QQBOT_APP_ID") or os.environ.get("QQBOT_APP_ID") or "").strip()
    secret = (data.get("QQBOT_APP_SECRET") or os.environ.get("QQBOT_APP_SECRET") or "").strip()
    return {"app_id": app_id, "secret": secret, "path": p,
            "source": str(p) if p.is_file() else "", "configured": bool(app_id and secret)}


def save_credentials(app_id: str, secret: str, root: Path | str | None = None) -> Path:
    app_id, secret = str(app_id or "").strip(), str(secret or "").strip()
    if not app_id or not secret:
        raise QQError("AppID 和 AppSecret 都要填", "去 q.qq.com → 你的机器人 → 开发设置里复制")
    if not app_id.isdigit():
        raise QQError("AppID 应该是一串数字", "别把 AppSecret 填到 AppID 里，两行分开填")
    if len(secret) < 16:
        raise QQError("AppSecret 太短，不像有效值", "整串复制，中间不要有空格或换行")
    return write_env(env_path(root), {"QQBOT_APP_ID": app_id, "QQBOT_APP_SECRET": secret})


# ---------------- 鉴权 / 发送 ----------------
def fetch_token(app_id: str, secret: str, timeout: float = 20) -> dict:
    """换 access_token；失败抛 QQError（带人话提示）。"""
    try:
        r = httpx.post(TOKEN_API, json={"appId": str(app_id), "clientSecret": str(secret)},
                       timeout=timeout)
    except httpx.HTTPError as e:
        raise QQError("连不上 QQ 鉴权接口：%s" % type(e).__name__,
                      "检查本机网络 / 代理；这个接口只在大陆网络下稳定") from e
    try:
        j = r.json()
    except ValueError:
        raise QQError("鉴权接口返回的不是 JSON（HTTP %s）" % r.status_code,
                      "可能是被网络中间设备拦了，换网络再试")
    if not j.get("access_token"):
        code = j.get("code")
        raise QQError("鉴权失败 code=%s message=%s" % (code, j.get("message") or ""),
                      _TOKEN_HINTS.get(code, "检查开放平台「开发设置」里的值是否复制完整"), code=code)
    return {"access_token": j["access_token"], "expires_in": int(j.get("expires_in") or 7200)}


def bot_profile(token: str, app_id: str = "", timeout: float = 20) -> dict:
    """GET /users/@me：机器人自己的名字/头像。拿不到不算失败（权限可能没开）。"""
    try:
        r = httpx.get(API + "/users/@me",
                      headers={"Authorization": "QQBot " + token,
                               "X-Union-Appid": str(app_id or "")},
                      timeout=timeout)
        if r.status_code // 100 != 2:
            return {}
        j = r.json()
    except (httpx.HTTPError, ValueError):
        return {}
    return {"id": str(j.get("id") or ""), "username": str(j.get("username") or ""),
            "avatar": str(j.get("avatar") or "")}


def verify(app_id: str, secret: str, timeout: float = 20) -> dict:
    """验一次凭据：换 token + 查机器人信息。返回 {ok, expires_in, bot{...}, ms}。"""
    t0 = time.time()
    tk = fetch_token(app_id, secret, timeout)
    bot = bot_profile(tk["access_token"], app_id, timeout)
    return {"ok": True, "expires_in": tk["expires_in"], "bot": bot,
            "ms": int((time.time() - t0) * 1000)}


def ids_path(root: Path | str | None = None) -> Path:
    return Path(root or ROOT) / IDS_FILE_NAME


def read_ids(path: Path | str | None = None) -> dict:
    p = Path(path) if path else ids_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_ids(values: dict, path: Path | str | None = None, merge: bool = True) -> Path:
    p = Path(path) if path else ids_path()
    old = read_ids(p) if merge else {}
    data = {"_说明": "推送目标的 QQ openid，由管理台 / p0_check listen 自动抓取，不要手填。"}
    for k, v in old.items():
        if k != "_说明" and v:
            data[k] = str(v)
    data.update({k: str(v) for k, v in (values or {}).items() if v})
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, p)
    return p


def clear_ids(path: Path | str | None = None) -> Path:
    p = Path(path) if path else ids_path()
    data = {"_说明": "推送目标的 QQ openid，由管理台 / p0_check listen 自动抓取，不要手填。"}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, p)
    return p


def send_text(app_id: str, secret: str, openid: str, kind: str = "user",
              content: str = "B站监控测试消息", timeout: float = 20) -> dict:
    """主动发一条纯文本。kind=user 私聊 / group 群。"""
    openid = str(openid or "").strip()
    if not openid:
        raise QQError("没有 openid", "先在「机器人」页等一条消息（或跑 p0_check.py listen）")
    tk = fetch_token(app_id, secret, timeout)
    path = ("/v2/groups/%s/messages" if kind == "group" else "/v2/users/%s/messages") % openid
    t0 = time.time()
    try:
        r = httpx.post(API + path, timeout=timeout,
                       headers={"Authorization": "QQBot " + tk["access_token"],
                                "X-Union-Appid": str(app_id),
                                "Content-Type": "application/json"},
                       json={"content": str(content)[:900], "msg_type": 0})
    except httpx.HTTPError as e:
        raise QQError("发送请求失败：%s" % type(e).__name__, "检查网络后重试") from e
    ms = int((time.time() - t0) * 1000)
    if r.status_code // 100 != 2:
        raise QQError("发送失败 HTTP %s" % r.status_code,
                      _SEND_HINTS.get(r.status_code, r.text[:200]), code=r.status_code)
    try:
        j = r.json()
    except ValueError:
        j = {}
    return {"ok": True, "status": r.status_code, "ms": ms, "id": str(j.get("id") or "")}


# ---------------- 收 openid（后台线程） ----------------
class OpenidListener:
    """连网关等一条私聊 / 群 @，把 openid 写进 .p0_ids.json。

    管理台点「开始监听」→ start()，页面每 2 秒读 status()，拿到就展示；
    超时或用户点「停止」→ 线程自己收尾。同一个实例可以重复 start（第二次会先停旧的）。
    """

    def __init__(self, app_id: str, secret: str, ids_file: Path | str | None = None,
                 timeout: float = 180.0):
        self.app_id = str(app_id or "")
        self.secret = str(secret or "")
        self.ids_file = Path(ids_file) if ids_file else ids_path()
        self.timeout = float(timeout or 0)   # <=0 表示不设超时，等用户手动停
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._events: list[dict] = []
        self._state: dict = {"running": False, "stage": "idle", "error": "", "hint": "",
                             "bot": "", "gateway": "", "events": 0,
                             "user_openid": "", "group_openid": "",
                             "started_at": None, "finished_at": None}

    # ---- 对外 ----
    def start(self) -> dict:
        if self._thread and self._thread.is_alive():
            return self.status()
        if not self.app_id or not self.secret:
            raise QQError("还没有机器人凭据", "先填 AppID / AppSecret 并保存")
        self._stop = threading.Event()
        self._events = []
        with self._lock:
            prev = read_ids(self.ids_file)
            self._state = {"running": True, "stage": "connecting", "error": "", "hint": "",
                           "bot": "", "gateway": "", "events": 0,
                           "user_openid": str(prev.get("user_openid") or ""),
                           "group_openid": str(prev.get("group_openid") or ""),
                           "started_at": time.time(), "finished_at": None}
        self._thread = threading.Thread(target=self._run, name="qq-openid", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        with self._lock:
            if self._state.get("running"):
                self._state.update({"running": False, "stage": "stopped",
                                    "finished_at": time.time(),
                                    "hint": self._state.get("hint") or "已手动停止"})
        return self.status()

    def status(self) -> dict:
        with self._lock:
            st = dict(self._state)
            st["events"] = len(self._events)
            st["last_event"] = self._events[-1] if self._events else None
            st["timeout"] = self.timeout
            if st.get("started_at"):
                end = st.get("finished_at") or time.time()
                st["elapsed"] = int(max(0, end - st["started_at"]))
            else:
                st["elapsed"] = 0
            return st

    # ---- 线程内 ----
    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except QQError as e:
            self._set(running=False, stage="error", error=e.message,
                      hint=e.hint, finished_at=time.time())
        except Exception as e:                                  # noqa: BLE001
            self._set(running=False, stage="error",
                      error="%s: %s" % (type(e).__name__, e),
                      hint="重试一次；反复失败看 logs/ 与网络", finished_at=time.time())
        finally:
            with self._lock:
                self._state["running"] = False
                if not self._state.get("finished_at"):
                    self._state["finished_at"] = time.time()
                if self._state.get("stage") in ("connecting", "waiting"):
                    self._state["stage"] = "done"

    async def _main(self) -> None:
        try:
            import websockets
            from websockets.exceptions import ConnectionClosed
        except ImportError as e:
            raise QQError("缺少 websockets 库", "发布包自带运行时里就有；解压不完整时重新解压，或在开发机上重建 runtime") from e

        tk = fetch_token(self.app_id, self.secret)
        self._set(bot="", gateway="")
        async with httpx.AsyncClient(timeout=20) as c:
            gw = await c.get(API + "/gateway",
                             headers={"Authorization": "QQBot " + tk["access_token"]})
            url = (gw.json() or {}).get("url") if gw.status_code // 100 == 2 else ""
        if not url:
            raise QQError("取 gateway 失败", "确认机器人已上线、AppID/AppSecret 正确")
        if not qq_gateway_ok(str(url)):
            # S2：只认 wss://*.qq.com；其余不连，避免被改写后的 /gateway 响应带去打内网
            raise QQError("gateway 地址不在 qq.com 白名单内，已拒绝连接",
                          "检查网络/DNS 是否被劫持，或代理设置是否把 api.sgroup.qq.com 指到了别处")
        self._set(gateway=url, stage="waiting")
        deadline = (time.time() + self.timeout) if self.timeout > 0 else None
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            hello = json.loads(await ws.recv())
            beat = float((hello.get("d") or {}).get("heartbeat_interval") or 30000) / 1000.0
            await ws.send(json.dumps({
                "op": 2,
                "d": {"token": "QQBot " + tk["access_token"], "intents": INTENTS,
                      "shard": [0, 1], "properties": {}},
            }))
            seq = None
            last_beat = time.time()
            while not self._stop.is_set() and (deadline is None or time.time() < deadline):
                if time.time() - last_beat >= beat:
                    await ws.send(json.dumps({"op": 1, "d": seq}))
                    last_beat = time.time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed:
                    raise QQError("WebSocket 断开", "可能是 token 过期或被平台踢下线，重新开始监听")
                ev = json.loads(raw)
                if ev.get("s") is not None:
                    seq = ev["s"]
                t, d = ev.get("t"), (ev.get("d") or {})
                if ev.get("op") == 0 and t == "READY":
                    u = d.get("user") or {}
                    self._set(bot=str(u.get("username") or ""))
                    continue
                if ev.get("op") != 0 or not t:
                    continue
                hit = None
                if t == "C2C_MESSAGE_CREATE":
                    hit = ("user_openid", str((d.get("author") or {}).get("user_openid") or ""),
                           "私聊")
                elif t == "GROUP_AT_MESSAGE_CREATE":
                    hit = ("group_openid", str(d.get("group_openid") or ""), "群@")
                elif t == "GROUP_MESSAGE_CREATE":
                    hit = ("group_openid", str(d.get("group_openid") or ""), "群消息")
                if not hit:
                    continue
                key, value, label = hit
                if not value:
                    continue
                with self._lock:
                    self._events.append({"kind": key, "openid": value, "where": label,
                                         "content": str(d.get("content") or "").strip()[:120],
                                         "at": time.time()})
                    self._state[key] = value
                write_ids({key: value}, self.ids_file)
                if self._state.get("user_openid") and self._state.get("group_openid"):
                    self._set(stage="done", hint="私聊和群的 openid 都拿到了")
                    return
        with self._lock:
            if self._state.get("stage") in ("connecting", "waiting"):
                self._state.update({"stage": "done", "finished_at": time.time()})
                if not self._state.get("user_openid"):
                    self._state["hint"] = ("等了 %d 秒没收到消息：先在 QQ 里私聊机器人发一句话"
                                           % int(self.timeout or 0))


def send_test(openid: str = "", kind: str = "user", content: str = "",
              root: Path | str | None = None) -> dict:
    """管理台「发一条测试消息」：凭据与 openid 都从当前配置里现取。"""
    c = credentials(root)
    if not c["configured"]:
        raise QQError("还没有机器人凭据", "先填 AppID / AppSecret")
    if not openid:
        ids = read_ids(ids_path(root))
        openid = str(ids.get("user_openid" if kind == "user" else "group_openid") or "")
    return send_text(c["app_id"], c["secret"], openid, kind=kind,
                     content=content or "B站监控测试消息：机器人已连通，收到说明推送链路是通的")
