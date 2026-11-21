"""从两份完整发行包生成一个已签名的代码增量包。"""
from __future__ import annotations

import argparse
import json
import sys
from io import BytesIO
from tempfile import TemporaryDirectory
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.online_update import (MAX_BYTES, MAX_MANIFEST, VERSION_RE, UpdateError, allowed,
                                  atomic_write, digest, package_name, rsa)
from shared.update import is_newer

DOCUMENTATION = {"CHANGELOG.md", "README.md", "AGENTS.md", "CONTRIBUTING.md"}


def inventory(archive: ZipFile) -> dict:
    prefix = None
    files = {}
    for entry in archive.infolist():
        if entry.is_dir():
            continue
        root, sep, path = entry.filename.partition("/")
        if not sep or not root.startswith("bili-monitor-") or prefix not in (None, root):
            raise UpdateError("需要由 release.py 生成的完整发行包")
        prefix = root
        if path in files:
            raise UpdateError("发行包包含重复文件")
        files[path] = entry
    return files


def build(base_path: Path, target_path: Path, private_key: Path, out: Path) -> Path:
    with ZipFile(base_path) as old, ZipFile(target_path) as new:
        before, after = inventory(old), inventory(new)
        base = old.read(before["VERSION"]).decode("utf-8").strip()
        target = new.read(after["VERSION"]).decode("utf-8").strip()
        if not (VERSION_RE.fullmatch(base) and VERSION_RE.fullmatch(target) and is_newer(target, base)):
            raise UpdateError("发行包版本无效或目标版本没有递增")
        if ("online-update.pub" not in before or "shared/online_update.py" not in before
                or "runtime/python.exe" not in before):
            raise UpdateError("基线发行包尚未配备在线更新")
        entries, contents = [], {}
        total = 0
        for path in sorted(before.keys() | after.keys()):
            if path in DOCUMENTATION or path.startswith("docs/"):
                continue
            baseline = old.read(before[path]) if path in before else None
            replacement = new.read(after[path]) if path in after else None
            if baseline == replacement:
                continue
            if not allowed(path):
                raise UpdateError(f"{path} 有变更，请使用完整发行包")
            entries.append({"path": path, "old": digest(baseline) if baseline is not None else None,
                            "new": digest(replacement) if replacement is not None else None})
            if replacement is not None:
                total += len(replacement)
                if total > MAX_BYTES:
                    raise UpdateError("增量文件超出大小限制")
                contents[path] = replacement
        manifest = json.dumps({"format": 1, "base": base, "target": target, "changes": entries},
                              ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(entries) > 4000 or len(manifest) > MAX_MANIFEST:
            raise UpdateError("增量清单超出大小限制")
        signature = rsa("sign", private_key, manifest)
        with TemporaryDirectory() as temp:
            public_key = Path(temp) / "public.xml"
            public_key.write_bytes(old.read(before["online-update.pub"]))
            rsa("verify", public_key, manifest, signature)
        buffer = BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as delta:
            delta.writestr("manifest.json", manifest)
            delta.writestr("manifest.sig", signature)
            for path, data in contents.items():
                delta.writestr("files/" + path, data)
        if len(buffer.getbuffer()) > MAX_BYTES:
            raise UpdateError("增量包超出下载大小限制")
        output = out / package_name(base, target)
        atomic_write(output, buffer.getvalue())
        return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--sign-key", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        print(build(args.base, args.target, args.sign_key, args.out))
    except (UpdateError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
