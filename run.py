#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py — 開這個程式的入口。

    bili-trim.bat        Windows，點兩下
    python3 run.py       其他系統

為什麼不是直接開 bili_gui.py
---------------------------
bili_gui.py 第一行就 import dearpygui。套件還沒裝的人執行它，看到的是一段
ImportError 堆疊 —— 那個訊息對會寫程式的人都不一定好懂，對只是想剪掉廣告
的人完全沒有意義。

所以入口是這個檔案：只用標準庫，先確認 Python 版本與三個套件，缺的話
問一聲就幫你裝，裝好再開視窗。至於 adb 與 ffmpeg 這兩個外部程式，
由視窗裡的「環境檢查」負責 —— 那時候已經有圖形介面可以好好說明了，
比在黑底白字的視窗裡丟一行「找不到 adb」有用得多。
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MIN_PY = (3, 8)


def _console():
    """
    Windows 主控台預設是 cp950 之類的舊編碼，輸出被導向檔案時遇到編不出來
    的字就 UnicodeEncodeError —— 程式因為「印不出一個字」而掛掉。
    """
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for s in (sys.stdout, sys.stderr):
        try:
            if s.isatty():
                s.reconfigure(errors="replace")
            else:
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _pause():
    """
    點兩下 .bat 進來的時候，視窗會在行程結束的瞬間消失。有話要說就得停住，
    不然使用者看到的是「閃一下，什麼都沒有」—— 這個專案真的發生過。
    """
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n按 Enter 關閉…")
    except (EOFError, KeyboardInterrupt):
        pass


def _ask(question):
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return False
        return input("%s [Y/n] " % question).strip().lower() in ("", "y", "yes", "是")
    except (EOFError, KeyboardInterrupt):
        print("")
        return False


def ensure_packages(env):
    """回傳 True 表示可以開視窗了。"""
    have = env.probe_modules()
    missing = [(m, why) for m, why, need in env.PACKAGES
               if need and have.get(m, "!").startswith("!")]
    if not missing:
        return True

    print("bili-trim 第一次啟動，還缺幾個 Python 套件：")
    for m, why in missing:
        print("    %-12s %s" % (m, why))
    # fonttools 不是必要的，但缺了標題會變成一排「?」，順手一起裝
    opt = [m for m, _, need in env.PACKAGES
           if not need and have.get(m, "!").startswith("!")]
    for m in opt:
        print("    %-12s （選用，缺了影片標題會出現「?」）" % m)
    print("")
    print("要裝到這個 Python：%s" % sys.executable)
    if not _ask("現在裝嗎？"):
        print("")
        print("好，那自己來：")
        print('    "%s" -m pip install -r requirements.txt' % sys.executable)
        return False

    print("")
    req = HERE / "requirements.txt"
    args = (["-r", str(req)] if req.is_file()
            else ["dearpygui", "numpy", "fonttools"])
    if not env.pip_install(args):
        print("")
        print("pip 裝不起來。建議改用虛擬環境：")
        print('    "%s" -m venv .venv' % sys.executable)
        print("    .venv/Scripts/python run.py" if os.name == "nt"
              else "    .venv/bin/python run.py")
        return False

    env.refresh_site()
    have = env.probe_modules()
    still = [m for m, _, need in env.PACKAGES
             if need and have.get(m, "!").startswith("!")]
    if still:
        print("")
        print("pip 說裝好了，但新的行程還是 import 不到：%s" % "、".join(still))
        print("這通常是同一台電腦上有好幾個 Python。試試：")
        print("    python setup.py")
        return False
    print("")
    print("裝好了，開啟介面…")
    return True


def main():
    _console()
    if sys.version_info < MIN_PY:
        print("bili-trim 需要 Python %d.%d 以上，這個是 %d.%d.%d"
              % (MIN_PY + sys.version_info[:3]))
        print("    %s" % sys.executable)
        if os.name == "nt":
            print("")
            print("裝新版：winget install -e --id Python.Python.3.12")
        _pause()
        return 1

    try:
        import bili_env as env
    except Exception as e:
        print("讀不到 bili_env.py（%s）" % e)
        print("這個檔案要跟專案裡其他 bili_*.py 放在一起：%s" % HERE)
        _pause()
        return 2

    if not ensure_packages(env):
        _pause()
        return 1

    try:
        import bili_gui
    except Exception:
        import traceback
        print("開啟介面失敗：")
        traceback.print_exc()
        print("")
        print("把上面這段貼出來就知道卡在哪。也可以先跑這個看環境：")
        print("    python setup.py --check")
        _pause()
        return 1

    bili_gui.main()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
