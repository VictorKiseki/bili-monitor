"""LiveDeck 的发弹幕与表情 mixin。"""
from __future__ import annotations

import json
import time

from livedeck import config
from livedeck.deck_base import EMOTICON_API, EMO_TTL, SEND_MSG, UA, DeckError


class DeckChat:
    # ---------- 发弹幕 ----------
    def send_danmaku(self, room_id: int, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise DeckError("弹幕内容不能为空")
        if len(text) > 100:
            raise DeckError("弹幕最多 100 字")
        creds = config.load_credentials()
        self.creds = creds
        if not creds.can_send:
            raise DeckError("未登录或缺少 bili_jct，请先扫码登录", 401)
        now = time.time()
        if now - self._send_at.get(room_id, 0) < 3:
            raise DeckError("同一房间 3 秒内只能发一条", 429)
        if now - self._last_send_global < 1.2:
            raise DeckError("发送太快了，稍等一下", 429)
        info = self.info.get(room_id) or {}
        real = int(info.get("room_id") or room_id)
        payload = {
            "bubble": "0", "color": "16777215", "fontsize": "25", "mode": "1",
            "msg": text, "reply_mid": "0", "reply_uname": "", "reply_attr": "0",
            "rnd": str(int(now)), "roomid": str(real),
            "csrf": creds.bili_jct, "csrf_token": creds.bili_jct,
            "statistics": json.dumps({"appId": 100, "platform": 5}),
        }
        headers = {
            "Cookie": "SESSDATA=%s; bili_jct=%s; DedeUserID=%s" % (
                creds.sessdata, creds.bili_jct, creds.dedeuserid),
            "Referer": "https://live.bilibili.com/%s" % real,
            "Origin": "https://live.bilibili.com",
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        uname = creds.uname or "我"
        # B 站会把自己发的这条再广播回来（走弹幕桥），先登记，收到时吞掉
        self.hub.pending_echo(room_id, uname, text)
        try:
            r = self.http.post(SEND_MSG, data=payload, headers=headers)
            body = r.json()
        except Exception as e:  # noqa: BLE001
            self.hub.cancel_echo(room_id, text)
            raise DeckError("发送失败：网络错误 %s" % e.__class__.__name__, 502)
        code = int(body.get("code") or 0)
        msg = str(body.get("message") or body.get("msg") or "")
        if code != 0:
            self.hub.cancel_echo(room_id, text)
            hints = {
                -101: "账号未登录（SESSDATA 失效）",
                -111: "csrf 校验失败（bili_jct 不对）",
                -400: "参数错误",
                10030: "发送太频繁，被限流",
                10031: "弹幕内容被拒绝",
                10032: "发送间隔太短",
            }
            raise DeckError("B 站拒绝：%s（code=%s）" % (hints.get(code) or msg, code), 502)
        self._send_at[room_id] = now
        self._last_send_global = now
        self.hub.emit_local(room_id, uname, text)
        return {"ok": True, "message": "已发送", "room": room_id, "text": text}

    # ---------- 表情包 ----------
    def emoticons(self, room_id: int, force: bool = False) -> dict:
        """当前直播间可用的表情包。B 站这个接口必须带登录 Cookie，匿名会 -101。"""
        creds = config.load_credentials()
        self.creds = creds
        now = time.time()
        if not force:
            hit = self._emo.get(room_id)
            if hit and now - hit[0] < EMO_TTL:
                return hit[1]
        if not creds.logged_in:
            out = {"ok": False, "need_login": True, "room": room_id, "packages": [],
                   "message": "登录后才能读取表情包"}
            return out
        info = self.info.get(room_id) or {}
        real = int(info.get("room_id") or room_id)
        headers = {
            "Cookie": "SESSDATA=%s; bili_jct=%s; DedeUserID=%s" % (
                creds.sessdata, creds.bili_jct or "", creds.dedeuserid or ""),
            "Referer": "https://live.bilibili.com/%s" % real,
        }
        try:
            r = self.http.get(EMOTICON_API, params={"platform": "pc", "room_id": real},
                              headers=headers)
            body = r.json()
        except Exception as e:  # noqa: BLE001
            raise DeckError("读取表情包失败：网络错误 %s" % e.__class__.__name__, 502)
        code = int(body.get("code") or 0)
        if code != 0:
            if code == -101:
                raise DeckError("读取表情包需要登录（SESSDATA 失效？）", 401)
            raise DeckError("读取表情包失败：%s（code=%s）" % (body.get("message") or "", code), 502)
        data = body.get("data") or {}
        packages = []
        for pk in data.get("data") or []:
            items = []
            for e in pk.get("emoticons") or []:
                url = str(e.get("url") or "")
                if url.startswith("http://"):
                    url = "https://" + url[len("http://"):]
                perm = int(e.get("perm") or 0)
                unlock = e.get("unlock_condition")
                label = ""
                if isinstance(unlock, dict):
                    label = str(unlock.get("condition") or unlock.get("desc") or "")
                    if not label and unlock.get("guard_level"):
                        label = "舰长限定"
                elif unlock:
                    label = str(unlock)
                if not label and e.get("unlock_need_gift"):
                    label = "赠送礼物解锁"
                items.append({
                    "unique": str(e.get("emoticon_unique") or ""),
                    "id": int(e.get("emoticon_id") or 0),
                    "name": str(e.get("emoji") or e.get("descript") or ""),
                    "url": url,
                    "width": int(e.get("width") or 0) or 0,
                    "height": int(e.get("height") or 0) or 0,
                    "perm": perm,
                    "locked": perm != 1 or bool(unlock),
                    "lock_label": label,
                })
            packages.append({
                "id": pk.get("pkg_id"),
                "name": str(pk.get("pkg_name") or ""),
                "type": int(pk.get("pkg_type") or 0),
                "items": items,
            })
        out = {"ok": True, "room": room_id, "real": real, "packages": packages}
        self._emo[room_id] = (now, out)
        return out

    def send_emoticon(self, room_id: int, unique: str) -> dict:
        """发一条表情弹幕。表情必须来自该直播间自己的表情包（否则 B 站会拒绝）。"""
        unique = (unique or "").strip()
        if not unique:
            raise DeckError("表情参数为空")
        creds = config.load_credentials()
        self.creds = creds
        if not creds.can_send:
            raise DeckError("未登录或缺少 bili_jct，请先扫码登录", 401)
        data = self.emoticons(room_id)
        found = None
        for pk in data.get("packages") or []:
            for it in pk["items"]:
                if it["unique"] == unique:
                    found = dict(it)
                    found["pkg"] = pk["name"]
                    break
            if found:
                break
        if not found:
            raise DeckError("这个表情不在当前直播间的表情包里", 404)
        if found.get("locked"):
            raise DeckError("这个表情需要%s，当前账号不可用" % (found.get("lock_label") or "粉丝牌 / 舰长权限"), 403)
        now = time.time()
        if now - self._send_at.get(room_id, 0) < 3:
            raise DeckError("同一房间 3 秒内只能发一条", 429)
        if now - self._last_send_global < 1.2:
            raise DeckError("发送太快了，稍等一下", 429)
        info = self.info.get(room_id) or {}
        real = int(info.get("room_id") or room_id)
        payload = {
            "bubble": "0", "color": "16777215", "fontsize": "25", "mode": "1",
            "msg": unique, "dm_type": "1",
            "emoticonOptions[emoticonUnique]": unique,
            "emoticonOptions[url]": found.get("url") or "",
            "emoticonOptions[width]": str(found.get("width") or 0),
            "emoticonOptions[height]": str(found.get("height") or 0),
            "emoticonOptions[emoji]": found.get("name") or "",
            "emoticonOptions[inPlayerArea]": "0",
            "emoticonOptions[isDynamic]": "0",
            "emoticonOptions[bulgeDisplay]": "0",
            "rnd": str(int(now)), "roomid": str(real),
            "csrf": creds.bili_jct, "csrf_token": creds.bili_jct,
            "statistics": json.dumps({"appId": 100, "platform": 5}),
        }
        headers = {
            "Cookie": "SESSDATA=%s; bili_jct=%s; DedeUserID=%s" % (
                creds.sessdata, creds.bili_jct, creds.dedeuserid),
            "Referer": "https://live.bilibili.com/%s" % real,
            "Origin": "https://live.bilibili.com",
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        uname = creds.uname or "我"
        emo_name = found.get("name") or "[表情]"
        self.hub.pending_echo(room_id, uname, emo_name, unique=unique)
        try:
            r = self.http.post(SEND_MSG, data=payload, headers=headers)
            body = r.json()
        except Exception as e:  # noqa: BLE001
            self.hub.cancel_echo(room_id, emo_name, unique=unique)
            raise DeckError("发送失败：网络错误 %s" % e.__class__.__name__, 502)
        code = int(body.get("code") or 0)
        if code != 0:
            self.hub.cancel_echo(room_id, emo_name, unique=unique)
            raise DeckError("B 站拒绝：%s（code=%s）" % (body.get("message") or body.get("msg") or "", code), 502)
        self._send_at[room_id] = now
        self._last_send_global = now
        self.hub.emit_local(room_id, uname, emo_name,
                            extra={"emoticon": {"url": found.get("url") or "",
                                                "unique": unique,
                                                "width": found.get("width") or 0,
                                                "height": found.get("height") or 0}})
        return {"ok": True, "message": "表情已发送", "room": room_id, "emoticon": unique}
