"""LiveDeck 的扫码 / Cookie 登录 mixin。"""
from __future__ import annotations

import time

from collector import bili_login
from livedeck import config
from livedeck.deck_base import NAV, DeckError

# 校验通过 / 明确失效后隔久一点再问；校验本身失败（网络不通、被风控）则短间隔重试，
# 否则一次网络抖动就会让顶栏挂 5 分钟「登录已失效」（用户 2026-11-20 报的现象）。
CHECK_TTL = 300
RETRY_TTL = 15


class DeckLogin:
    # ---------- 登录 ----------
    def _verify_login(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.login_checked < self.login_check_ttl:
            return
        self.login_checked = now
        creds = config.load_credentials()
        self.creds = creds
        if not creds.sessdata:
            self.login_ok = False
            self.login_state = "none"
            self.login_error = "未登录"
            self.login_check_ttl = CHECK_TTL
            return
        last = ""
        for attempt in (0, 1):
            try:
                r = self.http.get(NAV, headers={"Cookie": "SESSDATA=%s" % creds.sessdata})
            except Exception:  # noqa: BLE001
                last = "network"
                if attempt == 0:
                    # 连接池是建进程时按当时的环境变量建的：若从带旧代理的父进程继承，
                    # 代理软件退出后会一直连死端口。这里按配置文件重建一次再试。
                    self._heal_connections()
                    time.sleep(0.3)
                continue
            if r.status_code == 412:
                last = "risk"
                continue
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                last = "badjson"
                continue
            if body.get("code") == 0 and (body.get("data") or {}).get("isLogin"):
                d = body["data"] or {}
                self.login_ok = True
                self.login_state = "ok"
                self.login_error = ""
                self.login_check_ttl = CHECK_TTL
                self._remember_identity(d, creds)
                self.log("登录态有效：%s（mid %s）" % (d.get("uname"), d.get("mid")))
                return
            # 能解析、且明确 isLogin=false：这一份 Cookie 确实失效了
            self.login_ok = False
            self.login_state = "expired"
            self.login_error = "SESSDATA 已失效，请重新扫码"
            self.login_check_ttl = CHECK_TTL
            self.log("登录态失效：SESSDATA 已不被 B 站接受")
            return
        self.login_ok = False
        self.login_check_ttl = RETRY_TTL
        if last == "risk":
            self.login_state = "risk"
            self.login_error = "B 站风控拦截了登录态校验（HTTP 412），稍后自动重试"
        elif last == "badjson":
            self.login_state = "network"
            self.login_error = "登录态校验返回了无法解析的内容，稍后自动重试"
        else:
            self.login_state = "network"
            self.login_error = "网络异常，暂时连不上 B 站，稍后自动重试"
        self.log("登录态校验失败：%s（%d 秒后重试）" % (self.login_error, RETRY_TTL))

    def _heal_connections(self) -> None:
        """按当前代理配置重建连接池（死代理自愈）。"""
        try:
            if self.apply_proxy(force=True):
                self.log("登录态校验遇到网络错误，已按代理配置重建连接池")
        except Exception as e:  # noqa: BLE001
            self.log("重建连接池失败：%s: %s" % (e.__class__.__name__, e))

    def _remember_identity(self, d: dict, creds) -> None:
        """nav 里拿到了昵称/mid 就补进凭据并回写，别让名单里一直空着。"""
        uname = str(d.get("uname") or "")
        try:
            mid = int(d.get("mid") or 0)
        except (TypeError, ValueError):
            mid = 0
        if uname:
            creds.uname = uname
        if mid:
            creds.mid = mid
        if not creds.uname or not creds.mid:
            return
        try:
            self.creds = config.save_credentials(
                creds.sessdata, creds.bili_jct, creds.dedeuserid,
                uname=creds.uname, mid=creds.mid, source=creds.source or "登录态校验",
            )
        except Exception as e:  # noqa: BLE001
            self.log("回写登录昵称失败：%s" % e.__class__.__name__)

    def login_snapshot(self) -> dict:
        self._verify_login()
        snap = self.creds.masked()
        snap["verified"] = self.login_ok
        snap["state"] = self.login_state
        snap["error"] = self.login_error
        # 只有「确定失效」才禁发；网络不通 / 风控拦截时 Cookie 很可能还好着，
        # 允许继续试，真发不出去时 /msg/send 的返回码会说清楚。
        snap["can_send"] = bool(self.creds.can_send and self.login_state not in ("expired", "none"))
        return snap

    def login_start(self) -> dict:
        try:
            data = bili_login.start_login()
        except Exception as e:  # noqa: BLE001
            raise DeckError("申请二维码失败：%s" % e, 502)
        return data

    def login_poll(self, key: str) -> dict:
        res = bili_login.poll_login(key)
        if res.get("status") == "success":
            cookies = res.get("cookies") or {}
            sess = res.get("sessdata") or cookies.get("SESSDATA") or ""
            jct = cookies.get("bili_jct") or ""
            dede = cookies.get("DedeUserID") or str(res.get("mid") or "")
            if not jct:
                self.log("扫码成功但没拿到 bili_jct，发弹幕会不可用（可手动粘贴 Cookie）")
            self.creds = config.save_credentials(
                sessdata=sess, bili_jct=jct, dedeuserid=dede,
                uname=res.get("uname") or "", mid=int(res.get("mid") or 0), source="扫码登录",
            )
            self.login_checked = 0.0
            self._verify_login(force=True)
            self._on_creds_changed()
            msg = res.get("message") or "登录成功"
            if self.login_state == "network":
                msg = "登录信息已保存；暂时连不上 B 站，登录态稍后自动重试校验"
            elif self.login_state == "risk":
                msg = "登录信息已保存；登录态校验被风控拦截，稍后自动重试"
            return {"status": "success", "message": msg, "login": self.login_snapshot()}
        return res

    def login_paste(self, sessdata: str, bili_jct: str, dedeuserid: str) -> dict:
        sessdata = (sessdata or "").strip()
        bili_jct = (bili_jct or "").strip()
        dedeuserid = (dedeuserid or "").strip()
        if not sessdata:
            raise DeckError("SESSDATA 不能为空")
        self.creds = config.save_credentials(sessdata, bili_jct, dedeuserid, source="手动粘贴")
        self.login_checked = 0.0
        self._verify_login(force=True)
        if self.login_state == "expired":
            raise DeckError(self.login_error or "登录态校验失败")
        self._on_creds_changed()
        out = {"ok": True, "login": self.login_snapshot()}
        if self.login_state in ("network", "risk"):
            # Cookie 已保存，只是这一刻没法确认；别当成失败让用户重填。
            out["warn"] = self.login_error
        return out

    def logout(self) -> dict:
        config.clear_credentials()
        self.creds = config.Credentials()
        self.login_ok = False
        self.login_state = "none"
        self.login_error = "未登录"
        self._on_creds_changed()
        return {"ok": True}
