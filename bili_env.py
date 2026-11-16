# -*- coding: utf-8 -*-
"""
bili_env.py — 找外部工具（adb / ffmpeg / ffplay / ffprobe）。

為什麼不直接 shutil.which
------------------------
要使用者自己改 PATH 是這個專案最大的部署摩擦：下載 zip、解壓、翻到
「系統環境變數」、新增一列、重開終端機 —— 任何一步漏掉，結果都只是
「找不到 adb」，而且看不出是哪一步錯。

所以查找順序改成：

    1. 環境變數指名（BILI_ADB / BILI_FFMPEG …）
    2. 專案自己的 tools/ 資料夾
    3. PATH

setup.py 會把下載好的 platform-tools 與 ffmpeg 放進 tools/，於是 PATH
完全不用動，而且整個專案資料夾可以直接複製到另一台電腦照樣能跑。
已經把 adb／ffmpeg 加進 PATH 的人也不受影響 —— 第 3 步就會找到。
"""

import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS_DIR = Path(os.environ.get("BILI_TOOLS") or (HERE / "tools"))

# 每個工具：環境變數名、是否必要、缺了會怎樣
TOOLS = {
    "adb":     ("BILI_ADB", True,
                "連不到手機，列緩存、拉取、寫回全部要靠它"),
    "ffmpeg":  ("BILI_FFMPEG", False,
                "預覽畫面會不能用（剪輯本身不需要）"),
    "ffplay":  ("BILI_FFPLAY", False,
                "預覽會沒有聲音"),
    "ffprobe": ("BILI_FFPROBE", False,
                "讀不到影片的長寬與時長，預覽會不能用"),
}

_cache = {}


class ToolMissing(RuntimeError):
    pass


def _exe_names(name):
    if os.name == "nt":
        return (name + ".exe", name)
    return (name,)


def _search_tools_dir(name):
    """
    在 tools/ 裡找。不假設固定路徑，因為各家 zip 的目錄名都不一樣
    （ffmpeg-7.1-essentials_build/bin/、platform-tools/ …），
    版本一換名字就變。寧可用檔名找，深度限制 4 層避免掃到別的東西。
    """
    if not TOOLS_DIR.is_dir():
        return None
    wanted = set(n.lower() for n in _exe_names(name))
    found = []
    base = len(TOOLS_DIR.parts)
    for root, dirs, files in os.walk(TOOLS_DIR):
        rp = Path(root)
        if len(rp.parts) - base >= 4:
            dirs[:] = []
            continue
        for f in files:
            if f.lower() in wanted:
                found.append(rp / f)
    if not found:
        return None
    # 路徑最短的優先：同一個工具被解壓兩次時，挑看起來最像「正式位置」的
    found.sort(key=lambda p: (len(p.parts), str(p)))
    return found[0]


def find(name, refresh=False):
    """回傳可執行檔的 Path，找不到就 None。結果會記住。"""
    if not refresh and name in _cache:
        return _cache[name]
    env_name = TOOLS.get(name, (None,))[0]
    hit = None
    if env_name:
        raw = os.environ.get(env_name)
        if raw:
            p = Path(raw)
            # 允許指到資料夾（放整包工具的那層）
            if p.is_dir():
                for n in _exe_names(name):
                    if (p / n).is_file():
                        p = p / n
                        break
            if p.is_file():
                hit = p
    if hit is None:
        hit = _search_tools_dir(name)
    if hit is None:
        for n in _exe_names(name):
            w = shutil.which(n)
            if w:
                hit = Path(w)
                break
    _cache[name] = hit
    return hit


def have(name):
    return find(name) is not None


def path(name):
    """給 subprocess 用的字串；找不到就回工具名本身，讓它自己報 FileNotFoundError。"""
    p = find(name)
    return str(p) if p else name


def require(name):
    p = find(name)
    if p is None:
        raise ToolMissing(missing_hint(name))
    return str(p)


def reset():
    _cache.clear()


def missing_hint(name):
    _, need, why = TOOLS.get(name, (None, False, ""))
    pack = "Android platform-tools" if name == "adb" else "ffmpeg"
    return ("找不到 %s（%s 這一包）—— %s。\n"
            "跑一次 python setup.py 就會自動下載放進 tools/，PATH 不用改。"
            % (name, pack, why))


# --------------------------------------------------------------------------- #
# 環境自我檢查
# --------------------------------------------------------------------------- #

PACKAGES = (
    ("dearpygui", "圖形介面", True),
    ("numpy", "預覽影格的像素搬運", True),
    ("fontTools", "補字型（缺了標題會出現「?」）", False),
)


def check():
    """
    回傳 [(項目, 狀態, 說明)]，狀態是 "ok" / "warn" / "bad"。
    setup.py 和 GUI 都用這一份，不要各寫一套判斷。
    """
    rows = []

    v = sys.version_info
    ok = v >= (3, 8)
    rows.append(("Python", "ok" if ok else "bad",
                 "%d.%d.%d  %s" % (v[0], v[1], v[2], sys.executable)))

    for mod, why, need in PACKAGES:
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "")
            rows.append((mod, "ok", ver or why))
        except Exception as e:
            rows.append((mod, "bad" if need else "warn",
                         "沒有 —— %s（%s）" % (why, type(e).__name__)))

    for name, (_, need, why) in TOOLS.items():
        p = find(name, refresh=True)
        if p:
            where = "tools/" if _under(p, TOOLS_DIR) else "PATH"
            rows.append((name, "ok", "%s  （%s）" % (p, where)))
        else:
            rows.append((name, "bad" if need else "warn", "沒有 —— " + why))

    return rows


def _under(p, base):
    try:
        Path(p).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def font_state():
    """回傳 (狀態, 說明)，給檢查報告用。"""
    try:
        import bili_font
    except Exception as e:
        return "warn", "bili_font 匯入失敗：%s" % e
    problem = bili_font.fonttools_problem()
    if problem:
        return "warn", "不能合併字型：%s" % problem
    base = bili_font.base_font()
    if base is None:
        return "bad", "找不到任何中文字型，標題會整排都是「?」"
    try:
        c = bili_font.cached(base)
    except Exception as e:
        return "warn", "檢查快取失敗：%s" % e
    if c:
        return "ok", "%s（%.1f MB）" % (c.name, c.stat().st_size / 1e6)
    return "warn", "還沒建（第一次開 GUI 會自己建，或跑 python setup.py）"


def is_fatal(rows):
    return any(s == "bad" for _, s, _ in rows)


MARK = {"ok": "[ok] ", "warn": "[--] ", "bad": "[!!] "}


def print_check(rows, out=print):
    for item, state, note in rows:
        out("%s%-10s %s" % (MARK.get(state, "     "), item, note))


if __name__ == "__main__":
    rows = check()
    rows.append(("字型", ) + font_state())
    print_check(rows)
    print("")
    if is_fatal(rows):
        print("有必要的東西缺了。跑 python setup.py 補齊。")
        sys.exit(1)
    print("可以跑：python bili_gui.py")
