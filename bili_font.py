# -*- coding: utf-8 -*-
"""
bili_font.py — 把系統字型合併成一個「不缺字」的字型檔。

為什麼需要這個
--------------
B站 的影片標題什麼字都有：繁中、簡中、日文假名、韓文諺文，還有 ♡ ❤ ┊ ⌇
這些符號和 💓 💦 之類的表情。Android 上 B站 顯示得出來，是因為系統有一整條
字型後備鏈（fallback chain）—— 這個字沒有就換下一個字型找。

Dear PyGui 沒有這種東西：它一次只綁一個字型檔，缺的字會畫成「?」。
實測一份 64 筆標題／作者名的清單，有 15 筆會出現「?」。

而且單一系統字型也補不齊 —— 掃過本機所有大字型，涵蓋最好的也還缺 7 個字：

    msjh.ttc（微軟正黑）  缺 ┊♡❤기념생스일즈💓💗💦
    STXIHEI.TTF           缺 ⌇♡❤기💓💗💦

所以這裡做的事是：拿一個中文字型當底，把韓文和符號／表情從別的系統字型
子集化之後合併進去，產生一個單一字型檔給 Dear PyGui 用。合併結果會快取，
只有來源字型換了才重做。

需要 fonttools。裝不到或合併失敗都不會讓程式掛掉 —— 退回只用底字型，
就只是會看到「?」而已。
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

RANGES_VERSION = 1          # 改動下面的範圍時要加一，快取才會失效


def _r(a, b):
    return list(range(a, b + 1))


# 韓文：諺文音節與字母
HANGUL = _r(0x1100, 0x11FF) + _r(0x3130, 0x318F) + _r(0xAC00, 0xD7A3)

# 符號：箭頭、數學、技術符號（⌇）、製表（┊）、方塊、幾何、雜項符號（♡）、
#       裝飾符號（❤）、圈框文字
SYMBOLS = (_r(0x2190, 0x21FF) + _r(0x2200, 0x22FF) + _r(0x2300, 0x23FF) +
           _r(0x2460, 0x24FF) + _r(0x2500, 0x257F) + _r(0x2580, 0x259F) +
           _r(0x25A0, 0x25FF) + _r(0x2600, 0x26FF) + _r(0x2700, 0x27BF))

# 表情：seguisym 裡是單色字形，Dear PyGui 畫得出來（彩色的 COLR 畫不出來）
EMOJI = (_r(0x1F300, 0x1F5FF) + _r(0x1F600, 0x1F64F) +
         _r(0x1F680, 0x1F6FF) + _r(0x1F900, 0x1F9FF))

# 補字來源，依序嘗試；同一個字碼先被前面的字型補到就不再找後面的
FALLBACKS = (
    ("seguisym.ttf", SYMBOLS + EMOJI),      # Segoe UI Symbol
    ("malgun.ttf", HANGUL),                 # Malgun Gothic
    ("gulim.ttc", HANGUL),                  # 舊版 Windows 的韓文字型
    ("seguiemj.ttf", EMOJI),                # Segoe UI Emoji（慢，最後才用）
)

FONT_DIRS = ("C:/Windows/Fonts", "/System/Library/Fonts",
             "/usr/share/fonts/truetype", str(Path.home() / ".fonts"))

# 底字型候選，依序試。放在這裡而不是 GUI 裡，是因為 setup.py 也要用，
# 而它不能 import dearpygui（安裝流程的第一步就是去裝那個東西）。
FONT_CANDIDATES = (
    "C:/Windows/Fonts/msjh.ttc",                 # 微軟正黑體
    "C:/Windows/Fonts/msyh.ttc",                 # 微軟雅黑
    "C:/Windows/Fonts/NotoSansTC-VF.ttf",
    "C:/Windows/Fonts/DENG.TTF",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)


def base_font():
    """第一個存在的中文底字型；都沒有就回 None。"""
    for p in FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None

# 合併時一定要丟掉的表格：版面配置、簽章、點陣／彩色字形、可變字型。
# 這些表格在不同字型之間格式對不起來，fontTools 會丟 AssertionError。
_DROP_ALWAYS = {
    "GSUB", "GPOS", "GDEF", "BASE", "JSTF", "MATH", "DSIG", "VDMX",
    "COLR", "CPAL", "SVG ", "sbix", "CBDT", "CBLC", "EBDT", "EBLC",
    "avar", "fvar", "gvar", "HVAR", "MVAR", "STAT", "cvar",
    "LTSH", "MERG", "meta", "hdmx", "kern",
}


class FontError(RuntimeError):
    pass


def fonttools_problem():
    """fontTools 能不能用？能就回 None，不能就回原因字串。"""
    try:
        import fontTools  # noqa: F401
    except Exception as e:
        # 不是只接 ImportError —— 套件裝壞、版本不合、相依缺檔都會是別的例外，
        # 全部吞掉只回「沒有」的話，根本查不出到底卡在哪。
        return "%s: %s" % (type(e).__name__, e)
    for mod in ("fontTools.ttLib", "fontTools.subset", "fontTools.merge"):
        try:
            __import__(mod)
        except Exception as e:
            return "%s 匯入失敗 —— %s: %s" % (mod, type(e).__name__, e)
    return None


def have_fonttools():
    return fonttools_problem() is None


def find_font(name):
    for d in FONT_DIRS:
        p = Path(d) / name
        if p.is_file():
            return p
    return None


def cache_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "bili_trim_gui" / "fonts"


def _stamp(path):
    st = Path(path).stat()
    return "%s:%d:%d" % (Path(path).name, st.st_size, int(st.st_mtime))


def plan(base_path):
    """回傳 (快取檔路徑, [(補字字型, 字碼), ...])。沒有任何補字來源就回 (None, [])。"""
    base_path = Path(base_path)
    parts = [_stamp(base_path), "v%d" % RANGES_VERSION]
    chosen = []
    for name, cps in FALLBACKS:
        p = find_font(name)
        if p is None:
            continue
        chosen.append((p, cps))
        parts.append(_stamp(p))
    if not chosen:
        return None, []
    key = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return cache_dir() / ("merged-%s.ttf" % key), chosen


def cached(base_path):
    """已經有合併好的就回它的路徑，否則回 None。"""
    out, chosen = plan(base_path)
    if out is not None and out.is_file() and out.stat().st_size > 0:
        return out
    return None


def build(base_path, log=None, force=False):
    """
    合併並寫進快取，回傳字型檔路徑。整個流程大約十幾秒，只有第一次要跑。
    任何一步失敗都丟 FontError，呼叫端自己決定退回用底字型。
    """
    def say(msg):
        if log:
            log(msg)

    out, chosen = plan(base_path)
    if out is None:
        raise FontError("找不到任何可以補字的系統字型。")
    if out.is_file() and not force and out.stat().st_size > 0:
        return out
    if not have_fonttools():
        raise FontError("沒有安裝 fonttools，沒辦法合併字型（pip install fonttools）。")

    import logging
    from fontTools.ttLib import TTFont, TTLibFileIsCollectionError
    from fontTools import subset
    from fontTools.merge import Merger

    logging.getLogger("fontTools").setLevel(logging.ERROR)
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.parent / ("build-%d" % os.getpid())
    work.mkdir(parents=True, exist_ok=True)
    pieces = []
    t0 = time.perf_counter()

    try:
        # 1) 底字型：.ttc 是字型集合，要先抽出第一個 face 存成單獨的 ttf
        say("字型：抽出底字型 %s" % Path(base_path).name)
        try:
            base = TTFont(str(base_path), fontNumber=0)
        except TTLibFileIsCollectionError:
            base = TTFont(str(base_path), fontNumber=0)
        except Exception:
            base = TTFont(str(base_path))
        base_ttf = work / "base.ttf"
        base.save(str(base_ttf))
        covered = set()
        for tb in base["cmap"].tables:
            covered |= set(tb.cmap.keys())
        pieces.append(base_ttf)

        # 2) 補字型：只取「底字型還沒有」的字碼，子集化之後存起來
        for n, (path, cps) in enumerate(chosen):
            want = sorted(set(cps) - covered)
            if not want:
                continue
            try:
                f = TTFont(str(path), fontNumber=0)
            except Exception:
                try:
                    f = TTFont(str(path))
                except Exception as e:
                    say("字型：讀不了 %s（%s），跳過" % (Path(path).name, e))
                    continue
            have = set()
            for tb in f["cmap"].tables:
                have |= set(tb.cmap.keys())
            want = sorted(set(want) & have)
            if not want:
                continue
            say("字型：從 %s 取 %d 個字形" % (Path(path).name, len(want)))
            opts = subset.Options()
            opts.layout_features = []
            opts.name_IDs = ["*"]
            opts.notdef_outline = True
            opts.recalc_bounds = False
            s = subset.Subsetter(options=opts)
            s.populate(unicodes=want)
            s.subset(f)
            piece = work / ("fb%d.ttf" % n)
            f.save(str(piece))
            pieces.append(piece)
            covered |= set(want)

        if len(pieces) == 1:
            raise FontError("底字型以外沒有補到任何字形。")

        # 3) 把各字型都沒有的表格丟掉再合併。只有部分字型才有的表格
        #    （vhea/vmtx、hdmx、kern…）會讓 fontTools 的合併直接斷掉。
        say("字型：整理表格")
        keysets = [set(TTFont(str(p), lazy=True).keys()) for p in pieces]
        drop = (set.union(*keysets) - set.intersection(*keysets)) | _DROP_ALWAYS
        cleaned = []
        for i, p in enumerate(pieces):
            f = TTFont(str(p))
            for t in drop:
                if t in f:
                    del f[t]
            c = work / ("clean%d.ttf" % i)
            f.save(str(c))
            cleaned.append(str(c))

        say("字型：合併中…")
        merged = Merger().merge(cleaned)
        tmp = work / "merged.ttf"
        merged.save(str(tmp))
        if out.exists():
            out.unlink()
        tmp.replace(out)
        say("字型：完成，%s（%.1f MB，花了 %.0f 秒）"
            % (out.name, out.stat().st_size / 1e6, time.perf_counter() - t0))
        _prune(out)
        return out
    except FontError:
        raise
    except Exception as e:
        raise FontError("合併字型失敗（%s：%s）" % (type(e).__name__, str(e)[:200]))
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def _prune(keep):
    """清掉舊的合併結果，只留現在這一個。"""
    try:
        for p in keep.parent.glob("merged-*.ttf"):
            if p != keep:
                p.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 顯示用的文字清理
# --------------------------------------------------------------------------- #

# 這些是隱形的格式字元，本來就沒有字形。不清掉的話 Dear PyGui 會畫成「?」，
# 看起來就像標題多了幾個問號。
_INVISIBLE = (
    (0x200B, 0x200F),       # 零寬字元與書寫方向標記
    (0x202A, 0x202E),       # 方向覆寫
    (0x2060, 0x206F),       # 詞連接符等
    (0xFE00, 0xFE0F),       # 變化選擇器（❤︎ 的那個「︎」）
    (0xFFF9, 0xFFFB),       # 注釋字元
    (0xE0100, 0xE01EF),     # 變化選擇器補充區
)


def clean_text(text):
    """把隱形的格式字元拿掉；其餘原封不動。"""
    if not text:
        return text
    out = []
    for ch in text:
        c = ord(ch)
        if any(a <= c <= b for a, b in _INVISIBLE):
            continue
        out.append(ch)
    return "".join(out)


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "C:/Windows/Fonts/msjh.ttc"
    print(build(base, log=print, force="--force" in sys.argv))
