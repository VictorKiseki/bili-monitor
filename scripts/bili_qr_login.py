"""命令行扫码登录：给采集进程写一份独立于浏览器的 SESSDATA。

为什么不用浏览器里的 SESSDATA：那份凭据属于「浏览器里那次登录」，在浏览器点退出
登录、换小号/大号或清站点数据，服务端就会把它吊销，监控这边跟着失效。扫码登录
是采集侧自己的设备会话，之后浏览器怎么登录/退出都不影响它。

用法：
    python scripts/bili_qr_login.py                # 终端出码，扫码成功后写 config/bili.env
    python scripts/bili_qr_login.py --svg qr.svg   # 同时存 SVG（终端二维码显示不正常时用）
    python scripts/bili_qr_login.py --no-save      # 只登录并打印账号，不写配置
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.bili_login import (  # noqa: E402
    poll_login, qr_terminal, start_login, write_env_sessdata,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="B 站扫码登录（采集侧独立设备会话）")
    ap.add_argument("--env", default=str(ROOT / "config" / "bili.env"), help="凭据文件路径")
    ap.add_argument("--svg", default="", help="额外把二维码存成 SVG 文件")
    ap.add_argument("--no-save", action="store_true", help="只验证登录，不写配置文件")
    ap.add_argument("--interval", type=float, default=2.0, help="轮询间隔（秒）")
    args = ap.parse_args()

    print("[1/3] 申请二维码…")
    s = start_login()
    print(qr_terminal(s["url"]))
    print("      内容：", s["url"])
    if args.svg:
        Path(args.svg).write_text(s["svg"], "utf-8")
        print("      二维码 SVG 已存到", args.svg)
    print("[2/3] 用手机 B 站 App 扫码并确认（%d 秒内有效）" % s["expires_in"])

    last = None
    deadline = time.time() + s["expires_in"] + 10
    while time.time() < deadline:
        try:
            r = poll_login(s["qrcode_key"])
        except KeyboardInterrupt:
            print("\n[取消] 已中断，没有写入任何配置")
            return 130
        st = r.get("status")
        if st != last:
            print("      →", r.get("message") or st)
            last = st
        if st == "success":
            print("[3/3]", r["message"])
            print("      SESSDATA：", r["masked"])
            if args.no_save:
                print("      --no-save：没有写配置")
                return 0
            p = write_env_sessdata(args.env, r["sessdata"],
                                   uname=r["uname"], mid=r["mid"], source="qr")
            print("      已写入", p)
            print("      下一步：重启采集进程（管理台「重启采集进程」/ 守护脚本）后生效")
            return 0
        if st in ("expired", "error"):
            print("[失败]", r.get("message"))
            return 1
        time.sleep(max(0.5, args.interval))
    print("[超时] 二维码已过期，重新运行本脚本")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
