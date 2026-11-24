"""版本与发布工具：改版本、查泄漏、打干净包。

用法：
  python scripts/release.py show                    # 打印当前版本与最近一次 CHANGELOG 小节
  python scripts/release.py bump patch              # 0.2.0 -> 0.2.1（修复/细节，自动）
  python scripts/release.py bump minor              # 0.2.1 -> 0.3.0（多个重要功能合并，自动）
  python scripts/release.py bump major --allow-major  # 架构级/新模块，必须人工放行
  python scripts/release.py bump 0.2.0              # 直接指定版本号
  python scripts/release.py check                   # 扫凭据/个人数据，发现就退出码 1
  python scripts/release.py build                   # 产出 dist/bili-monitor-<version>.zip + SHA256SUMS
  python scripts/release.py build --out D:/rel      # 指定输出目录

build 之前一定会先跑 check；任何一项不干净就不出包。
打包规则是「排除法」：除了运行期数据、凭据、缓存，其余全进包。

自带 Python 运行时（runtime/）是个特例：它整目录放行，因为要「解压即用」。
但 runtime/python312.zip 是标准库本体，会被 *.zip 规则误杀，所以 should_pack()
在第一条就放行 runtime/，RUNTIME_FILES 负责兜住漏件。
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.version import VERSION_RULES  # noqa: E402
from shared.version import MajorBumpRefused, bump_checked  # noqa: E402
from shared.version import is_valid, read_version  # noqa: E402

VERSION_FILE = ROOT / "VERSION"
CHANGELOG = ROOT / "CHANGELOG.md"
PACKAGE_NAME = "bili-monitor"

#: 这些目录整个不进包
EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".idea", ".vscode",
    "node_modules", "data", "logs", "out", "dist", ".dsh-ops", ".tmp",
    ".research", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # 测试只留在开发线与源码仓库：发布包和发布线快照都不带（tests/fixtures 里是真实样本）
    "tests",
}

#: 这些**具体路径**（相对项目根）整棵子树不进包。
#: 目录名粗匹配（如 "img"）会连 livedeck/static/img/ 一起误杀 —— 那三张界面配图
#: 是运行时真要用的，缺了弹幕监控台的守护位就是空白框，所以只按路径精确排除。
EXCLUDE_PATH_PREFIXES = ("docs/img",)

PACKAGED_DOCS = {
    "12-Web管理台.md", "13-守护与开机自启.md", "20-录播自清理.md",
    "23-多屏直播监控台.md", "31-发布与交接手册.md", "33-AI日程识别.md",
    "35-关注流采集.md", "37-文档与代码索引.md",
}
DEVELOPMENT_FILES = {
    "代码审查报告-2026-11-07.md",
    "scripts/bench_dynamics.py", "scripts/demo_card.py", "scripts/demo_notify.py",
    "scripts/preview_cards.py", "scripts/preview_v6.py",
}

#: 这些文件名/后缀不进包（凭据、运行期产物、本机杂项）
EXCLUDE_GLOBS = [
    ".p0_ids.json", "BBOT.env", "BBOT.env.tmp", "*.env", "*.env.tmp", "*.bak",
    "*.tmp", "*.log", "*.db", "*.db-wal", "*.db-shm", "*.sqlite*", "*.zip",
    "SHA256SUMS", "nul", "Thumbs.db", "desktop.ini", "*.pyc", "*.pyo",
    # pip / 编辑器在本机留下的临时文件（曾经把打包搞挂过一次）；
    # tmp* 这种临时产物见下面的 is_tmp_artifact()：不能用 "tmp*" 粗匹配，
    # 否则 shared/tmpdir.py 会被一起排除，发布包里 livedeck 起不来。
    "pip-*", "pipout.txt", "*.whl", "*.egg-info",
    # 测试用 tempfile 在本机找不到可写临时目录时会落到当前目录，跑完/中断后会留下这些
    "harness-*", "agent-*", "ai-cfg-*", "fsync-*",
    # 手工自测脚本同样只留在开发线
    "selftest_*.py",
]

#: 保留的模板文件（即使命中 *.env / 配置 json 规则）
KEEP_GLOBS = ["*.example"]

#: tempfile 在本机找不到可写临时目录时会落到当前目录，留下 tmp + 8 位随机后缀
#: （tmprnkf9c99 / tmp91y7if3g 这种）。只认这个形状，别碰 tmpdir.py 之类的真源码。
TMP_ARTIFACT_RE = re.compile(r"^tmp[a-z0-9_]{8}(\.[a-z0-9]+)?$")


def is_tmp_artifact(name: str) -> bool:
    """是不是 tempfile 落下的临时产物（按名字判定，文件和目录都可能）。"""
    return bool(TMP_ARTIFACT_RE.match(name))

#: 自带运行时目录：整目录进包（里面 python312.zip 会被 *.zip 规则误杀）
RUNTIME_DIR = "runtime"

#: 运行时必须有这些文件，缺一个就不是「解压即用」的包
RUNTIME_FILES = [
    "runtime/python.exe",
    "runtime/pythonw.exe",
    "runtime/python312.zip",   # 标准库本体，最容易漏
    "runtime/python312.dll",
    "runtime/python312._pth",
    "runtime/LICENSE.txt",     # PSF 许可证，随 CPython 分发必须带上
]

#: 需要出现在发布包里的关键文件
REQUIRED_FILES = [
    "VERSION", "CHANGELOG.md", "README.md", "LICENSE", "AGENTS.md", "requirements.txt",
    "install.cmd", "scripts/install.ps1", "scripts/preflight.py",
    "scripts/build_runtime.py",
    "scripts/setup_wizard.py", "scripts/run_bot.py", "scripts/schedule_worker.py",
    "BBOT.env.example", "config/bili.env.example", "config/subjects.json.example",
    "config/weekly_agent.json.example", "core/weekly/agent_api.py",
    "core/weekly/agent.py", "core/weekly/harness.py", "core/weekly/push.py",
    "core/weekly/feature.py",
    # 运行时要用的静态资源：名字带 tmp 的模块和 livedeck 的三张界面配图
    "shared/tmpdir.py",
    "livedeck/static/img/guard1.png",
    "livedeck/static/img/guard2.png",
    "livedeck/static/img/guard3.png",
    "docs/31-发布与交接手册.md", "docs/33-AI日程识别.md", "docs/35-关注流采集.md",
]

#: 凭据泄漏扫描规则：(说明, 正则)
SECRET_PATTERNS = [
    ("B 站 SESSDATA 真值", re.compile(r"SESSDATA\s*=\s*[A-Za-z0-9%*.,_\-]{16,}")),
    ("B 站 bili_jct 真值", re.compile(r"bili_jct\s*=\s*[0-9a-f]{20,}")),
    # QQ 官方机器人 AppSecret 固定 32 位；阈值取 24 位是为了不误报测试用的短占位值
    ("QQ 机器人 Secret 真值", re.compile(r"QQBOT_APP_SECRET\s*=\s*[A-Za-z0-9_\-]{24,}")),
    ("QQ openid 真值", re.compile(r'"user_openid"\s*:\s*"[0-9A-Fa-f]{16,}"')),
    ("B 站 DedeUserID 真值", re.compile(r"DEDEUSERID\s*=\s*\d{5,}")),
    ("疑似 Bot token", re.compile(r"bot\s+[0-9A-Za-z.\-_]{30,}")),
    ("写死的本机路径", re.compile(
        r"[A-Za-z]:[\\/]+(?:Users[\\/]+[A-Za-z0-9_.-]+[\\/]+|DSH-WorkShell|ChatGPT_WorkShell|anaconda[0-9]*|BiliLiveRecord)"
        r"|(?<![\w/])/(?:home|Users)/[A-Za-z0-9_.-]+/", re.I
    )),
]


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def bad(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def head(title: str) -> None:
    print()
    print(f"== {title} " + "=" * max(0, 58 - len(title)))


def _is_kept(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, g) for g in KEEP_GLOBS)


def should_pack(rel: str) -> bool:
    """rel 用 / 分隔、相对项目根；返回 True 表示进包。"""
    parts = rel.split("/")
    # 自带运行时整目录放行，放在所有排除规则之前（python312.zip 是标准库本体）
    if parts[0] == RUNTIME_DIR:
        return not any(p == "__pycache__" or p.endswith((".pyc", ".pyo")) for p in parts)
    if any(rel == p or rel.startswith(p + "/") for p in EXCLUDE_PATH_PREFIXES):
        return False
    if rel in DEVELOPMENT_FILES or rel.startswith("scripts/diag_"):
        return False
    if rel.startswith("scripts/probe_") and rel != "scripts/probe_follow_feed.py":
        return False
    if parts[0] == "docs" and (len(parts) != 2 or parts[-1] not in PACKAGED_DOCS):
        return False
    # tempfile 的临时产物（任意一层）一律不进包；按精确形状判，tmpdir.py 不受影响
    if any(is_tmp_artifact(p) for p in parts):
        return False
    if any(p in EXCLUDE_DIRS for p in parts[:-1]):
        return False
    # 隐藏目录（.git/.dsh-ops 已在 EXCLUDE_DIRS 里）一律不进包，但根目录的 .gitignore 要留
    if any(p.startswith(".") for p in parts[:-1]):
        return False
    if parts[-1].startswith(".") and parts[-1] not in (".gitignore", ".gitattributes") and not _is_kept(rel):
        return False
    for pattern in EXCLUDE_GLOBS:
        if fnmatch.fnmatch(parts[-1], pattern):
            return _is_kept(rel)
    if parts[0] == "config" and parts[-1].endswith(".json") and not _is_kept(rel):
        return False  # config/*.json 是使用者自己的名单/清理配置
    return True


def collect_files() -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in EXCLUDE_DIRS
            and not d.startswith(".")
            and not is_tmp_artifact(d)
            and not any(fnmatch.fnmatch(d, g.rstrip("/")) for g in EXCLUDE_GLOBS if not g.startswith("*."))
        )
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(ROOT).as_posix()
            if should_pack(rel):
                out.append(p)
    return out


def scan_secrets(files: list[Path]) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        if rel.lower().endswith((".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".flac", ".mp3")):
            continue
        # 自带运行时不是我们写的，扫它纯属浪费时间（里面全是 .dll / .zip / .pyd）
        if rel.split("/")[0] == RUNTIME_DIR:
            continue
        try:
            if p.stat().st_size > 2_000_000:
                continue
            with p.open("rb") as fh:
                if b"\x00" in fh.read(8192):
                    continue  # 二进制文件不当文本扫，避免整段乱码误报
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append((rel, lineno, label))
    return findings


def check_git_tracking() -> list[str]:
    """确认 .p0_ids.json、BBOT.env 这类文件没有被 git 跟踪。"""
    problems: list[str] = []
    if not (ROOT / ".git").is_dir():
        warn("不是一个 git 仓库，跳过 git 跟踪检查")
        return problems
    try:
        proc = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"git 检查跳过：{exc}")
        return problems
    tracked = {p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p}
    for risky in (".p0_ids.json", "BBOT.env", "config/bili.env", "config/livedeck.env",
                  "config/subjects.json", "config/recorder_cleanup.json"):
        if risky in tracked:
            problems.append(f"{risky} 被 git 跟踪了，先 git rm --cached 它")
    return problems


def check_script_encodings() -> list[str]:
    """PowerShell 5.1 按 ANSI 读 .ps1：含非 ASCII 却没 BOM 会解析失败，必须提前拦住。"""
    problems: list[str] = []
    skip = {".git", ".venv", "venv", "node_modules", "data", "logs", "dist", "out",
            ".dsh-ops", ".research", ".tmp", RUNTIME_DIR}
    for p in ROOT.rglob("*.ps1"):
        parts = set(p.relative_to(ROOT).parts[:-1])
        if parts & skip:
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        has_non_ascii = any(byte >= 0x80 for byte in raw)
        if has_non_ascii and not has_bom:
            problems.append(f"{p.relative_to(ROOT).as_posix()} 含中文但没 UTF-8 BOM，PowerShell 5.1 会报语法错误")
    return problems


def check_runtime() -> tuple[list[str], list[str]]:
    """检查自带运行时。返回 (problems, warnings)。

    problems = 有 runtime/ 但残了（缺文件 / _pth 没配好）—— 必须修；
    warnings = 干脆没有 runtime/（开发机上正常，build 时会拦住）。
    """
    problems: list[str] = []
    warnings: list[str] = []
    if not (ROOT / RUNTIME_DIR).is_dir():
        warnings.append("没有 runtime/ 目录（开发机正常；打发布包前先跑 python scripts/build_runtime.py）")
        return problems, warnings
    for rel in RUNTIME_FILES:
        if not (ROOT / rel).is_file():
            problems.append(f"自带运行时缺文件：{rel}")
    pth = ROOT / "runtime" / "python312._pth"
    if pth.is_file():
        text = pth.read_text(encoding="utf-8", errors="replace")
        if "site-packages" not in text.replace("/", "\\"):
            problems.append("runtime/python312._pth 没有放开 Lib/site-packages")
        if not re.search(r"^\s*import\s+site\s*$", text, re.M):
            problems.append("runtime/python312._pth 里 import site 没打开")
    if not (ROOT / "runtime" / "Lib" / "site-packages").is_dir():
        problems.append("runtime/Lib/site-packages 不存在（依赖没装进运行时）")
    return problems, warnings


def cmd_check(_args: argparse.Namespace) -> int:
    head(f"发布前检查 v{read_version()}")
    failed = 0

    missing = [rel for rel in REQUIRED_FILES if not (ROOT / rel).exists()]
    if missing:
        failed += len(missing)
        for rel in missing:
            bad(f"缺少关键文件：{rel}")
    else:
        ok(f"关键文件齐全（{len(REQUIRED_FILES)} 个）")

    rt_problems, rt_warnings = check_runtime()
    for msg in rt_warnings:
        warn(msg)
    for msg in rt_problems:
        bad(msg)
    failed += len(rt_problems)
    if (ROOT / RUNTIME_DIR).is_dir() and not rt_problems:
        total = sum(f.stat().st_size for f in (ROOT / "runtime").rglob("*") if f.is_file())
        ok(f"自带运行时齐全（{total / (1024 * 1024):.0f} MB，解压即用）")

    problems = check_git_tracking()
    for msg in problems:
        bad(msg)
    if not problems:
        ok("敏感文件没有被 git 跟踪")
    failed += len(problems)

    enc_problems = check_script_encodings()
    for msg in enc_problems:
        bad(msg)
    if not enc_problems:
        ok("PowerShell 脚本编码统一（含中文的都带 UTF-8 BOM）")
    failed += len(enc_problems)

    files = collect_files()
    findings = scan_secrets(files)
    if findings:
        failed += len(findings)
        for rel, lineno, msg in findings[:40]:
            bad(f"{rel}:{lineno} {msg}")
        if len(findings) > 40:
            bad(f"……还有 {len(findings) - 40} 处，先修前面这些")
    else:
        ok(f"没扫到凭据（扫了 {len(files)} 个将进包的文件）")

    print()
    if failed:
        print(f"  结论：有 {failed} 个问题，先修掉再 build。")
        return 1
    print("  结论：干净，可以 build。")
    return 0


def _changelog_section(version: str, date: str) -> str:
    return (
        f"## [{version}] - {date}\n"
        "\n"
        "### 新增\n\n"
        "- 待补充\n"
        "\n"
        "### 变更\n\n"
        "- 待补充\n"
        "\n"
        "### 修复\n\n"
        "- 待补充\n"
        "\n"
    )


def cmd_bump(args: argparse.Namespace) -> int:
    current = read_version()
    kind = args.version.strip()
    target = kind
    if kind in ("major", "minor", "patch"):
        try:
            target = bump_checked(current, kind, allow_major=args.allow_major)
        except MajorBumpRefused as exc:
            bad(str(exc))
            return 2
        part, desc = {k: (p, d) for k, p, d in VERSION_RULES}[kind]
        ok(f"按规则递增：{part}（{desc}）")
    if not is_valid(target):
        bad(f"版本号不合法：{target}（要 1.2.3 这样的语义化版本，或 major/minor/patch）")
        return 2
    if target == current and not args.force:
        warn(f"已经是 {current} 了，没改动（要重写 CHANGELOG 加 --force）")
        return 0

    VERSION_FILE.write_text(target + "\n", encoding="utf-8")
    ok(f"VERSION: {current} → {target}")

    text = CHANGELOG.read_text(encoding="utf-8")
    if f"## [{target}]" in text:
        warn(f"CHANGELOG 里已经有 [{target}] 小节，没有重复插入")
    else:
        date = time.strftime("%Y-%m-%d")
        marker = "## [Unreleased]\n"
        if marker not in text:
            bad("CHANGELOG.md 里找不到 Unreleased 小节，请手动加小节")
            return 1
        text = text.replace(marker, marker + "\n" + _changelog_section(target, date), 1)
        CHANGELOG.write_text(text, encoding="utf-8")
        ok(f"CHANGELOG 已插入 {target} 小节（记得把待补充写成人话）")

    print()
    print("  下一步：")
    print(f"    1. 补 CHANGELOG 的 {target} 小节")
    print("    2. python scripts/release.py check")
    print("    3. python scripts/release.py build")
    print(f"    4. git add -A && git commit -m \"chore(release): v{target}\" && git tag v{target}")
    return 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_zip(files: list[Path], zip_path: Path, stem: str, *, update_key: bytes | None = None) -> list[str]:
    """把 files 写进 zip_path，arcname 统一带顶层目录 stem/；返回写进去的名单。

    抽成函数是为了让测试能拿真实写入逻辑验「某些文件确实在包里」，
    而不是自己另写一遍 zipfile 逻辑（那样等于测了个假的）。
    """
    names: list[str] = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            if rel.lower() == "online-update.pub":
                continue
            arc = f"{stem}/{rel}"
            zf.write(p, arcname=arc)
            names.append(arc)
        if update_key is not None:
            arc = f"{stem}/online-update.pub"
            zf.writestr(arc, update_key)
            names.append(arc)
    return names


def cmd_build(args: argparse.Namespace) -> int:
    if not args.skip_check and cmd_check(args) != 0:
        bad("检查没通过，不打这个包。")
        return 1

    # build 必须有自带运行时，否则打出来的包在没装 Python 的机器上跑不起来
    rt_problems, rt_warnings = check_runtime()
    if not (ROOT / RUNTIME_DIR).is_dir():
        bad("没有 runtime/，打出来的包不能「解压即用」")
        print("       先跑：.\\runtime\\python.exe scripts/build_runtime.py（或用任意 3.12 x64 python）")
        for msg in rt_warnings:
            print(f"       {msg}")
        return 1
    if rt_problems:
        for msg in rt_problems:
            bad(msg)
        return 1

    import os
    from xml.etree import ElementTree

    key_path = getattr(args, "update_public_key", None) or os.environ.get("BILI_UPDATE_PUBLIC_KEY")
    update_key = None
    if key_path:
        try:
            with Path(key_path).open("rb") as key_file:
                update_key = key_file.read(8193)
            key = ElementTree.fromstring(update_key)
            if (len(update_key) > 8192 or key.tag != "RSAKeyValue"
                    or [child.tag for child in key] != ["Modulus", "Exponent"]):
                raise ValueError("需要仅包含 Modulus 和 Exponent 的 RSA 公钥 XML")
        except (OSError, ValueError, ElementTree.ParseError) as exc:
            bad(f"无法打包在线更新公钥：{exc}")
            return 1
    version = read_version()
    out_dir = Path(args.out).resolve() if args.out else ROOT / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{PACKAGE_NAME}-{version}"
    zip_path = out_dir / f"{stem}.zip"
    if zip_path.exists():
        zip_path.unlink()

    files = collect_files()
    head(f"打包 {stem}.zip（{len(files)} 个文件）")
    write_zip(files, zip_path, stem, update_key=update_key)
    digest = _sha256(zip_path)
    sums = out_dir / "SHA256SUMS"
    with sums.open("w", encoding="utf-8") as fh:
        fh.write(f"{digest}  {zip_path.name}\n")

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    ok(str(zip_path))
    ok(f"大小 {size_mb:.2f} MB，SHA256 {digest}")
    ok(f"校验文件 {sums}")
    print()
    print("  别人拿到包之后：解压 → 双击 install.cmd → runtime\\python.exe scripts\\setup_wizard.py")
    return 0


def cmd_show(_args: argparse.Namespace) -> int:
    version = read_version()
    print(f"当前版本：{version}")
    print(f"项目目录：{ROOT}")
    if CHANGELOG.is_file():
        text = CHANGELOG.read_text(encoding="utf-8")
        m = re.search(rf"^## \[{re.escape(version)}\].*?(?=^## \[|\Z)", text, re.M | re.S)
        if m:
            print()
            print(m.group(0).rstrip())
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="版本与发布工具")
    sub = ap.add_subparsers(dest="cmd")

    p_show = sub.add_parser("show", help="打印版本与 CHANGELOG 小节")
    p_show.set_defaults(func=cmd_show)

    p_bump = sub.add_parser("bump", help="改版本号并写 CHANGELOG 小节")
    p_bump.add_argument("version", help="major / minor / patch 或形如 1.2.0")
    p_bump.add_argument("--force", action="store_true", help="版本号没变也重写 CHANGELOG")
    p_bump.add_argument("--allow-major", action="store_true",
                        help="允许人工递增第一位（架构级调整 / 新增功能模块时才用）")
    p_bump.set_defaults(func=cmd_bump)

    p_check = sub.add_parser("check", help="发布前检查（凭据扫描 + 关键文件）")
    p_check.set_defaults(func=cmd_check)

    p_build = sub.add_parser("build", help="打干净的发布 zip")
    p_build.add_argument("--out", default="", help="输出目录（默认 dist/）")
    p_build.add_argument("--update-public-key", help="随包安装受信任的 RSA 更新公钥 XML")
    p_build.add_argument("--skip-check", action="store_true", help="跳过发布前检查（不推荐）")
    p_build.set_defaults(func=cmd_build)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
