"""一次性工具：把某个 B 站「关注分组」里的主播批量导入 config/subjects.json。

该接口需要登录态（不带 SESSDATA 会返回 -101 账号未登录），
所以本脚本只在你本地跑一次，不会把 SESSDATA 写进任何文件或日志。

用法（推荐：先存到文件，跑完删掉）：
    # 1. 把 SESSDATA 的值存进 .sessdata（一行，只写值，不要 SESSDATA= 前缀）
    python scripts/import_follows.py --mid 274172 --tagid 344518481 --sessdata-file .sessdata
    # 2. 确认 config/subjects.json 已更新后删除
    del .sessdata

也可以走环境变量（PowerShell）：
    $env:BILI_SESSDATA = 你的SESSDATA值
    python scripts/import_follows.py --mid 274172 --tagid 344518481

只想看看这个分组里有哪些人、先不写文件：加 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector.bili import API, BiliClient  # noqa: E402


def read_sessdata(args) -> str:
    if args.sessdata:
        return args.sessdata.strip()
    if args.sessdata_file:
        return Path(args.sessdata_file).read_text("utf-8").strip()
    return os.environ.get("BILI_SESSDATA", "").strip()


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", type=int, default=274172, help="分组所属的账号 UID")
    ap.add_argument("--tagid", type=int, default=344518481, help="关注分组的 tagid")
    ap.add_argument("--sessdata", help="SESSDATA 的值（不推荐，会进 shell 历史）")
    ap.add_argument("--sessdata-file", help="存放 SESSDATA 的文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写 config")
    ap.add_argument("--limit", type=int, default=30, help="最多写入多少个（默认 30）")
    args = ap.parse_args()

    sess = read_sessdata(args)
    if not sess:
        print("缺少 SESSDATA。请用 --sessdata-file 指向一个只含该值的文件。")
        return 2

    with BiliClient() as c:
        b3 = c._ensure_buvid3()
        c.http.headers["Cookie"] = f"SESSDATA={sess}; buvid3={b3}"
        c.http.headers["Referer"] = f"https://space.bilibili.com/{args.mid}/relation/follow"

        # 1. 先确认分组存在、拿到分组名
        tag_name = ""
        try:
            body = c._get(f"{API}/x/relation/tags", {"mid": args.mid}, allow_code=(-101,))
            if body.get("code") == -101:
                print("SESSDATA 无效或已过期（接口返回 -101 账号未登录）。")
                return 3
            for t in body.get("data") or []:
                if int(t.get("tagid") or 0) == args.tagid:
                    tag_name = t.get("name") or ""
        except Exception as e:  # noqa: BLE001
            print(f"取分组列表失败：{e}")

        # 2. 取分组内成员
        body = c._get(f"{API}/x/relation/tag", {"mid": args.mid, "tagid": args.tagid},
                      allow_code=(-101, 11010))
        code = body.get("code")
        if code == -101:
            print("SESSDATA 无效或已过期（-101）。")
            return 3
        if code == 11010:
            print(f"这个账号没有 tagid={args.tagid} 的分组（-11010 分组不存在）。")
            return 4
        members = body.get("data") or []
        print(f"分组 {tag_name or args.tagid} 共 {len(members)} 人")

        if not members:
            print("分组是空的，没东西可导入。")
            return 5

        uids = [int(m["mid"]) for m in members if m.get("mid")]
        if len(uids) > args.limit:
            print(f"超过上限 {args.limit}，只取前 {args.limit} 个")
            uids = uids[: args.limit]

        # 3. 用批量接口补齐昵称与直播间号（1 个请求）
        rooms: dict[int, tuple[str, int]] = {}
        try:
            live = c.fetch_live_batch(uids)
            for uid, st in live.items():
                rooms[uid] = (st.uname, st.room_id)
        except Exception as e:  # noqa: BLE001
            print(f"补昵称失败（不影响导入）：{e}")

        names = {int(m["mid"]): (m.get("uname") or "") for m in members if m.get("mid")}
        subjects = []
        for i, uid in enumerate(uids, 1):
            uname = names.get(uid) or rooms.get(uid, ("", 0))[0] or f"uid {uid}"
            room = rooms.get(uid, ("", 0))[1]
            note = uname + (f"，房间 {room}" if room else "，无直播间")
            subjects.append({"uid": uid, "note": note})
            print(f"  {i:2}. {uid:14}  {note}")

        if args.dry_run:
            print("")
            print("--dry-run：未写入 config/subjects.json")
            return 0

        cfg_path = ROOT / "config" / "subjects.json"
        old = json.loads(cfg_path.read_text("utf-8")) if cfg_path.exists() else {}
        cfg = {
            "_说明": f"来自 B 站关注分组 {tag_name or args.tagid} 的导入结果。uid 是 B 站 UID（不是直播间号）。",
            "subjects": subjects,
            "poll": old.get("poll") or {"live_interval_s": 45, "dynamic_interval_s": 180},
        }
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print("")
        print(f"已写入 {cfg_path.relative_to(ROOT)}，共 {len(subjects)} 个主播")
        print("提示：确认无误后删掉 .sessdata；SESSDATA 等同登录凭据，别外传。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
