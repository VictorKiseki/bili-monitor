"""按用户名批量解析 UID，并写入 config/subjects.json。

搜索接口（x/web-interface/wbi/search/type）实测无需登录，只要 WBI + buvid3。
匹配策略：全等 > 前缀 > 粉丝数最高；低置信度的会标出来让你人工确认。

用法：
    python scripts/import_by_names.py --names-file names.txt --dry-run
    python scripts/import_by_names.py --names-file names.txt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector.bili import API, BiliClient  # noqa: E402


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def clean(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def search(c: BiliClient, kw: str) -> list[dict]:
    body = c._get(
        f"{API}/x/web-interface/wbi/search/type",
        {"search_type": "bili_user", "keyword": kw, "page": 1, "page_size": 10},
        wbi=True, allow_code=(-352, -412, -799),
    )
    return (body.get("data") or {}).get("result") or []


def pick(kw: str, cands: list[dict]) -> tuple[dict | None, str]:
    if not cands:
        return None, "none"
    nk = norm(kw)
    for r in cands:
        if norm(clean(r.get("uname", ""))) == nk:
            return r, "exact"
    for r in cands:
        n = norm(clean(r.get("uname", "")))
        if n and (n.startswith(nk) or nk.startswith(n)):
            return r, "prefix"
    return cands[0], "fuzzy"


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--names-file", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    raw = Path(args.names_file).read_text("utf-8").splitlines()
    names = [x.strip() for x in raw if x.strip() and not x.strip().startswith("#")]
    print(f"待解析 {len(names)} 个名字")

    rows: list[dict] = []
    with BiliClient() as c:
        for i, kw in enumerate(names, 1):
            try:
                cands = search(c, kw)
            except Exception as e:  # noqa: BLE001
                print(f"{i:2}. {kw}  搜索失败：{str(e)[:60]}")
                rows.append({"kw": kw, "mid": 0, "uname": "", "conf": "error", "fans": 0})
                continue
            r, conf = pick(kw, cands)
            if not r:
                print(f"{i:2}. {kw}  未找到")
                rows.append({"kw": kw, "mid": 0, "uname": "", "conf": "none", "fans": 0})
                continue
            uname = clean(r.get("uname", ""))
            mid = int(r.get("mid") or 0)
            fans = int(r.get("fans") or 0)
            flag = "" if conf == "exact" else f"  <-- {conf} 需确认"
            print(f"{i:2}. {kw:22} -> {mid:14} {uname}  fans={fans}{flag}")
            rows.append({"kw": kw, "mid": mid, "uname": uname, "conf": conf, "fans": fans})

        uids = [r["mid"] for r in rows if r["mid"]]
        live = c.fetch_live_batch(uids) if uids else {}
        print("")
        print("补直播间信息完成")

    subjects = []
    for r in rows:
        if not r["mid"]:
            continue
        st = live.get(r["mid"])
        room = st.room_id if st else 0
        note = r["uname"] + (f"，房间 {room}" if room else "，无直播间")
        if r["conf"] != "exact":
            note += f"（搜索匹配 {r['conf']}）"
        subjects.append({"uid": r["mid"], "note": note})

    miss = [r["kw"] for r in rows if not r["mid"]]
    print("")
    print(f"解析成功 {len(subjects)}/{len(rows)}")
    if miss:
        print("没解析到：" + "、".join(miss))

    if args.dry_run:
        print("--dry-run：未写 config")
        return 0

    cfg_path = ROOT / "config" / "subjects.json"
    old = json.loads(cfg_path.read_text("utf-8")) if cfg_path.exists() else {}
    cfg = {
        "_说明": "来自 B 站关注分组截图（psplive 相关主播）。uid 是 B 站 UID（不是直播间号）。",
        "subjects": subjects,
        "poll": old.get("poll") or {"live_interval_s": 45, "dynamic_interval_s": 240},
    }
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"已写入 {cfg_path.relative_to(ROOT)}：{len(subjects)} 个主播")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
