"""LiveDeck 与各 deck_* mixin 共用的协议常量、文本清洗、错误类型与轮询线程包装。"""
from __future__ import annotations

import threading

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
LIVE_API = "https://api.live.bilibili.com"
ROOM_INFO = LIVE_API + "/room/v1/Room/get_info?room_id=%s"
# 房间「看过」人数只在 getInfoByRoom（需 WBI 签名）里，get_info 不带
WATCHED_API = LIVE_API + "/xlive/web-room/v1/index/getInfoByRoom"
DANMU_INFO = LIVE_API + "/xlive/web-room/v1/index/getDanmuInfo"
SEND_MSG = LIVE_API + "/msg/send"
NAV = "https://api.bilibili.com/x/web-interface/nav"
MASTER_INFO = LIVE_API + "/live_user/v1/Master/info?uid=%s"
# 主播 uid → 直播间号：Master/info 的 room_id 多数是 null，这里才可靠
MASTER_ROOM = LIVE_API + "/room/v1/Room/getRoomInfoOld?mid=%s"
PLAY_URL = LIVE_API + "/room/v1/Room/playUrl"
PLAY_INFO = LIVE_API + "/xlive/web-room/v2/index/getRoomPlayInfo"
EMOTICON_API = LIVE_API + "/xlive/web-ucenter/v2/emoticon/GetEmoticons"

# 画质档位（qn）→ 显示名。B 站直播间不一定全档可用，实际以 accept_qn 为准。
QUALITY_LABEL = [
    (30000, "杜比"), (20000, "4K"), (15000, "2K"), (10000, "原画"),
    (400, "蓝光"), (250, "超清"), (150, "高清"), (80, "流畅"),
]
QUALITY_KEYS = {qn for qn, _ in QUALITY_LABEL}

MAX_ROOMS = 16
MAX_LISTEN = 16        # 只收弹幕、不上舞台的房间上限（不占舞台格子、不拉流）
MAX_COMBO_ROOMS = MAX_ROOMS + MAX_LISTEN   # 组合可跨舞台与只听弹幕，最多把两边的名额都用满
MAX_GROUPS = 100         # 左栏分组顺序最多记 100 个组名；实际分组数远小于它
GROUP_NAME_MAX = 40      # 与 set_group 的上限一致，超长分组名本来也写不进 subject.grp
TOKEN_TTL = 480        # 秒；到期重新签 getDanmuInfo
PLAY_TTL = 100         # 秒；FLV 直链有效期很短，缓存别太久
INFO_TTL = 60
EMO_TTL = 600          # 秒；表情包列表缓存
FACE_TTL = 7 * 86400     # 秒；头像缓存的硬过期时间，超期就不再拿来渲染
FACE_REFRESH = 6 * 3600  # 秒；头像缓存超过这个时间就后台刷新（先用旧图顶上，不闪白）
FACE_WORKERS = 6         # 补齐头像用的线程数


def clean_text(s, default: str = "") -> str:
    """B 站部分昵称带孤立代理字符（invalid UTF-8 的残留），去掉再落库/下发。"""
    if not s:
        return default
    try:
        return str(s).encode("utf-8", "replace").decode("utf-8")
    except Exception:  # noqa: BLE001
        return default


def normalize_color(v) -> str:
    """把颜色归一成 #rrggbb；认 #rgb / #rrggbb / rgb(r,g,b) / "r,g,b"，失败返回空串。

    印象色只取色相，亮度与饱和度由页面按对比度钳制，所以这里只校验格式与范围。
    """
    if not isinstance(v, str):
        return ""
    s = v.strip().lower()
    if s.startswith("#"):
        s = s[1:]
    if s.startswith("rgb(") and s.endswith(")"):
        s = s[4:-1]
    if "," in s:
        parts = [x.strip() for x in s.split(",")]
        if len(parts) != 3:
            return ""
        try:
            vals = [int(x) for x in parts]
        except ValueError:
            return ""
    else:
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) != 6:
            return ""
        try:
            vals = [int(s[i:i + 2], 16) for i in (0, 2, 4)]
        except ValueError:
            return ""
    if any(n < 0 or n > 255 for n in vals):
        return ""
    return "#%02x%02x%02x" % tuple(vals)


class DeckError(RuntimeError):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code

def _loop(name: str, fn, every: float, stop: threading.Event):
    def runner():
        while True:
            if stop.is_set():
                return
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
            if stop.wait(every):
                return
    return runner
