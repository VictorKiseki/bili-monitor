"""T5 Web 管理台（本地、零外网依赖、只用标准库）

设计约束（docs/02 D11）：
  · 只监听 127.0.0.1，不做公网访问；非环回来源一律 403
  · 只做「看 + 轻改」：名单增删改、目标开关、事件与死信查看、失败重投
  · 不参与采集与发送，避免和主循环抢逻辑；同一个 SQLite（WAL）并发读写安全

启动：
    python scripts/run_console.py                 # 默认 http://127.0.0.1:8765
    python scripts/run_bot.py --channel qq --web  # 主循环内嵌管理台
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent

import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import webutil  # noqa: E402
from store.sqlite import SCHEMA, migrate  # noqa: E402
from web.cleanup import CleanupError, CleanupManager  # noqa: E402

from web.console_core import ApiError, ConsoleCore  # noqa: E402
from web.console_bili import ConsoleBili  # noqa: E402
from web.console_qq import ConsoleQQ  # noqa: E402
from web.console_weekly import ConsoleWeekly  # noqa: E402
from web.console_views import TYPE_LABEL, ConsoleViews  # noqa: E402
from web.console_tasks import ConsoleTasks  # noqa: E402
from web.console_feed import ConsoleFeed  # noqa: E402
from web.console_stack import ConsoleStack  # noqa: E402
from web.console_http import Handler, make_server, serve_in_thread  # noqa: E402

class Console(ConsoleCore, ConsoleBili, ConsoleQQ, ConsoleWeekly, ConsoleViews, ConsoleTasks,
              ConsoleFeed, ConsoleStack):
    """管理台的数据层：直接用 sqlite3 读库，改名单时同步写回 config/subjects.json。"""


del ConsoleCore, ConsoleBili, ConsoleQQ, ConsoleWeekly, ConsoleViews, ConsoleTasks, ConsoleFeed
del ConsoleStack
