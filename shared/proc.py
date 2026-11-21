"""后台子进程的 Windows 控制台开关。

pythonw.exe 自身没有控制台；它启动 powershell.exe / taskkill / netstat 这类
控制台程序时，如果不传 CREATE_NO_WINDOW，Windows 会给子进程新建一个可见的
控制台窗口，表现为任务运行时「终端闪窗」。

统一从这里取 creationflags，避免每个调用点各写一份、漏掉一处又闪一次。
"""
from __future__ import annotations

import os
import subprocess


def no_window_flags() -> int:
    """让控制台子进程不弹窗的 creationflags；非 Windows 返回 0。"""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)
