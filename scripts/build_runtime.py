"""构建项目自带的 Python 运行时（runtime/）。

绿色包的目标是「解压即用」：用户机器上没有 Python 也能跑。
做法是把 CPython 官方 embeddable 包放进 runtime/，再把依赖预装进它的 site-packages。

本脚本做四件事：
  1. 下载 python-<ver>-embed-amd64.zip（官方地址，约 11 MB）
  2. 解压到 runtime/
  3. 改写 runtime/python312._pth（放开 Lib/site-packages 并打开 import site）
  4. 用 pip --target 把 requirements.txt + requirements-optional.txt 装进去
  5. 冒烟校验：runtime\\python.exe 能 import 五个必需依赖

用法：
  python scripts/build_runtime.py                       # 默认 3.12.7
  python scripts/build_runtime.py --check               # 只校验现有 runtime/
  python scripts/build_runtime.py --force               # 删掉旧 runtime/ 重建
  python scripts/build_runtime.py --ver 3.12.7          # 指定 CPython 版本
  python scripts/build_runtime.py --index-url https://pypi.tuna.tsinghua.edu.cn/simple

注意：
  * runtime/ 不进 git（见 .gitignore），发布包由 release.py build 现场收集；
  * 用于 pip --target 的构建解释器必须是 3.12 x64，否则会拿到错版本的 wheel；
  * 升级 CPython = 换掉整个 runtime/ 重做一遍，同时升版本号并写 CHANGELOG。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_VERSION = "3.12.7"
RUNTIME = ROOT / "runtime"
SITE_PACKAGES = RUNTIME / "Lib" / "site-packages"
REQ_MAIN = ROOT / "requirements.txt"
REQ_OPT = ROOT / "requirements-optional.txt"

#: 必须有、缺了就只能报错的文件（对应 CPython embeddable 包内容）
RUNTIME_FILES = ("python.exe", "pythonw.exe", "python312.zip", "python312.dll", "python312._pth")

#: 冒烟探针：导入五个必需依赖并打印解释器信息（不带引号，避免各层转义问题）
PROBE = (
    "import sys,platform;"
    "import httpx,PIL,websockets,websocket,segno;"
    "print('RUNTIME_OK');"
    "print('VER=' + '.'.join(map(str, sys.version_info[:3])));"
    "print('BITS=' + platform.architecture()[0]);"
    "print('EXE=' + sys.executable);"
    "print('PREFIX=' + sys.prefix)"
)


def pth_content() -> str:
    """embeddable 的 _pth：路径相对 python.exe，python312.zip 是标准库本体，不能删。"""
    return "python312.zip\r\n.\r\nLib\\site-packages\r\n\r\nimport site\r\n"


def say(msg: str = "") -> None:
    print(msg)


def step(n: int, total: int, msg: str) -> None:
    print()
    print(f"[{n}/{total}] {msg}")


def runtime_python() -> Path:
    return RUNTIME / "python.exe"


def runtime_pythonw() -> Path:
    return RUNTIME / "pythonw.exe"


def run_probe(exe: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [str(exe), "-c", PROBE],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"跑不起 {exe}：{exc}"
    if proc.returncode != 0 or "RUNTIME_OK" not in proc.stdout:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, "探针失败：" + (detail[-1] if detail else f"退出码 {proc.returncode}")
    return True, "\n".join(line.strip() for line in proc.stdout.strip().splitlines())


def dir_size(path: Path) -> tuple[int, int]:
    total = 0
    count = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
                count += 1
            except OSError:
                continue
    return count, total


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    say(f"  下载 {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as fh:
        length = resp.headers.get("Content-Length") or "?"
        say(f"  包大小 {length} 字节")
        shutil.copyfileobj(resp, fh, 1024 * 256)
    tmp.replace(dest)
    say(f"  已保存 {dest}（{dest.stat().st_size} 字节）")


def pick_build_python(explicit: str) -> Path:
    """挑一个能跑 pip --target 的构建解释器（必须 3.12 x64）。"""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    candidates.append(sys.executable)
    candidates.extend(["python", "python3"])
    for cand in candidates:
        exe = shutil.which(cand) or cand
        try:
            proc = subprocess.run(
                [exe, "-c", "import sys,platform;print(sys.version_info[0], sys.version_info[1], platform.architecture()[0])"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        parts = proc.stdout.split()
        if len(parts) >= 3 and parts[0] == "3" and parts[1] == "12" and parts[2].startswith("64"):
            return Path(exe)
    raise SystemExit(
        "[失败] 找不到 Python 3.12 (64 位) 构建解释器。\n"
        "       装一个 3.12 x64，或用 --python 指定：python scripts/build_runtime.py --python C:\\Python312\\python.exe"
    )


def install_deps(build_python: Path, index_url: str) -> None:
    if not REQ_MAIN.is_file():
        raise SystemExit("[失败] 缺少 requirements.txt")
    args = [
        str(build_python), "-m", "pip", "install",
        "--target", str(SITE_PACKAGES),
        "--only-binary=:all:",
        "--upgrade",
        "--timeout", "60",
        "--retries", "3",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "-r", str(REQ_MAIN),
    ]
    if REQ_OPT.is_file():
        args += ["-r", str(REQ_OPT)]
    if index_url:
        args += ["--index-url", index_url]
    SITE_PACKAGES.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(args, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"[失败] 装依赖失败（退出码 {proc.returncode}）。换镜像试试：--index-url https://pypi.tuna.tsinghua.edu.cn/simple")


def check(verbose: bool = True) -> bool:
    problems: list[str] = []
    if not RUNTIME.is_dir():
        problems.append("runtime/ 不存在，先跑 python scripts/build_runtime.py")
    for name in RUNTIME_FILES:
        if not (RUNTIME / name).is_file():
            problems.append(f"缺少 runtime/{name}")
    if (RUNTIME / "python312._pth").is_file():
        text = (RUNTIME / "python312._pth").read_text(encoding="utf-8", errors="replace")
        flat = text.replace("/", "\\")
        if "Lib\\site-packages" not in flat:
            problems.append("runtime/python312._pth 里没有 Lib/site-packages")
        # 必须是独立一行的 import site：#import site 里也含这个子串，不能只做包含判断
        if not re.search(r"^\s*import\s+site\s*$", text, re.M):
            problems.append("runtime/python312._pth 里 import site 没有打开")
    if not SITE_PACKAGES.is_dir():
        problems.append("runtime/Lib/site-packages 不存在（依赖没装进去）")
    if problems:
        if verbose:
            for msg in problems:
                print(f"  [FAIL] {msg}")
        return False
    ok, detail = run_probe(runtime_python())
    if not ok:
        if verbose:
            print(f"  [FAIL] {detail}")
        return False
    if verbose:
        for line in detail.splitlines():
            print(f"  [OK]   {line}")
        count, total = dir_size(RUNTIME)
        print(f"  [OK]   runtime/ 共 {count} 个文件，{total / (1024 * 1024):.2f} MB")
    return True


def cmd_check(_args: argparse.Namespace) -> int:
    print("== 校验自带运行时 " + "=" * 40)
    return 0 if check() else 1


def cmd_build(args: argparse.Namespace) -> int:
    ver = args.ver.strip()
    url = args.url.strip() or f"https://www.python.org/ftp/python/{ver}/python-{ver}-embed-amd64.zip"
    arch_bits = struct.calcsize("P") * 8

    print(f"== 构建自带运行时 Python {ver} " + "=" * 32)
    print(f"  项目根：{ROOT}")
    if arch_bits != 64:
        print("[失败] 这个脚本只能在 64 位 Python 下跑（要拉 amd64 的 embeddable 包）")
        return 1

    if RUNTIME.exists() and args.force:
        print(f"  按 --force 删除旧的 {RUNTIME}")
        shutil.rmtree(RUNTIME)
    if RUNTIME.exists() and runtime_python().is_file() and not args.force:
        print("  runtime/ 已存在，跳过下载（要重建加 --force）")
    else:
        step(1, 4, "下载并解压 CPython embeddable")
        zip_path = ROOT / ".tmp" / f"python-{ver}-embed-amd64.zip"
        if not zip_path.is_file() or zip_path.stat().st_size < 1_000_000:
            download(url, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(RUNTIME)
        print(f"  已解压到 {RUNTIME}")

    step(2, 4, "配置 runtime/python312._pth")
    pth = RUNTIME / "python312._pth"
    pth.write_text(pth_content(), encoding="utf-8", newline="")
    print("  已放开 Lib/site-packages 并打开 import site")

    step(3, 4, "预装依赖到 runtime/Lib/site-packages")
    if args.skip_deps:
        print("  按 --skip-deps 跳过")
    else:
        build_python = pick_build_python(args.python)
        print(f"  构建解释器：{build_python}")
        install_deps(build_python, args.index_url)

    step(4, 4, "冒烟校验")
    if not check():
        print()
        print("  结论：运行时不可用，别拿去打包。")
        return 1
    print()
    print("  结论：runtime/ 就绪。接下来 python scripts/release.py check / build")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="构建项目自带的 Python 运行时（runtime/）")
    sub = ap.add_subparsers(dest="cmd")

    p_build = sub.add_parser("build", help="下载 embeddable + 装依赖（默认动作）")
    p_build.add_argument("--ver", default=DEFAULT_VERSION, help=f"CPython 版本（默认 {DEFAULT_VERSION}）")
    p_build.add_argument("--url", default="", help="自定义 embeddable zip 地址（内网镜像用）")
    p_build.add_argument("--python", default="", help="用于 pip --target 的 3.12 x64 解释器")
    p_build.add_argument("--index-url", default="", help="pip 索引（默认用环境配置）")
    p_build.add_argument("--force", action="store_true", help="删掉旧 runtime/ 重建")
    p_build.add_argument("--skip-deps", action="store_true", help="只放运行时，不装依赖（调试用）")
    p_build.set_defaults(func=cmd_build)

    p_check = sub.add_parser("check", help="只校验现有 runtime/")
    p_check.set_defaults(func=cmd_check)

    # 兼容无子命令：python scripts/build_runtime.py --force
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv = ["build"] + argv
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
