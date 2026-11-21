"""签名增量包、在线下载与可恢复的本地文件切换。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urljoin, urlsplit
from zipfile import ZipFile

from shared.update import GITHUB_RELEASES_API, is_newer

ROOT = Path(__file__).resolve().parents[1]
KEY = "online-update.pub"
API = GITHUB_RELEASES_API + "/tags/"
MAX_BYTES = 32 * 1024 * 1024
MAX_MANIFEST = 512 * 1024
VERSION_RE = re.compile(r"[0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8}")
SAFE_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
HEX = re.compile(r"[0-9a-f]{64}")
FOLDERS = {"channels", "collector", "core", "livedeck", "notifications", "shared", "web", "scripts"}
FIXED = {
    "scripts/supervisor.py", "scripts/service.ps1", "scripts/run_console.py",
    "shared/online_update.py", "core/weekly/store.py",
}
ACTIVE = {"downloading", "applying", "starting"}
_READY = "BILI_UPDATE_TRIAL"
_request_lock = threading.Lock()

# 利用 Windows PowerShell 自带的 .NET RSA，便携 Python 无需安装额外依赖。
_RSA = r"""
$ErrorActionPreference = 'Stop'
$csp = [System.Security.Cryptography.CspParameters]::new(24)
$rsa = [System.Security.Cryptography.RSACryptoServiceProvider]::new($csp)
$rsa.PersistKeyInCsp = $false
try {
    $rsa.FromXmlString([IO.File]::ReadAllText($env:BILI_RSA_KEY))
    if ($rsa.KeySize -lt 2048) { exit 2 }
    $content = [IO.File]::ReadAllBytes($env:BILI_RSA_DATA)
    $hash = [System.Security.Cryptography.CryptoConfig]::MapNameToOID('SHA256')
    if ($env:BILI_RSA_MODE -eq 'sign') {
        [IO.File]::WriteAllBytes($env:BILI_RSA_SIG, $rsa.SignData($content, $hash))
    } elseif (!$rsa.VerifyData($content, $hash, [IO.File]::ReadAllBytes($env:BILI_RSA_SIG))) {
        exit 1
    }
} finally { $rsa.Dispose() }
"""


class UpdateError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rsa(mode: str, key: Path, data: bytes, signature: bytes = b"") -> bytes:
    if os.name != "nt":
        raise UpdateError("签名工具和在线更新需要 Windows")
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    with tempfile.TemporaryDirectory() as work:
        content = Path(work) / "manifest"
        sig = Path(work) / "signature"
        content.write_bytes(data)
        sig.write_bytes(signature)
        env = dict(os.environ, BILI_RSA_MODE=mode, BILI_RSA_KEY=str(key.resolve()),
                   BILI_RSA_DATA=str(content), BILI_RSA_SIG=str(sig))
        proc = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", _RSA],
                              env=env, capture_output=True, timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        if proc.returncode:
            raise UpdateError("增量包签名校验或签名失败")
        return sig.read_bytes()


def allowed(path: str) -> bool:
    parts = path.split("/")
    if not all(SAFE_PART.fullmatch(part) and not part.endswith(".") and
               not re.fullmatch(r"(?i)CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", part.split(".")[0])
               for part in parts):
        return False
    if path == "VERSION":
        return True
    if len(parts) < 2 or parts[0] not in FOLDERS or path.casefold() in FIXED:
        return False
    suffix = PurePosixPath(path).suffix.lower()
    return suffix == ".py" or (parts[0] in {"web", "livedeck"} and
                               suffix in {".html", ".css", ".js", ".json", ".svg", ".png", ".ico", ".woff2"})


def locked_path(root: Path, relative: str) -> Path:
    path = root
    for name in relative.split("/"):
        path /= name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise UpdateError("更新路径含链接或重解析点")
    return path


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path = path.parent.resolve() / path.name
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        temp = Path(tmp.name)
        try:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            tmp.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False).encode("utf-8"))


def package_name(base: str, target: str) -> str:
    return f"bili-monitor-update-{base}-{target}.zip"


def _trusted_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc in
            {"api.github.com", "github.com", "release-assets.githubusercontent.com",
             "objects.githubusercontent.com"})


def _fetch(client, url: str, limit: int) -> bytes:
    deadline = time.monotonic() + 90
    for _ in range(5):
        if not _trusted_url(url):
            raise UpdateError("更新下载地址不在允许的 GitHub 域名内")
        with client.stream("GET", url) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers["location"])
                continue
            if response.status_code == 404:
                raise UpdateError("该版本尚未发布增量包，请使用完整发行包")
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes(65536):
                data.extend(chunk)
                if len(data) > limit or time.monotonic() > deadline:
                    raise UpdateError("更新下载超出大小或时间限制")
            return bytes(data)
    raise UpdateError("更新下载重定向次数过多")


def download(base: str, target: str) -> bytes:
    import httpx
    from shared import proxy

    network = proxy.validate(proxy.load())
    settings = {"trust_env": network["mode"] == "system"}
    if network["mode"] == "custom":
        settings["proxy"] = network["url"]
    with httpx.Client(timeout=10, follow_redirects=False, headers={"User-Agent": "BiliMonitor-Update", "Accept": "application/vnd.github+json"},
                      **settings) as client:
        release = json.loads(_fetch(client, API + quote("v" + target, safe=""), MAX_MANIFEST))
        if (not isinstance(release, dict) or release.get("tag_name") != "v" + target
                or release.get("draft") or release.get("prerelease")):
            raise UpdateError("GitHub 返回的发行版与请求版本不一致")
        name = package_name(base, target)
        for asset in release.get("assets") or []:
            if isinstance(asset, dict) and asset.get("name") == name:
                url = asset.get("browser_download_url")
                if not isinstance(url, str):
                    raise UpdateError("GitHub 附件缺少下载地址")
                return _fetch(client, url, MAX_BYTES)
    raise UpdateError("该版本尚未发布适用的增量包，请使用完整发行包")


class OnlineUpdate:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.work = locked_path(self.root, "data/online-update")
        self.request = self.work / "request.json"
        self.status_file = self.work / "status.json"
        self.journal = self.work / "journal.json"

    @contextmanager
    def runtime_lock(self):
        if os.name != "nt":
            yield
            return
        import msvcrt

        self.work.mkdir(parents=True, exist_ok=True)
        with (self.work / "runtime.lock").open("a+b") as handle:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise UpdateError("直播监控室或在线更新正在运行，请先关闭监控室") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def current(self) -> str:
        return (self.root / "VERSION").read_text(encoding="utf-8").strip()

    def status(self) -> dict:
        value = read_json(self.status_file)
        if self.request.exists() and value.get("state") not in ACTIVE:
            return {"state": "queued", "message": "等待守护进程处理",
                    "target": read_json(self.request).get("target", "")}
        if value.get("state") in ACTIVE and not self.request.exists() and not self.journal.exists():
            return {"state": "idle", "message": ""}
        return value or {"state": "idle", "message": ""}

    def report(self, state: str, message: str, target: str = "") -> None:
        write_json(self.status_file, {"state": state, "message": message, "target": target})

    def queue(self, target: str) -> None:
        base = self.current()
        if not (VERSION_RE.fullmatch(base) and VERSION_RE.fullmatch(target) and is_newer(target, base)):
            raise UpdateError("请求的版本无效或已安装")
        with _request_lock:
            if self.request.exists() or self.journal.exists():
                raise UpdateError("已有更新正在处理")
            write_json(self.request, {"base": base, "target": target})

    def stage(self, archive: Path, base: str, target: str) -> list[dict]:
        changes = self.work / "files"
        backup = self.work / "backup"
        for directory in (changes, backup):
            if directory.exists():
                shutil.rmtree(directory)
            directory.mkdir(parents=True)
        key = locked_path(self.root, KEY)
        with ZipFile(archive) as package:
            def read_entry(name, limit):
                info = package.getinfo(name)
                if info.file_size > limit:
                    raise UpdateError("增量包文件超出大小限制")
                return package.read(info)

            manifest = read_entry("manifest.json", MAX_MANIFEST)
            rsa("verify", key, manifest, read_entry("manifest.sig", 1024))
            data = json.loads(manifest)
            if not isinstance(data, dict) or data.get("format") != 1 or data.get("base") != base or data.get("target") != target:
                raise UpdateError("增量包版本与本机不匹配")
            entries = data.get("changes")
            if not isinstance(entries, list) or not 1 <= len(entries) <= 4000:
                raise UpdateError("增量包文件清单无效")
            seen = set()
            total = 0
            for item in entries:
                relative = item["path"]
                old, new = item["old"], item["new"]
                if (not isinstance(relative, str) or not allowed(relative) or relative.casefold() in seen
                        or old == new or any(h is not None and
                           (not isinstance(h, str) or not HEX.fullmatch(h)) for h in (old, new))):
                    raise UpdateError("增量包文件清单包含无效条目")
                seen.add(relative.casefold())
                destination = locked_path(self.root, relative)
                prior = destination.read_bytes() if destination.is_file() else None
                actual = digest(prior) if prior is not None else None
                if actual != old or (destination.exists() and prior is None):
                    raise UpdateError(f"本机文件与发行版不一致：{relative}")
                if prior is not None:
                    atomic_write(backup / relative, prior)
                if new is not None:
                    contents = read_entry("files/" + relative, MAX_BYTES - total)
                    total += len(contents)
                    if digest(contents) != new:
                        raise UpdateError(f"增量包文件校验失败：{relative}")
                    if relative.endswith(".py"):
                        compile(contents, relative, "exec")
                    atomic_write(changes / relative, contents)
            if "version" not in seen or (changes / "VERSION").read_text(encoding="utf-8").strip() != target:
                raise UpdateError("增量包 VERSION 不正确")
        return entries

    def clear_bytecode(self, relative: str) -> None:
        source = PurePosixPath(relative)
        if source.suffix != ".py":
            return
        cache = locked_path(self.root, (source.parent / "__pycache__").as_posix())
        for compiled in cache.glob(source.stem + ".*.pyc"):
            compiled.unlink()

    def apply(self, entries: list[dict], target: str) -> None:
        # 写持久化日志前再核对一次，避免准备期间用户改动文件。
        for item in entries:
            destination = locked_path(self.root, item["path"])
            actual = digest(destination.read_bytes()) if destination.is_file() else None
            if actual != item["old"] or (destination.exists() and not destination.is_file()):
                raise UpdateError(f"准备更新期间文件已变更：{item['path']}")
        write_json(self.journal, {"changes": entries, "target": target})
        for item in entries:
            path = locked_path(self.root, item["path"])
            if item["new"] is None:
                path.unlink()
            else:
                atomic_write(path, (self.work / "files" / item["path"]).read_bytes())
            self.clear_bytecode(item["path"])

    def rollback(self) -> None:
        if not self.journal.exists():
            return
        journal = read_json(self.journal)
        for item in reversed(journal["changes"]):
            path = locked_path(self.root, item["path"])
            if item["old"] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, (self.work / "backup" / item["path"]).read_bytes())
            self.clear_bytecode(item["path"])
        self.journal.unlink()
        self.report("rolled_back", "更新未通过启动检查，已恢复原版本", journal["target"])

    def finish(self, target: str) -> None:
        self.report("complete", "更新已完成，刷新页面加载新版本", target)
        self.journal.unlink()

    def mark_ready(self, token: str) -> None:
        write_json(self.work / "ready.json", {"token": token, "version": self.current()})

    def trial_accepted(self, token: str) -> bool:
        state = self.status()
        return (state.get("state") == "complete" and not self.journal.exists()
                and read_json(self.work / "ready.json").get("token") == token)


def trial_token() -> str:
    return os.environ.get(_READY, "")
