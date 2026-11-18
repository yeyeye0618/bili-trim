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

RANGES_VERSION = 2          # 改動下面的範圍時要加一，快取才會失效


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

# 漢字與假名：介面的內文是 Google Fonts 版的 Noto Sans TC，它只收繁體會用到的字 ——
# 實測 GB2312 的 6763 個簡體字缺 1898 個（们说这为时书…），日文的 歩桜気図 也缺。
# B站 的標題大半是簡體，所以從微軟正黑／雅黑把缺的漢字補進來。
CJK = (_r(0x2E80, 0x2FDF) + _r(0x3000, 0x30FF) + _r(0x3400, 0x4DBF) +
       _r(0x4E00, 0x9FFF) + _r(0xF900, 0xFAFF) + _r(0xFF00, 0xFFEF))

# 補字來源，依序嘗試；同一個字碼先被前面的字型補到就不再找後面的
FALLBACKS = (
    ("msjh.ttc", CJK),                      # 微軟正黑體（底字型不是它時才有用）
    ("msyh.ttc", CJK),                      # 微軟雅黑
    ("seguisym.ttf", SYMBOLS + EMOJI),      # Segoe UI Symbol
    ("malgun.ttf", HANGUL),                 # Malgun Gothic
    ("gulim.ttc", HANGUL),                  # 舊版 Windows 的韓文字型
    ("seguiemj.ttf", EMOJI),                # Segoe UI Emoji（慢，最後才用）
)

# 專案自己帶的字型（Barlow Semi Condensed，OFL）。放最前面：它不是系統字型，
# 但每台電腦都一樣，介面的數字長得才一致。
# exe 版的 fonts/ 封在 exe 裡，PyInstaller 執行時解到 sys._MEIPASS
BUNDLED_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "fonts"

FONT_DIRS = (str(BUNDLED_DIR), "C:/Windows/Fonts", "/System/Library/Fonts",
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
    path = Path(path)
    st = path.stat()
    try:
        bundled = path.resolve().parent == BUNDLED_DIR.resolve()
    except OSError:
        bundled = False
    if bundled:
        # 專案自帶的字型：exe 版每次啟動都重新解壓，修改時間每次都不一樣，
        # 用它當快取鍵會讓字型每次開都重做一遍。這幾個檔案很小，直接看內容。
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
        return "%s:%d:%s" % (path.name, st.st_size, digest)
    return "%s:%d:%d" % (path.name, st.st_size, int(st.st_mtime))


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
            # 合併要求每個字型的 unitsPerEm 一樣：Noto 是 1000，Windows 的
            # 補字來源都是 2048，不換算的話 fontTools 直接拒絕合併。
            if f["head"].unitsPerEm != base["head"].unitsPerEm:
                from fontTools.ttLib.scaleUpem import scale_upem
                scale_upem(f, base["head"].unitsPerEm)
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


def _prune(keep, pattern="merged-*.ttf"):
    """清掉舊的產物，只留現在這一個。刪不掉的（被防毒軟體或別的程式開著）跳過，下次再清。"""
    for p in keep.parent.glob(pattern):
        if p != keep:
            try:
                p.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# 介面的字型組
# --------------------------------------------------------------------------- #
#
# 介面用四種字：內文（Noto Sans TC）、粗體（同一套的 Bold）、標題（Noto Serif TC）、
# 數字（Barlow Semi Condensed，專案的 fonts/ 裡帶著；沒有的話退回 Windows 的
# Bahnschrift 半窄體）。
#
# Windows 11 內建的 Noto 是可變字型，預設實例是 Thin / ExtraLight —— Dear PyGui
# 不認可變字型的軸，直接載入就是一整片細線。所以先用 fontTools 把需要的粗細
# 「固定」成一般字型檔，快取起來。每個只要幾秒，而且只有第一次要做。
#
# 數字一定要等寬，不然時間碼在播放時會左右抖。Dear PyGui 不跑 OpenType 功能：
# 字型有 tnum 就把它的字形直接寫進 cmap（Bahnschrift）；沒有 tnum 的（Google Fonts
# 版的 Barlow 就沒有）把 0–9 的字寬拉成一樣、字形置中。改的是快取裡那一份，
# fonts/ 裡的原檔不動。
#
# 找不到這些字型（Windows 10、macOS、Linux）就退回原本的底字型，介面照樣能用。

FACES_VERSION = 1

FACES = {
    # 角色: (候選 [(檔名, 軸座標)]（依序找第一個有的）, 等寬數字)
    "body": ((("NotoSansTC-VF.ttf", {"wght": 400}),), False),
    "strong": ((("NotoSansTC-VF.ttf", {"wght": 700}),), False),
    "serif": ((("NotoSerifTC-VF.ttf", {"wght": 600}),), False),
    "num": ((("BarlowSemiCondensed-SemiBold.ttf", {}),
             ("bahnschrift.ttf", {"wght": 600, "wdth": 87.5})), True),
    # 資訊欄的數值跟中文內文排在一起，要同一個粗細（一般體）
    "numr": ((("BarlowSemiCondensed-Regular.ttf", {}),
              ("bahnschrift.ttf", {"wght": 400, "wdth": 87.5})), True),
}

# 沒有 Noto 的時候，粗體退回這些（不必加工就能用）
STRONG_FALLBACKS = ("C:/Windows/Fonts/msjhbd.ttc", "C:/Windows/Fonts/msyhbd.ttc")


def face_source(role):
    """(來源檔, 軸座標, 等寬數字)；這台電腦一個候選都沒有就回 None。"""
    candidates, tnum = FACES[role]
    for name, loc in candidates:
        p = find_font(name)
        if p is not None:
            return p, loc, tnum
    return None


def face_path(role):
    """加工後的快取檔路徑（不管存不存在）；沒有來源字型就回 None。"""
    src = face_source(role)
    if src is None:
        return None
    path, loc, tnum = src
    key = "|".join([_stamp(path), "v%d" % FACES_VERSION, "tnum" if tnum else "",
                    ",".join("%s=%s" % kv for kv in sorted(loc.items()))])
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return cache_dir() / ("face-%s-%s.ttf" % (role, digest))


def face_ready(role):
    p = face_path(role)
    if p is not None and p.is_file() and p.stat().st_size > 0:
        return p
    return None


def _bake_tnum(font):
    """把 GSUB 裡 tnum 的單一替換直接寫進 cmap。回傳換了幾個字。"""
    if "GSUB" not in font:
        return 0
    table = font["GSUB"].table
    lookups = set()
    for rec in table.FeatureList.FeatureRecord:
        if rec.FeatureTag == "tnum":
            lookups.update(rec.Feature.LookupListIndex)
    mapping = {}
    for i in sorted(lookups):
        lk = table.LookupList.Lookup[i]
        for st in lk.SubTable:
            if lk.LookupType == 7:            # extension 包了一層
                st = st.ExtSubTable
            m = getattr(st, "mapping", None)
            if m:
                mapping.update(m)
    n = 0
    for sub in font["cmap"].tables:
        for cp, g in list(sub.cmap.items()):
            if g in mapping:
                sub.cmap[cp] = mapping[g]
                n += 1
    return n


def _force_tabular(font, chars="0123456789"):
    """沒有 tnum 的字型：把數字的字寬拉成最寬那一個，字形左右置中。"""
    if "glyf" not in font:
        return 0
    cmap = font.getBestCmap()
    hm, glyf = font["hmtx"], font["glyf"]
    names = [cmap[ord(c)] for c in chars if ord(c) in cmap]
    if not names:
        return 0
    target = max(hm[n][0] for n in names)
    for n in names:
        adv, lsb = hm[n]
        d = (target - adv) // 2
        g = glyf[n]
        if d:
            if g.isComposite():
                for c in g.components:
                    c.x += d
            elif g.numberOfContours > 0:
                g.coordinates.translate((d, 0))
            g.recalcBounds(glyf)
        hm[n] = (target, lsb + d)
    return len(names)


def make_face(role, log=None):
    """把可變字型固定成需要的粗細，存進快取，回傳路徑。失敗丟 FontError。"""
    out = face_path(role)
    if out is None:
        raise FontError("這台電腦沒有 %s 需要的字型。" % role)
    if out.is_file() and out.stat().st_size > 0:
        return out
    if not have_fonttools():
        raise FontError("沒有安裝 fonttools，沒辦法調字重（pip install fonttools）。")
    import logging
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    logging.getLogger("fontTools").setLevel(logging.ERROR)
    path, loc, tnum = face_source(role)
    t0 = time.perf_counter()
    if log:
        log("字型：從 %s 做出 %s%s" % (
            path.name, role, "（%s）" % ", ".join("%s %s" % kv for kv in loc.items())
            if loc else ""))
    try:
        f = TTFont(str(path))
        if "fvar" in f:
            axes = {a.axisTag for a in f["fvar"].axes}
            instancer.instantiateVariableFont(
                f, {k: v for k, v in loc.items() if k in axes}, inplace=True,
                updateFontNames=False)
        if tnum and not _bake_tnum(f):
            _force_tabular(f)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part")
        f.save(str(tmp))
        tmp.replace(out)
    except Exception as e:
        raise FontError("做 %s 字型失敗（%s：%s）"
                        % (role, type(e).__name__, str(e)[:200]))
    _prune(out, "face-%s-*.ttf" % role)
    if log:
        log("字型：%s 好了（%.1f MB，%.0f 秒）"
            % (out.name, out.stat().st_size / 1e6, time.perf_counter() - t0))
    return out


def resolve(make=False, log=None):
    """
    介面要用的字型檔，回傳 dict：

        body    內文（補過字的版本優先）     strong  粗體
        serif   標題（沒有就是 None）        num     數字（沒有就是 None）
        pending 還有沒做好的（make=False 時才會有東西）

    make=True 會把缺的做出來（第一次大約一分鐘，給背景執行緒或 setup.py 用）。
    任何一步失敗都只是退回下一個選擇，不丟例外。
    """
    def say(msg):
        if log:
            log(msg)

    can = have_fonttools()
    got, pending = {}, []
    for role in FACES:
        p = face_ready(role)
        if p is None and face_source(role) is not None:
            if make and can:
                try:
                    p = make_face(role, say)
                except FontError as e:
                    say("字型：%s" % e)
            else:
                pending.append(role)
        got[role] = p

    # 內文：Noto（固定好的）優先，沒有就用原本的底字型；兩者都要再補字
    base = got["body"] or base_font()
    body = None
    if base is not None:
        body = cached(base)
        if body is None and plan(base)[0] is not None:
            if make and can:
                try:
                    body = build(base, log=say)
                except FontError as e:
                    say("字型：%s" % e)
            else:
                pending.append("merge")
        body = body or Path(base)

    strong = got["strong"]
    if strong is None:
        strong = next((Path(p) for p in STRONG_FALLBACKS if os.path.isfile(p)),
                      body)
    return {"body": body, "strong": strong, "serif": got["serif"],
            "num": got["num"], "numr": got["numr"], "pending": pending}


_COVERAGE = {}
_BASELINE = {}


def baseline(path):
    """
    基線離行頂多遠（以 Dear PyGui 的字級為 1）。Dear PyGui 依 ascent - descent
    縮放字型，所以兩套字同樣標 17，基線位置和字的大小都不一樣；
    要排在同一行就得用這個對齊。讀不出來就回 None。
    """
    key = str(path)
    if key not in _BASELINE:
        try:
            from fontTools.ttLib import TTFont
            f = TTFont(key, fontNumber=0, lazy=True)
            hh = f["hhea"]
            _BASELINE[key] = hh.ascent / float(hh.ascent - hh.descent)
        except Exception:
            _BASELINE[key] = None
    return _BASELINE[key]


def coverage(path):
    """字型 cmap 涵蓋的字碼（快取）。讀不出來就回 None。"""
    key = str(path)
    if key not in _COVERAGE:
        try:
            from fontTools.ttLib import TTFont
            f = TTFont(key, fontNumber=0, lazy=True)
            cps = set()
            for sub in f["cmap"].tables:
                cps |= set(sub.cmap.keys())
            _COVERAGE[key] = cps
        except Exception:
            _COVERAGE[key] = None
    return _COVERAGE[key]


def covers(path, text):
    """path 這個字型畫得出 text 的每一個字嗎？判斷不了就當作不行。"""
    cps = coverage(path) if path else None
    if not cps:
        return False
    return all(ord(ch) in cps or ch in " \t" for ch in text or "")


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
    # python bili_font.py           把介面的字型組全部做好（setup.py 用這個）
    # python bili_font.py <字型檔>  只把那個字型拿去補字（除錯用）
    # --force                       重做
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    if args:
        print(build(args[0], log=print, force=force))
        sys.exit(0)
    if force:
        base = face_ready("body") or base_font()
        old = cached(base) if base else None
        if old is not None:
            old.unlink()
        for role in FACES:
            p = face_ready(role)
            if p is not None:
                p.unlink()
    res = resolve(make=True, log=print)
    for role in ("body", "strong", "serif", "num"):
        print("%-7s %s" % (role, res[role] or "（沒有，用內文代替）"))
    sys.exit(0 if res["body"] is not None else 1)
