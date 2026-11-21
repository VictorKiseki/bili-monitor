"""LiveDeck 的房间信息抓取 mixin。"""
from __future__ import annotations

import concurrent.futures
import time

from livedeck import config
from livedeck.deck_base import (
    FACE_REFRESH,
    FACE_TTL,
    FACE_WORKERS,
    MASTER_INFO,
    ROOM_INFO,
    WATCHED_API,
    DeckError,
    clean_text,
)


class DeckInfo:
    # ---------- 主播头像 / 昵称 ----------
    def _prime_faces(self) -> None:
        """补头像/昵称：先用本地缓存渲染，只有缺失或过期的才去拉，且是并发的。

        名单里每个主播一张头像，串行 + 0.12 秒间隔在 30 多个房间时要好几秒才铺满；
        改成小线程池后基本一两秒就能推到页面（拉到一张推一张，不等全部完成）。
        """
        with self.lock:
            uids = sorted({e.uid for e in self.rooms.values() if e.uid})
        now = time.time()
        todo: list[int] = []
        for uid in uids:
            cached = self._faces.get(uid) or {}
            if not cached.get("face") or now - float(cached.get("ts") or 0.0) > FACE_REFRESH:
                todo.append(uid)
        if not todo:
            return
        saved = False
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=FACE_WORKERS,
                                                     thread_name_prefix="deck-face")
        try:
            futures = {pool.submit(self._fetch_anchor, uid): uid for uid in todo}
            for fut in concurrent.futures.as_completed(futures):
                if self._stop.is_set():
                    break
                uid = futures[fut]
                try:
                    out = fut.result()
                except Exception:  # noqa: BLE001
                    continue
                if not out.get("face"):
                    continue        # 拉失败就不写缓存，下一轮再试
                saved = True
                self._announce_face(uid, out)
        finally:
            pool.shutdown(wait=False)
        if saved:
            self._save_faces()

    def _fetch_anchor(self, uid: int) -> dict:
        """真去拉一次主播资料；失败或没数据就返回空表（不落缓存，留给下一轮重试）。"""
        if not uid:
            return {}
        try:
            body = self.http.get(MASTER_INFO % uid).json()
            info = (body.get("data") or {}).get("info") or {}
            out = {"uname": clean_text(info.get("uname")), "face": info.get("face") or ""}
        except Exception:  # noqa: BLE001
            return {}
        if not (out["face"] or out["uname"]):
            return {}
        with self.lock:
            self._faces[uid] = {"face": out["face"], "uname": out["uname"], "ts": time.time()}
            self._anchors[uid] = dict(out)
            for e in self.rooms.values():
                if e.uid == uid and (not e.name or e.name.startswith("房间 ")):
                    e.name = out["uname"] or e.name
        return out

    def _anchor(self, uid: int) -> dict:
        """主播昵称 + 头像：room/get_info 不返回这些，用 Master/info 补。

        命中顺序：内存 → 本地文件缓存（FACE_TTL 内直接用，秒开）→ 网络。
        网络失败不写任何缓存：一次抖动不该把头像一个永久空白钉死。
        """
        if not uid:
            return {}
        with self.lock:
            hit = self._anchors.get(uid)
            if hit is not None:
                return hit
            cached = self._faces.get(uid) or {}
            if cached.get("face") and time.time() - float(cached.get("ts") or 0.0) <= FACE_TTL:
                out = {"uname": cached.get("uname") or "", "face": cached.get("face") or ""}
                self._anchors[uid] = out
                return out
        out = self._fetch_anchor(uid)
        if out:
            self._save_faces()
        return out

    def _announce_face(self, uid: int, out: dict) -> None:
        """头像补到后立刻推给页面（SSE room_face），不用等 12 秒轮询。"""
        with self.lock:
            rids = [e.room_id for e in self.rooms.values() if e.uid == uid]
        for rid in rids:
            self.hub.publish({"type": "room_face", "room": rid, "uid": uid,
                              "face": out.get("face") or "", "uname": out.get("uname") or "",
                              "ts": time.time()})

    def _save_faces(self) -> None:
        """落盘头像缓存（剪掉很久没用的），失败不影响功能。"""
        now = time.time()
        with self.lock:
            snap = {uid: dict(v) for uid, v in self._faces.items()
                    if v.get("face") and now - float(v.get("ts") or 0.0) <= 60 * 86400}
        try:
            config.save_faces(snap)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 房间信息 ----------
    def fetch_info(self, room_id: int) -> dict:
        try:
            r = self.http.get(ROOM_INFO % room_id)
            body = r.json()
        except Exception as e:  # noqa: BLE001
            raise DeckError("请求直播间信息失败：%s" % e.__class__.__name__, 502)
        if body.get("code") != 0:
            raise DeckError("直播间信息 code=%s" % body.get("code"), 502)
        d = body.get("data") or {}
        real = int(d.get("room_id") or room_id)
        uid = int(d.get("uid") or 0)
        anchor = self._anchor(uid) if uid else {}
        out = {
            "room_id": real, "short_id": int(d.get("short_id") or 0),
            "uid": uid,
            "uname": clean_text(d.get("uname")) or anchor.get("uname") or "",
            "face": anchor.get("face") or "",
            "title": d.get("title") or "", "live_status": int(d.get("live_status") or 0),
            "online": int(d.get("online") or 0),
            "watched": 0,
            "watched_kind": "",
            "guards": 0,
            "cover": d.get("user_cover") or d.get("keyframe") or "",
            "live_time": d.get("live_time") or "", "area_name": d.get("area_name") or "",
            "description": d.get("description") or "",
        }
        # 房间页展示给观众的「看过」人数（累计观看），get_info 没有，需要 WBI 的 getInfoByRoom
        if out["live_status"] == 1:
            w = self._watched_info(real)
            out["watched"] = w.get("num") or 0
            out["watched_kind"] = w.get("kind") or ""
            out["guards"] = int(w.get("guard") or 0)
        with self.lock:
            self.info[real] = out
            if real != room_id:
                self.info[room_id] = out
            e = self.rooms.get(room_id)
            if e is not None:
                e.live_status = out["live_status"]
                e.uid = uid or e.uid
                e.face = out["face"] or e.face
                e.cover = out["cover"] or e.cover
                if out["uname"] and (not e.name or e.name.startswith("房间 ")):
                    e.name = out["uname"]
        return out

    def _watched_info(self, real: int) -> dict:
        """房间展示的观看数字 + 大航海总人数。

        B 站对每个房间只展示一种口径（watched_show.switch）：
        switch=true -> 「N 万人看过」（累计观看）
        switch=false -> 「N 万人气」（就是 online）
        没有公开的「当前在线人数」接口，所以按房间实际口径取，别把人气标成看过。

        同一次请求里顺带取 guard_info.count（大航海：舰长 + 提督 + 总督），
        房间页顶部「大航海(N)」就是这个数；弹幕流没有总数，只有 GUARD_BUY 的增加量。
        """
        try:
            params = self._client._sign({"room_id": int(real)})
            body = self.http.get(WATCHED_API, params=params).json()
        except Exception:  # noqa: BLE001
            return {}
        if body.get("code") != 0:
            return {}
        data = body.get("data") or {}
        ws = data.get("watched_show") or {}
        try:
            num = int(ws.get("num") or 0)
        except (TypeError, ValueError):
            num = 0
        # 大航海（舰长/提督/总督）总人数：房间页「大航海(N)」就是这个数。
        # 弹幕流里没有总数（只有 GUARD_BUY 的增加量和每个人的 guard_level），只能在这里取。
        try:
            guard = int((data.get("guard_info") or {}).get("count") or 0)
        except (TypeError, ValueError):
            guard = 0
        if not num and not guard:
            return {}
        return {"num": num, "kind": ("watched" if ws.get("switch") else "popularity") if num else "",
                "guard": guard}

    # ---------- 采集进程状态 ----------
    def _own_status(self) -> bool:
        """名单里不在舞台的房间，开播状态要不要本工具自己轮询。

        正常由监控台采集进程写 subject_state，本工具只读；但采集没绑定 / 没启动时，
        那个值会一直停在旧状态（下播了还亮 LIVE）。读 data/stack.json 就知道采集在不在跑；
        读不到（只起了监控室、没起监控台）时保守地自己轮询——多几个小请求，好过显示错状态。
        """
        now = time.time()
        with self.lock:
            if self._own_at and now - self._own_at < 20:
                return self._own_val
        alive = config.collector_alive()
        own = True if alive is None else (not alive)
        with self.lock:
            self._own_at = now
            self._own_val = own
        return own

    def _info_watch_list(self) -> dict[int, str]:
        """要轮询房间信息的房间 → 刷新档位。

        stage 在舞台 / 只听弹幕：在播 20 秒、未开播 60 秒；
        rail  临时房间（手填房间号，不在监控名单里）：数据库没有兜底，必须自己刷，30/90；
        db    监控名单房间但采集进程没在跑：自己刷，60/180。

        采集进程正常时，名单房间的开播状态由它写 subject_state，本工具不重复拉。
        """
        own = self._own_status()
        with self.lock:
            out: dict[int, str] = {r: "stage" for r in self.active}
            # 只听弹幕的房间也要保持昵称/开播状态新鲜，且不被下面的临时房上限挤掉
            for r in self.listening:
                out.setdefault(r, "stage")
            hidden = set(self.hidden)
            for rid, e in self.rooms.items():
                if rid in out or rid in hidden:
                    continue
                if e.from_db:
                    if own:
                        out[rid] = "db"
                else:
                    out[rid] = "rail"
        # 不在舞台的房间最多轮询这么多个，避免名单很长时把 API 打爆
        extra = [r for r, kind in out.items() if kind != "stage"][60:]
        for r in extra:
            out.pop(r, None)
        return out

    _INFO_NEXT = {"stage": (20, 60), "rail": (30, 90), "db": (60, 180)}

    def _refresh_info_loop(self) -> None:
        """按房间到期时间刷新，间隔按档位（见 _info_watch_list）。"""
        due: dict[int, float] = {}
        while not self._stop.wait(1):
            now = time.time()
            watch = self._info_watch_list()
            for r in list(due):
                if r not in watch:
                    due.pop(r, None)
            for rid in list(watch):
                if self._stop.is_set():
                    return
                if due.get(rid, 0.0) > now:
                    continue
                try:
                    info = self.fetch_info(rid)
                except DeckError:
                    due[rid] = time.time() + 15   # 拉取失败稍后重试，别把旧状态一直挂着
                    continue
                self.hub.publish({"type": "room_info", "room": rid, "info": info, "ts": time.time()})
                live = info.get("live_status") == 1
                on_iv, off_iv = self._INFO_NEXT.get(watch.get(rid, "rail"), (30, 90))
                due[rid] = time.time() + (on_iv if live else off_iv)
                time.sleep(0.15)
