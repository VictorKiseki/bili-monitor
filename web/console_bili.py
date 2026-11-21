"""Console 的 B 站登录 / 扫码 / 服务重启部分（由 web/console.py 拆出）。"""
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

from web.console_core import ApiError  # noqa: E402

class ConsoleBili:
    def _bili_env_path(self) -> Path:
        return self.subjects_path.parent / "bili.env"

    def _read_bili_env(self) -> str:
        p = self._bili_env_path()
        try:
            return p.read_text("utf-8") if p.exists() else ""
        except OSError:
            return ""

    def _write_bili_env(self, sessdata: str | None, uname: str = "", mid: int = 0,
                        source: str = "paste") -> Path:
        """原子改写 config/bili.env 里的 SESSDATA 行，其余注释原样保留。

        具体写法统一收在 collector/bili_login.write_env_sessdata，扫码登录与本面板共用。
        """
        from collector.bili_login import write_env_sessdata
        return write_env_sessdata(self._bili_env_path(), sessdata,
                                  uname=uname, mid=mid, source=source)

    def bili_auth_status(self, force: bool = False) -> dict:
        """读配置里的 SESSDATA，并向 B 站确认它是否还有效（结果缓存 60 秒）。"""
        now = time.time()
        if not force and self._auth_cache and now - self._auth_at < 60:
            # M10：缓存里的字典不能直接交出去，否则调用方一改就把缓存改坏了
            return dict(self._auth_cache)
        out: dict = {"ok": True, "configured": False, "valid": False, "masked": "未配置",
                     "uname": "", "mid": 0, "message": "", "state": "unknown", "checked_at": int(now)}
        try:
            from collector.bili import load_sessdata, mask_sessdata, BiliClient
        except Exception as e:                    # noqa: BLE001
            out["message"] = "采集模块不可用：%s" % e
            return out
        sess = load_sessdata()
        out["configured"] = bool(sess)
        out["masked"] = mask_sessdata(sess)
        if not sess:
            out["message"] = "未配置：当前是匿名取数（空响应多、可能撞 -352）"
        else:
            try:
                cli = BiliClient(sessdata=sess)
                try:
                    info = cli.check_login()
                finally:
                    cli.close()
                out["valid"] = bool(info["ok"])
                out["uname"] = info["uname"]
                out["mid"] = info["mid"]
                out["state"] = info.get("state", "unknown")
                out["message"] = "有效" if info["ok"] else (info["message"] or "暂未确认登录状态")
            except Exception as e:                # noqa: BLE001
                out["message"] = "校验失败：%s: %s" % (type(e).__name__, e)
        # M10：存副本、返副本，谁也别持有缓存本体（层级只有一层标量，浅拷贝够）
        self._auth_cache, self._auth_at = dict(out), now
        return dict(out)

    def set_bili_auth(self, sessdata: str) -> dict:
        s = (sessdata or "").strip().strip('"').strip("'").strip()
        if not s:
            raise ApiError("SESSDATA 不能为空")
        if len(s) < 20:
            raise ApiError("SESSDATA 太短，不像有效值（一般 100 位以上）")
        try:
            from collector.bili import BiliClient, mask_sessdata
        except Exception as e:                    # noqa: BLE001
            raise ApiError("采集模块不可用：%s" % e, 500)
        cli = BiliClient(sessdata=s)
        try:
            info = cli.check_login()
        finally:
            cli.close()
        if not info["ok"]:
            message = ("SESSDATA 无效或已过期" if info.get("state") == "expired"
                       else "暂时无法校验 SESSDATA")
            raise ApiError("%s（B 站：%s），没有保存" % (message, info["message"] or "未确认登录状态"))
        p = self._write_bili_env(s, uname=info["uname"], mid=info["mid"])
        self._auth_cache, self._auth_at = None, 0.0
        return {"ok": True, "valid": True, "uname": info["uname"], "mid": info["mid"],
                "masked": mask_sessdata(s), "file": str(p), "need_restart": True,
                "message": "已保存到 %s，点「重启采集进程」生效" % p.name}

    def clear_bili_auth(self) -> dict:
        p = self._write_bili_env(None)
        self._auth_cache, self._auth_at = None, 0.0
        return {"ok": True, "cleared": True, "file": str(p), "need_restart": True,
                "message": "已清除 SESSDATA，重启后回到匿名取数"}

    def bili_qr_new(self) -> dict:
        """申请一个扫码会话，返回二维码 SVG 与轮询密钥；不写任何配置。"""
        try:
            from collector.bili_login import start_login
            s = start_login()
        except Exception as e:                    # noqa: BLE001
            raise ApiError("申请二维码失败：%s" % e, 502)
        return {"ok": True, "url": s["url"], "qrcode_key": s["qrcode_key"],
                "expires_in": s["expires_in"], "svg": s["svg"]}

    def bili_qr_poll(self, key: str) -> dict:
        """轮询扫码状态；成功才把 SESSDATA 写进 config/bili.env。"""
        try:
            from collector.bili_login import poll_login
        except Exception as e:                    # noqa: BLE001
            raise ApiError("扫码模块不可用：%s" % e, 500)
        res = poll_login(key)
        if res.get("status") != "success":
            return {"ok": True, **{k: v for k, v in res.items() if k != "sessdata"}}
        p = self._write_bili_env(res["sessdata"], uname=res["uname"], mid=res["mid"], source="qr")
        self._auth_cache, self._auth_at = None, 0.0
        return {"ok": True, "status": "success", "uname": res["uname"], "mid": res["mid"],
                "masked": res["masked"], "file": str(p), "need_restart": True,
                "message": "扫码登录成功（账号 %s），已保存 %s，点「重启采集进程」生效"
                           % (res["uname"] or "-", p.name)}

    def restart_service(self) -> dict:
        """给守护写重启请求文件，子进程会优雅退出后立刻重启（不计入崩溃）。"""
        flag = Path(self.db_path).parent / "restart.request"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), "utf-8")
        return {"ok": True, "queued": True, "file": str(flag),
                "message": "已通知守护重启采集进程，几秒后生效（日志页会看到「收到管理台的重启请求」）"}

