# -*- coding: utf-8 -*-
"""
assets/make_icon.py — 畫出 bili-trim 的印章 icon、字標和 GitHub 的分享圖。

    python assets/make_icon.py

全部是程式畫的，不是生成的圖：字是專案自帶的 Barlow Semi Condensed（fonts/，
SIL OFL 1.1），其餘只有方塊和圓角方塊。改了這個檔案就重跑一次，輸出會覆蓋
同一個資料夾裡的圖。需要 Pillow（pip install pillow），程式本身用不到它。

設計：
  * 印章（白文：粉底白字），內圈一道邊框，刻「bili／trim」兩行，
    中間的分隔線就是介面時間軸上的刪除號（一條橫線、兩端帶回鉤）。
  * 工作列的 16～40 px 刻不下兩行字，換成同一顆印章只刻一個「b」——
    一個 .ico 裡本來就可以每個尺寸放不同的圖。
  * 字標「bili-trim」的連字號換成同一個刪除號。
  * 粉色取 B站 那一系的粉，壓深一點讓白字對比夠；不用 B站 的 logo、
    字標或吉祥物 —— 這個工具跟 B站 沒有關係，圖示不能看起來像官方的。
"""

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BARLOW = ROOT / "fonts" / "BarlowSemiCondensed-SemiBold.ttf"

SEAL = (231, 76, 122)        # 印章的粉
WHITE = (255, 255, 255)
INK = (24, 25, 28)           # 字標在淺色底上的字色
PAPER_DARK = (240, 240, 240)  # 字標在深色底上的字色

S = 1024                     # 印章的作圖尺寸，最後再縮
SOURCE = "assets/make_icon.py (drawn in code; lettering: Barlow Semi Condensed, SIL OFL 1.1)"


# --------------------------------------------------------------------------- #
# 基本零件
# --------------------------------------------------------------------------- #

def font(px):
    return ImageFont.truetype(str(BARLOW), px)


def glyph_mask(text, px):
    f = font(px)
    l, t, r, b = f.getbbox(text)
    im = Image.new("L", (r - l + 4, b - t + 4), 0)
    ImageDraw.Draw(im).text((2 - l, 2 - t), text, font=f, fill=255)
    return im


def cut_mark(d, x0, x1, ym, bar, hook, hook_w, color):
    """刪除號：一條橫線，兩端各一個往上下伸的回鉤。"""
    d.rectangle((x0, ym - bar / 2.0, x1, ym + bar / 2.0), fill=color)
    d.rectangle((x0, ym - hook / 2.0, x0 + hook_w, ym + hook / 2.0), fill=color)
    d.rectangle((x1 - hook_w, ym - hook / 2.0, x1, ym + hook / 2.0), fill=color)


def paste_white(dst, mask, box_w, box_h, cx, cy):
    k = min(box_w / float(mask.width), box_h / float(mask.height))
    m = mask.resize((max(int(mask.width * k), 1), max(int(mask.height * k), 1)),
                    Image.LANCZOS)
    dst.paste(Image.new("RGBA", m.size, WHITE + (255,)),
              (int(cx - m.width / 2.0), int(cy - m.height / 2.0)), m)


def seal_base(frame=True):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.10),
                        fill=SEAL + (255,))
    if frame:
        inset = 64
        d.rounded_rectangle((inset, inset, S - 1 - inset, S - 1 - inset),
                            radius=int(S * 0.05), outline=WHITE + (255,), width=20)
    return im


# --------------------------------------------------------------------------- #
# 印章
# --------------------------------------------------------------------------- #

def seal_full():
    """bili／trim 兩行，刪除號當分隔線。48 px 以上用這個。"""
    im = seal_base()
    paste_white(im, glyph_mask("bili", 600), 640, 300, S / 2.0, 300)
    paste_white(im, glyph_mask("trim", 600), 640, 300, S / 2.0, 735)
    cut_mark(ImageDraw.Draw(im), 230, S - 230, S / 2.0 + 4, 26, 80, 30,
             WHITE + (255,))
    return im


def seal_small(frame):
    """只刻一個 b。16～40 px 用這個（32 px 以上還放得下內框）。"""
    im = seal_base(frame)
    paste_white(im, glyph_mask("b", 800), 560 if frame else 640,
                640 if frame else 720, S / 2.0 - 6, S / 2.0)
    return im


def seal_at(size):
    art = seal_full() if size >= 48 else seal_small(frame=size >= 32)
    return art.resize((size, size), Image.LANCZOS)


# --------------------------------------------------------------------------- #
# 字標
# --------------------------------------------------------------------------- #

def wordmark(px, color):
    """bili-trim，連字號換成刪除號。回傳裁好邊的 RGBA。"""
    f = font(px)
    w = int(f.getlength("bili") + f.getlength("trim") + px * 1.2)
    h = int(px * 1.4)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    base = int(px * 1.05)
    x = 0
    d.text((x, base), "bili", font=f, fill=color + (255,), anchor="ls")
    x += f.getlength("bili") + px * 0.06
    x_top = f.getbbox("x", anchor="ls")[1]           # 負值：x 字高
    ym = base + x_top / 2.0
    mark_w = px * 0.40
    bar = px * 0.075
    cut_mark(d, x, x + mark_w, ym, bar, bar * 2.8, max(px * 0.05, 2),
             SEAL + (255,))
    x += mark_w + px * 0.06
    d.text((x, base), "trim", font=f, fill=color + (255,), anchor="ls")
    return im.crop(im.getbbox())


def lockup(text_color, bg, height=240):
    """印章＋字標並排，給 README 標頭用。透明底（bg=None）或指定底色。"""
    seal = seal_at(height)
    word = wordmark(int(height * 0.95), text_color)
    k = height * 0.62 / word.height
    word = word.resize((int(word.width * k), int(word.height * k)), Image.LANCZOS)
    gap = int(height * 0.22)
    W = seal.width + gap + word.width
    im = Image.new("RGBA", (W, height), (bg + (255,)) if bg else (0, 0, 0, 0))
    im.paste(seal, (0, 0), seal)
    im.paste(word, (seal.width + gap, int((height - word.height) / 2.0)), word)
    return im


# --------------------------------------------------------------------------- #
# GitHub 分享圖（Settings → Social preview，1280×640）
# --------------------------------------------------------------------------- #

def cjk_font(px, bold=False):
    """說明文字用的中文字型：bili-trim 自己做好的 Noto，沒有就用微軟正黑體。"""
    sys.path.insert(0, str(ROOT))
    try:
        import bili_font
        res = bili_font.resolve(make=False)
        path = res["strong"] if bold else res["body"]
        if path:
            return ImageFont.truetype(str(path), px)
    except Exception:
        pass
    return ImageFont.truetype("C:/Windows/Fonts/msjhbd.ttc" if bold
                              else "C:/Windows/Fonts/msjh.ttc", px)


def social_preview():
    W, H = 1280, 640
    im = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    lock = lockup(INK, None, height=200)
    im.paste(lock, (int((W - lock.width) / 2.0), 150), lock)
    d = ImageDraw.Draw(im)
    line1 = "把 B站 緩存影片裡的工商片段剪掉，剪完還是在 App 裡播"
    line2 = "播放器、彈幕、進度記憶全部照舊"
    f1, f2 = cjk_font(38), cjk_font(28)
    d.text((W / 2.0, 430), line1, font=f1, fill=INK + (255,), anchor="mm")
    d.text((W / 2.0, 485), line2, font=f2, fill=(96, 98, 104, 255), anchor="mm")
    return im


# --------------------------------------------------------------------------- #

def save_png(img, path):
    meta = PngInfo()
    meta.add_text("Source", SOURCE)
    img.save(str(path), pnginfo=meta)
    print("  %s  %dx%d" % (path.relative_to(ROOT), img.width, img.height))


def main():
    if not BARLOW.is_file():
        print("找不到 %s" % BARLOW)
        return 1
    print("輸出到 %s" % HERE)
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    frames = [seal_at(s) for s in sizes]
    ico = HERE / "bili-trim.ico"
    frames[-1].save(str(ico), format="ICO", sizes=[(s, s) for s in sizes],
                    append_images=frames[:-1])
    print("  %s  %s" % (ico.relative_to(ROOT), ", ".join(str(s) for s in sizes)))
    save_png(seal_at(1024), HERE / "bili-trim-icon.png")
    save_png(lockup(INK, None), HERE / "logo-light.png")
    save_png(lockup(PAPER_DARK, None), HERE / "logo-dark.png")
    save_png(social_preview(), HERE / "social-preview.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
