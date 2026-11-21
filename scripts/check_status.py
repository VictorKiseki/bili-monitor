"""手动测试：拉取主播的直播状态与最新动态（不推送、不落库）。

用法：
  python scripts/check_status.py
  python scripts/check_status.py --uids <UID1>,<UID2> --rounds 3 --interval 20
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector.bili import BiliClient, BiliError  # noqa: E402


def load_subjects(path: Path) -> tuple[list[int], dict[int, str]]:
    """返回 (uid 列表, uid->名字) 。名字只用于展示，接口没回 uname 时兜底。"""
    if not path.exists():
        return [], {}
    data = json.loads(path.read_text("utf-8"))
    subs = data.get("subjects", [])
    uids = [int(s["uid"]) for s in subs]
    names = {int(s["uid"]): (s.get("name") or s.get("note") or "") for s in subs}
    return uids, names


def fmt_time(ts: int) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def run_round(client: BiliClient, uids: list[int], dyn_limit: int,
              names: dict[int, str] | None = None) -> list[dict]:
    names = names or {}
    rows: list[dict] = []
    try:
        live = client.fetch_live_batch(uids)
    except BiliError as e:
        live = {}
        print(f"  !!! 批量取直播状态失败：{e.code} {e.message}")

    for uid in uids:
        row: dict = {"uid": uid, "name": names.get(uid, "")}
        st = live.get(uid)
        if st:
            row.update(room_id=st.room_id, live_status=st.live_status,
                       live_text=st.status_text, uname=st.uname,
                       live_title=st.title, online=st.online,
                       live_time=st.live_time, area=st.area_name)
        else:
            try:
                info = client.get_room_by_mid(uid)
                row.update(room_id=int(info.get("roomid") or 0),
                           live_status=int(info.get("liveStatus") or 0),
                           live_title=info.get("title") or "",
                           uname=info.get("uname") or "")
                row["live_text"] = {0: "未开播", 1: "直播中", 2: "轮播"}.get(row["live_status"], "?")
            except BiliError as e:
                row["error"] = f"直播 {e.code} {e.message}"

        try:
            dyns, _, retried = client.fetch_dynamics(uid, limit=dyn_limit)
            row["dyn_retries"] = retried
            row["dyn_dropped_rcmd"] = client.last_dropped_rcmd
            row["dynamics"] = [
                {"id_str": d.id_str, "subtype": d.subtype, "pub_ts": d.pub_ts,
                 "title": d.title, "summary": d.summary, "url": d.url,
                 "image_count": d.image_count}
                for d in dyns
            ]
        except BiliError as e:
            row["dyn_error"] = f"动态 {e.code} {e.message}"
        # 置顶动态会无视时间顺序出现在首条；标记出来，避免把它当成"最新一条"去推送
        ds = row.get("dynamics") or []
        if len(ds) >= 2 and ds[0]["pub_ts"] < max(d["pub_ts"] for d in ds[1:]):
            row["pinned_first"] = True
            ds[0]["pinned"] = True
        rows.append(row)
    return rows


def print_round(rows: list[dict], i: int, total: int) -> None:
    print(f"\n{'=' * 80}")
    print(f"第 {i}/{total} 轮    {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    for r in rows:
        who = r.get("uname") or r.get("name") or ("uid " + str(r["uid"]))
        head = f"[{r.get('live_text', '?'):4}] {who}"
        print(f"{head}    room={r.get('room_id')}  uid={r['uid']}  人气={r.get('online', 0)}")
        if r.get("live_title"):
            print(f"          标题：{r['live_title']}   分区：{r.get('area', '-')}")
        if r.get("error"):
            print(f"          !! {r['error']}")
        if r.get("dyn_error"):
            print(f"          !! {r['dyn_error']}")
        for d in (r.get("dynamics") or [])[:3]:
            label = d["title"] or d["summary"][:48] or "(无正文)"
            if d.get("pinned"):
                label = "【置顶】" + label
            pics = f" [{d['image_count']}图]" if d.get("image_count") else ""
            print(f"          - [{d['subtype']:7}] {fmt_time(d['pub_ts'])}  {label}{pics}")
            print(f"                     id_str={d['id_str']}")
        if r.get("dyn_dropped_rcmd"):
            print(f"          (已过滤 {r['dyn_dropped_rcmd']} 条平台自动开播提醒)")
        if not r.get("dynamics") and not r.get("dyn_error"):
            if r.get("dyn_dropped_rcmd"):
                print("          - (本批只有平台开播提醒，已全部跳过，不算动态)")
            else:
                n = r.get("dyn_retries") or 0
                tag = f"（已重试 {n} 次仍为空，可能该号确实没有动态）" if n else "（首轮即空）"
                print(f"          - (无动态) {tag}")


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--uids", help="逗号分隔 UID，覆盖配置文件")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--dyn-limit", type=int, default=5)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    cfg_uids, names = load_subjects(ROOT / "config" / "subjects.json")
    uids = ([int(x) for x in args.uids.split(",") if x.strip()] if args.uids else cfg_uids)
    if not uids:
        print("没有 UID：请填 config/subjects.json 或用 --uids 传入")
        return 2

    t_all = time.time()
    rounds = []
    with BiliClient() as client:
        for i in range(1, args.rounds + 1):
            t0 = time.time()
            rows = run_round(client, uids, args.dyn_limit, names)
            print_round(rows, i, args.rounds)
            ok = sum(1 for r in rows if not r.get("error"))
            print(f"\n-- 本轮 {ok}/{len(rows)} 成功，耗时 {time.time() - t0:.2f}s，累计请求 {client.request_count}")
            rounds.append({"at": int(time.time()), "rows": rows})
            if i < args.rounds:
                time.sleep(args.interval)

    print(f"\n总计 {time.time() - t_all:.1f}s，HTTP 请求 {client.request_count} 次")
    if args.save:
        out = ROOT / "out" / f"status_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(rounds, ensure_ascii=False, indent=2), "utf-8")
        print(f"已保存 {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
