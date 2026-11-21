"""Harness 交接后端（2026-11-08）：把「读图判日程」交给 DSH 会话，用文件握手。

为什么不直连 DSH：它没有 OpenAI 兼容端点、没有 CLI/无头模式，模型只在「会话」里
可用。所以做成两边都能遵守的契约：

    监控台   ──写──▶ out/ai_tasks/<batch_id>.json    素材 + 图片绝对路径 + 输出 schema
    DSH 会话 ──写──▶ out/ai_results/<batch_id>.json  只输出 JSON 结果
    监控台   ──读──▶ 校验 → core.weekly.ingest 入库 → 两边归档到 done/

同一套契约支撑两个入口：

* A 手动：管理台「导出任务包」把任务文件交给会话，「导入结果」把 JSON 贴回来；
* B 自动：用户在 DSH 里建一条 automation，每 5 分钟扫 tasks 目录、写完 results
  就退出（提示词见 automation_prompt()）。

设计约束：本模块不 import core.weekly.agent（agent 要 import 本模块），也不碰
数据库——校验完把结果交回调用方入库。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

#: 契约版本：字段有破坏性改动时 +1，旧结果文件会被拒绝并提示重新导出
CONTRACT = "bbot.weekly-ai/1"

ROOT = Path(__file__).resolve().parent.parent.parent

#: 任务文件与结果文件的落点（相对项目根）
TASK_SUBDIR = Path("out") / "ai_tasks"
RESULT_SUBDIR = Path("out") / "ai_results"

#: 交给会话的判读规则（与 core.weekly.agent.SYSTEM_PROMPT 同口径，但更啰嗦一点：
#: 会话没有 messages 结构，规则得写在文件里）
INSTRUCTIONS = """你在替「B 站主播日程表」判图。items 里每一项是一条 B 站动态：
material 是素材（主播、发布时间、正文、直播预约卡、OCR 原文、该主播已排场次），
images 是要看的图片绝对路径（可能为空；没有图就只按 material 判断）。

判读规则（务必遵守）：
1. 以图片为准。OCR 只作参考，手写体常读错，与图上明显不符时不要照抄。
2. 预约卡（reserve）上的时间是平台生成的，比较可靠：用它定 date/time，图片与正文补内容。
3. scope 三选一：
   - week：整周日程表（周一到周日多天多场，通常是一张周表图）；
   - single：单场预告（今晚几点播什么、临时加播、改档、请假/取消、开播倒计时）；
   - none：不是日程（闲聊、抽奖、切片、播后感想、道别、招募等）。
4. 必须二次判读：先初判，再把整段动态连同 ctx（该主播已排场次）重读一遍，确认
   「这到底是不是新预告」，结论写进 recheck：confirmed / downgraded / unsure。
   判定不是预告时同时 is_schedule=false、scope="none"。
   典型「不是预告」：播后感想与感谢、收尾道别（明天见/下次见）、只泛泛说「明天也播」
   却没有时间和节目、回应弹幕或吐槽自己的行程。
   典型「是预告」：给出新的时间/节目、改档、临时加播、请假/取消、开播前的倒计时。
5. 时间口径：能看清具体几点就写 HH:MM（time）；只写了模糊时段（上午/中午/下午/傍晚/
   晚上/深夜/凌晨，或「9~12」这种区间）不要替主播编一个钟点——time 留空、
   time_text 填原文（例：time_text="晚上"）。
6. 只输出一个 JSON 对象，不要 markdown 围栏、不要任何解释，写到 output_file 指的路径。

输出格式：
{
  "contract": "bbot.weekly-ai/1",
  "batch_id": "<原样带回>",
  "source": "harness",
  "results": [
    {
      "event_id": "<原样带回>",
      "is_schedule": true/false,
      "scope": "week" | "single" | "none",
      "confidence": 0~1 的小数,
      "week_label": "11.2~11.8" 或空,
      "week_start": "2026-11-02" 或空,
      "days": [ {"day": "周一", "entries": [ {"time": "20:00", "time_text": "", "text": "歌回"} ]} ],
      "sessions": [ {"date": "2026-11-09", "day": "周日", "time": "20:00", "time_text": "",
                     "text": "新衣发布会", "kind": "supplement|override|cancel", "replaces": ""} ],
      "note": "一句话说明依据",
      "recheck": "confirmed" | "downgraded" | "unsure",
      "handwritten": true/false,
      "needs_review": true/false
    }
  ]
}
week 用 days（周一~周日给全部场次）；single 用 sessions；none 时两个数组都给空。
day 只接受 周一~周日。判不了的那条也要给结果（scope="none"、note 写原因），不要漏项。
"""


# ---------------- 目录 ----------------

def _base(root=None) -> Path:
    return Path(root) if root else ROOT


def task_dir(root=None) -> Path:
    return _base(root) / TASK_SUBDIR


def result_dir(root=None) -> Path:
    return _base(root) / RESULT_SUBDIR


def done_dir(kind: str = "tasks", root=None) -> Path:
    base = task_dir(root) if kind == "tasks" else result_dir(root)
    return base / "done"


def new_batch_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


# ---------------- 任务 ----------------

def _abs_path(path: str, root=None) -> str:
    p = Path(str(path or ""))
    if not str(p):
        return ""
    if not p.is_absolute():
        p = _base(root) / p
    return str(p)


def _fallback_material(item: dict) -> str:
    """给没有 agent._material 的调用方（测试/脚本）一个最简素材文本。"""
    lines = ["【主播】%s（uid %s）" % (item.get("uname") or "", item.get("uid") or "")]
    if item.get("title"):
        lines.append("【动态标题】" + str(item["title"]))
    lines.append("【动态正文】" + (str(item.get("text") or "")[:1200] or "（无正文）"))
    return "\n".join(lines)


def build_task(items, root=None, batch_id: str | None = None, material=None) -> dict:
    """把一批候选组装成任务文件内容（纯 dict，不落盘）。

    material 是可调用的素材渲染函数（agent 传自己的 _material，保证两边文案一致）。
    """
    render = material or _fallback_material
    out_items = []
    for item in items or []:
        imgs = [_abs_path((img or {}).get("path") or "", root)
                for img in (item.get("images") or [])]
        out_items.append({"event_id": str(item.get("event_id") or ""),
                          "uid": int(item.get("uid") or 0),
                          "uname": str(item.get("uname") or ""),
                          "posted_at": float(item.get("posted_at") or 0),
                          "images": [p for p in imgs if p],
                          "material": render(item)})
    bid = str(batch_id or new_batch_id())
    return {"contract": CONTRACT, "batch_id": bid, "created_at": int(time.time()),
            "project_root": str(_base(root)), "instructions": INSTRUCTIONS,
            "output_file": str(result_dir(root) / (bid + ".json")),
            "items": out_items}


def write_task(payload: dict, root=None, batch_id: str | None = None) -> Path:
    bid = str(batch_id or payload.get("batch_id") or new_batch_id())
    payload = dict(payload)
    payload["batch_id"] = bid
    d = task_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    p = d / (bid + ".json")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return p


def list_tasks(root=None) -> list[dict]:
    """待处理任务（按创建时间升序）。坏文件跳过并在 reason 里记一笔。"""
    out = []
    d = task_dir(root)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out.append({"batch_id": p.stem, "path": str(p), "created": 0,
                        "count": 0, "error": "%s: %s" % (type(e).__name__, e)})
            continue
        items = data.get("items") or []
        out.append({"batch_id": str(data.get("batch_id") or p.stem), "path": str(p),
                    "created": float(data.get("created_at") or 0),
                    "count": len(items),
                    "event_ids": [str(i.get("event_id") or "") for i in items]})
    out.sort(key=lambda x: x.get("created") or 0)
    return out


def open_event_ids(root=None) -> set[str]:
    """已经躺在未完成任务里的候选（避免同一批候选被反复导出）。"""
    ids: set[str] = set()
    for t in list_tasks(root):
        ids.update(x for x in (t.get("event_ids") or []) if x)
    return ids


def prompt_for(payload: dict, root=None) -> str:
    """把任务文件变成一句能直接粘给 DSH 会话/自动化的提示词。"""
    task_path = str(task_dir(root) / (str(payload.get("batch_id")) + ".json"))
    return ("读取任务文件 %s，按里面 instructions 的规则逐条判图，"
            "把结果写到 output_file（%s）。只写这一个文件，不要改其他文件；"
            "判不了的条目也要给 scope=none 的结果。"
            % (task_path, payload.get("output_file") or ""))


def automation_prompt(root=None) -> str:
    """B 方案：用户建 DSH 定时自动化时粘这段（每 5 分钟扫一次任务目录）。"""
    return ("每 5 分钟检查 %s 下是否有 *.json 任务文件。有就逐个处理：读任务里的 images "
            "（绝对路径）和 material，按 instructions 判读，把结果 JSON 写到 "
            "%s/<batch_id>.json（contract/batch_id 原样带回），处理完删除任务文件；"
            "读图失败就写同名 .error.json 说明原因。没有任务就直接结束，不要做别的事。"
            % (task_dir(root), result_dir(root)))


# ---------------- 结果 ----------------

def _loads_json(text: str):
    """能解析就返回对象，否则 None（会话输出常常夹着说明文字）。"""
    try:
        return json.loads(text)
    except ValueError:
        return None


def _salvage_json(text: str):
    """从夹了说明文字的文本里抠出 JSON：先看围栏块，再按括号配对截取。"""
    cands = []
    if "```" in text:
        for part in text.split("```")[1:]:
            head, _, rest = part.partition("\n")
            body = part if head.strip()[:1] in ("{", "[") else rest
            for cand in (part, body):
                cand = cand.strip()
                if cand and cand not in cands:
                    cands.append(cand)
    for cand in cands:
        data = _loads_json(cand)
        if isinstance(data, (list, dict)):
            return data
    spans = [(text.find("["), text.rfind("]")), (text.find("{"), text.rfind("}"))]
    for i, j in spans:
        if 0 <= i < j:
            data = _loads_json(text[i:j + 1])
            if isinstance(data, (list, dict)):
                return data
    return None


def parse_results(raw) -> list[dict]:
    """接受三种常见形状：结果数组 / 带 results 的对象 / 单条结果对象。"""
    data = raw
    if isinstance(raw, (str, bytes)):
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        text = text.strip()
        if not text:
            return []
        if text.startswith("```"):
            text = "\n".join(line for line in text.splitlines()
                             if not line.strip().startswith("```"))
        data = _loads_json(text)
        if data is None:
            data = _salvage_json(text)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        rows = data.get("results")
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
        if data.get("event_id"):
            return [data]
    return []


def validate(results, known=None) -> tuple[list[dict], list[str]]:
    """洗一遍结果：必须有 event_id；known 给了就拒绝凭空冒出来的条目。"""
    ok, problems = [], []
    seen = set()
    for i, res in enumerate(results or []):
        eid = str((res or {}).get("event_id") or "").strip()
        if not eid:
            problems.append("第 %d 条缺 event_id" % (i + 1))
            continue
        if known is not None and eid not in known:
            problems.append("event_id %s 不在这次任务里" % eid)
            continue
        if eid in seen:
            problems.append("event_id %s 重复" % eid)
            continue
        seen.add(eid)
        row = dict(res)
        row["event_id"] = eid
        ok.append(row)
    return ok, problems


def save_result(payload, root=None, batch_id: str | None = None) -> Path:
    """把 Harness 输出（或人工粘贴的 JSON）落成结果文件。"""
    rows = parse_results(payload)
    if not rows:
        raise ValueError("结果里没有可用的 results 数组")
    bid = str(batch_id or (payload.get("batch_id") if isinstance(payload, dict) else "") or "")
    if not bid:
        tasks = list_tasks(root)
        bid = str(tasks[-1]["batch_id"]) if tasks else new_batch_id()
    payload = {"contract": CONTRACT, "batch_id": bid, "source": "harness",
               "saved_at": int(time.time()), "results": rows}
    d = result_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    p = d / (bid + ".json")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return p


def list_results(root=None) -> list[dict]:
    out = []
    d = result_dir(root)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out.append({"batch_id": p.stem, "path": str(p), "results": [], "created": 0,
                        "error": "%s: %s" % (type(e).__name__, e)})
            continue
        rows = parse_results(data)
        out.append({"batch_id": str(data.get("batch_id") or p.stem), "path": str(p),
                    "created": float(data.get("saved_at") or 0), "results": rows})
    out.sort(key=lambda x: x.get("created") or 0)
    return out


def error_files(root=None) -> list[str]:
    d = result_dir(root)
    return sorted(str(p) for p in d.glob("*.error.json")) if d.is_dir() else []


def archive(batch_id: str, root=None, result_path: Path | None = None) -> dict:
    """入库成功后把任务与结果都挪进 done/（留档，便于事后核对是谁判的）。"""
    moved = {"task": "", "result": ""}
    bid = str(batch_id or "")
    if not bid:
        return moved
    src_task = task_dir(root) / (bid + ".json")
    if src_task.is_file():
        dst = done_dir("tasks", root)
        dst.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_task), str(dst / src_task.name))
        moved["task"] = str(dst / src_task.name)
    src_res = Path(result_path) if result_path else (result_dir(root) / (bid + ".json"))
    if src_res.is_file():
        dst = done_dir("results", root)
        dst.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_res), str(dst / src_res.name))
        moved["result"] = str(dst / src_res.name)
    return moved


def stats(root=None) -> dict:
    """管理台/CLI 用：待处理任务数、待导入结果数、最近活动时间。"""
    tasks = list_tasks(root)
    results = list_results(root)
    errs = error_files(root)
    return {"contract": CONTRACT, "root": str(_base(root)),
            "task_dir": str(task_dir(root)), "result_dir": str(result_dir(root)),
            "tasks": len(tasks), "results": len(results), "errors": len(errs),
            "open_event_ids": len(open_event_ids(root)),
            "last_task": max([t.get("created") or 0 for t in tasks] or [0]),
            "last_result": max([r.get("created") or 0 for r in results] or [0]),
            "prompt": automation_prompt(root)}
