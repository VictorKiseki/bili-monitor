"""一条命令发版：测试 -> 检查 -> 提交 -> tag -> 发布线快照 -> 打包 -> 推送 -> Gitee 发行版。

用法（在仓库根目录，自带运行时优先）：
  python scripts/release_gitee.py                        # 发布当前 VERSION（CHANGELOG 该版小节必须先写好）
  python scripts/release_gitee.py --bump patch           # 先按规则涨版本号；生成的骨架补完后再跑一次
  python scripts/release_gitee.py --dry-run              # 只做前置检查并打印计划，不写任何东西
  python scripts/release_gitee.py --no-release           # 不建 Gitee 发行版（没有令牌时用）
  python scripts/release_gitee.py --no-push              # 只做本地：提交 / tag / 快照 / 打包
  python scripts/release_gitee.py --body-file out/公告.md  # 发行版正文用自己的稿子
  python scripts/release_gitee.py --yes                  # 不交互确认（脚本/CI 里用）
  python scripts/release_gitee.py --resume               # Git 引用已推送，用现有包补发行版和附件

它把 docs/31 §11 与 docs/34 §9 的手工步骤按固定顺序串起来：
  1. 前置检查：VERSION 合法、CHANGELOG 有这一版小节、工作树只脏了 VERSION/CHANGELOG、
     tag 没被占用、令牌能读到（没有的话什么都不会写）
  2. 全量测试（--skip-tests 可跳）
  3. scripts/release.py check（密钥扫描 + 缺件检查）
  4. 提交 VERSION + CHANGELOG（chore(release): vX.Y.Z）
  5. annotated tag
  6. opensource 快照：临时索引从当前分支树写一条提交，剔除 tests/ 与 scripts/selftest_*.py
  7. scripts/release.py build，并核对 dist/SHA256SUMS 与实算哈希
  8. 推送 分支 / opensource / tag，再建（或更新）Gitee 发行版并补附件

任何一步失败就停下，退出码：0 成功、1 执行失败、2 前置条件不满足。
中途失败可以修完直接重跑：已提交的不会再提交、tag 已指向 HEAD 会跳过、
发行版会按附件名字跳过已上传的文件。如果 tag 已经推出去、之后又补了提交
（HEAD 不再等于 tag），重跑会被 tag 规则拦住——这时加 --resume：它要求 tag 在
HEAD 的历史里、工作树干净，核对已发布的 Git 引用后，用 dist 里现有的包补发行版。

令牌查找顺序：--token-file > 环境变量 GITEE_TOKEN / GITEE_ACCESS_TOKEN > ~/.dsh/secrets/gitee.env。
后者的格式可以是一行 'GITEE_TOKEN=xxx'，也可以是「说明文字 "<token>"」这种带引号的单行。
令牌只用于请求，任何输出都不打印它。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.version import MajorBumpRefused, bump_checked, is_valid, read_version  # noqa: E402

CHANGELOG = ROOT / "CHANGELOG.md"
DIST_DIR = ROOT / "dist"
PACKAGE_NAME = "bili-monitor"
API_BASE = "https://gitee.com/api/v5"
DEFAULT_TOKEN_FILE = Path.home() / ".dsh" / "secrets" / "gitee.env"
SNAPSHOT_MSG = "chore(release): 发布线快照 · 不含测试文件（{branch} v{version}）"
#: 发版只允许这两个文件在提交前处于未提交状态
RELEASE_FILES = ("VERSION", "CHANGELOG.md")


class ReleaseError(RuntimeError):
    """执行期失败（退出码 1）。"""


class PreconditionError(ReleaseError):
    """前置条件不满足（退出码 2）：先修好再跑。"""


# --------------------------------------------------------------------------- 输出

def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def bad(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def head(title: str) -> None:
    print()
    print(f"== {title} " + "=" * max(0, 56 - len(title)))


# ----------------------------------------------------------------------- git 封装

def _git_env() -> dict:
    env = dict(os.environ)
    # 发版不能停在交互提示上：SSH 走 BatchMode，git 自己的密码提示直接失败
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")
    return env


def git(*args, check: bool = True, capture: bool = True, env: dict | None = None):
    proc = subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env or _git_env(),
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ReleaseError(f"git {' '.join(args)} 失败（退出码 {proc.returncode}）：{detail[:800]}")
    return proc


def git_out(*args, env: dict | None = None) -> str:
    return git(*args, env=env).stdout.strip()


def _porcelain_path(line: str) -> str:
    """git status --porcelain 的一行 -> 相对路径（处理 rename 与引号）。"""
    text = line.rstrip("\r\n")
    # 路径从第 4 列开始；但要是调用方把行首空格 strip 掉了（"M  path"），
    # 再切 3 个字符就会把路径第一个字母也切掉，所以先认一认状态列还在不在
    if len(text) > 3 and text[2] == " ":
        path = text[3:].strip()
    else:
        path = text.strip()
    if " -> " in path:
        path = path.split(" -> ")[-1]
    return path.strip('"')


def dirty_paths() -> list[str]:
    # 不能用 git_out：它会 strip 整段输出，行首那个空格是状态列的一部分，
    # 被吃掉之后 _porcelain_path 会把路径首字符一起切掉（VERSION 就认不出来了）
    text = git("status", "--porcelain").stdout
    return [_porcelain_path(ln) for ln in text.splitlines() if ln.strip()]


def python_exe() -> str:
    """自带运行时优先；源码仓库没生成 runtime/ 时退回当前解释器。"""
    runtime = ROOT / "runtime" / "python.exe"
    return str(runtime) if runtime.exists() else sys.executable


# ------------------------------------------------------------------------ 版本/日志

def current_version() -> str:
    version = read_version()
    if not is_valid(version):
        raise PreconditionError(f"VERSION 里的版本号不合法：{version!r}（要 1.2.3 这样的语义化版本）")
    return version


def read_changelog() -> str:
    try:
        return CHANGELOG.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreconditionError(f"读不到 CHANGELOG.md：{exc}")


_HEADING = re.compile(
    r"^##\s*\[(?P<version>[^\]]+)\](?:\s*-\s*(?P<date>[\d-]+))?(?:\s*(?:—|–|-){1,2}\s*(?P<title>.+))?$"
)


def changelog_section(text: str, version: str) -> str:
    """取 CHANGELOG 里 '## [version]' 到下一个二级标题之间的整段。"""
    lines = text.splitlines()
    # 用正则精确匹配 [version]：单纯 startswith 会让 0.4.1 命中 0.4.10 那一节
    wanted = re.compile(r"^## \[" + re.escape(version) + r"\](?:\s|$)")
    start = None
    for i, ln in enumerate(lines):
        if wanted.match(ln):
            start = i
            break
    if start is None:
        raise PreconditionError(
            f"CHANGELOG.md 里没有 ## [{version}] 小节："
            "先按 AGENTS.md §1/R4 写清这一版的新增 / 变更 / 修复 / 安全"
        )
    body = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        body.append(ln)
    return "\n".join(body).strip()


def release_title(section: str, version: str) -> str:
    """发行版标题：小节标题里 '—' 后面的副标题，没有就只用 vX.Y.Z。"""
    first = section.splitlines()[0].strip() if section else ""
    m = _HEADING.match(first)
    title = (m.group("title") or "").strip() if m else ""
    return f"v{version} — {title}" if title else f"v{version}"


# bump 留下的占位行只长这样；正文里顺口提到「待补充」不算骨架
# （2026-11-20：描述这条守卫自身的条目就写了「待补充」，按子串判断会自己把自己拦下）
_PLACEHOLDER_LINE = re.compile(r"^\s*-\s*待补充\s*$", re.M)


def assert_changelog_filled(section: str, version: str) -> None:
    if _PLACEHOLDER_LINE.search(section):
        raise PreconditionError(
            f"CHANGELOG.md 的 [{version}] 小节还是 bump 留下的骨架（「- 待补充」没填）："
            "先写成人话，再跑一次（这次不用带 --bump）"
        )


# ---------------------------------------------------------------------- 令牌/仓库名

_QUOTED_TOKEN = re.compile(r'"([^"\s]{16,})"')
# 引号用 \x27 / \x22 写，免得正则里塞满转义
_KEYED_TOKEN = re.compile(r"(?im)^\s*(?:GITEE_[A-Z0-9_]*|ACCESS_TOKEN|TOKEN)\s*[=:]\s*[\x27\x22]?([^\s\x27\x22]{8,})")


def parse_token(text: str) -> str:
    """从令牌文件正文里取 token：带引号的一行 / KEY=VALUE / 整行就是 token。"""
    raw = (text or "").strip()
    if not raw:
        raise PreconditionError("令牌文件是空的")
    m = _QUOTED_TOKEN.search(raw)
    if m:
        return m.group(1)
    m = _KEYED_TOKEN.search(raw)
    if m:
        return m.group(1)
    lines = [ln.strip().strip("'\"") for ln in raw.splitlines() if ln.strip()]
    if len(lines) == 1 and len(lines[0]) >= 8:
        return lines[0]
    raise PreconditionError(
        "认不出令牌文件的格式：把 token 单独放一行，或者写成 GITEE_TOKEN=xxx，"
        "也可以用「说明文字 \"<token>\"」这种带引号的单行"
    )


def resolve_token(token_file: str | None) -> tuple[str, str]:
    """返回 (token, 来源说明)；来源只说位置，不含 token 本身。"""
    if token_file:
        path = Path(token_file)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            raise PreconditionError(f"--token-file 指向的文件不存在：{path}")
        return parse_token(path.read_text(encoding="utf-8", errors="replace")), str(path)
    for key in ("GITEE_TOKEN", "GITEE_ACCESS_TOKEN"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value, f"环境变量 {key}"
    if DEFAULT_TOKEN_FILE.exists():
        return parse_token(DEFAULT_TOKEN_FILE.read_text(encoding="utf-8", errors="replace")), str(DEFAULT_TOKEN_FILE)
    raise PreconditionError(
        "找不到 Gitee 令牌：用 --token-file 指定，或设环境变量 GITEE_TOKEN，"
        f"或放到 {DEFAULT_TOKEN_FILE}；不想建发行版就加 --no-release，只做到打包为止"
    )


def parse_repo_slug(url: str) -> str:
    """git 远端地址 -> owner/repo。"""
    text = (url or "").strip()
    if not text:
        raise PreconditionError("远端地址为空：git remote -v 看看")
    if text.endswith(".git"):
        text = text[:-4]
    m = re.search(r"[:/]([^/:]+)/([^/:]+)$", text)
    if not m:
        raise PreconditionError(f"没法从远端地址解析 owner/repo：{url}")
    return f"{m.group(1)}/{m.group(2)}"


# ------------------------------------------------------------------ Gitee HTTP 封装

def encode_multipart(files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----releasegitee" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for field, filename, content in files:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def api(method: str, path: str, token: str, payload=None, files=None, timeout: int = 180, allow_404: bool = False):
    """调 Gitee v5；返回解析后的 JSON（空 / 'null' 返回 None）。令牌不进任何报错文本。"""
    url = API_BASE + path
    url += ("&" if "?" in url else "?") + "access_token=" + urlparse.quote(token, safe="")
    headers = {"Accept": "application/json", "User-Agent": "bili-monitor-release-gitee"}
    data = None
    if files:
        data, ctype = encode_multipart(files)
        headers["Content-Type"] = ctype
    elif payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定 Gitee 域名
            raw = resp.read()
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if allow_404 and exc.code in (404, 403):
            # Gitee 对「没有这个发行版」有时给 404、有时给 200 + null，两种都当不存在
            return None
        raise ReleaseError(f"Gitee {method} {path} 返回 {exc.code}：{detail}") from None
    except urlerror.URLError as exc:
        raise ReleaseError(f"连不上 Gitee（{exc.reason}）：检查网络或代理设置") from None
    text = raw.decode("utf-8", "replace").strip()
    if not text or text == "null":
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ReleaseError(f"Gitee 返回的不是 JSON（{method} {path}）：{exc}") from None


def asset_sizes(release) -> dict[str, int]:
    """已有附件 -> 字节数（拿不到大小就记 -1）。"""
    sizes: dict[str, int] = {}
    for item in (release or {}).get("assets") or []:
        if not isinstance(item, dict):
            continue
        name = ""
        size = item.get("size")
        for key in ("name", "filename"):
            if item.get(key):
                name = str(item[key])
                break
        inner = item.get("file")
        if not name and isinstance(inner, dict):
            name = str(inner.get("name") or "")
            size = inner.get("size", size)
        if name:
            try:
                sizes[name] = int(size)
            except (TypeError, ValueError):
                sizes[name] = -1
    return sizes


def asset_names(release) -> set[str]:
    return set(asset_sizes(release))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------------ 前置检查

def apply_bump(args, current: str) -> str:
    kind = args.bump.strip()
    target = kind
    if kind in ("major", "minor", "patch"):
        try:
            target = bump_checked(current, kind, allow_major=args.allow_major)
        except MajorBumpRefused as exc:
            raise PreconditionError(str(exc)) from None
    if not is_valid(target):
        raise PreconditionError(f"--bump 只接受 major / minor / patch 或 1.2.3 这样的版本号，收到 {kind!r}")
    if target == current:
        warn(f"版本号已经是 {current}，不重复 bump")
        return current
    if args.dry_run:
        print(f"  [计划] VERSION 会从 {current} 改成 {target}（--dry-run 不落盘）")
        return target
    cmd = [python_exe(), "scripts/release.py", "bump", kind]
    if args.allow_major:
        cmd.append("--allow-major")
    print(f"  → {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise ReleaseError(f"release.py bump 失败（退出码 {proc.returncode}）")
    return read_version()


def tag_commit(tag: str) -> str:
    proc = git("rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def preflight(args, version: str, tag: str) -> dict:
    facts: dict = {}

    branch = git_out("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise PreconditionError("当前是 detached HEAD：先 git switch 到一个分支再发版")
    facts["branch"] = branch
    if branch != "master":
        warn(f"当前分支是 {branch}，发布线按它的树同步（AGENTS.md §3 要求业务改动只在 master）")

    dirty = dirty_paths()
    others = [p for p in dirty if p not in RELEASE_FILES]
    if others:
        shown = "、".join(others[:8])
        more = "" if len(others) <= 8 else f"（还有 {len(others) - 8} 个）"
        raise PreconditionError(
            f"工作树里还有别的改动没提交：{shown} {more}\n"
            "         发版只允许 VERSION 与 CHANGELOG.md 未提交；先把这些改动提交或收起（git stash）"
        )
    facts["dirty"] = dirty

    head_sha = git_out("rev-parse", "HEAD")
    existing = tag_commit(tag)
    if args.resume and not existing:
        raise PreconditionError(f"--resume 需要已经存在的本地发版标签 {tag}")
    if args.resume and git_out("show", f"refs/tags/{tag}:VERSION").strip() != version:
        raise PreconditionError(f"标签 {tag} 中的 VERSION 与待续跑版本 {version} 不一致")
    if existing and existing != head_sha:
        if not getattr(args, "resume", False):
            raise PreconditionError(
                f"tag {tag} 已经存在并指向 {existing[:8]}（当前 HEAD {head_sha[:8]}）："
                "要么这个版本发过了，要么上次发版中断了。\n"
                f"         已发过就换新版本号；只是中断在最后几步（tag 已推、只想补发行版）就加 --resume 重跑"
            )
        # --resume：tag 已经推出去，后面又补了提交。只补第 8 步，不重做提交 / 快照 / 打包
        if git("merge-base", "--is-ancestor", existing, "HEAD", check=False).returncode != 0:
            raise PreconditionError(
                f"--resume 要求 tag {tag}（{existing[:8]}）在 HEAD（{head_sha[:8]}）的历史里："
                "HEAD 退回去过就说明树不是那次发版的树了，先 git log 看一眼"
            )
        if dirty:
            raise PreconditionError(
                "--resume 要求工作树干净：补提交之后不能再有未提交改动，否则 dist 里的包对不上 tag"
            )
        warn(f"--resume：沿用标签 {tag}（{existing[:8]}），只补 Gitee 发行版")
    if existing and existing == head_sha and dirty:
        # 提交步骤会造出新提交，tag 就落到旧提交上了；在写任何东西之前拦住
        raise PreconditionError(
            f"tag {tag} 已经指向当前 HEAD，但 VERSION / CHANGELOG.md 还有未提交改动："
            "提交后会打不到这个 tag 上。\n"
            f"         先 git commit 这两处，再重跑；或者 git tag -d {tag} 让脚本自己打"
        )
    facts["tag_commit"] = existing

    facts["remote_url"] = ""
    facts["slug"] = ""
    if not (args.no_push and args.no_release):
        proc = git("remote", "get-url", args.remote, check=False)
        if proc.returncode != 0:
            raise PreconditionError(f"找不到远端 {args.remote}：git remote -v 看看，或用 --remote 指定")
        facts["remote_url"] = proc.stdout.strip()
        facts["slug"] = args.repo or parse_repo_slug(facts["remote_url"])

    if args.resume:
        validate_published_refs(args.remote, tag, existing)
        changelog = git_out("show", f"refs/tags/{tag}:CHANGELOG.md")
    else:
        changelog = read_changelog()
    section = changelog_section(changelog, version)
    assert_changelog_filled(section, version)
    facts["section"] = section
    facts["title"] = args.release_name or release_title(section, version)
    if not args.no_release and not args.no_push:
        token, where = resolve_token(args.token_file)
        facts["token"] = token
        facts["token_from"] = where
    return facts


# -------------------------------------------------------------------------- 八个步骤

def run_step_tests(args) -> None:
    if args.skip_tests:
        warn("按 --skip-tests 跳过全量测试")
        return
    cmd = [python_exe(), "scripts/run_tests.py"]
    print(f"  → {' '.join(cmd)}")
    if subprocess.run(cmd, cwd=str(ROOT)).returncode != 0:
        raise ReleaseError("测试没通过：先修好再发版（确认只是环境问题时可以 --skip-tests，但要自己说清）")
    ok("全量测试通过")


def run_step_check() -> None:
    cmd = [python_exe(), "scripts/release.py", "check"]
    print(f"  → {' '.join(cmd)}")
    if subprocess.run(cmd, cwd=str(ROOT)).returncode != 0:
        raise ReleaseError("release.py check 没通过：按上面的提示逐条处理")
    ok("发布检查通过")


def run_step_commit(version: str) -> str:
    targets = [p for p in dirty_paths() if p in RELEASE_FILES]
    if not targets:
        ok("VERSION 与 CHANGELOG 没有未提交改动，跳过提交")
        return git_out("rev-parse", "HEAD")
    git("add", "VERSION", "CHANGELOG.md")
    message = f"chore(release): v{version}"
    git("commit", "-m", message)
    head_sha = git_out("rev-parse", "HEAD")
    ok(f"已提交 {message}（{head_sha[:8]}）")
    return head_sha


def run_step_tag(version: str, tag: str) -> None:
    head_sha = git_out("rev-parse", "HEAD")
    existing = tag_commit(tag)
    if existing == head_sha:
        ok(f"tag {tag} 已指向当前提交，跳过")
        return
    if existing:
        raise ReleaseError(f"tag {tag} 已存在且指向 {existing[:8]}，不是当前提交；确认后再处理")
    git("tag", "-a", tag, "-m", f"v{version}")
    ok(f"已打 annotated tag {tag}")


def run_step_snapshot(version: str, branch: str) -> str:
    if git("rev-parse", "--verify", "refs/heads/opensource", check=False).returncode != 0:
        raise PreconditionError("本地没有 opensource 分支：先按 docs/34 §9 把发布线建起来")
    parent = git_out("rev-parse", "refs/heads/opensource")
    index_file = ROOT / ".git" / "tmp-openindex-release"
    if index_file.exists():
        index_file.unlink()
    env = _git_env()
    env["GIT_INDEX_FILE"] = str(index_file)
    try:
        git("read-tree", branch + "^{tree}", env=env)
        git("rm", "--cached", "-r", "-q", "--ignore-unmatch", "--", "tests", env=env)
        git("rm", "--cached", "-q", "--ignore-unmatch", "--", ":(glob)scripts/selftest_*.py", env=env)
        tree = git_out("write-tree", env=env)
        commit = git_out(
            "commit-tree", tree, "-p", parent,
            "-m", SNAPSHOT_MSG.format(branch=branch, version=version),
            env=env,
        )
    finally:
        if index_file.exists():
            try:
                index_file.unlink()
            except OSError:
                pass
    git("update-ref", "refs/heads/opensource", commit)

    def count(rev: str) -> int:
        return len([ln for ln in git_out("ls-tree", "-r", "--name-only", rev).splitlines() if ln.strip()])

    ok(f"发布线快照 {commit[:8]}（{count(commit)} 个文件；{branch} 是 {count(branch)} 个，已剔除测试）")
    return commit


def run_step_build(version: str) -> tuple[Path, Path]:
    cmd = [python_exe(), "scripts/release.py", "build"]
    print(f"  → {' '.join(cmd)}")
    if subprocess.run(cmd, cwd=str(ROOT)).returncode != 0:
        raise ReleaseError("打包失败（退出码见上）")
    zip_path = DIST_DIR / f"{PACKAGE_NAME}-{version}.zip"
    sums = DIST_DIR / "SHA256SUMS"
    if not zip_path.exists():
        raise ReleaseError(f"打包说成功了，但没找到 {zip_path}")
    if not sums.exists():
        raise ReleaseError(f"打包说成功了，但没找到 {sums}")
    digest = sha256_file(zip_path)
    if digest not in sums.read_text(encoding="utf-8", errors="replace"):
        raise ReleaseError(f"{sums.name} 里的哈希与实算的 {digest} 不一致")
    ok(f"{zip_path.name}（{zip_path.stat().st_size / 1048576:.2f} MB）sha256 {digest}")
    return zip_path, sums


def check_existing_build(version: str) -> tuple[Path, Path]:
    """--resume 用：确认 dist 里的包还在，并且与 SHA256SUMS 对得上。"""
    zip_path = DIST_DIR / f"{PACKAGE_NAME}-{version}.zip"
    sums = DIST_DIR / "SHA256SUMS"
    if not zip_path.exists():
        raise ReleaseError(f"--resume 要沿用现有的包，但没找到 {zip_path}：去掉 --resume 重跑整条流程")
    if not sums.exists():
        raise ReleaseError(f"--resume 要沿用现有的包，但没找到 {sums}：去掉 --resume 重跑整条流程")
    digest = sha256_file(zip_path)
    if digest not in sums.read_text(encoding="utf-8", errors="replace"):
        raise ReleaseError(f"{sums.name} 里的哈希与实算的 {digest} 不一致：包被动过，去掉 --resume 重跑")
    ok(f"沿用 {zip_path.name}（{zip_path.stat().st_size / 1048576:.2f} MB）sha256 {digest}")
    return zip_path, sums


def validate_published_refs(remote: str, tag: str, commit: str) -> None:
    """续跑只接受已推送的原标签和对应的发布线快照。"""
    tag_ref = f"refs/tags/{tag}"
    snapshot_ref = "refs/heads/opensource"
    refs = {}
    for line in git_out("ls-remote", remote, tag_ref, tag_ref + "^{}", snapshot_ref).splitlines():
        oid, ref = line.split("\t", 1)
        refs[ref] = oid
    if refs.get(tag_ref + "^{}", refs.get(tag_ref)) != commit:
        raise PreconditionError(f"远端标签 {tag} 不存在或与本地发版提交不一致")
    snapshot = refs.get(snapshot_ref)
    if not snapshot or git("cat-file", "-e", snapshot + "^{commit}", check=False).returncode:
        raise PreconditionError("找不到远端发布快照的本地对象，请先 fetch opensource")
    result = git("diff", "--quiet", commit, snapshot, "--", ".",
                 ":(exclude)tests", ":(exclude,glob)scripts/selftest_*.py", check=False)
    if result.returncode:
        raise PreconditionError(f"远端 opensource 快照与标签 {tag} 内容不一致")


def run_step_push(branch: str, tag: str, remote: str) -> None:
    git("push", remote, f"{branch}:refs/heads/{branch}")
    ok(f"已推送 {branch} → {remote}")
    git("push", remote, "refs/heads/opensource")
    ok(f"已推送 opensource → {remote}")
    git("push", remote, f"refs/tags/{tag}")
    ok(f"已推送 tag {tag}")


def release_payload(tag: str, name: str, body: str, branch: str) -> dict:
    """POST /releases 的请求体。

    target_commitish 必须带：2026-11-20 实测，缺这个字段 Gitee 直接回
    400 {"messages":["target_commitish is missing"]}，发行版建不出来
    （tag 已经推上去了，只有网页那一步失败）。
    """
    return {
        "tag_name": tag,
        "target_commitish": branch,
        "name": name,
        "body": body,
        "prerelease": False,
    }


def run_step_release(facts: dict, version: str, tag: str, zip_path: Path, sums: Path, body: str) -> None:
    token = facts["token"]
    slug = facts["slug"]
    name = facts["title"]
    existing = api(
        "GET",
        f"/repos/{slug}/releases/tags/{urlparse.quote(tag, safe='')}",
        token,
        allow_404=True,
    )
    if isinstance(existing, dict) and existing.get("id"):
        release_id = existing["id"]
        api(
            "PATCH",
            f"/repos/{slug}/releases/{release_id}",
            token,
            payload={"name": name, "body": body, "prerelease": False},
        )
        ok(f"发行版已存在（id={release_id}），标题与正文已更新")
    else:
        created = api(
            "POST",
            f"/repos/{slug}/releases",
            token,
            payload=release_payload(tag, name, body, facts["branch"]),
        )
        if not isinstance(created, dict) or not created.get("id"):
            raise ReleaseError(f"Gitee 没有返回发行版 id：{str(created)[:300]}")
        release_id = created["id"]
        existing = created
        ok(f"已新建发行版 {tag}（id={release_id}）")
    have = asset_sizes(existing if isinstance(existing, dict) else {})
    for path in (zip_path, sums):
        if path.name in have:
            size = path.stat().st_size
            if have[path.name] not in (-1, size):
                warn(f"附件 {path.name} 已在，但大小 {have[path.name]} ≠ 本地 {size}：没有覆盖。"
                     "要换包请先到 Gitee 删掉旧附件再重跑")
            else:
                ok(f"附件 {path.name} 已在，跳过")
            continue
        api(
            "POST",
            f"/repos/{slug}/releases/{release_id}/attach_files",
            token,
            files=[("file", path.name, path.read_bytes())],
        )
        ok(f"已上传附件 {path.name}")
    print()
    print(f"  发行版：https://gitee.com/{slug}/releases/tag/{tag}")


# ---------------------------------------------------------------------------- 主流程

def load_body(args, section: str, version: str) -> tuple[str, str]:
    if args.body_file:
        path = Path(args.body_file)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            raise PreconditionError(f"--body-file 指向的文件不存在：{path}")
        return path.read_text(encoding="utf-8"), str(path)
    return section, f"CHANGELOG.md 的 [{version}] 小节"


def print_plan(facts: dict, version: str, tag: str, zip_path: Path, body_source: str) -> None:
    print()
    print("  将要做的事：")
    print(f"    版本       {version}")
    print(f"    分支       {facts['branch']}")
    if facts.get("resume"):
        print(f"    范围       补建发行版与附件（沿用 tag {tag} 与 dist 现有包）")
        print(f"    tag        {tag}（已存在，不再打）")
        print(f"    打包       {zip_path.name}（已存在，不再打）")
    else:
        if facts.get("dirty"):
            print(f"    提交       chore(release): v{version}")
        else:
            print("    提交       没有待提交的版本文件，跳过")
        print(f"    tag        {tag}（annotated）")
        print(f"    快照       {facts['branch']} 的树 → opensource（剔除 tests/ 与 scripts/selftest_*.py）")
        print(f"    打包       {zip_path.name}")
    if facts.get("remote_url"):
        print(f"    远端       {facts['remote_url']} → {facts['slug']}")
    print(f"    发行版名   {facts['title']}")
    print(f"    正文       {body_source}")
    if facts.get("token_from"):
        print(f"    令牌       来自 {facts['token_from']}（不会打印内容）")
    else:
        print("    发行版     不建（--no-release 或 --no-push）")


def confirm(args) -> None:
    if getattr(args, "yes", False):
        ok("按 --yes 直接放行")
        return
    if not sys.stdin.isatty():
        raise PreconditionError("非交互环境（没有终端）请加 --yes 明确放行")
    print()
    answer = input("  继续？输入 y 确认，其它任意键退出：").strip().lower()
    if answer not in ("y", "yes", "是"):
        raise PreconditionError("已取消，没有做任何改动")


def run(args) -> int:
    head("0/8 读取版本号")
    if args.resume and (args.bump or args.no_push or args.no_release):
        raise PreconditionError("--resume 只补 Gitee 发行版，不能与 --bump、--no-push 或 --no-release 合用")
    current = current_version()
    version = apply_bump(args, current) if args.bump else current
    tag = args.tag or f"v{version}"
    ok(f"版本 {version}，tag {tag}")

    head("1/8 前置检查")
    facts = preflight(args, version, tag)
    facts["resume"] = bool(args.resume)
    zip_path = DIST_DIR / f"{PACKAGE_NAME}-{version}.zip"
    body, body_source = ("", "不用正文") if (args.no_release or args.no_push) else load_body(args, facts["section"], version)
    ok("前置检查通过")
    print_plan(facts, version, tag, zip_path, body_source)

    if args.dry_run:
        print()
        ok("--dry-run：以上都检查过了，没有写任何东西")
        return 0

    confirm(args)

    commit = snapshot = ""
    if args.resume:
        head("2-7/8 跳过（--resume）")
        warn("按 --resume 补建发行版与附件")
        zip_path, sums = check_existing_build(version)
    else:
        head("2/8 全量测试")
        run_step_tests(args)

        head("3/8 发布检查")
        run_step_check()

        head("4/8 提交 VERSION 与 CHANGELOG")
        commit = run_step_commit(version)

        head("5/8 打 tag")
        run_step_tag(version, tag)

        head("6/8 发布线快照")
        snapshot = run_step_snapshot(version, facts["branch"])

        head("7/8 打包")
        zip_path, sums = run_step_build(version)

    head("8/8 推送与 Gitee 发行版")
    if args.no_push:
        warn("按 --no-push 停在本地：没有推送，也没有建发行版")
    else:
        if not args.resume:
            run_step_push(facts["branch"], tag, args.remote)
        if args.no_release:
            warn("按 --no-release 跳过了 Gitee 发行版")
        else:
            run_step_release(facts, version, tag, zip_path, sums, body)

    print()
    ok(f"发版完成：v{version}")
    if commit:
        print(f"    提交     {commit[:8]}")
    if snapshot:
        print(f"    快照     opensource {snapshot[:8]}")
    print(f"    包       {zip_path}")
    if not args.no_push and not args.no_release:
        print(f"    发行版   https://gitee.com/{facts['slug']}/releases/tag/{tag}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_gitee.py",
        description="一条命令发版：测试 → 检查 → 提交 → tag → 发布线快照 → 打包 → 推送 → Gitee 发行版。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            "  python scripts/release_gitee.py --dry-run\n"
            "  python scripts/release_gitee.py --bump patch      # 生成小节 -> 补完 -> 再跑一次\n"
            "  python scripts/release_gitee.py --yes\n"
            "  python scripts/release_gitee.py --no-release      # 只在本地出包\n"
            "  python scripts/release_gitee.py --resume          # 上次卡在第 8 步，只补发行版\n"
        ),
    )
    parser.add_argument("--bump", metavar="patch|minor|major|X.Y.Z",
                        help="先把版本号涨上去（major 还要 --allow-major；bump 后要先补 CHANGELOG 再跑一次）")
    parser.add_argument("--allow-major", action="store_true", help="允许 bump major（第一位只能人工加）")
    parser.add_argument("--tag", help="tag 名，默认 v{version}")
    parser.add_argument("--remote", default="origin", help="远端名，默认 origin")
    parser.add_argument("--repo", help="Gitee 仓库 owner/repo，默认从远端地址解析")
    parser.add_argument("--token-file", help=f"令牌文件，默认环境变量 GITEE_TOKEN 或 {DEFAULT_TOKEN_FILE}")
    parser.add_argument("--body-file", help="发行版正文文件，默认摘 CHANGELOG 该版小节")
    parser.add_argument("--release-name", help="发行版标题，默认从 CHANGELOG 小节标题的副标题取")
    parser.add_argument("--skip-tests", action="store_true", help="跳过全量测试（不建议）")
    parser.add_argument("--no-push", action="store_true", help="只做本地：提交 / tag / 快照 / 打包")
    parser.add_argument("--no-release", action="store_true", help="不建 Gitee 发行版")
    parser.add_argument("--dry-run", action="store_true", help="只做前置检查并打印计划，不写任何东西")
    parser.add_argument("--yes", action="store_true", help="不交互确认")
    parser.add_argument("--resume", action="store_true",
                        help="分支和标签已推送后，用 dist 现有包补建 Gitee 发行版和附件")
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except PreconditionError as exc:
        print()
        bad(str(exc))
        print("  已停止；修好后可以直接重跑（已提交 / tag / 已传附件都会跳过）。")
        print("  如果 tag 已经推上去、之后又补了提交，重跑会被 tag 规则拦住，改用 --resume。")
        return 2
    except ReleaseError as exc:
        print()
        bad(str(exc))
        print("  已停止；修好后可以直接重跑（已提交 / tag / 已传附件都会跳过）。")
        print("  如果 tag 已经推上去、之后又补了提交，重跑会被 tag 规则拦住，改用 --resume。")
        return 1
    except KeyboardInterrupt:
        print()
        bad("被中断")
        return 1


if __name__ == "__main__":
    sys.exit(main())
