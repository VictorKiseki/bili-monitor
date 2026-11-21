#!/usr/bin/env python
"""DD全能工作间 · 本地多屏监控室入口。

用法：
    python run_livedeck.py                # 默认 http://127.0.0.1:8760/
    python run_livedeck.py --port 8800    # 换端口
    python run_livedeck.py --no-open      # 不自动开浏览器

依赖：httpx、brotli、sqlite3（均为现有环境已装）。弹幕通过本机 Edge 的
DevTools 桥转发，因此 Windows 上需要安装 Edge（系统自带即可）。
"""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # 控制台默认 GBK，统一成 UTF-8 免得日志乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from shared import online_update
from shared import proxy as proxy_cfg  # noqa: E402  启动时套用 config/proxy.json

def main(argv):
    try:
        with online_update.OnlineUpdate().runtime_lock():
            proxy_cfg.apply_to_env()
            from livedeck.server import main as serve

            return serve(argv)
    except online_update.UpdateError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
