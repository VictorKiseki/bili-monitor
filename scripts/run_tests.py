"""跑全量离线单元测试（自带运行时的正确入口）。

为什么需要它：发布包用的是 CPython **embeddable** 运行时，它的 `sys.path` 由
`runtime/python312._pth` 固定，**不含当前工作目录**。于是
`runtime\\python.exe -m unittest discover ...` 在导入 `shared` / `core` 这类项目内
模块时会报 `ModuleNotFoundError`（用 `.venv` 时 cwd 恰好在 `sys.path` 里，所以看不出来）。

这个脚本先把仓库根目录放进 `sys.path`，再按同样的规则发现并运行 `tests/test_*.py`，
两种解释器下行为一致。

用法：

  .\\runtime\\python.exe scripts\\run_tests.py            # 全量
  .\\runtime\\python.exe scripts\\run_tests.py -v         # 每个用例打一行
  .\\runtime\\python.exe scripts\\run_tests.py tests.test_version   # 只跑某个模块
  .\\runtime\\python.exe scripts\\run_tests.py -p "test_feed_*.py"  # 自定义文件匹配

退出码：0 全绿；1 有失败/错误（CI 依赖这个）。
"""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑 tests/ 下的离线单元测试")
    ap.add_argument("modules", nargs="*", help="只跑指定模块（如 tests.test_version）")
    ap.add_argument("-p", "--pattern", default="test_*.py", help="文件名匹配（默认 test_*.py）")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐条输出")
    ap.add_argument("-q", "--quiet", action="store_true", help="只输出结论")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    # 关键一步：让 shared / core / web 这些项目内包可导入（embeddable 不认 cwd）
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    if args.modules:
        suite = unittest.defaultTestLoader.loadTestsFromNames(args.modules)
    else:
        suite = unittest.defaultTestLoader.discover(str(TESTS), pattern=args.pattern, top_level_dir=str(TESTS))

    verbosity = 0 if args.quiet else (2 if args.verbose else 1)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
