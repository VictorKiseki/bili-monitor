"""收紧敏感配置文件的权限（H 系列加固，纯标准库实现）。

默认目标（存在才处理）：
  - config/*.env
  - 项目根目录的 BBOT.env、BILI.env、.env

Windows 用 icacls 去掉继承并只保留：当前用户、SYSTEM、Administrators；
非 Windows 退化为 os.chmod(0o600)。逐个文件打印修改前后的权限摘要，
单个文件失败只记日志、继续处理后面的文件。

用法：
  python scripts/secure_config.py --dry-run      # 只看计划，不改任何东西
  python scripts/secure_config.py                # 真正收紧
  python scripts/secure_config.py config/bili.env  # 只处理指定文件
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Windows 内置主体的 SID：用 SID 而不是中文/英文组名，避免系统语言不同导致失效
SYSTEM_SID = "*S-1-5-18"
ADMINS_SID = "*S-1-5-32-544"

_ACL_LINE_RE = re.compile(r"^([^:]+):(\(.*\))$")


def discover_targets(root: Path = ROOT) -> list[Path]:
    """收集默认目标：config/*.env + 根目录的 BBOT.env / BILI.env / .env。"""
    root = Path(root)
    found: list[Path] = []
    cfg = root / "config"
    if cfg.is_dir():
        found.extend(sorted(p for p in cfg.glob("*.env") if p.is_file()))
    for name in ("BBOT.env", "BILI.env", ".env"):
        p = root / name
        if p.is_file():
            found.append(p)
    # 去重并保持顺序
    seen: set[str] = set()
    result: list[Path] = []
    for p in found:
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def current_user() -> str:
    """当前 Windows 账户（DOMAIN\\user）；拿不到域名时只用用户名。"""
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN") or ""
    return f"{domain}\\{user}" if domain else user


def _run(cmd: list[str]) -> tuple[int, str]:
    """跑一个子进程并合并 stdout/stderr；异常也转成 (非 0, 文本)。"""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=60
        )
    except FileNotFoundError:
        return 127, f"找不到命令：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时：{cmd[0]}"
    except OSError as exc:
        return 1, f"命令执行失败：{exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _format_cmd(cmd: list[str]) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in cmd)


def _acl_summary(output: str, limit: int = 6) -> str:
    """从 icacls 输出里提炼「主体:(权限)」；解析不了就回退到前两行。"""
    entries: list[str] = []
    for line in output.splitlines():
        s = line.strip()
        if not s:
            continue
        m = _ACL_LINE_RE.match(s)
        if m:
            entries.append(f"{m.group(1).strip()}{m.group(2)}")
    if not entries:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return " / ".join(lines[:2]) if lines else "（无输出）"
    head = "  ".join(entries[:limit])
    if len(entries) > limit:
        head += f"  …共 {len(entries)} 项"
    return head


def describe_permissions(path: Path) -> str:
    """当前权限摘要：Windows 走 icacls，其它平台给八进制 mode。"""
    if os.name == "nt":
        rc, out = _run(["icacls", str(path)])
        return _acl_summary(out) if rc == 0 else f"（读取失败：{out.strip()[:120]}）"
    try:
        return f"{stat.S_IMODE(path.stat().st_mode):04o}"
    except OSError as exc:
        return f"（读取失败：{exc}）"


def _harden_windows(path: Path, dry_run: bool) -> tuple[bool, str]:
    user = current_user()
    cmd = [
        "icacls", str(path),
        "/inheritance:r",                 # 去掉从父目录继承的宽权限
        "/grant:r",
        f"{user}:F",                      # 当前用户完全控制
        f"{SYSTEM_SID}:F",
        f"{ADMINS_SID}:F",
    ]
    before = describe_permissions(path)
    if dry_run:
        return True, f"修改前: {before}\n  计划: {_format_cmd(cmd)}\n  （dry-run，未执行）"
    rc, out = _run(cmd)
    after = describe_permissions(path)
    if rc != 0:
        return False, f"修改前: {before}\n  失败(icacls rc={rc}): {out.strip()[:300]}\n  修改后: {after}"
    return True, f"修改前: {before}\n  修改后: {after}"


def _harden_posix(path: Path, dry_run: bool) -> tuple[bool, str]:
    before = describe_permissions(path)
    if dry_run:
        return True, f"修改前: {before}\n  计划: chmod 0600 {path}\n  （dry-run，未执行）"
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        return False, f"修改前: {before}\n  失败(chmod): {exc}"
    return True, f"修改前: {before}\n  修改后: {describe_permissions(path)}"


def harden_file(path: Path, dry_run: bool = False) -> tuple[bool, str]:
    """收紧单个文件权限；返回 (是否成功, 摘要文本)。失败不抛异常。"""
    path = Path(path)
    if not path.exists():
        return False, "文件不存在，跳过"
    if not path.is_file():
        return False, "不是普通文件，跳过"
    if os.name == "nt":
        return _harden_windows(path, dry_run)
    return _harden_posix(path, dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="收紧 config/*.env 与 BBOT.env 等敏感配置的文件权限")
    parser.add_argument("paths", nargs="*", help="要处理的文件；缺省用内置目标列表")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不修改任何权限")
    args = parser.parse_args(argv)

    if args.paths:
        targets: list[Path] = []
        for raw in args.paths:
            p = Path(raw)
            if not p.is_absolute():
                p = ROOT / p
            if p.is_dir():
                targets.extend(sorted(q for q in p.glob("*.env") if q.is_file()))
            else:
                targets.append(p)
    else:
        targets = discover_targets()

    if not targets:
        print("没有找到需要处理的配置文件（config/*.env、BBOT.env 等）。")
        return 0

    mode = "Windows / icacls" if os.name == "nt" else "POSIX / chmod 0600"
    print(f"平台：{mode}    共 {len(targets)} 个文件" + ("    [dry-run]" if args.dry_run else ""))
    failed = 0
    for path in targets:
        ok, detail = harden_file(path, dry_run=args.dry_run)
        print(f"[{'OK' if ok else '失败'}] {path}")
        for line in detail.splitlines():
            print(f"  {line}")
        if not ok:
            failed += 1
    print(f"完成：成功 {len(targets) - failed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
