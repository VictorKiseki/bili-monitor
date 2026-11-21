"""C. 卡片图：Event -> PNG 文件路径（开播/动态以图片形式推送）

为什么自己画：B 站 App 的分享卡片是客户端本地合成的，没有公开接口；
用 Pillow 按同样的版式复刻，拿得到的数据（头像、封面、正文、首图、时间、链接）足够还原观感。
画不出来不阻塞业务：任何异常都返回 None，发送层自动退回纯文字。

2026-10-28 v3：
  - 链接统一画成二维码（segno），动态卡与开播卡的二维码都放大到 ≈ 资料圆角卡的高度；
  - 动态卡：整体浅色斜向渐变底 + 资料圆角卡（背景用主播主页顶部图高斯模糊，落盘缓存 7 天）+ 内容圆角卡也是浅色斜向渐变；
  - 资料卡、正文卡各自独立圆角，层次更接近 B 站 App 分享卡。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import time as _time
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from core.weekly.fetch import DOWNLOAD_MAX_BYTES, _img_url_ok  # S3：图片白名单与上限复用唯一实现
from shared.contracts import Event, EventType

log = logging.getLogger("cards")

W = 900
FONT_DIR = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "C:/Windows") / "Fonts"
PINK = (251, 114, 153)
INK = (26, 28, 34)
SUB = (122, 128, 140)
WHITE = (255, 255, 255)

NAME_BLUE = (44, 132, 194)
DATE_BLUE = (96, 168, 206)

IMG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}

# 每位主播固定的斜向渐变配色（取名字哈希），都是浅色调，白字/深字都压得住
_PALETTE = [
    ((0xa8, 0xc4, 0xff), (0xcd, 0xec, 0xff), (0xff, 0xe3, 0xf0)),
    ((0xff, 0xc0, 0xd4), (0xff, 0xe0, 0xea), (0xe6, 0xe9, 0xff)),
    ((0xa9, 0xe6, 0xd6), (0xd6, 0xf6, 0xea), (0xe8, 0xf3, 0xff)),
    ((0xc9, 0xbb, 0xff), (0xe4, 0xdd, 0xff), (0xff, 0xe6, 0xf4)),
    ((0xff, 0xd6, 0xa8), (0xff, 0xec, 0xd6), (0xff, 0xe2, 0xe9)),
    ((0xa9, 0xd6, 0xff), (0xd8, 0xec, 0xff), (0xf3, 0xe4, 0xff)),
]

TOP_TTL = 7 * 86400          # 主页顶部图缓存 7 天
AVATAR_TTL = 7 * 86400       # 本地头像回写间隔（2026-11-08）：7 天内不重复写盘
CARD_KEEP_HOURS = 24.0       # 卡片图兜底（正式清理在推送完成后立刻删）
IMG_KEEP_DAYS = 2.0          # 图源缓存：卡片删掉后没人引用，按天清
TOP_KEEP_DAYS = 30.0         # 主页顶部图：7 天内复用，30 天没再画卡就删


def _font(size: int, bold: bool = False):
    name = "msyhbd.ttc" if bold else "msyh.ttc"
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except Exception:
        return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    """按像素宽度折行；超出行数时最后一行加省略号。"""
    text = (text or "").strip()
    if not text:
        return []
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if _measure(draw, cur + ch, font) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _measure(draw, last + "…", font) > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _strip_image_tags(tokens) -> list:
    """去掉 B 站正文里的 [图片] 占位：图片另外画，正文里留一句 [图片] 只会让人困惑。"""
    out = []
    for tok in tokens or []:
        if tok and tok[0] == "t":
            s = (tok[1] or "").replace("\n[图片]\n", "\n").replace("[图片]", "")
            if s:
                out.append(["t", s])
        else:
            out.append(tok)
    return out


def _emoji_px(font) -> int:
    """内联表情的边长：略大于字身，看起来才像表情而不是小图标。"""
    return int(getattr(font, "size", 30) * 1.06) + 2


# --- Unicode emoji（💤🐔🍄 这类）--------------------------------------------
# 微软雅黑没有 emoji 字形，正文里出现 emoji 会画成一排豆腐块 □□□□。
# Segoe UI Emoji 的彩色位图配 Pillow 的 embedded_color=True 能直接画彩色，
# 所以把连续 emoji 切成独立段，用另一个字体单独绘制。
_EMOJI_FONT_PATHS = (
    str(FONT_DIR / "seguiemj.ttf"),
    str(FONT_DIR / "NotoColorEmoji.ttf"),
)
_SYM_FONT_PATHS = (
    str(FONT_DIR / "seguisym.ttf"),
    str(FONT_DIR / "DejaVuSans.ttf"),
)


@lru_cache(maxsize=16)
def _emoji_font(size: int):
    """彩色 emoji 字体；系统里没有就返回 None，调用方退回普通字体（宁可豆腐块也别崩）。"""
    for p in _EMOJI_FONT_PATHS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, max(8, int(size)))
            except OSError:
                continue
    return None


def _emoji_size(font) -> int:
    return max(8, int(getattr(font, "size", 30) * 1.05))


def _is_emoji_char(ch: str, nxt: str = "") -> bool:
    """真正需要换字体画的 emoji。★ ✓ 这类雅黑自带字形的不动，除非后面跟了 U+FE0F。"""
    o = ord(ch)
    if 0x1F000 <= o <= 0x1FAFF:      # 绝大多数 emoji（含国旗、肤色）
        return True
    if 0x2600 <= o <= 0x27BF:        # 只有显式要求彩色呈现时才算
        return nxt == "\ufe0f"
    if 0x2B00 <= o <= 0x2BFF:
        return nxt == "\ufe0f"
    return False


def _sym_font(size: int):
    """单色符号字体（✓ ☑ 这类 emoji 字体也画得出来，但彩色不好看时优先用它）。"""
    for p in _SYM_FONT_PATHS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, max(8, int(size)))
            except OSError:
                continue
    return None


@lru_cache(maxsize=8192)
def _has_glyph(font_path: str, ch: str) -> bool:
    """这个字体有没有该字形：拿 U+E000（私用区，必然缺字）的 notdef 位图做对照。"""
    try:
        f = ImageFont.truetype(font_path, 40)
        m = f.getmask(ch)
        n = f.getmask("\ue000")
        return (tuple(m.size), bytes(m)) != (tuple(n.size), bytes(n))
    except Exception:
        return True


_BODY_FONT_PATH = str(FONT_DIR / "msyh.ttc")


@lru_cache(maxsize=8192)
def _char_class(ch: str, nxt: str = "") -> str:
    """字符该怎么画："" = 正文字体直接画 / "u" = 彩色 emoji / "s" = 单色符号。"""
    if ord(ch) < 0x20:          # \n 由排版层当换行处理，别当符号
        return ""
    if _is_emoji_char(ch, nxt):
        return "u"
    if _has_glyph(_BODY_FONT_PATH, ch):
        return ""
    ef = _emoji_font(40)
    if ef is not None and getattr(ef, "path", None) and _has_glyph(ef.path, ch):
        return "u"
    sf = _sym_font(40)
    if sf is not None and getattr(sf, "path", None) and _has_glyph(sf.path, ch):
        return "s"
    return ""


def _split_emoji(s: str) -> list[tuple[str, str]]:
    """把一段文字切成 ("t", 正文) / ("u", 彩色 emoji) / ("s", 单色符号) 片段。

    ZWJ 组合、变体选择符（U+FE0F）、肤色修饰符都并进同一段，避免拆开后画成散件。
    """
    out: list[tuple[str, str]] = []
    buf, buf_kind = "", ""
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        cls = _char_class(ch, s[i + 1] if i + 1 < n else "")
        if cls:
            if buf and buf_kind != cls:
                out.append((buf_kind, buf))
                buf = ""
            buf_kind = cls
            buf += ch
            i += 1
            while i < n:
                c2 = s[i]
                o2 = ord(c2)
                if 0x1F3FB <= o2 <= 0x1F3FF:
                    # 肤色修饰符单独画会变成一个色块方块（FreeType 不做 emoji 组合），
                    # 直接吞掉：底色仍是默认肤色，但没有突兀的方块
                    i += 1
                    continue
                if o2 == 0x200D:
                    # ZWJ 同样不画（会变成一条细竖线），组合不了的 emoji 并排显示即可
                    i += 1
                    continue
                if o2 in (0xFE0F, 0xFE0E):
                    buf += c2
                    i += 1
                    continue
                if _char_class(c2, s[i + 1] if i + 1 < n else "") == cls:
                    buf += c2
                    i += 1
                    continue
                break
            continue
        if buf and buf_kind != "t":
            out.append((buf_kind, buf))
            buf = ""
        buf_kind = "t"
        buf += ch
        i += 1
    if buf:
        out.append((buf_kind, buf))
    return out


def _alt_font(kind: str, size: int):
    return _emoji_font(size) if kind == "u" else _sym_font(size)


def _alt_w(draw, text: str, font, kind: str) -> float:
    af = _alt_font(kind, _emoji_size(font))
    try:
        return float(draw.textlength(text, font=(af or font)))
    except Exception:
        return float(draw.textlength(text, font=font))


def _measure(draw, s: str, font) -> float:
    """量一段文字的真实宽度：emoji / 缺字符号按替代字体算，否则折行会溢出。"""
    w = 0.0
    for kind, chunk in _split_emoji(s or ""):
        if kind in ("u", "s"):
            w += _alt_w(draw, chunk, font, kind)
        else:
            w += float(draw.textlength(chunk, font=font))
    return w


def _draw_line(canvas, xy, text: str, font, fill,
               stroke_width: int = 0, stroke_fill=None) -> float:
    """单行画字：emoji / 雅黑缺字的符号自动换字体（基线对齐），返回画完的宽度。

    2026-11-01 修：视频卡的标题、直播卡的主播名和标题原来直接 draw.text，
    标题里的 emoji（🥳🎬）会画成空心方框，和正文卡的处理不一致。
    """
    d = ImageDraw.Draw(canvas)
    x, y = float(xy[0]), xy[1]
    for kind, chunk in _split_emoji(text or ""):
        if not chunk:
            continue
        if kind in ("u", "s"):
            af = _alt_font(kind, _emoji_size(font))
            w = _alt_w(d, chunk, font, kind)
            if af is not None:
                # 基线对齐：普通字从 ascender 顶部起画，替代字形贴同一条基线
                d.text((x, y + font.getmetrics()[0]), chunk, font=af, fill=fill,
                       anchor="ls", embedded_color=(kind == "u"))
            else:
                d.text((x, y), chunk, font=font, fill=fill,
                       stroke_width=stroke_width, stroke_fill=stroke_fill)
                w = float(d.textlength(chunk, font=font))
            x += w
            continue
        d.text((x, y), chunk, font=font, fill=fill,
               stroke_width=stroke_width, stroke_fill=stroke_fill)
        x += float(d.textlength(chunk, font=font))
    return x - xy[0]


def _layout_rich(draw, tokens, font, max_w: int, max_lines: int, emoji_px: int,
                 has_emoji: bool = True) -> tuple[list, bool]:
    """富文本排版：按像素宽度折行，支持 \n 强制换行与内联表情图。

    tokens 结构：[["t", 文本], ["e", 表情图 url, 表情名]]。
    返回 (lines, truncated)；lines 每项是段列表 ["t", 文本, 宽] / ["e", url, 名, 宽]。
    2026-10-28 修：原来正文先被 collector 截成 80 字、换行被压成空格，
    长动态必然显示不全也就谈不上换行 —— 现在正文完整保留给这里排。
    """
    lines: list[list] = [[]]
    widths: list[float] = [0.0]

    def wrap_line():
        lines.append([])
        widths.append(0.0)

    for tok in tokens or []:
        if not tok:
            continue
        kind = tok[0]
        if kind == "e" and has_emoji and len(tok) > 1 and tok[1]:
            w = float(emoji_px)
            if widths[-1] + w > max_w and lines[-1]:
                wrap_line()
            lines[-1].append(["e", tok[1], (tok[2] if len(tok) > 2 else ""), w])
            widths[-1] += w
            continue
        s = (tok[2] if kind == "e" else tok[1]) or ""
        for ckind, chunk in _split_emoji(s):
            if ckind in ("u", "s"):
                w = _alt_w(draw, chunk, font, ckind)
                if widths[-1] + w > max_w and lines[-1]:
                    wrap_line()
                lines[-1].append(["u", chunk, w])
                widths[-1] += w
                continue
            for ch in chunk:
                if ch == "\n":
                    wrap_line()
                    continue
                w = draw.textlength(ch, font=font)
                if widths[-1] + w > max_w and lines[-1]:
                    wrap_line()
                if lines[-1] and lines[-1][-1][0] == "t":
                    lines[-1][-1][1] += ch
                    lines[-1][-1][2] += w
                else:
                    lines[-1].append(["t", ch, w])
                widths[-1] += w

    # 连续空行最多留一行，否则长文里的空行堆叠会把卡片撑得很难看
    cleaned: list[list] = []
    blanks = 0
    for ln in lines:
        if not ln:
            blanks += 1
            if blanks > 1:
                continue
            cleaned.append([])
        else:
            blanks = 0
            cleaned.append(ln)
    while cleaned and not cleaned[-1]:
        cleaned.pop()

    truncated = False
    if len(cleaned) > max_lines:
        cleaned = cleaned[:max_lines]
        truncated = True
        last = cleaned[-1]
        ell = "…"
        ew = draw.textlength(ell, font=font)
        if last and last[-1][0] in ("u", "s") and sum(x[2] for x in last) + ew > max_w:
            # 末段是 emoji：放不下就把整段拿掉，再补省略号
            last.pop()
        if last and last[-1][0] == "t":
            while last[-1][1] and last[-1][2] + ew > max_w:
                last[-1][1] = last[-1][1][:-1]
                last[-1][2] = draw.textlength(last[-1][1], font=font) if last[-1][1] else 0.0
            last[-1][1] += ell
            last[-1][2] += ew
        else:
            if len(cleaned) < max_lines:
                cleaned.append([["t", ell, ew]])
            else:
                cleaned[-1] = [["t", ell, ew]]
    return cleaned, truncated


def _draw_rich(canvas, lines, x: int, y: int, font, line_h: int, fill,
               emojis: dict | None, emoji_px: int) -> int:
    """把排版好的富文本画到 RGBA 画布上，返回画完后的 y。"""
    d = ImageDraw.Draw(canvas)
    emoji_ops = []
    for ln in lines:
        cx = x
        for seg in ln:
            if seg[0] == "t":
                if seg[1]:
                    d.text((cx, y), seg[1], font=font, fill=fill)
            elif seg[0] in ("u", "s"):
                af = _alt_font(seg[0], _emoji_size(font))
                if af is not None and seg[1]:
                    # 基线对齐：普通文字用 "la" 从 ascender 顶部起画，替代字形贴同一条基线
                    d.text((cx, y + font.getmetrics()[0]), seg[1], font=af, fill=fill,
                           anchor="ls", embedded_color=(seg[0] == "u"))
                elif seg[1]:
                    d.text((cx, y), seg[1], font=font, fill=fill)
            else:
                im = (emojis or {}).get(seg[1])
                if im is not None:
                    emoji_ops.append((im, int(cx), int(y + (line_h - emoji_px) // 2)))
            cx += seg[-1]          # 末位统一是宽度：文本段 3 项、表情段 4 项
        y += line_h
    # 表情最后合成：alpha_composite 会改画布，先画完文字再贴图更稳
    for im, ex, ey in emoji_ops:
        tile = im.resize((emoji_px, emoji_px), Image.LANCZOS).convert("RGBA")
        canvas.alpha_composite(tile, (ex, ey))
    return y


def _count_rich(lines) -> int:
    return len(lines or [])

def _grad(w: int, h: int, c1, c2) -> Image.Image:
    strip = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        strip.putpixel((0, y), tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    return strip.resize((w, h))


def _diag(w: int, h: int, c1, c2, c3=None) -> Image.Image:
    """斜向（左上 -> 右下）浅色渐变；小图插值放大，避免逐像素算大图。"""
    step = 8
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for y in range(64):
        for x in range(64):
            t = (x + y) / 126.0
            if c3 is not None and t < 0.5:
                a, b, tt = c1, c3, t * 2
            elif c3 is not None:
                a, b, tt = c3, c2, (t - 0.5) * 2
            else:
                a, b, tt = c1, c2, t
            px[x, y] = tuple(int(a[i] + (b[i] - a[i]) * tt) for i in range(3))
    return small.resize((w, h), Image.BICUBIC)


def _to_rgb(im: Image.Image) -> Image.Image:
    """带透明通道的图片（B 站 opus 常见）转 RGB 会变黑底，必须先垫白。"""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, (0, 0), im)
        return bg
    return im.convert("RGB")


def _cover(im: Image.Image, w: int, h: int) -> Image.Image:
    im = _to_rgb(im)
    r = max(w / im.width, h / im.height)
    im2 = im.resize((max(w, int(im.width * r)), max(h, int(im.height * r))))
    x = (im2.width - w) // 2
    y = (im2.height - h) // 2
    return im2.crop((x, y, x + w, y + h))


def _circle(im: Image.Image, size: int) -> Image.Image:
    im = _cover(im, size, size)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _paste_round(base: Image.Image, im: Image.Image, xy, radius: int = 20) -> None:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.size[0] - 1, im.size[1] - 1),
                                           radius, fill=255)
    base.paste(im, xy, mask)


def _pill(draw, xy, text: str, font, bg=PINK, fg=WHITE, h: int = 38, pad_x: int = 16) -> None:
    x, y = xy
    tw = draw.textlength(text, font=font)
    draw.rounded_rectangle((x, y, x + tw + pad_x * 2, y + h), radius=h // 2, fill=bg)
    draw.text((x + pad_x, y + h / 2), text, font=font, fill=fg, anchor="lm")


def _ts(v: int | None) -> str:
    if not v:
        return ""
    try:
        return datetime.fromtimestamp(int(v)).strftime("%Y年%m月%d日 %H:%M:%S")
    except Exception:
        return ""


def _big(n) -> str:
    try:
        n = int(n or 0)
    except Exception:
        return "-"
    if n >= 10000:
        return "%.1f万" % (n / 10000)
    return str(n)


def _qr(url: str, box: int, quiet: int = 1) -> Image.Image | None:
    """把链接画成二维码，**铺满给定的 box**（先按整数倍画大，再 LANCZOS 缩到精确尺寸）。

    quiet 是码图自带的静区模块数，白边主要由外层的圆角白底留（pad），这里取 1 就够。
    """
    if not url:
        return None
    try:
        import segno
    except Exception:
        log.warning("未安装 segno，卡片不显示二维码（pip install segno）")
        return None
    try:
        q = segno.make(url, error="m")
        m = [list(row) for row in q.matrix]
    except Exception:
        log.exception("二维码生成失败 %s", url[:60])
        return None
    n = len(m)
    total = n + quiet * 2
    scale = max(2, (box * 2) // total)     # 先画大一倍以上，缩放后边缘才干净
    im = Image.new("RGB", (total * scale, total * scale), WHITE)
    d = ImageDraw.Draw(im)
    for y, row in enumerate(m):
        for x, v in enumerate(row):
            if v:
                d.rectangle(((x + quiet) * scale, (y + quiet) * scale,
                             (x + quiet + 1) * scale - 1, (y + quiet + 1) * scale - 1),
                            fill=(18, 20, 26))
    return im.resize((box, box), Image.LANCZOS)


def _light(c, k: float = 0.82):
    """把颜色往白色里混，得到浅色调（k 越大越白）。"""
    return tuple(int(c[i] + (255 - c[i]) * k) for i in range(3))


def _qr_badge(canvas: Image.Image, xy, url: str, size: int = 120, radius: int = 18,
              tint=None, pad: int | None = None) -> None:
    """圆角白底二维码。

    v5 起**不再染底色、不画描边**：底色是接近纯白的半透明圆角块（只留 4% 透出卡片底），
    扫码对比度最高，也不会在浅色卡上出现一块彩色方块。tint 保留但只用于无码时的占位。
    pad 是白边宽度：必须大到让二维码的直角落在圆角弧线**里面**（pad >= radius*0.293），
    否则二维码的角会看起来戳出圆角卡之外。
    """
    x, y = xy
    if pad is None:
        pad = max(8, size // 15)
    if pad < radius * 0.293 + 1:
        pad = int(radius * 0.293) + 2
    box = size + pad * 2
    tile = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    td.rounded_rectangle((0, 0, box - 1, box - 1), radius=radius, fill=(255, 255, 255, 246))
    q = _qr(url, size, quiet=0)
    if q is not None:
        tile.paste(q, (pad, pad))
    else:
        td.text((box // 2, box // 2), "链接", font=_font(22), fill=(150, 156, 168), anchor="mm")
    sh = Image.new("RGBA", (box + 16, box + 16), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((8, 8, box + 7, box + 7), radius=radius,
                                         fill=(90, 102, 124, 40))
    sh = sh.filter(ImageFilter.GaussianBlur(4))
    canvas.alpha_composite(sh, (x - 8, y - 8))
    canvas.alpha_composite(tile, (x, y))


def _spaced_text(draw, xy, text: str, font, fill, spacing: float = 1.2,
                 stroke_width: int = 0, stroke_fill=None) -> float:
    """带字距的文字（中文默认排得太挤，日期这类小字加一点字距更好看）。

    stroke_width / stroke_fill 用来描边——比偏移一像素画两遍的「影子」干净，
    在浅色渐变底上不会糊成一团。
    """
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        x += draw.textlength(ch, font=font) + spacing
    return x - xy[0]


def _grad_piece(base: Image.Image, xy, text: str, font, c1, c2, stroke_w: int,
                stroke_fill, grad_w: int = 0, grad_off: int = 0) -> float:
    """一段（不含 emoji 的）文字：描边 + 渐变字身。返回该段的宽度。

    grad_w / grad_off 让多段共用同一条渐变：标题被 emoji 切开时，
    每段各自从 c1 渐变到 c2 会看出断层，所以先按整行宽度取渐变再裁。
    """
    # 用 textbbox（含描边）算尺寸：getbbox 是「字形墨迹框」，ascender 偏移会让字底被切掉
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    bx0, by0, bx1, by1 = probe.textbbox((0, 0), text, font=font, stroke_width=stroke_w)
    m = stroke_w + 2
    size = (max(1, bx1 - bx0) + m * 2, max(1, by1 - by0) + m * 2)
    pos = (int(xy[0] + bx0 - m), int(xy[1] + by0 - m))
    ox, oy = -bx0 + m, -by0 + m
    # 1) 描边层
    smask = Image.new("L", size, 0)
    ImageDraw.Draw(smask).text((ox, oy), text, font=font, fill=255,
                               stroke_width=stroke_w, stroke_fill=255)
    stroke = Image.new("RGBA", size, stroke_fill)
    base.alpha_composite(Image.composite(stroke, Image.new("RGBA", size, (0, 0, 0, 0)), smask), pos)
    # 2) 渐变字身
    tmask = Image.new("L", size, 0)
    ImageDraw.Draw(tmask).text((ox, oy), text, font=font, fill=255)
    gw = max(size[0], int(grad_w or size[0]))
    grad = _grad(gw, size[1], c1, c2).convert("RGBA")
    off = max(0, min(gw - size[0], int(grad_off)))
    grad = grad.crop((off, 0, off + size[0], size[1]))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.paste(grad, (0, 0), tmask)
    base.alpha_composite(layer, pos)
    return float(probe.textlength(text, font=font))


def _grad_text(base: Image.Image, xy, text: str, font, c1, c2,
               stroke_w: int = 3, stroke_fill=(255, 255, 255, 235)) -> None:
    """渐变字身 + 白色描边的标题字（Pillow 没有渐变色文字，用蒙版贴出来）。

    2026-11-01 修：原来整串丢进蒙版，emoji 会被当成「实心字形」涂上渐变色
    （变成一块彩色剪影）。现在按 emoji 拆段：文字段走渐变，emoji 段用彩色字体直画。
    """
    if not text:
        return
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    total = int(_measure(probe, text, font)) + stroke_w * 2 + 6
    x = float(xy[0])
    gd = ImageDraw.Draw(base)
    for kind, chunk in _split_emoji(text):
        if not chunk:
            continue
        if kind in ("u", "s"):
            af = _alt_font(kind, _emoji_size(font))
            w = _alt_w(probe, chunk, font, kind)
            if af is not None:
                gd.text((x, xy[1] + font.getmetrics()[0]), chunk, font=af,
                        fill=(0, 0, 0, 255), anchor="ls", embedded_color=(kind == "u"))
            x += w
            continue
        x += _grad_piece(base, (x, xy[1]), chunk, font, c1, c2, stroke_w, stroke_fill,
                         total, int(x - xy[0]))


class CardMaker:
    """Event -> 卡片 PNG。out/imgs/tops 都是磁盘缓存，同一 event_id 只画一次。"""

    def __init__(self, out_dir: str = "data/cache/cards", img_dir: str = "data/cache/img",
                 top_dir: str | None = None, timeout: float = 12.0, enabled: bool = True):
        self.out = Path(out_dir)
        self.imgs = Path(img_dir)
        self.tops = Path(top_dir) if top_dir else self.imgs.parent / "top"
        # 本地头像兜底（2026-11-08）：data/cache/img 的上一级就是 data，和每日推送脚本
        # scripts/daily_post.py 共用一个 data/avatars 目录
        self.avatars = self.imgs.parent.parent / "avatars"
        self.timeout = timeout
        self.enabled = enabled
        self._http: httpx.AsyncClient | None = None
        self.gc()

    def _gc_dir(self, d: Path, cutoff: float) -> tuple[int, int]:
        n = 0
        nb = 0
        try:
            for f in d.glob("*"):
                if not f.is_file():
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_mtime >= cutoff:
                    continue
                try:
                    f.unlink()
                except OSError:
                    continue
                n += 1
                nb += st.st_size
        except OSError:
            pass
        return n, nb

    def gc(self, card_hours: float | None = None, img_days: float | None = None,
           top_days: float | None = None) -> dict:
        """按时间清缓存（兜底）：卡片图、图源、没人再画卡的主页顶部图。

        卡片图的正式清理不在这里：一条事件的所有投递都到终态时，
        Dispatcher 会调 discard() 立刻删掉。这里只兜两种情况——进程崩在
        「已发送」和「删文件」之间，或事件行被删了没人再调 discard。
        """
        now = _time.time()
        ch = CARD_KEEP_HOURS if card_hours is None else float(card_hours)
        idays = IMG_KEEP_DAYS if img_days is None else float(img_days)
        tdays = TOP_KEEP_DAYS if top_days is None else float(top_days)
        cn, cb = self._gc_dir(self.out, now - ch * 3600.0)
        ino, ib = self._gc_dir(self.imgs, now - idays * 86400.0)
        tn, tb = self._gc_dir(self.tops, now - tdays * 86400.0)
        res = {"cards": cn, "cards_mb": round(cb / 1048576.0, 1),
               "imgs": ino, "imgs_mb": round(ib / 1048576.0, 1),
               "tops": tn, "tops_mb": round(tb / 1048576.0, 1)}
        if cn or ino or tn:
            log.info("缓存清理：卡片 %d 张 %.1f MB、图源 %d 张 %.1f MB、顶部图 %d 张 %.1f MB",
                     cn, cb / 1048576.0, ino, ib / 1048576.0, tn, tb / 1048576.0)
        return res

    def card_path(self, ev: Event) -> Path | None:
        """这条事件对应的卡片文件路径（还没画也返回路径，可能就是 None 表示这类事件不画卡）。"""
        if not self.enabled:
            return None
        if ev.type is EventType.LIVE_START:
            kind = "live"
        elif ev.type is EventType.VIDEO:
            kind = "video"
        elif ev.type in (EventType.DYNAMIC, EventType.ARTICLE):
            kind = "post"
        else:
            return None
        hid = hashlib.sha1(ev.event_id.encode("utf-8")).hexdigest()[:16]
        return self.out / ("%s_%s.png" % (kind, hid))

    def discard(self, ev: Event) -> bool:
        """这条事件已经发完了：把卡片图删掉（多条投递共用一张，调用方先确认都到终态）。"""
        p = self.card_path(ev)
        if p is None:
            return False
        ok = False
        for f in (p, p.with_name(p.name + ".tmp")):
            try:
                if f.exists():
                    f.unlink()
                    ok = True
            except OSError:
                pass
        return ok

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _img(self, url: str | None, retries: int = 1) -> Image.Image | None:
        """取一张图进卡片。S3（2026-11-14）：url 来自 B 站 JSON，等于半个不可信输入。

        以前这里只有一个 startswith("http") 判断：任意主机都能下、下载不设上限、
        content-type 不校验、302 之后不复检 —— 与 README「图片下载只允许 B 站图床、
        流式硬限 20 MiB」不符。现在与 core/weekly/fetch.py 的候选图下载用同一套边界：
        域名白名单（跳转后按最终地址再校验一次）+ 流式读取 + 20 MiB 硬上限 + image/* 校验。
        file:// 只允许读本类的图片缓存目录（预览脚本的样张就在里面）。
        """
        if not url:
            return None
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("file:"):
            # 本地已缓存的图（预览脚本传 file:// URI 时用）：只认缓存目录内的文件
            try:
                from urllib.request import url2pathname
                from urllib.parse import urlparse
                path = Path(url2pathname(urlparse(url).path)).resolve()
                root = self.imgs.resolve()
                if path != root and root not in path.parents:
                    log.warning("拒绝读取缓存目录外的本地图 %s", str(path)[:120])
                    return None
                return _to_rgb(Image.open(path))
            except Exception as e:
                log.debug("读本地图失败 %s %s", url[:80], e)
                return None
        if not _img_url_ok(url):
            log.warning("拒绝下载非白名单图片 %s（只允许 B 站图床）", str(url)[:100])
            return None
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        p = self.imgs / (key + ".img")
        if p.exists() and p.stat().st_size > 0:
            try:
                return _to_rgb(Image.open(p))
            except Exception:
                pass
        for i in range(retries + 1):
            try:
                async with self._client().stream("GET", url, headers=IMG_HEADERS) as r:
                    if not _img_url_ok(str(r.url)):
                        raise ValueError("跳转后的地址不在白名单内")
                    if r.status_code != 200:
                        raise ValueError("HTTP %s" % r.status_code)
                    ctype = (r.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                    if not ctype.startswith("image/"):
                        raise ValueError("响应不是图片：%s" % (ctype or "空 Content-Type"))
                    buf = bytearray()
                    async for chunk in r.aiter_bytes(65536):
                        buf += chunk
                        if len(buf) > DOWNLOAD_MAX_BYTES:
                            raise ValueError("图片超过 %.1fMB 上限" % (DOWNLOAD_MAX_BYTES / 1048576.0))
                if not buf:
                    raise ValueError("空响应")
                data = bytes(buf)
                self.imgs.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                return _to_rgb(Image.open(io.BytesIO(data)))
            except ValueError as e:
                # 协议/安全层面的失败：重试没用，也不能把脏数据写进缓存
                log.warning("放弃取图 %s：%s", str(url)[:80], e)
                return None
            except Exception as e:
                log.debug("取图失败 %s %s", url[:60], e)
        return None

    # ---------- 头像（远端漏字段/404 时用本地缓存兜底） ----------

    async def face(self, uid: int, url: str | None) -> Image.Image | None:
        """主播头像：优先按 URL 取，取不到就用本地 data/avatars/<uid> 兜底。

        2026-11-08 实测：批量直播接口偶尔把 uname/face 漏成空，卡片上就是个空白圆。
        远端拿到了顺手回写一份本地头像，下次远端不给也有得用。
        """
        im = await self._img(url)
        p = self.avatars / ("%s.jpg" % uid)
        if im is not None:
            try:
                self.avatars.mkdir(parents=True, exist_ok=True)
                if not p.exists() or _time.time() - p.stat().st_mtime > AVATAR_TTL:
                    im.save(p, "JPEG", quality=92)
            except Exception as e:                     # noqa: BLE001
                log.debug("头像缓存写入失败 uid=%s %s", uid, e)
            return im
        for cand in (p, self.avatars / ("%s.png" % uid)):
            try:
                if cand.exists() and cand.stat().st_size > 0:
                    return _to_rgb(Image.open(cand))
            except Exception:                          # noqa: BLE001
                continue
        return None

    # ---------- 主播主页顶部图（资料卡背景，落盘缓存 7 天） ----------

    async def top_photo(self, uid: int) -> Image.Image | None:
        p = self.tops / ("%s.img" % uid)
        try:
            if p.exists() and p.stat().st_size > 0 and _time.time() - p.stat().st_mtime < TOP_TTL:
                return _to_rgb(Image.open(p))
        except Exception:
            pass
        url = await self._top_url(uid)
        if not url:
            return None
        im = await self._img(url)
        if im is not None:
            try:
                self.tops.mkdir(parents=True, exist_ok=True)
                im.save(p, "PNG")
            except Exception:
                log.debug("顶部图缓存写入失败 uid=%s", uid)
        return im

    async def _top_url(self, uid: int) -> str:
        """x/space/acc/info 免签名（WBI 版本反而 -352）；-799 限流时静默返回空。"""
        try:
            r = await self._client().get("https://api.bilibili.com/x/space/acc/info",
                                         params={"mid": int(uid)}, headers=IMG_HEADERS)
            if r.status_code != 200:
                return ""
            j = r.json() or {}
            if j.get("code") != 0:
                log.debug("顶部图取不到 uid=%s code=%s", uid, j.get("code"))
                return ""
            d = j.get("data") or {}
            v2 = d.get("top_photo_v2") or {}
            url = v2.get("l_img") or ""
            if not url and d.get("top_photo"):
                url = "https://i0.hdslb.com/" + str(d["top_photo"]).lstrip("/")
            return url if url.startswith("http") else ""
        except Exception as e:
            log.debug("顶部图接口异常 uid=%s %s", uid, e)
            return ""

    async def build(self, ev: Event) -> str | None:
        out = self.card_path(ev)
        if out is None:
            return None
        kind = out.name.split("_", 1)[0]
        if out.exists() and out.stat().st_size > 0:
            return str(out)
        try:
            if kind == "live":
                img = await self._live_card(ev)
            elif kind == "video":
                img = await self._video_card(ev)
            else:
                img = await self._post_card(ev)
        except Exception:
            log.exception("画卡片失败 %s", ev.event_id)
            return None
        if img is None:
            return None
        self.out.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        img.convert("RGB").save(tmp, "PNG")
        tmp.replace(out)
        return str(out)

    # ---------- 表情图 ----------

    async def _emojis(self, tokens) -> dict:
        """把 token 里出现的表情图下载到本地缓存，返回 {url: Image}。

        表情图同样走 _img 的磁盘缓存；拿不到就按占位文字（[表情名]）画，
        不能让一个 404 的表情把整张卡片搞挂。
        """
        urls = [t[1] for t in (tokens or []) if t and t[0] == "e" and len(t) > 1 and t[1]]
        urls = list(dict.fromkeys(urls))[:40]
        if not urls:
            return {}
        got = await asyncio.gather(*[self._img(u) for u in urls], return_exceptions=True)
        out = {}
        for u, im in zip(urls, got):
            if isinstance(im, Image.Image):
                out[u] = im
        return out

    # ---------- 顶部资料卡（动态卡 / 视频卡共用） ----------

    async def _header(self, canvas: Image.Image, ev: Event, uname: str, label: str,
                      c1, c2, c3, pill_bg=(255, 255, 255, 236),
                      pill_fg=NAME_BLUE) -> int:
        """资料圆角卡：主页顶部图高斯模糊 + 头像 + 昵称（渐变字）+ 日期 + 胶囊 + 二维码。

        返回内容圆角卡的起始 y。两处版式共用一份实现，避免改一处忘另一处。
        """
        face = await self.face(ev.subject_uid, ev.extra.get("face"))
        top = await self.top_photo(ev.subject_uid)
        prof_h = 152
        prof_w = W - 44
        prof = Image.new("RGBA", (prof_w, prof_h), (255, 255, 255, 255))
        bg_img = top if top is not None else face
        if bg_img is not None:
            blurred = _cover(bg_img, prof_w, prof_h).filter(ImageFilter.GaussianBlur(12))
            prof = blurred.convert("RGBA")
            # 白色柔化做成左浓右淡：左边压住昵称保证可读，右边留出顶部图本身的纹理
            veil = Image.new("RGBA", (64, 1))
            vd = ImageDraw.Draw(veil)
            for x in range(64):
                t = x / 63.0
                vd.point((x, 0), fill=(255, 255, 255, int(170 - 110 * t)))
            veil = veil.resize((prof_w, prof_h), Image.BILINEAR)
            prof = Image.alpha_composite(prof, veil)
        else:
            prof = _diag(prof_w, prof_h, c1, c2, c3).convert("RGBA")
            prof = Image.alpha_composite(prof, Image.new("RGBA", (prof_w, prof_h), (255, 255, 255, 70)))
        mask = Image.new("L", (prof_w, prof_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, prof_w - 1, prof_h - 1), 28, fill=255)
        canvas.paste(prof, (22, 22), mask)
        d = ImageDraw.Draw(canvas)
        d.rounded_rectangle((22, 22, 22 + prof_w - 1, 22 + prof_h - 1), 28,
                            outline=(255, 255, 255, 210), width=2)

        av = 108
        ax, ay = 22 + 26, 22 + (prof_h - av) // 2
        if face is not None:
            ring = Image.new("RGBA", (av + 10, av + 10), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse((0, 0, av + 9, av + 9), fill=(255, 255, 255, 238))
            canvas.paste(ring, (ax - 5, ay - 5), ring)
            cimg = _circle(face, av)
            canvas.paste(cimg, (ax, ay), cimg)
        else:
            circ = Image.new("RGBA", (av + 10, av + 10), (0, 0, 0, 0))
            cd = ImageDraw.Draw(circ)
            cd.ellipse((0, 0, av + 9, av + 9), fill=(255, 255, 255, 240))
            cd.ellipse((5, 5, av + 4, av + 4), fill=tuple(c1))
            canvas.paste(circ, (ax - 5, ay - 5), circ)
            d.text((ax + av // 2, ay + av // 2 - 2), (uname or "?")[0], font=_font(50, True),
                   fill=WHITE, anchor="mm")

        tx = ax + av + 24
        f_name = _font(40, True)
        f_date = _font(22)
        f_pill = _font(20, True)

        # 二维码：嵌在资料卡内部右侧，纯白圆角静区（不染色）
        qr_size = 116
        qr_pad = 10
        qr_box = qr_size + qr_pad * 2
        qx = 22 + prof_w - 14 - qr_box
        qy = 22 + (prof_h - qr_box) // 2
        _qr_badge(canvas, (qx, qy), ev.url or "", qr_size, radius=18, tint=None, pad=qr_pad)

        pill_w = d.textlength(label, font=f_pill) + 32
        show_pill = (qx - 14 - pill_w) - tx > 300
        name_max = (qx - 14 - pill_w if show_pill else qx) - tx - 12
        shown = uname
        while shown and _measure(d, shown + "…", f_name) > max(120, name_max):
            shown = shown[:-1]
        if shown and shown != uname:
            shown += "…"

        _grad_text(canvas, (tx, 22 + 26), shown or uname, f_name, NAME_BLUE, (146, 96, 208))
        d2 = ImageDraw.Draw(canvas)
        date_txt = _ts(ev.occurred_at)
        # 发布时间：字号 22，白色描边（原来偏移 1px 画两遍做「影子」，浅色底上发糊）
        _spaced_text(d2, (tx + 2, 22 + 82), date_txt, f_date, (78, 140, 182), spacing=1.1,
                     stroke_width=2, stroke_fill=(255, 255, 255, 225))
        if show_pill:
            _pill(d2, (qx - 14 - pill_w, 22 + 20), label, f_pill,
                  bg=pill_bg, fg=pill_fg, h=32, pad_x=15)
        return 22 + prof_h + 14

    # ---------- 内容圆角卡底 ----------

    def _content_card(self, canvas: Image.Image, y: int, h: int, c1, c2, c3) -> Image.Image:
        cw = W - 44
        card_bg = _diag(cw, h, c1, c2, c3).convert("RGBA")
        card_bg = Image.alpha_composite(card_bg, Image.new("RGBA", (cw, h), (255, 255, 255, 120)))
        sh = Image.new("RGBA", (W, canvas.height), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle((22, y + 6, W - 22, y + h + 2), 30,
                                             fill=(110, 130, 160, 45))
        sh = sh.filter(ImageFilter.GaussianBlur(10))
        canvas = Image.alpha_composite(canvas, sh)
        mask2 = Image.new("L", (cw, h), 0)
        ImageDraw.Draw(mask2).rounded_rectangle((0, 0, cw - 1, h - 1), 30, fill=255)
        canvas.paste(card_bg, (22, y), mask2)
        return canvas

    # ---------- 动态 / 专栏卡 ----------

    async def _post_card(self, ev: Event) -> Image.Image | None:
        uname = ev.uname or ("UID%s" % ev.subject_uid)
        label = {"DYNAMIC": "新动态", "ARTICLE": "新专栏"}.get(ev.type.value, "新动态")
        c1, c2, c3 = _PALETTE[sum(ord(c) for c in uname) % len(_PALETTE)]
        f_title = _font(36, True)
        f_body = _font(31)
        f_qname = _font(23, True)
        f_qtitle = _font(28, True)
        f_qtext = _font(28)
        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        inner_w = W - 2 * 22 - 2 * 40      # 内容卡内宽

        title = ev.title if ev.type is EventType.DYNAMIC else ""
        if ev.type is EventType.DYNAMIC and ev.subtype == "FORWARD":
            title = "转发了 @%s" % (ev.extra.get("forward_from") or "动态")
        # 正文：v6 起用完整原文（保留换行），旧事件只有 summary 也能退化显示
        raw_text = (ev.extra.get("text") or ev.summary or "").strip()
        tokens = _strip_image_tags(ev.extra.get("rich")
                                   or ([["t", raw_text]] if raw_text else []))
        emojis = await self._emojis(tokens)
        body_px = _emoji_px(f_body)
        body_lines, body_cut = _layout_rich(probe, tokens, f_body, inner_w, 30, body_px,
                                           bool(emojis))

        # 转发原文：作者 + 原文（含表情）+ 首图，单独一块引用卡
        q_inner = inner_w - 2 * 22 - 6
        q = await self._quote_parts(ev, probe, q_inner=q_inner, f_qtitle=f_qtitle, f_qtext=f_qtext)
        hero = await self._img(ev.cover_url) if (ev.cover_url and not q["fwd"]) else None

        title_lines = _wrap(probe, title, f_title, inner_w, 2)
        # 兜底数据（desktop 端点）可能既没正文也没图，画出来是一张空卡 —— 不如退回纯文字
        if not (title_lines or body_lines or hero is not None or q["title_lines"]
                or q["lines"] or q["img"] is not None):
            log.info("动态 %s 没有正文也没有配图，跳过卡片走纯文字", ev.event_id)
            return None

        pad_top, pad_x, pad_bot = 40, 40, 30
        title_h = len(title_lines) * 54
        body_h = len(body_lines) * 48
        gap_after_body = 26 if body_cut else 0
        q_pad, q_hdr = 20, 34
        quote_h = self._quote_height(q, q_inner, q_pad=q_pad, q_hdr=q_hdr)
        hero_h = 0
        if hero is not None:
            hero_h = min(560, max(170, int(inner_w * hero.height / max(1, hero.width))))
        foot_h = 46
        content_h = (pad_top + title_h + (16 if title_lines and body_lines else 0) + body_h
                     + gap_after_body + (16 + quote_h if quote_h else 0)
                     + (24 + hero_h if hero_h else 0) + foot_h + pad_bot)
        H = 22 + 152 + 14 + content_h + 22

        canvas = _diag(W, H, c1, c2, c3).convert("RGBA")
        cy = await self._header(canvas, ev, uname, label, c1, c2, c3)
        canvas = self._content_card(canvas, cy, content_h, c1, c2, c3)

        x = 22 + pad_x
        y = cy + pad_top
        for ln in title_lines:
            _draw_line(canvas, (x, y), ln, f_title, INK)
            y += 54
        if title_lines and body_lines:
            y += 16
        y = _draw_rich(canvas, body_lines, x, y, f_body, 48, (46, 50, 58), emojis, body_px)

        if quote_h:
            y += 16
            qy0 = y
            self._draw_quote(canvas, x, qy0, inner_w, quote_h, q, q_inner,
                             f_qname=f_qname, f_qtitle=f_qtitle, f_qtext=f_qtext,
                             q_pad=q_pad, q_hdr=q_hdr)
            y = qy0 + quote_h
        if hero_h:
            y += 24
            _paste_round(canvas, _cover(hero, inner_w, hero_h), (x, y), 20)

        d = ImageDraw.Draw(canvas)
        d.text((x, cy + content_h - pad_bot - 26), "B 站 · BiliMonitor", font=_font(22),
               fill=(96, 104, 120))
        return canvas.convert("RGB")

    async def _quote_parts(self, ev: Event, probe, *, q_inner: int,
                           f_qtitle, f_qtext) -> dict:
        """转发引用块的内容：作者、标题行、正文行（含表情）、首图。"""
        fwd = ev.extra.get("fwd") or {}
        fwd_name = fwd.get("uname") or ""
        fwd_text = (fwd.get("text") or "").strip()
        fwd_tokens = _strip_image_tags(fwd.get("rich")
                                       or ([["t", fwd_text]] if fwd_text else []))
        fwd_emojis = await self._emojis(fwd_tokens)
        q_px = _emoji_px(f_qtext)
        fwd_lines, _ = _layout_rich(probe, fwd_tokens, f_qtext, q_inner, 16, q_px,
                                    bool(fwd_emojis))
        q_title_lines = _wrap(probe, fwd.get("title") or "", f_qtitle, q_inner, 2)
        fwd_src = (fwd.get("images") or [None])[0] or fwd.get("cover") or ""
        fwd_img = await self._img(fwd_src) if fwd_src else None
        return {"fwd": fwd, "name": fwd_name, "title_lines": q_title_lines,
                "lines": fwd_lines, "emojis": fwd_emojis, "px": q_px, "img": fwd_img}

    def _quote_height(self, q: dict, q_inner: int, *, q_pad: int = 20, q_hdr: int = 34) -> int:
        """引用块总高；没有标题、正文和配图时返回 0（不画）。"""
        img = q["img"]
        img_h = 0
        if img is not None:
            img_h = min(420, max(140, int(q_inner * img.height / max(1, img.width))))
        if not (q["title_lines"] or q["lines"] or img is not None):
            return 0
        return (q_pad + q_hdr + len(q["title_lines"]) * 40 + len(q["lines"]) * 44
                + (16 + img_h if img_h else 0) + q_pad)

    def _draw_quote(self, canvas, x: int, qy0: int, inner_w: int, quote_h: int,
                    q: dict, q_inner: int, *, f_qname, f_qtitle, f_qtext,
                    q_pad: int, q_hdr: int) -> None:
        """画转发引用卡：圆角底、左侧粉色竖条、作者行、标题、正文、首图。"""
        qx0, qw = x, inner_w
        q_bg = Image.new("RGBA", (qw, quote_h), (255, 255, 255, 96))
        maskq = Image.new("L", (qw, quote_h), 0)
        ImageDraw.Draw(maskq).rounded_rectangle((0, 0, qw - 1, quote_h - 1), 18, fill=255)
        canvas.paste(q_bg, (qx0, qy0), maskq)
        d = ImageDraw.Draw(canvas)
        d.rounded_rectangle((qx0, qy0, qx0 + qw - 1, qy0 + quote_h - 1), 18,
                            outline=(255, 255, 255, 170), width=2)
        d.rounded_rectangle((qx0 + 12, qy0 + q_pad, qx0 + 16, qy0 + quote_h - q_pad),
                            2, fill=PINK)
        qtx = qx0 + 32
        _draw_line(canvas, (qtx, qy0 + q_pad - 4),
                   "原动态 · @%s" % (q["name"] or "原作者"), f_qname, (96, 104, 120))
        yy = qy0 + q_pad + q_hdr
        for ln in q["title_lines"]:
            _draw_line(canvas, (qtx, yy - 4), ln, f_qtitle, INK)
            yy += 40
        yy = _draw_rich(canvas, q["lines"], qtx, yy, f_qtext, 44, (52, 56, 64),
                        q["emojis"], q["px"])
        img = q["img"]
        if img is not None:
            img_h = min(420, max(140, int(q_inner * img.height / max(1, img.width))))
            yy += 16
            _paste_round(canvas, _cover(img, q_inner, img_h), (qtx, yy), 14)

    # ---------- 视频卡（独立版式：大封面 + 时长 + 标题 + 播放/弹幕） ----------

    async def _video_card(self, ev: Event) -> Image.Image | None:
        uname = ev.uname or ("UID%s" % ev.subject_uid)
        c1, c2, c3 = _PALETTE[sum(ord(c) for c in uname) % len(_PALETTE)]
        vid = ev.extra.get("video") or {}
        cover = await self._img(ev.cover_url)
        f_title = _font(40, True)
        f_meta = _font(24)
        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        inner_w = W - 2 * 22 - 2 * 40
        title_lines = _wrap(probe, ev.title or "（无标题）", f_title, inner_w, 3)
        cover_h = int(inner_w * 9 / 16)

        pad_top, pad_x, pad_bot = 34, 40, 30
        content_h = (pad_top + cover_h + 28 + len(title_lines) * 58 + 8 + 36
                     + 42 + pad_bot)
        H = 22 + 152 + 14 + content_h + 22
        canvas = _diag(W, H, c1, c2, c3).convert("RGBA")
        cy = await self._header(canvas, ev, uname, "新视频", c1, c2, c3,
                                pill_bg=PINK, pill_fg=WHITE)
        canvas = self._content_card(canvas, cy, content_h, c1, c2, c3)

        x = 22 + pad_x
        y = cy + pad_top
        if cover is not None:
            cv = _cover(cover, inner_w, cover_h).convert("RGBA")
        else:
            cv = Image.new("RGBA", (inner_w, cover_h), (18, 20, 28, 255))
        _paste_round(canvas, cv, (x, y), 18)
        d = ImageDraw.Draw(canvas)

        # 播放按钮：半透明圆 + 白色三角（视频卡与动态卡的视觉区分点）
        cxp, cyp = x + inner_w // 2, y + cover_h // 2
        d.ellipse((cxp - 46, cyp - 46, cxp + 46, cyp + 46), fill=(0, 0, 0, 46))
        d.ellipse((cxp - 40, cyp - 40, cxp + 40, cyp + 40), fill=(255, 255, 255, 74))
        d.polygon([(cxp - 14, cyp - 22), (cxp - 14, cyp + 22), (cxp + 24, cyp)],
                  fill=(255, 255, 255, 240))
        # 时长：右下角黑底胶囊
        dur = str(vid.get("duration") or "")
        if dur:
            f_dur = _font(22, True)
            tw = d.textlength(dur, font=f_dur) + 24
            bx = x + inner_w - 14 - tw
            by = y + cover_h - 14 - 34
            d.rounded_rectangle((bx, by, bx + tw, by + 34), 10, fill=(0, 0, 0, 150))
            d.text((bx + 12, by + 5), dur, font=f_dur, fill=(255, 255, 255))

        y += cover_h + 28
        for ln in title_lines:
            _draw_line(canvas, (x, y), ln, f_title, INK)
            y += 58
        bits = []
        if vid.get("play"):
            bits.append("播放 %s" % _big(vid["play"]))
        if vid.get("danmaku"):
            bits.append("弹幕 %s" % _big(vid["danmaku"]))
        if vid.get("duration"):
            bits.append("时长 %s" % vid["duration"])
        d.text((x, y + 8), "　·　".join(bits) if bits else "bilibili 视频",
               font=f_meta, fill=(96, 104, 120))
        d.text((x, cy + content_h - pad_bot - 26), "B 站 · BiliMonitor", font=_font(22),
               fill=(96, 104, 120))
        return canvas.convert("RGB")

    async def _live_card(self, ev: Event) -> Image.Image | None:
        H = 620
        cover = await self._img(ev.extra.get("cover") or ev.cover_url)
        face = await self.face(ev.subject_uid, ev.extra.get("face"))
        if cover is not None:
            base = _cover(cover, W, H).convert("RGBA")
        else:
            base = _grad(W, H, (0x2b, 0x2f, 0x3a), (0x4a, 0x3d, 0x50)).convert("RGBA")
        base = Image.alpha_composite(base, Image.new("RGBA", (W, H), (0, 0, 0, 90)))
        dark = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dd = ImageDraw.Draw(dark)
        half = H // 2
        for y in range(half, H):
            t = (y - half) / max(1, half - 1)
            dd.line([(0, y), (W, y)], fill=(0, 0, 0, int(165 * t)))
        base = Image.alpha_composite(base, dark)
        d = ImageDraw.Draw(base)

        av = 96
        if face is not None:
            ring = Image.new("RGBA", (av + 8, av + 8), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse((0, 0, av + 7, av + 7), fill=(255, 255, 255, 235))
            base.paste(ring, (36, 32), ring)
            cimg = _circle(face, av)
            base.paste(cimg, (40, 36), cimg)
        else:
            d.ellipse((40, 36, 40 + av, 36 + av), fill=(255, 255, 255, 60))

        f_name = _font(40, True)
        name = ev.uname or ("UID%s" % ev.subject_uid)
        nx = 152 if face is not None else 44
        _draw_line(base, (nx, 52), name, f_name, WHITE)
        name_w = min(_measure(d, name, f_name), 420)
        _pill(d, (nx + name_w + 18, 60), "直播中", _font(24, True))

        f_title = _font(46, True)
        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        title_lines = _wrap(probe, ev.title or ("%s 正在直播" % name), f_title, W - 260, 2)
        ty = 372
        for ln in title_lines:
            _draw_line(base, (40, ty), ln, f_title, WHITE)
            ty += 68

        f_meta = _font(26)
        bits = []
        if ev.extra.get("area_name"):
            bits.append(str(ev.extra["area_name"]))
        if ev.extra.get("online"):
            bits.append("人气 %s" % _big(ev.extra.get("online")))
        if ev.occurred_at:
            bits.append(datetime.fromtimestamp(int(ev.occurred_at)).strftime("%m-%d %H:%M") + " 开播")
        d.text((42, ty + 8), "　·　".join(bits), font=f_meta, fill=(232, 236, 245))

        # 二维码：v5 起纯白圆角静区（不染色、不描边），码图完整落在圆角弧线内
        qr_size = 116
        qr_pad = 10
        qr_box = qr_size + qr_pad * 2
        _qr_badge(base, (W - 26 - qr_box, H - 26 - qr_box), ev.url or "", qr_size,
                  radius=18, tint=None, pad=qr_pad)
        d.text((40, H - 70), "扫码看直播", font=_font(26), fill=(232, 236, 244))
        d.text((40, H - 36), "BILIBILI LIVE", font=_font(21, True), fill=(255, 255, 255, 178))
        return base.convert("RGB")

    # ---------- 动态 / 视频 / 专栏卡 ----------


