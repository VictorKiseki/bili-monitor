"""LiveDeck 的名单、选台与布局持久化 mixin。"""
from __future__ import annotations

import re
import threading
import time

from livedeck import config
from livedeck.deck_base import (GROUP_NAME_MAX, MASTER_INFO, MASTER_ROOM, MAX_COMBO_ROOMS, MAX_GROUPS,
                                MAX_LISTEN, MAX_ROOMS, ROOM_INFO, DeckError, clean_text, normalize_color)


class DeckRooms:
    def _save_state(self) -> None:
        with self.lock:
            temp = [{"room_id": e.room_id, "uid": e.uid, "name": e.name, "grp": e.grp}
                    for e in self.rooms.values() if not e.from_db][:200]
            state = {"active": list(self.active), "listening": list(self.listening),
                     "grid": self.grid, "hidden": sorted(self.hidden), "temp": temp,
                     "combos": list(self.combos),
                     "colors": {str(r): dict(c) for r, c in self.colors.items()},
                     "group_order": list(self.group_order)}
        config.save_state(state)

    # ---------- 名单 ----------
    def load_rooms(self) -> None:
        entries = config.load_rooms()
        with self.lock:
            old = self.rooms
            fresh = {e.room_id: e for e in entries}
            # 手填房间号加进来的临时房间不在监控名单里，刷新名单时不能把它们踢出舞台
            for rid, e in old.items():
                if rid not in fresh and not e.from_db:
                    fresh[rid] = e
            self.rooms = fresh
            known = set(self.rooms)
            self.active = [r for r in self.active if r in known][:MAX_ROOMS]
            self.listening = [r for r in self.listening
                              if r in known and r not in self.active][:MAX_LISTEN]
            for r in self.active:
                self.hub.stat(r)

    def set_combos(self, items) -> dict:
        """整表替换临时组合；id 由服务端分配，前端只送 name/rooms。"""
        if not isinstance(items, list):
            raise DeckError("combos 必须是数组")
        out: list[dict] = []
        used: set[int] = set()
        for item in items[:20]:
            if not isinstance(item, dict):
                continue
            rooms: list[int] = []
            for r in item.get("rooms") or []:
                try:
                    rid = int(r)
                except (TypeError, ValueError):
                    continue
                if rid > 0 and rid not in rooms:
                    rooms.append(rid)
            if not rooms:
                continue
            try:
                cid = int(item.get("id") or 0)
            except (TypeError, ValueError):
                cid = 0
            if cid <= 0 or cid in used:
                cid = 1
                while cid in used:
                    cid += 1
            used.add(cid)
            name = clean_text(str(item.get("name") or ""), "").strip()[:24] or ("组合 %d" % cid)
            out.append({"id": cid, "name": name, "rooms": rooms[:MAX_COMBO_ROOMS]})
        with self.lock:
            self.combos = out
        self._save_state()
        self.log("临时组合：%d 个（%s）" % (len(out), "、".join(c["name"] for c in out[:4])))
        return {"ok": True, "combos": out}

    def _rail_live(self) -> dict[int, tuple[int, str]]:
        """名单里各房间的最新开播状态和直播标题（10 秒节流重读一次）。

        监控台每轮抓取都会把 live_status 写进 subject_state，但本工具只在启动时读一次；
        不在舞台上的房间没有 info 刷新，于是会一直显示很久以前的状态（下播了还亮 LIVE）。
        """
        now = time.time()
        if now - getattr(self, "_rail_at", 0.0) < 10.0:
            return getattr(self, "_rail_map", {})
        try:
            fresh = config.load_rail_live()
        except Exception:  # noqa: BLE001
            fresh = {}
        # 采集进程没在跑时这份状态是旧的，别拿它盖掉自己轮询到的结果
        own = self._own_status()
        with self.lock:
            if fresh:
                self._rail_at = now
                self._rail_map = fresh
                if not own:
                    for rid, (status, _) in fresh.items():
                        e = self.rooms.get(rid)
                        if e is not None and e.from_db:
                            e.live_status = status
        return getattr(self, "_rail_map", {})

    def rooms_payload(self) -> list[dict]:
        rail = self._rail_live()
        out = []
        with self.lock:
            entries = list(self.rooms.values())
            active = set(self.active)
            listening = set(self.listening)
            hidden = set(self.hidden)
        for e in entries:
            if e.room_id in hidden:
                continue
            # 临时房间移出舞台后仍然保留在列表里，只有手动删除才消失
            info = self.info.get(e.room_id) or {}
            anchor = self._anchors.get(e.uid) or {}
            saved_status, saved_title = rail.get(e.room_id, (e.live_status, ""))
            title = info.get("title") or saved_title
            if e.room_id in active and info:
                # 舞台上的房间用刚拉到的信息
                live = int(info.get("live_status") or 0)
            elif e.from_db:
                # 不在舞台上的房间平时以监控台 subject_state 为准，别用很久以前的 info 缓存；
                # 采集进程没在跑时那个值不会更新，改用自己的轮询结果（见 _own_status）
                if self._own_status():
                    live = int(info.get("live_status", e.live_status) or 0)
                else:
                    live = int(saved_status or 0)
                    if e.room_id not in listening:
                        title = saved_title or title
            else:
                live = int(info.get("live_status", e.live_status) or 0)
            out.append({
                "uid": e.uid, "name": info.get("uname") or anchor.get("uname") or e.name,
                "room_id": e.room_id, "grp": e.grp, "enabled": e.enabled,
                "live_status": live, "title": title,
                "cover": info.get("cover") or "",
                "face": info.get("face") or anchor.get("face") or "",
                "online": int(info.get("online") or 0),
                "active": e.room_id in active,
                "listening": e.room_id in listening,
                "temp": not e.from_db,
                "in_db": e.from_db,
            })
        out.sort(key=lambda r: (-r["active"], -int(r["live_status"] == 1), r["grp"], r["name"]))
        return out

    # ---------- 印象色 ----------
    def colors_payload(self) -> dict[str, dict]:
        """房间印象色（只含设过主色的房间）：前端弹窗回显用，房间列表本身不按它着色。"""
        with self.lock:
            return {str(r): dict(c) for r, c in self.colors.items() if c.get("main")}

    def set_room_color(self, room_id: int, main: str = "", second: str = "",
                       on: bool = True, clear: bool = False) -> dict:
        """设置/关闭/清除房间印象色。

        开启时主色必填、辅色选填；关闭（on=False）只置开关、保留种子，方便再打开；
        清除（clear=True）才把这条记录整条删掉。
        """
        room_id = int(room_id)
        with self.lock:
            if clear:
                self.colors.pop(room_id, None)
            else:
                m = normalize_color(main)
                s = normalize_color(second)
                if s and s == m:
                    s = ""
                if on and not m:
                    raise DeckError("开启印象色需要先填主色", 400)
                if not m:
                    self.colors.pop(room_id, None)
                else:
                    self.colors[room_id] = {"main": m, "second": s, "on": bool(on)}
            saved = dict(self.colors[room_id]) if room_id in self.colors else None
        self._save_state()
        return {"ok": True, "room": room_id, "color": saved}

    # ---------- 分组顺序 ----------
    def set_group_order(self, order) -> dict:
        """保存左栏分组的显示顺序（自上而下）。

        只记「分组名」的先后，不记房间：分组是 subject.grp 上的字符串，房间换组不影响顺序。
        不认识 / 暂时没有房间的分组名也先留着——以后新建同名分组会直接出现在这个位置；
        渲染时再和当前名单里真实存在的分组合并，缺的补在后面。
        """
        if not isinstance(order, list):
            raise DeckError("order 必须是数组")
        out: list[str] = []
        for g in order[:MAX_GROUPS]:
            name = clean_text(g, "").strip() if isinstance(g, str) else ""
            if not name:
                continue
            name = name[:GROUP_NAME_MAX]
            if name not in out:
                out.append(name)
        with self.lock:
            self.group_order = out
        self._save_state()
        return {"ok": True, "order": out}

    # ---------- 选台 ----------
    def set_active(self, rooms: list[int] | list[str]) -> dict:
        want: list[int] = []
        seen = set()
        with self.lock:
            known = set(self.rooms)
            # 之前舞台是不是空的：空舞台时引擎按设计没起，这次加房要立刻连（见下面的 kick）
            was_empty = not self.active and not self.listening
        for r in rooms or []:
            try:
                rid = int(r)
            except (TypeError, ValueError):
                continue
            if rid in seen:
                continue
            if rid not in known:
                # 允许直接填房间号（不在监控名单里也能看）
                with self.lock:
                    self.rooms[rid] = config.RoomEntry(
                        uid=0, name="房间 %d" % rid, room_id=rid, grp="临时")
                known.add(rid)
            seen.add(rid)
            want.append(rid)
            if len(want) >= MAX_ROOMS:
                break
        with self.lock:
            removed = [r for r in self.active if r not in seen]
            self.active = want
            # 同一房间不能同时在舞台和「只听」列表里：上舞台就自动退出只听
            self.listening = [r for r in self.listening if r not in seen]
            for r in removed:
                self.hub.reset_room(r)
                # 下屏房间的取流地址 / 弹幕 token / 表情包缓存也一起清掉（重新上屏会重新取）
                with self.lock:
                    for k in [k for k in self._play if k[0] == r]:
                        self._play.pop(k, None)
                    self._danmu.pop(r, None)
                    self._emo.pop(r, None)
            self.hidden -= set(want)          # 重新放上舞台的房间不再隐藏
            fresh = [r for r in want if r not in self.info]
        self._save_state()
        self.prune_streams()
        self._dirty = True
        kick = getattr(self, "_engine_kick", None)
        publish = getattr(self, "_publish_engine", None)
        if was_empty and want and kick is not None:
            # 空舞台 → 立刻起引擎：不必等下一轮 reconcile（8 秒），页面马上能看到「启动中」
            kick("舞台新增房间")
        else:
            if publish is not None:
                publish()
            threading.Thread(target=self._sync_bridge, kwargs={"force": True}, daemon=True).start()
        if fresh:
            threading.Thread(target=self._prime_info, args=(fresh,), daemon=True,
                             name="deck-prime").start()
        return {"ok": True, "active": want, "removed": removed}

    def _watched_rooms(self) -> list[int]:
        """正在收弹幕的房间：舞台在前，只听弹幕的在后，去重。"""
        with self.lock:
            out = list(self.active)
            for r in self.listening:
                if r not in out:
                    out.append(r)
        return out

    def set_listening(self, rooms) -> dict:
        """整表替换「只听弹幕」列表：不占舞台格子，也不拉视频流。"""
        if not isinstance(rooms, list):
            raise DeckError("rooms 必须是数组")
        want: list[int] = []
        seen: set[int] = set()
        with self.lock:
            known = set(self.rooms)
            active = set(self.active)
            was_empty = not self.active and not self.listening
        for r in rooms or []:
            try:
                rid = int(r)
            except (TypeError, ValueError):
                continue
            if rid <= 0 or rid in seen or rid in active:
                continue
            if rid not in known:
                # 直接填房间号也能只听，不要求先加入监控名单
                with self.lock:
                    self.rooms[rid] = config.RoomEntry(
                        uid=0, name="房间 %d" % rid, room_id=rid, grp="临时")
                known.add(rid)
            seen.add(rid)
            want.append(rid)
            if len(want) >= MAX_LISTEN:
                break
        with self.lock:
            removed = [r for r in self.listening if r not in seen]
            self.listening = want
            fresh = [r for r in want if r not in self.info]
        for r in removed:
            # 停止只听的房间把缓冲/统计一起清掉，避免旧弹幕继续占内存
            self.hub.reset_room(r)
        self._save_state()
        self._dirty = True
        kick = getattr(self, "_engine_kick", None)
        publish = getattr(self, "_publish_engine", None)
        if was_empty and want and kick is not None:
            kick("新增只听房间")
        else:
            if publish is not None:
                publish()
            threading.Thread(target=self._sync_bridge, kwargs={"force": True}, daemon=True).start()
        if fresh:
            threading.Thread(target=self._prime_info, args=(fresh,), daemon=True,
                             name="deck-prime-listen").start()
        return {"ok": True, "listening": want, "max_listen": MAX_LISTEN}

    def _prime_info(self, rooms: list[int]) -> None:
        """新加的房间立刻拉一次信息，别等 60s 轮询（不然会长时间显示「房间 123」）。"""
        for rid in rooms:
            if self._stop.is_set():
                return
            try:
                info = self.fetch_info(rid)
            except Exception:  # noqa: BLE001
                continue
            self.hub.publish({"type": "room_info", "room": rid, "info": info, "ts": time.time()})

    # ---------- 手填输入：房间号 / 主播 UID / 直播间链接 ----------
    def resolve_room(self, query) -> dict:
        """把用户随手填进去的一串东西解析成一个直播间。

        房间号和主播 UID 都是纯数字，没法靠格式区分，所以按证据强度依次判断：
        监控名单里正好有这个房间号 / uid → 直接用它；否则两边都查一次网络，
        房间的主人 uid 恰好等于输入时说明两种解释是同一个人，根本不必挑；
        剩下真正歧义的（A 的房间号刚好是 B 的 uid）按「谁在播算谁」，都没播按房间号；
        但如果输入正好是某个房间的短号（靓号，例如 6 → 7734200），优先按房间解释——
        那是 B 站自己的入口，用户填 6 想要的就是这个房间，不是 uid=6 的主播。
        """
        hint, num = parse_room_query(query)
        if hint is None:
            raise DeckError("请输入直播间房间号、主播 UID 或直播间链接")
        if hint == "uid":
            return self._resolve_from_uid(num)
        if hint == "room":
            room = self._lookup_room(num)
            if room is None:
                raise DeckError("没找到直播间 %s，可能已注销或填错了" % num, 404)
            return self._resolve_payload(room, "room")
        # 纯数字：先说监控名单，省一次网络请求，也更符合用户本意
        hit = self._db_hit(num)
        if hit is not None:
            e, via = hit
            return self._resolve_payload({"room": e.room_id, "uid": e.uid, "uname": e.name,
                                          "live_status": e.live_status}, via)
        room = self._lookup_room(num)
        raw_user = self._lookup_uid(num)
        user = raw_user
        if user is not None and not user.get("room"):
            user = None          # 这个 uid 没开直播间，解释不成「主播 UID」，只剩房间号一种可能
        if room and user and int(room.get("uid") or 0) == num:
            user = None          # 房间号就是主播 uid：两种填法是同一个人
        if room and user:
            live_room = int(room.get("live_status") or 0) == 1
            live_user = int(user.get("live_status") or 0) == 1
            if int(room.get("short_id") or 0) == num:
                # 靓号：数字本身就是房间短号，按房间解释（真开播的是 uid 时也放 alt）
                return self._resolve_payload(room, "room", alt=_alt_of(user))
            if live_user and not live_room:
                return self._resolve_payload(user, "uid", alt=_alt_of(room))
            return self._resolve_payload(room, "room", alt=_alt_of(user))
        if room is not None:
            return self._resolve_payload(room, "room")
        if user is not None:
            if not user.get("room"):
                raise DeckError("主播 %s 还没有开通直播间" % (user.get("uname") or num), 404)
            return self._resolve_payload(user, "uid")
        if raw_user is not None:
            raise DeckError("主播 %s 还没有开通直播间" % (raw_user.get("uname") or num), 404)
        raise DeckError("没找到这个房间或主播：%s" % num, 404)

    def _resolve_from_uid(self, uid: int) -> dict:
        user = self._lookup_uid(uid)
        if user is None:
            raise DeckError("没找到主播 %s" % uid, 404)
        if not user.get("room"):
            raise DeckError("主播 %s 还没有开通直播间" % (user.get("uname") or uid), 404)
        return self._resolve_payload(user, "uid")

    def _db_hit(self, num: int):
        """监控名单里能直接对上：先按房间号，再按主播 uid。"""
        with self.lock:
            entries = [e for e in self.rooms.values() if e.from_db]
        for e in entries:
            if e.room_id == num:
                return e, "db_room"
        for e in entries:
            if e.uid == num and e.room_id:
                return e, "db_uid"
        return None

    def _lookup_room(self, room_id: int):
        """房间号（也接受短号）→ 房间信息；不存在返回 None。"""
        try:
            body = self.http.get(ROOM_INFO % int(room_id)).json()
        except Exception:  # noqa: BLE001
            return None
        if body.get("code") != 0:
            return None
        d = body.get("data") or {}
        return {"room": int(d.get("room_id") or room_id),
                "short_id": int(d.get("short_id") or 0),
                "uid": int(d.get("uid") or 0),
                "uname": clean_text(d.get("uname")) or "",
                "live_status": int(d.get("live_status") or 0),
                "title": clean_text(d.get("title")) or ""}

    def _lookup_uid(self, uid: int):
        """主播 uid → {room, uname, live_status}；查不到这个人返回 None。"""
        try:
            body = self.http.get(MASTER_INFO % int(uid)).json()
        except Exception:  # noqa: BLE001
            return None
        if body.get("code") != 0:
            return None
        data = body.get("data") or {}
        info = data.get("info") or {}
        name = clean_text(info.get("uname"))
        room = 0
        try:
            old = self.http.get(MASTER_ROOM % int(uid)).json()
            if old.get("code") == 0:
                room = int((old.get("data") or {}).get("roomid") or 0)
        except Exception:  # noqa: BLE001
            room = 0
        if not room:
            room = int(data.get("room_id") or info.get("room_id") or 0)
        if not name and not room:
            return None
        live, title = 0, ""
        if room:
            rinfo = self._lookup_room(room)
            if rinfo:
                live = int(rinfo.get("live_status") or 0)
                title = rinfo.get("title") or ""
        return {"room": room, "uid": int(uid), "uname": name or "",
                "live_status": live, "title": title}

    def _resolve_payload(self, room: dict, via: str, alt=None) -> dict:
        rid = int(room.get("room") or 0)
        if rid <= 0:
            raise DeckError("没找到这个主播的直播间", 404)
        info = {}
        try:
            info = self.fetch_info(rid)      # 顺带写好昵称/头像/开播状态，页面不用再等轮询
        except DeckError:
            info = {}
        uid = int(room.get("uid") or info.get("uid") or 0)
        name = (info.get("uname") or "") or clean_text(room.get("uname"))
        if not name and uid:
            name = (self._anchor(uid) or {}).get("uname") or ""
        return {"ok": True, "room": rid, "uid": uid, "uname": clean_text(name) or "",
                "live_status": int(info.get("live_status", room.get("live_status") or 0) or 0),
                "title": info.get("title") or room.get("title") or "",
                "via": via, "alt": alt}

    # ---------- 名单操作 ----------
    def set_group(self, room_id: int, grp: str) -> dict:
        room_id = int(room_id)
        grp = (grp or "").strip()
        with self.lock:
            e = self.rooms.get(room_id)
        if e is None or not e.uid:
            raise DeckError("只有监控名单里的直播间才能设置分组", 400)
        if len(grp) > 40:
            raise DeckError("分组名最多 40 个字符")
        if not config.set_group(room_id, grp):
            raise DeckError("写入监控台分组失败", 502)
        with self.lock:
            e.grp = grp
        return {"ok": True, "room": room_id, "grp": grp, "groups": config.list_groups()}

    def add_room_db(self, room_id: int) -> dict:
        """把舞台上的临时房间加入监控台名单（subject 表），之后就能设分组/停用/删除。"""
        room_id = int(room_id)
        with self.lock:
            e = self.rooms.get(room_id)
        if e is None:
            raise DeckError("舞台上没有这个房间", 404)
        if e.from_db:
            return {"ok": True, "room": room_id, "already": True}
        if not e.uid:
            try:
                self.fetch_info(room_id)
            except DeckError:
                pass
            with self.lock:
                e = self.rooms.get(room_id)
        if not e or not e.uid:
            raise DeckError("拿不到这个直播间的主播 uid，稍后再试", 502)
        if not config.add_subject(e.uid, e.name, room_id, e.grp if e.grp != "临时" else ""):
            raise DeckError("写入监控台名单失败", 502)
        self.load_rooms()
        with self.lock:
            self.hidden.discard(room_id)
            e2 = self.rooms.get(room_id)
            if e2 is not None:
                e2.from_db = True
                e2.grp = e2.grp or ""
        self._save_state()
        return {"ok": True, "room": room_id, "groups": config.list_groups()}

    def remove_room(self, room_id: int, mode: str = "hide") -> dict:
        """mode: hide=仅本工具隐藏 / disable=监控台停用 / delete=从监控台名单删除。"""
        room_id = int(room_id)
        mode = (mode or "hide").strip()
        if mode not in ("hide", "disable", "delete"):
            raise DeckError("未知操作：%s" % mode)
        with self.lock:
            e = self.rooms.get(room_id)
        in_db = bool(e and e.from_db)
        if mode == "disable" and not in_db:
            raise DeckError("临时房间只能从列表里删除", 400)
        if mode == "disable" and not config.set_enabled(room_id, False):
            raise DeckError("停用失败：监控台名单里没有这个房间", 502)
        if mode == "delete" and in_db and not config.delete_subject(room_id):
            raise DeckError("删除失败：监控台名单里没有这个房间", 502)
        with self.lock:
            if mode == "delete":
                self.rooms.pop(room_id, None)
                self.hidden.discard(room_id)
            else:
                self.hidden.add(room_id)
                if e is not None and mode == "disable":
                    e.enabled = False
            self.active = [x for x in self.active if x != room_id]
            self.listening = [x for x in self.listening if x != room_id]
            self.info.pop(room_id, None)
        self.hub.reset_room(room_id)
        self._save_state()
        self._dirty = True
        threading.Thread(target=self._sync_bridge, kwargs={"force": True}, daemon=True).start()
        return {"ok": True, "room": room_id, "mode": mode, "hidden": len(self.hidden)}

    def unhide_rooms(self) -> dict:
        with self.lock:
            self.hidden.clear()
        self._save_state()
        return {"ok": True, "hidden": 0}


# 手填输入里允许的几种写法：直播间链接、主播空间链接、带前缀的数字、纯数字
_ROOM_URL_RE = re.compile(r"live\.bilibili\.com/(?:blanc/|h5/)?(\d{1,16})", re.I)
_SPACE_URL_RE = re.compile(r"space\.bilibili\.com/(\d{1,16})", re.I)
_ROOM_PREFIX_RE = re.compile(r"^(?:直播间?号?|房间号?|room|rid)\s*[:：#]?\s*(\d{1,16})$", re.I)
_UID_PREFIX_RE = re.compile(r"^(?:uid|up|主播\s*(?:id|uid)?)\s*[:：#]?\s*(\d{1,16})$", re.I)
_DIGITS_RE = re.compile(r"^(\d{1,16})$")


def parse_room_query(text):
    """用户填的一行字 → (hint, 数字)。hint 是 "room" / "uid" / ""（纯数字，两种都可能）。

    认不出来返回 (None, 0)。这里的 hint 只是「用户明说的线索」，
    纯数字到底当房间号还是 uid，交给 resolve_room 按证据判断。
    """
    s = clean_text(text, "").strip().strip("#").strip()
    if not s:
        return None, 0
    m = _SPACE_URL_RE.search(s)
    if m:
        return "uid", int(m.group(1))
    m = _ROOM_URL_RE.search(s)
    if m:
        return "room", int(m.group(1))
    m = _UID_PREFIX_RE.match(s)
    if m:
        return "uid", int(m.group(1))
    m = _ROOM_PREFIX_RE.match(s)
    if m:
        return "room", int(m.group(1))
    m = _DIGITS_RE.match(s)
    if m:
        return "", int(m.group(1))
    return None, 0


def _alt_of(room: dict) -> dict:
    """另一种解释，给页面提示「也可能是指 …」。"""
    return {"room": int(room.get("room") or 0), "uname": clean_text(room.get("uname")) or ""}
