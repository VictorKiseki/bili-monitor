"""Console 的 QQ 官方机器人接入部分（由 web/console.py 拆出）。

原来只能跑 scripts/p0_check.py + scripts/setup_wizard.py 才能绑机器人；
现在管理台「机器人」页做同样的事：
  1. 填 AppID / AppSecret → 保存到 BBOT.env（404 权限）
  2. 校验凭据（换 token + 拉机器人资料）
  3. 开始监听 → 你在 QQ 里私聊机器人发一句话 → 自动抓 openid 写 .p0_ids.json
  4. 发一条测试消息，确认手机能收到
底层全部复用 channels/qq_setup.py，命令行与管理台只有一份逻辑。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from channels import qq_setup as Q  # noqa: E402

from web.console_core import ApiError  # noqa: E402


class ConsoleQQ:
    # ---- 状态 ----------------------------------------------------------
    def _qq_listener(self) -> "Q.OpenidListener | None":
        return getattr(self, "_qq_lst", None)

    def _qq_lock(self) -> threading.Lock:
        lock = getattr(self, "_qq_mu", None)
        if lock is None:
            lock = threading.Lock()
            self._qq_mu = lock
        return lock

    def qq_status(self) -> dict:
        c = Q.credentials(ROOT)
        ids = Q.read_ids(Q.ids_path(ROOT))
        lst = self._qq_listener()
        ls = lst.status() if lst else {"running": False, "stage": "idle"}
        need = []
        if not (c["app_id"] and c["secret"]):
            need.append("credentials")
        if not ids.get("user_openid"):
            need.append("openid")
        return {
            "ok": True,
            "configured": bool(c["app_id"] and c["secret"]),
            "app_id": Q.mask(c["app_id"]),
            "app_id_raw": c["app_id"] if not c["app_id"] else "",   # 只回空，避免把凭据再吐一遍
            "secret": Q.mask(c["secret"]),
            "env_file": str(c["path"]),
            "env_source": c["source"],
            "ids_file": str(Q.ids_path(ROOT)),
            "user_openid": ids.get("user_openid", ""),
            "group_openid": ids.get("group_openid", ""),
            "ready": not need,
            "need": need,
            "listener": ls,
            "need_restart": False,
        }

    # ---- 凭据 ----------------------------------------------------------
    def qq_save_credentials(self, body: dict) -> dict:
        app_id = str(body.get("app_id") or "").strip()
        secret = str(body.get("secret") or "").strip()
        if not app_id and not secret:
            raise ApiError("AppID 和 AppSecret 不能都为空")
        with self._qq_lock():
            cur = Q.credentials(ROOT)
            # 只改一个字段时，另一个沿用已保存的值（页面上看不到原值）
            app_id = app_id or cur["app_id"]
            secret = secret or cur["secret"]
            try:
                path = Q.save_credentials(app_id, secret, ROOT)
            except Q.QQError as e:
                raise ApiError(e.message) from e
        verify = {"ok": False, "message": "已保存，但还没校验"}
        try:
            verify = {"ok": True, **Q.verify(app_id, secret)}
        except Q.QQError as e:
            verify = {"ok": False, "message": e.message, "hint": e.hint}
        out = self.qq_status()
        out.update({
            "saved": True, "file": str(path), "verify": verify, "need_restart": True,
            "message": ("已保存到 %s。重启推送进程后生效。" % Path(path).name) if verify["ok"]
                       else ("已写入 %s，但校验没通过：%s" % (Path(path).name, verify.get("message"))),
        })
        return out

    def qq_verify(self, body: dict) -> dict:
        app_id = str(body.get("app_id") or "").strip()
        secret = str(body.get("secret") or "").strip()
        if not app_id or not secret:
            cur = Q.credentials(ROOT)
            app_id = app_id or cur["app_id"]
            secret = secret or cur["secret"]
        if not app_id or not secret:
            raise ApiError("还没有保存 AppID / AppSecret")
        try:
            r = Q.verify(app_id, secret)
        except Q.QQError as e:
            raise ApiError(e.message + ("（%s）" % e.hint if e.hint else "")) from e
        return {"ok": True, "verified": True, **r,
                "message": "凭据有效，机器人：%s" % ((r.get("bot") or {}).get("username") or "未知")}

    # ---- openid --------------------------------------------------------
    def qq_listen_start(self, body: dict) -> dict:
        c = Q.credentials(ROOT)
        if not (c["app_id"] and c["secret"]):
            raise ApiError("先填 AppID / AppSecret 并保存")
        try:
            timeout = float(body.get("timeout") or 180)
        except (TypeError, ValueError):
            timeout = 180.0
        timeout = max(10.0, min(timeout, 900.0))
        with self._qq_lock():
            cur = self._qq_listener()
            if cur and cur.status().get("running"):
                return {"ok": True, "already": True, **cur.status(),
                        "message": "已经有一个监听在跑，先停下再重开"}
            lst = Q.OpenidListener(c["app_id"], c["secret"], Q.ids_path(ROOT), timeout=timeout)
            try:
                lst.start()
            except Q.QQError as e:
                raise ApiError(e.message) from e
            self._qq_lst = lst
        return {"ok": True, "already": False, **lst.status(),
                "message": "监听已启动：现在去 QQ 私聊机器人发一句话"}

    def qq_listen_status(self) -> dict:
        lst = self._qq_listener()
        if not lst:
            return {"ok": True, "running": False, "stage": "idle", "user_openid": "",
                    "group_openid": "", "events": 0}
        st = lst.status()
        if st.get("running"):
            # 进程内状态可能落后于文件（另一路径写入），以文件为准补一次
            ids = Q.read_ids(Q.ids_path(ROOT))
            st.setdefault("user_openid", ids.get("user_openid", ""))
        return {"ok": True, **st}

    def qq_listen_stop(self) -> dict:
        lst = self._qq_listener()
        if not lst:
            return {"ok": True, "stopped": False, "message": "没有在跑的监听"}
        lst.stop()
        st = lst.status()
        return {"ok": True, "stopped": True, **st,
                "user_openid": st.get("user_openid") or Q.read_ids(Q.ids_path(ROOT)).get("user_openid", ""),
                "message": "监听已停止"}

    def qq_save_ids(self, body: dict) -> dict:
        """手动粘贴 openid 的兜底通道（正常情况下用监听自动抓）。"""
        vals = {}
        for k in ("user_openid", "group_openid"):
            v = str(body.get(k) or "").strip()
            if v:
                if len(v) < 20 or not all(ch in "0123456789ABCDEFabcdef" for ch in v):
                    raise ApiError("%s 看着不像 openid（应为 32 位十六进制）" % k)
                vals[k] = v.upper()
        if not vals:
            raise ApiError("没有可保存的 openid")
        path = Q.write_ids(vals, Q.ids_path(ROOT), merge=True)
        return {"ok": True, "file": str(path), "need_restart": True,
                "ids": Q.read_ids(path),
                "message": "已保存，重启推送进程后生效"}

    def qq_clear_ids(self) -> dict:
        path = Q.ids_path(ROOT)
        Q.clear_ids(path)
        return {"ok": True, "cleared": True, "file": str(path), "need_restart": True,
                "message": "已清除 openid，重新监听即可再抓一次"}

    # ---- 发测试 --------------------------------------------------------
    def qq_test_send(self, body: dict) -> dict:
        content = str(body.get("content") or "").strip() or "这是一条来自 B 站监控台的测试消息"
        kind = "group" if str(body.get("kind") or "") == "group" else "user"
        openid = str(body.get("openid") or "").strip()
        ids = Q.read_ids(Q.ids_path(ROOT))
        if not openid:
            openid = ids.get("group_openid" if kind == "group" else "user_openid", "")
        if not openid:
            raise ApiError("还没有 %s，先监听抓一次或手动粘贴"
                           % ("群 openid" if kind == "group" else "私聊 openid"))
        c = Q.credentials(ROOT)
        if not (c["app_id"] and c["secret"]):
            raise ApiError("先填 AppID / AppSecret 并保存")
        try:
            r = Q.send_text(c["app_id"], c["secret"], openid, kind=kind, content=content)
        except Q.QQError as e:
            raise ApiError(e.message + ("（%s）" % e.hint if e.hint else "")) from e
        return {"ok": True, "sent": True, "kind": kind, "status": r["status"], "ms": r["ms"],
                "message": "已发出（接口 %sms），看一眼手机有没有响" % r["ms"]}
