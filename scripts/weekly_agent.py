#!/usr/bin/env python3
"""日程识别 Harness 交接的命令行（2026-11-08）。

产品侧只负责闸门与文件交接，读图交给 DSH 会话（手动或定时自动化）：

    python scripts/weekly_agent.py status                  # 开关/时段/队列/上次结果/任务统计
    python scripts/weekly_agent.py export --limit 6 --dry-run
    python scripts/weekly_agent.py export                  # 把待判候选写进 out/ai_tasks/
    python scripts/weekly_agent.py ingest                  # 回收 out/ai_results/ 并入库

不配置、不调用任何模型服务；任务文件与结果文件的契约见 core/weekly/harness.py。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.weekly import agent as agentmod  # noqa: E402


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/bot.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="开关/时段/队列/上次结果/任务统计")
    p_exp = sub.add_parser("export", help="把待判候选导出成任务包（out/ai_tasks）")
    p_exp.add_argument("--limit", type=int, default=0)
    p_exp.add_argument("--dry-run", action="store_true")
    p_ing = sub.add_parser("ingest", help="回收 out/ai_results 并入库")
    p_ing.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = agentmod.open_conn(args.db)

    if args.cmd == "status":
        sp = agentmod.status(conn)
        sp["gate"] = agentmod.gate(conn)
        sp["slots_due"] = agentmod.slot_due(conn)
        _print(sp)
        return 0

    if args.cmd == "export":
        res = agentmod.harness_export(conn, limit=(args.limit or None),
                                      dry_run=bool(args.dry_run),
                                      log=lambda m: print(m, file=sys.stderr))
        _print(res)
        return 0 if res.get("ok", True) else 1

    if args.cmd == "ingest":
        res = agentmod.harness_ingest(conn, dry_run=bool(args.dry_run),
                                      log=lambda m: print(m, file=sys.stderr))
        _print(res)
        return 0 if not res.get("problems") else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
