"""进程栈 / 账号绑定状态的数据层（由 web/console.py 拆出）。

两条通道：
  GET  /api/stack         绑定状态 + 采集进程/推送进程状态（页面首屏、首次引导都读它）
  POST /api/stack/action  {"role": "collector|pusher", "action": "restart|start|stop|auto"}

为什么页面上要有手动按钮：自动门控出问题时（凭据刚换、进程卡在退避里），
用户需要一个「重来一次」的入口，而不是去任务管理器里翻进程。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import readiness  # noqa: E402
from web.console_core import ApiError  # noqa: E402

ROLE_ALIASES = {
    "collector": "collector", "采集": "collector", "采集进程": "collector", "采集程序": "collector",
    "pusher": "pusher", "推送": "pusher", "推送进程": "pusher", "推送程序": "pusher",
}
ACTION_ALIASES = {
    "auto": "auto", "自动": "auto", "跟随": "auto",
    "start": "start", "启动": "start", "开": "start",
    "stop": "stop", "停止": "stop", "关": "stop", "停": "stop",
    "restart": "restart", "重启": "restart",
}
BINDING_HINT = {
    "collector": "先绑定 B 站账号（管理台「B 站登录态」→ 扫码登录），采集进程才有账号可用",
    "pusher": "先在「机器人」页填 AppID / AppSecret 并抓取推送目标，推送进程才有东西可发",
}


def normalize_role(value) -> str:
    key = str(value or "").strip().lower()
    role = ROLE_ALIASES.get(key) or ROLE_ALIASES.get(str(value or "").strip())
    if not role:
        raise ApiError("没有这个进程：%s（只有 collector / pusher）" % (value or "空"), 400)
    return role


def normalize_action(value) -> str:
    key = str(value or "").strip().lower()
    action = ACTION_ALIASES.get(key) or ACTION_ALIASES.get(str(value or "").strip())
    if not action:
        raise ApiError("不支持的动作：%s（restart / start / stop / auto）" % (value or "空"), 400)
    return action


class ConsoleStack:
    """混入 Console：进程栈状态与手动干预。"""

    stack = None            # 由 scripts/run_console.py 注入 StackManager

    def _root(self) -> Path:
        # subjects_path = <root>/config/subjects.json
        return Path(self.subjects_path).resolve().parent.parent

    def bindings_state(self) -> dict:
        mgr = getattr(self, "stack", None)
        if mgr is not None:
            return mgr.binding_state()
        return readiness.summary(self._root())

    def stack_status(self) -> dict:
        mgr = getattr(self, "stack", None)
        if mgr is None:
            return {
                "ok": True,
                "available": False,
                "bindings": self.bindings_state(),
                "roles": {},
                "manager": {},
                "hint": "当前进程由 run_bot.py --web 启动，没有托管采集/推送；"
                        "用 install.cmd / service.ps1 启动的监控台会自动托管。",
            }
        st = mgr.status()
        st["ok"] = True
        st["available"] = True
        return st

    def stack_action(self, role, action) -> dict:
        mgr = getattr(self, "stack", None)
        if mgr is None:
            raise ApiError("当前监控台没有托管采集/推送进程，无法执行该操作", 409)
        role = normalize_role(role)
        action = normalize_action(action)
        if action in ("start", "restart"):
            bind = self.bindings_state()
            key = "bili" if role == "collector" else "qq"
            if not bind[key]["bound"]:
                raise ApiError(BINDING_HINT[role] + "（" + str(bind[key]["text"]) + "）", 409)
        if action in ("auto", "restart"):
            mgr.set_manual(role, None)
        elif action == "start":
            mgr.set_manual(role, "on")
        elif action == "stop":
            mgr.set_manual(role, "off")
        if action == "restart":
            mgr.restart(role)
        actions = mgr.reconcile()
        out = self.stack_status()
        out["actions"] = list(actions)
        out["role"] = role
        out["action"] = action
        return out
