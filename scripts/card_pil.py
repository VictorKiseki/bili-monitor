# -*- coding: utf-8 -*-
"""用 PIL 画「每日直播日程」PSPLIVE 蓝白卡（1080x1440），不依赖浏览器。

为什么不用 Edge 截图：2026-11-04 晚上 msedge.exe 在本机变成静默退出（rc=0、
不产出任何文件），截图链路整条失效。PIL 版是确定性渲染，字体、圆角、阴影
全部自己画，离线也能出图。

入口：render(day_label, entries, faces, logo_path, badge_path, d, out_path) -> Path
entries 走 core.weekly.week_view() 的 day["entries"] 结构。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import weekly as WEEKLY  # noqa: E402
W, H = 1080, 1440
BLUE = (51, 177, 255)
BLUE_HEX = "#33B1FF"
INK = (13, 43, 75)
SUB = (74, 104, 138)
NAME = (18, 54, 92)
TXT = (74, 104, 132)
MUTED_TXT = (125, 151, 174)
BORDER = (220, 235, 250)
FONT_DIR = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "C:/Windows") / "Fonts"
FONTS = {"r": FONT_DIR / "msyh.ttc", "b": FONT_DIR / "msyhbd.ttc"}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS["b" if bold else "r"]), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, f, maxw: float) -> list[str]:
    """按像素宽度折行；中文逐字断，英文/数字按词断。"""
    text = (text or "").strip()
    if not text:
        return [""]
    lines, cur = [], ""
    tokens: list[str] = []
    buf = ""
    for ch in text:
        if ch.isascii() and not ch.isspace():
            buf += ch
        else:
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(ch)
    if buf:
        tokens.append(buf)
    for tk in tokens:
        trial = cur + tk
        if draw.textlength(trial, font=f) <= maxw or not cur:
            cur = trial
        else:
            lines.append(cur.rstrip())
            cur = tk.lstrip()
    if cur:
        lines.append(cur.rstrip())
    return lines or [""]


def _bg(height: int = H) -> Image.Image:
    img = Image.new("RGB", (W, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    stops = [(0.0, (234, 245, 255)), (0.42, (247, 251, 255)), (1.0, (255, 255, 255))]
    for y in range(height):
        t = y / (height - 1)
        for i in range(len(stops) - 1):
            a, ca = stops[i]
            b, cb = stops[i + 1]
            if a <= t <= b:
                k = (t - a) / (b - a) if b > a else 0
                c = tuple(round(ca[j] + (cb[j] - ca[j]) * k) for j in range(3))
                d.line([(0, y), (W, y)], fill=c)
                break
    # 右上淡蓝光斑
    glow = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([W - 640, -320, W + 320, 640], fill=95)
    gd.ellipse([-260, height - 300, 400, height + 300], fill=55)
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    blue = Image.new("RGB", (W, H), BLUE)
    img = Image.composite(blue, img, glow.point(lambda v: int(v * 0.55)))
    return img


def _stripes(base: Image.Image) -> Image.Image:
    """右上角旋转 8° 的斜纹块（照 CSS repeating-linear-gradient 135deg）。

    透明底色必须用白色 (255,255,255,0)：用黑色透明色旋转后 BICUBIC 插值会把
    线条边缘混成黑边，整块看上去发黑（2026-11-04 踩过）。
    """
    box = Image.new("RGBA", (300, 300), (255, 255, 255, 0))
    d = ImageDraw.Draw(box)
    for i in range(-300, 600, 26):
        d.line([(i, 0), (i + 300, 300)], fill=(191, 228, 255, 120), width=11)
    mask = Image.new("L", (300, 300), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, 299, 299], radius=30, fill=255)
    box.putalpha(mask)
    box = box.rotate(8, resample=Image.BICUBIC, expand=True)
    base.alpha_composite(box, (int(W - box.width + 30), -70))
    return base


def _circle_avatar(path: str, size: int) -> Image.Image | None:
    if not path or not Path(path).exists():
        return None
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return None
    side = min(im.size)
    im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                  (im.width + side) // 2, (im.height + side) // 2))
    im = im.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size * 4 - 1, size * 4 - 1], fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _brand_logo(path: str, h: int) -> Image.Image | None:
    if not path or not Path(path).exists():
        return None
    im = Image.open(path).convert("RGBA")
    if Path(path).suffix.lower() == ".webp" and "homelogo" in path:
        pix = im.load()
        for y in range(im.height):
            for x in range(im.width):
                r, g, b, a = pix[x, y]
                if a > 20:
                    pix[x, y] = (BLUE[0], BLUE[1], BLUE[2], a)
    w = round(im.width * h / im.height)
    return im.resize((w, h), Image.LANCZOS)


def _badge(path: str, h: int, tint: tuple[int, int, int] = (255, 255, 255)) -> Image.Image | None:
    if not path or not Path(path).exists():
        return None
    im = Image.open(path).convert("RGBA")
    px = im.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if a > 10:
                px[x, y] = (tint[0], tint[1], tint[2], a)
    w = round(im.width * h / im.height)
    return im.resize((w, h), Image.LANCZOS)


def _people(entry: dict) -> list[tuple[int, str]]:
    uname = (entry.get("uname") or "").strip()
    names = [n.strip() for n in uname.split(" × ") if n.strip()]
    uids = [int(u) for u in (entry.get("uids") or [])]
    if not uids:
        uids = [int(entry.get("uid") or 0)]
    if len(uids) == len(names) and len(names) > 1:
        return list(zip(uids, names))
    return [(uids[0], uname)]


def _row_height(entry: dict, faces: dict[int, str], k: float = 1.0) -> int:
    """行高按系数 k 缩放（场次多的时候整体压紧，避免最后一行压到页脚）。"""
    people = _people(entry)
    collab = len(people) > 1
    muted = WEEKLY.entry_time_label(entry) in ("", "休息", "待定")
    pad_y = 20 * k
    tx = (20 if muted else 22) * k
    if not collab:
        av = 66 * k
        nm_h = (30 if not muted else 28) * k
        body = max(av, nm_h)
        return int(pad_y * 2 + max(body + 6 * k + round(tx * 1.4), 92 * k))
    av = 54 * k
    per = 120 * k + 26 * k
    cols = 968 - 7 - 24 * k - 118 * k - 16 * k - 24 * k
    per_line = max(1, int(cols // per))
    rows = (len(people) + per_line - 1) // per_line
    body = int(rows * (av + 6 * k + 17 * k * 1.25 * 2) + (rows - 1) * 12 * k)
    return int(pad_y * 2 + body + 6 * k + round(tx * 1.4))


def _draw_rows(entries: list[dict], faces: dict[int, str], width: int,
               gap: int, k: float = 1.0) -> Image.Image:
    heights = [_row_height(e, faces, k) for e in entries]
    total = sum(heights) + gap * max(0, len(entries) - 1)
    img = Image.new("RGBA", (width, max(total, 10)), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    y = 0
    for e, hh in zip(entries, heights):
        muted = WEEKLY.entry_time_label(e) in ("", "休息", "待定")
        people = _people(e)
        collab = len(people) > 1
        # 阴影 + 卡片
        sh = Image.new("RGBA", (width, hh), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([0, 0, width - 1, hh - 1], radius=18,
                                             fill=(51, 177, 255, 26))
        sh = sh.filter(ImageFilter.GaussianBlur(5))
        img.alpha_composite(sh, (0, int(y + 4)))
        card = Image.new("RGBA", (width, hh), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)
        fill = (247, 251, 255) if muted else (255, 255, 255)
        cd.rounded_rectangle([0, 0, width - 1, hh - 1], radius=18, fill=fill,
                             outline=(191, 216, 238) if muted else BORDER, width=1)
        bar = (191, 216, 238) if muted else BLUE
        cd.rounded_rectangle([0, 0, int(7 * k) or 7, hh - 1], radius=18,
                             corners=(True, False, False, True), fill=bar)
        img.alpha_composite(card, (0, int(y)))
        # 时间列：具体钟点 H:mm 40px；休息/待定 24px
        tm = WEEKLY.entry_time_label(e) or "待定"
        f_tm = _font(max(13, int((24 if muted else 40) * k)), True)
        ty = y + (34 * k if muted else 26 * k)
        d.text((7 * k + 24 * k, ty), tm, font=f_tm,
               fill=(147, 174, 199) if muted else BLUE)
        bx = 7 * k + 24 * k + 118 * k + 16 * k
        bw = width - bx - 24 * k
        by = y + 20 * k
        if collab:
            per = 120 * k + 26 * k
            per_line = max(1, int((bw + 26 * k) // per))
            rows_h = ((len(people) + per_line - 1) // per_line) * (54 * k + 6 * k + 17 * k * 1.25 * 2 + 12 * k)
            for i, (u, nm) in enumerate(people):
                cx = bx + (i % per_line) * per
                cy = by + (i // per_line) * (54 * k + 6 * k + 17 * k * 1.25 * 2 + 12 * k)
                sz = int(54 * k)
                av = _circle_avatar(faces.get(u, ""), sz)
                if av:
                    d.ellipse([cx - 3, cy - 3, cx + sz + 3, cy + sz + 3],
                              outline=(205, 231, 255), width=3)
                    img.alpha_composite(av, (int(cx), int(cy)))
                f_nm = _font(max(11, int(17 * k)), True)
                for j, ln in enumerate(_wrap(d, nm, f_nm, 120 * k)[:2]):
                    tw = d.textlength(ln, font=f_nm)
                    d.text((cx + (sz - tw) / 2, cy + sz + 6 * k + j * 21 * k), ln,
                           font=f_nm, fill=(27, 69, 115))
            ty2 = by + rows_h
        else:
            u, nm = people[0]
            sz = int(66 * k)
            av = _circle_avatar(faces.get(u, ""), sz)
            if av:
                d.ellipse([bx - 3, by - 3, bx + sz + 3, by + sz + 3],
                          outline=(205, 231, 255), width=3)
                img.alpha_composite(av, (int(bx), int(by)))
            f_nm = _font(max(14, int((23 if muted else 27) * k)), True)
            d.text((bx + sz + 12, by + sz / 2), nm, font=f_nm,
                   fill=(65, 99, 127) if muted else NAME, anchor="lm")
            ty2 = by + sz + 6 * k
        text = WEEKLY.entry_body_text(e)
        fs = max(13, int((20 if muted else 22) * k))
        f_tx = _font(fs)
        for i, ln in enumerate(_wrap(d, text, f_tx, bw)):
            d.text((bx, ty2 + i * round(fs * 1.4)), ln, font=f_tx,
                   fill=MUTED_TXT if muted else TXT)
        y += hh + gap
    return img


def render(day_label: str, entries: list[dict], faces: dict[int, str],
           logo_path: str, badge_path: str, d: dt.date, out_path: Path) -> Path:
    """画卡。高度不设上限：场次多了画布往下长，绝不压扁/缩放内容。

    2026-11-04 修正：早先固定 1440 高、内容多就整体缩放，10 场时头像和字都
    被压变形。现在按 rows 实际高度算画布高度，最矮 1440（只有几场时保持原比例）。
    """
    visible = [e for e in entries if not e.get("cancelled")]
    count = len([e for e in visible
                 if WEEKLY.entry_time_label(e) not in ("", "休息", "待定")])
    top = 52
    ry = top + 130
    my = ry + 22
    list_top = my + 60
    gap = 20
    rows_img = (_draw_rows(visible, faces, 968, gap, 1.0) if visible
                else Image.new("RGBA", (968, 1), (255, 255, 255, 0)))
    foot_h = 40 + 42 + 20          # 底部留白 + 页脚块 + 列表与页脚的间距
    height = max(H, int(list_top + rows_img.height + foot_h))

    img = _bg(height).convert("RGBA")
    img = _stripes(img)
    d0 = ImageDraw.Draw(img)

    # 页头
    logo = _brand_logo(logo_path, 86)
    if logo:
        img.alpha_composite(logo, (56, int(top + 6)))
    mm, dd = "%02d" % d.month, "%02d" % d.day
    f_d1 = _font(78, True)
    right = W - 56
    dot_w = d0.textlength(".", font=f_d1)
    dd_w = d0.textlength(dd, font=f_d1)
    x_dd = right - dd_w
    x_dot = x_dd - dot_w
    mm_w = d0.textlength(mm, font=f_d1)
    x_mm = x_dot - mm_w
    ty = top - 4
    d0.text((x_mm, ty), mm, font=f_d1, fill=INK)
    d0.text((x_dot, ty), ".", font=f_d1, fill=BLUE)
    d0.text((x_dd, ty), dd, font=f_d1, fill=INK)
    f_d2 = _font(22, True)
    en = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][d.weekday()]
    cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][d.weekday()]
    d0.text((right, ty + 92), "%s · %s" % (en, cn), font=f_d2,
            fill=(92, 134, 174), anchor="ra")

    # 分隔线
    for i in range(968):
        t = i / 967
        col = (round(BLUE[0] + (143 - BLUE[0]) * t),
               round(BLUE[1] + (211 - BLUE[1]) * t),
               round(BLUE[2] + (255 - BLUE[2]) * t))
        a = 255 if t < 0.38 else max(0, int(255 * (1 - (t - 0.38) / 0.62)))
        img.alpha_composite(Image.new("RGBA", (1, 4), col + (a,)), (56 + i, ry))

    # 元信息
    f_meta = _font(24, True)
    pre = "今日直播日程 · 共 "
    d0.text((56, my), pre, font=f_meta, fill=SUB)
    x = 56 + d0.textlength(pre, font=f_meta)
    d0.text((x, my), str(count), font=f_meta, fill=BLUE)
    x += d0.textlength(str(count), font=f_meta)
    d0.text((x, my), " 场", font=f_meta, fill=SUB)

    # 右上角 PSPLIVE 药丸
    f_pill = _font(18, True)
    pad = 18
    badge = _badge(badge_path, 20)
    pill_w = int(pad + 20 + 8 + d0.textlength("PSPLIVE", font=f_pill) + pad)
    px1 = W - 56
    px0 = px1 - pill_w
    py0, py1 = my - 6, my + 44
    d0.rounded_rectangle([px0, py0, px1, py1], radius=999, fill=BLUE)
    if badge:
        img.alpha_composite(badge, (int(px0 + pad), int(py0 + (py1 - py0 - 20) // 2)))
    d0.text((px0 + pad + 20 + 8, my + 8), "PSPLIVE", font=f_pill, fill=(255, 255, 255))

    # 列表
    img.alpha_composite(rows_img, (56, int(list_top)))

    # 页脚
    fy = height - 40 - 42
    d0.line([(56, fy - 20), (W - 56, fy - 20)], fill=BORDER, width=2)
    if badge:
        small = _badge(badge_path, 28, (143, 211, 255))
        img.alpha_composite(small, (56, int(fy)))
        tx = 56 + small.width + 12
    else:
        tx = 56
    f_foot = _font(18, True)
    d0.text((tx, fy + 4), "PSPLIVE 直播日程 · 周表自动识别", font=f_foot, fill=(92, 134, 174))
    f_fr = _font(17)
    d0.text((W - 56, fy + 6), "时间以主播实际开播为准", font=f_fr, fill=(143, 169, 194), anchor="ra")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "PNG", optimize=True)
    return out_path
