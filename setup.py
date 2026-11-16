#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup.py — 在一台新電腦上把這個專案弄到可以跑，一個命令。

    python setup.py            裝好缺的東西
    python setup.py --check    只檢查，什麼都不改
    python setup.py --pack     打包成一個 zip 帶去別台電腦

為什麼需要這個
--------------
手動部署要做的事其實不少：裝 Python 套件、下載 Android platform-tools、
下載 ffmpeg、把兩個都解壓、翻到「系統環境變數」把路徑加進 PATH、重開
終端機、第一次啟動再等字型合併。中間任何一步漏掉，看到的結果都只是
一句「找不到 adb」，而且看不出是哪一步錯。

所以這裡把那些步驟收進一個檔案，而且刻意不碰 PATH —— 下載的工具放進
專案的 tools/，由 bili_env.py 負責找（tools/ 優先，再找 PATH）。好處是
整個專案資料夾可以直接複製到另一台電腦照樣能跑，也不會在使用者的系統
環境變數裡留下東西。

這個檔案只用標準庫，而且不 import 專案裡需要第三方套件的模組 ——
它要能在「什麼都還沒裝」的電腦上跑起來。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
UA = "bili-trim-setup"

WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"

# 跟著 repo 一起來的啟動檔。使用者解開 zip 應該只看到一個能點的東西。
LAUNCHER = "bili-trim.bat"


# --------------------------------------------------------------------------- #
# 輸出
# --------------------------------------------------------------------------- #

def _setup_console():
    """
    這個檔案會印一堆中文。Windows 的主控台預設是 cp950 之類的舊編碼，
    輸出被導向檔案時 Python 會照那個編碼寫，遇到編不出來的字就
    UnicodeEncodeError —— 安裝程式因為「印不出一個字」而中斷，最糟的第一印象。
    """
    if WIN:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for s in (sys.stdout, sys.stderr):
        try:
            if s.isatty():
                # 主控台本身用 Windows 的 Unicode API，編碼不用動，
                # 只要保證有編不出來的字也不會丟例外。
                s.reconfigure(errors="replace")
            else:
                # 導向檔案或管線：Python 會改用系統編碼（cp950…），
                # 那就留不住中文。既然是給人看的紀錄，固定寫 UTF-8。
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_step = [0, 0]


def step(title):
    _step[0] += 1
    print("")
    print("[%d/%d] %s" % (_step[0], _step[1], title))


def say(msg, mark="     "):
    print("%s%s" % (mark, msg))


def ok(msg):
    say(msg, "[ok] ")


def warn(msg):
    say(msg, "[--] ")


def bad(msg):
    say(msg, "[!!] ")


def interactive():
    return bool(sys.stdin and sys.stdin.isatty()) and not FLAGS.yes


def ask(question, default=True):
    """default 是「直接按 Enter」的意思。不是互動終端機就當作沒答應。"""
    if FLAGS.yes:
        print("     %s [自動回答：是]" % question)
        return True
    if not sys.stdin or not sys.stdin.isatty():
        warn("%s —— 不是互動視窗，當作不要。要自動同意請加 --yes" % question)
        return False
    tail = "[Y/n] " if default else "[y/N] "
    try:
        a = input("     %s %s" % (question, tail)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("")
        return False
    if not a:
        return default
    return a in ("y", "yes", "是")


# --------------------------------------------------------------------------- #
# 下載與解壓
# --------------------------------------------------------------------------- #

ADB_URL = {
    "win32": "https://dl.google.com/android/repository/"
             "platform-tools-latest-windows.zip",
    "darwin": "https://dl.google.com/android/repository/"
              "platform-tools-latest-darwin.zip",
}.get(sys.platform, "https://dl.google.com/android/repository/"
                    "platform-tools-latest-linux.zip")

# adb.exe 不能單獨搬 —— 那兩個 dll 要躺在它旁邊，不然一執行就是缺模組
ADB_FILES = (["adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"] if WIN
             else ["adb"])

_FF = {
    "win32": [
        # ffmpeg.org 官方下載頁列的 Windows 建置
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-win64-gpl.zip",
    ],
    "linux": [
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-linux64-gpl.tar.xz",
    ],
}
FFMPEG_URLS = _FF.get("win32" if WIN else
                      "linux" if sys.platform.startswith("linux") else "?", [])

FF_FILES = (["ffmpeg.exe", "ffplay.exe", "ffprobe.exe"] if WIN
            else ["ffmpeg", "ffplay", "ffprobe"])


def _human(n):
    if n >= 1e9:
        return "%.1f GB" % (n / 1e9)
    return "%.1f MB" % (n / 1e6)


def _progress(label, got, total, t0):
    dt = max(time.time() - t0, 0.001)
    rate = got / dt / 1e6
    if total:
        line = ("     %s  %s / %s  %3d%%  %.1f MB/s"
                % (label, _human(got), _human(total), got * 100 // total, rate))
    else:
        line = "     %s  %s  %.1f MB/s" % (label, _human(got), rate)
    sys.stdout.write("\r" + line + "   ")
    sys.stdout.flush()


def download(url, dest, label):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.parent / (dest.name + ".part")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        with open(str(tmp), "wb") as f:
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                _progress(label, got, total, t0)
    sys.stdout.write("\n")
    if total and got < total:
        tmp.unlink()
        raise IOError("只收到 %s，應該是 %s" % (_human(got), _human(total)))
    tmp.replace(dest)
    return dest


def extract(archive, dest, basenames):
    """
    只把指定檔名的東西平鋪解到 dest，不是整包解開。兩個理由：

    1. 各家壓縮檔的目錄名都不一樣（ffmpeg-7.1-essentials_build/bin/、
       platform-tools/…），而且版本一換就變，寫死路徑等著壞。
    2. 輸出路徑由這裡自己組（dest / 檔名），壓縮檔裡寫 ../../ 也影響不到
       別的地方 —— 下載來的東西不該有機會決定要寫到哪裡去。
    """
    dest.mkdir(parents=True, exist_ok=True)
    want = set(n.lower() for n in basenames)
    written = []

    def take(name, opener):
        base = name.replace(os.sep, "/").replace("\\", "/").rsplit("/", 1)[-1]
        if base.lower() not in want:
            return
        out = dest / base
        if out in written:
            return          # 同名只取第一個，後面的不要蓋掉前面的
        with opener() as src, open(str(out), "wb") as f:
            shutil.copyfileobj(src, f)
        written.append(out)

    if zipfile.is_zipfile(str(archive)):
        with zipfile.ZipFile(str(archive)) as z:
            for info in z.infolist():
                if info.filename.endswith("/"):
                    continue
                take(info.filename, lambda i=info: z.open(i))
    else:
        with tarfile.open(str(archive)) as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue
                take(m.name, lambda mm=m: t.extractfile(mm))

    for p in written:
        try:
            os.chmod(str(p), 0o755)
        except OSError:
            pass
    return written


def fetch_tool(urls, dest, basenames, label, size_hint):
    if not urls:
        warn("這個平台沒有自動下載的來源，請自己裝 %s" % label)
        return []
    if not ask("要下載 %s 嗎？（%s，放進 tools/，不會動到 PATH）"
               % (label, size_hint)):
        return []
    cache = TOOLS / "_download"
    cache.mkdir(parents=True, exist_ok=True)
    last = None
    for url in urls:
        arc = cache / url.rsplit("/", 1)[-1]
        try:
            if arc.is_file() and arc.stat().st_size > 0:
                say("已經有下載好的 %s，直接用" % arc.name)
            else:
                say("來源 %s" % url)
                download(url, arc, label)
            got = extract(arc, dest, basenames)
            if not got:
                raise IOError("壓縮檔裡找不到 %s" % "、".join(basenames))
            ok("解出 %s" % "、".join(p.name for p in got))
            return got
        except Exception as e:
            last = e
            warn("這個來源不行：%s" % e)
    bad("下載失敗：%s" % last)
    return []


# --------------------------------------------------------------------------- #
# 步驟
# --------------------------------------------------------------------------- #

def check_python():
    v = sys.version_info
    say("%d.%d.%d  %s" % (v[0], v[1], v[2], sys.executable))
    if v < (3, 8):
        bad("這個專案需要 Python 3.8 以上。")
        if WIN:
            say("    winget install -e --id Python.Python.3.12")
        return False
    ok("版本可以")
    return True


def _env():
    """bili_env 到這裡一定讀得到（main 開頭就驗過了）。"""
    import bili_env
    return bili_env


def _report_modules(have):
    missing = []
    for mod, why, need in _env().PACKAGES:
        v = have.get(mod, "!unknown")
        if v.startswith("!"):
            (bad if need else warn)("%s 沒有 —— %s" % (mod, why))
            missing.append((mod, need))
        else:
            ok("%s %s" % (mod, "" if v == "yes" else v))
    return missing


def check_packages():
    need = _report_modules(_env().probe_modules())
    if not need:
        return True

    req = HERE / "requirements.txt"
    args = (["-r", str(req)] if req.is_file()
            else ["dearpygui", "numpy", "fonttools"])
    say("用這個直譯器安裝：%s" % sys.executable)
    done = _env().pip_install(args, log=warn)
    if done:
        _env().refresh_site()
        print("")
        say("再確認一次（用新的行程，確定開程式時真的 import 得到）：")
        left = _report_modules(_env().probe_modules())
        if not [m for m, req in left if req]:
            return True
        bad("pip 說裝好了，但新的行程還是 import 不到。")
        say("這通常是同一台電腦上有多個 Python。請用 %s 開程式 —— "
            "它會讀 python-path.txt，裡面記的就是這一個：" % LAUNCHER)
        say("    %s" % sys.executable)
        return False
    bad("pip 裝不起來。建議改用虛擬環境：")
    say('    "%s" -m venv .venv' % sys.executable)
    say("    .venv/Scripts/python setup.py" if WIN
        else "    .venv/bin/python setup.py")
    return False


def _version_line(exe, args):
    """
    回 (跑得起來嗎, 說明)。真的執行一次而不是只看檔案在不在 ——
    被防毒軟體隔離、抄到一半、少了旁邊的 dll，檔案都還在，但一跑就死。
    """
    try:
        r = subprocess.run([str(exe)] + args, capture_output=True, timeout=20)
        out = (r.stdout or r.stderr).decode("utf-8", "replace").strip()
        if not out:
            return False, "跑得起來但問不出版本"
        return True, out.splitlines()[0][:70]
    except Exception as e:
        return False, "執行失敗 —— %s: %s" % (type(e).__name__, str(e)[:80])


def check_tools(env):
    for name, label in (("adb", "Android platform-tools"), ("ffmpeg", "ffmpeg")):
        p = env.find(name, refresh=True)
        if p:
            where = "tools/" if str(p).startswith(str(TOOLS)) else "PATH"
            ok("%s 已經有了：%s（%s）" % (name, p, where))
            continue
        warn("還沒有 %s（%s 這一包）" % (name, label))
        if FLAGS.offline:
            say("--offline，不下載")
            continue
        if name == "adb":
            fetch_tool([ADB_URL], TOOLS / "platform-tools", ADB_FILES,
                       "platform-tools", "約 8 MB")
        elif MAC:
            say("macOS 沒有現成的單檔下載，請用 Homebrew：brew install ffmpeg")
        else:
            fetch_tool(FFMPEG_URLS, TOOLS / "ffmpeg", FF_FILES,
                       "ffmpeg", "約 115 MB")

    env.reset()
    good = True
    for name in ("adb", "ffmpeg", "ffplay", "ffprobe"):
        _, need, why = env.TOOLS[name]
        p = env.find(name, refresh=True)
        if p:
            runs, ver = _version_line(p, ["version"] if name == "adb"
                                      else ["-version"])
            if runs:
                ok("%-7s %s" % (name, ver))
            elif need:
                bad("%-7s %s" % (name, ver))
                say("        %s" % p)
                good = False
            else:
                warn("%-7s %s" % (name, ver))
        elif need:
            bad("還是找不到 %s —— %s" % (name, why))
            good = False
        else:
            warn("還是找不到 %s —— %s" % (name, why))
    return good


def check_font():
    try:
        import bili_font as BF
    except Exception as e:
        warn("讀不到 bili_font：%s" % e)
        return
    base = BF.base_font()
    if base is None:
        warn("找不到中文字型，標題大概會整排都是「?」")
        return
    say("底字型 %s" % base)
    problem = _fonttools_problem_fresh()
    if problem:
        warn("不能合併字型（%s）—— ♡ ❤ 韓文、表情會顯示成「?」" % problem)
        return
    have = BF.cached(base)
    if have and not FLAGS.force_font:
        ok("合併字型已經建好：%s（%s）"
           % (have.name, _human(have.stat().st_size)))
        return
    if FLAGS.skip_font:
        warn("--skip-font，跳過。第一次開 GUI 它會自己在背景建。")
        return
    if not ask("要現在把韓文與符號合併進字型嗎？（約半分鐘，只做一次）"):
        say("跳過。第一次開 GUI 它會自己在背景建。")
        return
    # 開新行程來建：剛剛才裝好的 fontTools，這個行程的 sys.path 可能還看不到。
    cmd = [sys.executable, str(HERE / "bili_font.py"), base]
    if FLAGS.force_font:
        cmd.append("--force")
    if subprocess.run(cmd, cwd=str(HERE)).returncode == 0:
        ok("字型建好了")
    else:
        warn("字型沒建成功 —— 不影響剪輯，只是會看到「?」")


def _fonttools_problem_fresh():
    """
    同樣開新行程問 —— 理由跟 bili_env.probe_modules 一樣。這裡要是判斷錯，
    結果是「剛裝好 fonttools 卻跳過合併字型」，使用者下次開程式滿滿的「?」。
    """
    code = ("import sys\n"
            "sys.path.insert(0, %r)\n"
            "import bili_font\n"
            "sys.stdout.write(bili_font.fonttools_problem() or '')\n"
            % str(HERE))
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(HERE),
                           capture_output=True, timeout=180)
        if r.returncode:
            return (r.stderr.decode("utf-8", "replace").strip().splitlines()
                    or ["問不出來"])[-1][:160]
        return r.stdout.decode("utf-8", "replace").strip() or None
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)


def write_launcher():
    """
    記住「跑 setup.py 的那個直譯器」，寫進 python-path.txt。

    啟動檔（bili-trim.bat）本身是跟著 repo 一起來的，不是產生的 ——
    使用者解開 zip 就要看得到一個能點的東西。這裡只負責告訴它該用哪一個
    Python：一台電腦上常常有好幾個（市集版、python.org 版、conda…），
    套件裝在哪一個就要用哪一個開，不然就是明明裝好了卻說找不到 dearpygui。
    """
    p = HERE / "python-path.txt"
    try:
        # 刻意不寫換行：批次檔是用 set /p 讀的，結尾的 CR 會被當成路徑的
        # 一部分，然後「檔案不存在」。
        p.write_text(sys.executable, encoding="utf-8", newline="")
    except OSError as e:
        warn("寫不進 %s：%s" % (p.name, e))
        return None
    ok("啟動用的 Python 記在 %s" % p.name)
    return p


def make_shortcut(launcher):
    if not WIN:
        return
    desktop = Path(os.path.expanduser("~")) / "Desktop"
    if not desktop.is_dir():
        return
    if not ask("要在桌面放一個捷徑嗎？", default=False):
        return
    lnk = desktop / "bili-trim.lnk"
    TOOLS.mkdir(parents=True, exist_ok=True)
    vbs = TOOLS / "_shortcut.vbs"
    # 建 .lnk 要用 COM，標準庫沒有介面。借一行 VBScript 比為了一個捷徑
    # 叫使用者去裝 pywin32 划算。
    vbs.write_text(
        'Set s = CreateObject("WScript.Shell")\n'
        'Set l = s.CreateShortcut("%s")\n'
        'l.TargetPath = "%s"\n'
        'l.WorkingDirectory = "%s"\n'
        'l.Description = "bili-trim"\n'
        'l.Save\n' % (lnk, launcher, HERE), encoding="mbcs", errors="replace")
    try:
        r = subprocess.run(["cscript", "//nologo", str(vbs)],
                           capture_output=True, timeout=30)
        if lnk.is_file():
            ok("桌面捷徑：%s" % lnk)
        else:
            msg = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
            warn("捷徑沒建成功：%s" % msg[:120])
    except Exception as e:
        warn("捷徑沒建成功：%s" % e)
    finally:
        try:
            vbs.unlink()
        except OSError:
            pass


HOW_TO_USB = """手機上要做的事：
  1. 設定 → 關於手機 → 連點「版本號碼」七次，開出「開發人員選項」
  2. 設定 → 系統 → 開發人員選項 → 開啟「USB 偵錯」
  3. 用 USB 線接上這台電腦（要能傳資料的線，不是只能充電的那種）
  4. 手機會跳出「要允許 USB 偵錯嗎？」—— 勾「一律允許」再按允許"""


def _adb_rows(exe):
    """回傳 [(序號, 狀態)]。狀態是 device / unauthorized / offline 之類。"""
    try:
        r = subprocess.run([str(exe), "devices"], capture_output=True, timeout=30)
    except Exception:
        return None
    rows = []
    for line in r.stdout.decode("utf-8", "replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            rows.append((parts[0], parts[1]))
    return rows


def wait_for_device(env, limit=180):
    """
    等到手機真的是 device 狀態。會一直等而不是檢查一次就放棄，因為
    使用者需要的時間都花在手機上：點七次版本號碼、翻開發人員選項、
    按授權對話框。檢查一次就說「沒偵測到手機」，等於叫人自己重跑。
    """
    exe = env.find("adb")
    if exe is None:
        return None
    said = set()
    on_status = [False]

    def clear():
        # 只有真的印過「等手機…」那一行才需要擦掉它
        if on_status[0]:
            sys.stdout.write("\r" + " " * 60 + "\r")
            on_status[0] = False

    t0 = time.time()
    while True:
        rows = _adb_rows(exe)
        if rows is None:
            warn("adb devices 跑不起來")
            return None
        ready = [sn for sn, st in rows if st == "device"]
        if ready:
            clear()
            return ready[0]
        kinds = set(st for _, st in rows)
        if "unauthorized" in kinds and "auth" not in said:
            said.add("auth")
            clear()
            warn("手機接上了，但還沒授權這台電腦。")
            say("看一下手機螢幕：「要允許 USB 偵錯嗎？」勾「一律允許」再按允許。")
        elif ("offline" in kinds or "recovery" in kinds) and "off" not in said:
            said.add("off")
            clear()
            warn("手機是 offline 狀態。拔掉重插，或是換一個 USB 孔。")
        elif not rows and "none" not in said:
            said.add("none")
            clear()
            warn("還沒看到手機。")
            for line in HOW_TO_USB.splitlines():
                say(line)
        left = limit - (time.time() - t0)
        if left <= 0:
            clear()
            return None
        sys.stdout.write("\r     等手機…（還會等 %d 秒，不等就按 Ctrl+C）  "
                         % int(left))
        sys.stdout.flush()
        on_status[0] = True
        time.sleep(1.0)


def setup_device(env):
    """
    等手機 → 找 B站 的緩存目錄 → 真的列一次影片。
    這一步過了，GUI 開起來按「重新掃描」就一定有東西，不用再猜。
    回傳 (順利嗎, 找到幾支可以剪的)。
    """
    if not env.have("adb"):
        bad("沒有 adb，手機這一步跳過。")
        return False, 0

    if not (sys.stdin and sys.stdin.isatty()):
        limit = 20          # 自動化跑的，沒人會在這一刻跑去插線
    elif FLAGS.yes:
        limit = 60
    else:
        limit = 180
    try:
        serial = wait_for_device(env, limit)
    except KeyboardInterrupt:
        print("")
        warn("不等了。手機弄好之後跑：python setup.py --check")
        return False, 0
    if serial is None:
        warn("等不到手機。弄好之後不用整個重跑，只要：python setup.py")
        return False, 0

    try:
        import bili_device as DEV
    except Exception as e:
        warn("讀不到 bili_device：%s" % e)
        return False, 0

    try:
        devs = DEV.list_devices()
        if not devs:
            warn("adb 看得到裝置，但列不出來 —— 再跑一次看看")
            return False, 0
        dev = devs[0]
        ok("手機：%s" % dev.label)

        say("找 B站 的緩存目錄…")
        roots = dev.find_roots()
        if not roots:
            return _no_roots(dev), 0
        for r in roots:
            ok("%s：%s" % (r.label, r.path))

        say("列出已緩存的影片…")
        items = dev.list_caches(roots)
        if not items:
            warn("找到目錄，但裡面沒有緩存影片。")
            say("先在 B站 App 裡下載一支影片（要選「下載」而不是只收藏），"
                "再跑 python setup.py --check 確認。")
            return True, 0
        good = [it for it in items if it.unsupported_reason() is None]
        (ok if good else warn)("找到 %d 支緩存影片，其中 %d 支這個工具可以處理"
                               % (len(items), len(good)))
        for it in items[:3]:
            why = it.unsupported_reason()
            mark = "    - " if why is None else "    ! "
            say("%s%s%s" % (mark, it.display_name[:48],
                            "" if why is None else "（%s）" % why))
        if len(items) > 3:
            say("    …另外還有 %d 支" % (len(items) - 3))
        if not good:
            warn("沒有一支是本工具能處理的格式（只支援 DASH 緩存）。")
        return True, len(good)
    except DEV.AdbError as e:
        warn("跟手機溝通出錯：%s" % e)
        return False, 0


def _no_roots(dev):
    """找不到緩存目錄 —— 分清楚是「沒裝 App」還是「讀不到那個目錄」。"""
    import bili_device as DEV
    warn("找不到 B站 的緩存目錄。")
    installed = []
    try:
        out = dev.shell("pm list packages 2>/dev/null", timeout=60)
        names = set(l.strip()[8:] for l in out.splitlines()
                    if l.startswith("package:"))
        installed = [(pkg, label) for pkg, label in DEV.PACKAGES
                     if pkg in names]
    except Exception:
        pass
    if not installed:
        say("這支手機上沒有找到 B站（檢查過 %s）。"
            % "、".join(pkg for pkg, _ in DEV.PACKAGES))
        say("裝的是別的版本的話，請回報套件名，加進支援清單很簡單。")
        return False
    say("B站 有裝（%s），但它的下載目錄讀不到。"
        % "、".join(label for _, label in installed))
    say("最常見的原因是 App 裡還沒下載過任何影片 —— 目錄要下載過才會建出來。")
    say("先在 App 裡下載一支，再跑 python setup.py --check。")
    return False


def _pick_backup_dir(cur):
    if interactive() and not FLAGS.backup_dir:
        say("寫回手機之前一定會先備份一份乾淨原檔，一支就是原檔大小"
            "（幾百 MB），所以挑個有空間的地方。")
    for attempt in range(3):
        if FLAGS.backup_dir:
            cand = FLAGS.backup_dir
        elif not interactive():
            cand = cur
        else:
            say("放哪裡？直接按 Enter 就用 %s" % cur)
            try:
                a = input("     > ").strip().strip(chr(34)).strip(chr(39))
            except (EOFError, KeyboardInterrupt):
                print("")
                a = ""
            cand = os.path.expanduser(a) if a else cur
        d = Path(cand)
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".bili_trim_write_test"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as e:
            bad("這個位置不能用：%s" % e)
            if FLAGS.backup_dir or not interactive():
                return None
            continue
        try:
            free = shutil.disk_usage(str(d)).free
            ok("備份位置：%s（這個磁碟還剩 %s）" % (d, _human(free)))
        except OSError:
            ok("備份位置：%s" % d)
        return str(d)
    return None


def setup_config():
    """
    把設定檔先寫好，讓 GUI 第一次開起來就是設定好的狀態 ——
    不然使用者裝完還要自己去選備份位置，才算真的能用。
    """
    try:
        import bili_config as CFGM
    except Exception as e:
        warn("讀不到 bili_config：%s" % e)
        return False
    cfg = CFGM.load_config()

    # 暫存目錄一律指向現在這個資料夾。專案被搬過位置的話，設定檔裡
    # （它住在 APPDATA，不會跟著專案走）記的還是舊路徑。
    cfg["stage_dir"] = CFGM.DEFAULTS["stage_dir"]

    # 專案被解壓到 Program Files 之類寫不進去的地方，這裡就會先擋下來。
    # 不先驗的話，症狀會是「從手機拉了兩百 MB 然後失敗」。
    stage = Path(cfg["stage_dir"])
    try:
        stage.mkdir(parents=True, exist_ok=True)
        probe = stage / ".bili_trim_write_test"
        probe.write_bytes(b"ok")
        probe.unlink()
        ok("暫存位置：%s" % stage)
    except OSError as e:
        bad("暫存位置寫不進去：%s" % e)
        say("這個資料夾：%s" % HERE)
        say("把整個專案搬到你自己的資料夾（桌面、文件都可以）再跑一次。")
        return False

    chosen = _pick_backup_dir(cfg.get("backup_dir")
                              or CFGM.DEFAULTS["backup_dir"])
    if chosen is None:
        warn("備份位置沒設好 —— GUI 裡還是可以改（備份 → 變更備份位置…）")
    else:
        cfg["backup_dir"] = chosen
    try:
        CFGM.save_config(cfg, strict=True)
    except OSError as e:
        bad("設定檔寫不進去：%s" % e)
        say("路徑：%s" % CFGM.CONFIG_PATH)
        return False
    ok("設定檔：%s" % CFGM.CONFIG_PATH)
    return chosen is not None


# --------------------------------------------------------------------------- #
# 打包
# --------------------------------------------------------------------------- #

SKIP_DIRS = {"__pycache__", "bili_gui_stage", "bili_trim_stage", "preview",
             ".git", ".venv", "_download"}
SKIP_SUFFIX = (".bilibak", ".part", ".pyc", ".lnk", ".log", ".zip")

# python-path.txt 記的是「打包那台電腦」的 Python 路徑，帶過去會指到
# 一個不存在的檔案。啟動檔自己會處理這種情況，但乾脆別包進去。
SKIP_NAMES = {"python-path.txt"}


def pack(out, with_tools=True):
    out = Path(out)
    if not out.is_absolute():
        out = HERE / out
    n = 0
    total = 0
    with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(str(HERE)):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            rp = Path(root)
            rel_root = rp.relative_to(HERE)
            if not with_tools and rel_root.parts[:1] == ("tools",):
                dirs[:] = []
                continue
            for f in files:
                src = rp / f
                if f.lower().endswith(SKIP_SUFFIX) or f in SKIP_NAMES:
                    continue
                z.write(str(src), str(rel_root / f))
                n += 1
                total += src.stat().st_size
    say("%d 個檔案，原始 %s，壓成 %s"
        % (n, _human(total), _human(out.stat().st_size)))
    ok("包好了：%s" % out)
    print("")
    print("帶到新電腦之後，在那邊解壓，然後：")
    print("    Windows   點兩下 setup.bat")
    print("    其他系統  python3 setup.py")
    if with_tools and (HERE / "tools").is_dir():
        print("")
        print("（tools/ 一起包進去了，所以新電腦不必再下載 adb 跟 ffmpeg）")


# --------------------------------------------------------------------------- #

def _config_row():
    try:
        import bili_config as CFGM
    except Exception as e:
        return ("設定", "warn", "讀不到 bili_config：%s" % e)
    cfg = CFGM.load_config()
    d = Path(cfg.get("backup_dir") or "")
    if not CFGM.CONFIG_PATH.is_file():
        return ("設定", "warn", "還沒有設定檔（跑 python setup.py 會建）")
    if not d.is_dir():
        return ("備份位置", "warn", "%s —— 這個資料夾不存在" % d)
    if not os.access(str(d), os.W_OK):
        return ("備份位置", "warn", "%s —— 寫不進去" % d)
    try:
        return ("備份位置", "ok", "%s（剩 %s）"
                % (d, _human(shutil.disk_usage(str(d)).free)))
    except OSError:
        return ("備份位置", "ok", str(d))


def _phone_check(env):
    """--check 不等手機，看一次就回報。等待是 setup 才做的事。"""
    exe = env.find("adb")
    if exe is None:
        return False
    rows = _adb_rows(exe)
    if rows is None:
        warn("adb 跑不起來")
        return False
    ready = [sn for sn, st in rows if st == "device"]
    if not ready:
        kinds = set(st for _, st in rows)
        if "unauthorized" in kinds:
            warn("手機接上了但還沒授權 —— 看手機螢幕按「允許 USB 偵錯」")
        elif kinds:
            warn("手機狀態是 %s" % "、".join(sorted(kinds)))
        else:
            warn("沒接手機")
        return False
    try:
        import bili_device as DEV
        devs = DEV.list_devices()
        if not devs:
            warn("列不出裝置")
            return False
        dev = devs[0]
        ok("手機：%s" % dev.label)
        roots = dev.find_roots()
        if not roots:
            warn("找不到 B站 的緩存目錄")
            return False
        for r in roots:
            ok("%s：%s" % (r.label, r.path))
        items = dev.list_caches(roots)
        good = [it for it in items if it.unsupported_reason() is None]
        if good:
            ok("緩存影片 %d 支，可以剪的 %d 支" % (len(items), len(good)))
            return True
        warn("緩存影片 %d 支，但沒有一支是能處理的格式" % len(items))
        return False
    except Exception as e:
        warn("跟手機溝通出錯：%s" % e)
        return False


def do_check(env):
    print("bili-trim 環境檢查")
    print("")
    rows = env.check()
    rows.append(("字型", ) + env.font_state())
    rows.append(_config_row())
    env.print_check(rows)
    print("")
    print("手機")
    phone = _phone_check(env)
    print("")
    if env.is_fatal(rows):
        print("有必要的東西缺了。跑這個補齊：")
        print("    python setup.py")
        return 1
    if phone and not any(st == "warn" for _, st, _ in rows):
        print("都齊了，接上手機就能用。")
    elif phone:
        print("可以用，只是有東西缺著（上面標 [--] 的）。")
    else:
        print("電腦這邊沒問題，手機那邊還沒接好（看上面 [--] 的說明）。")
    print("")
    print("開程式：%s" % (LAUNCHER if WIN else "python3 run.py"))
    return 0


FLAGS = None


def main():
    global FLAGS
    _setup_console()
    ap = argparse.ArgumentParser(description="把 bili-trim 在這台電腦上裝起來。")
    ap.add_argument("--check", action="store_true", help="只檢查，什麼都不改")
    ap.add_argument("--yes", "-y", action="store_true", help="所有詢問都當同意")
    ap.add_argument("--offline", action="store_true", help="不下載任何東西")
    ap.add_argument("--skip-font", action="store_true", help="不建合併字型")
    ap.add_argument("--force-font", action="store_true", help="重建合併字型")
    ap.add_argument("--pack", nargs="?", const="bili-trim-portable.zip",
                    metavar="ZIP", help="打包成一個 zip 帶去別台電腦")
    ap.add_argument("--no-tools", action="store_true",
                    help="--pack 時不要把 tools/ 包進去")
    ap.add_argument("--backup-dir", metavar="PATH",
                    help="備份位置，不給就問（或沿用現有設定）")
    FLAGS = ap.parse_args()

    sys.path.insert(0, str(HERE))
    try:
        import bili_env as env
    except Exception as e:
        print("讀不到 bili_env.py（%s）—— 這個檔案要跟專案放在一起。" % e)
        return 2

    if FLAGS.pack:
        print("打包 %s" % HERE)
        pack(FLAGS.pack, with_tools=not FLAGS.no_tools)
        return 0

    if FLAGS.check:
        return do_check(env)

    print("bili-trim 安裝")
    print("位置：%s" % HERE)
    _step[1] = 7

    step("Python")
    if not check_python():
        return 1

    step("Python 套件（dearpygui / numpy / fonttools）")
    pkg_ok = check_packages()

    step("外部工具（adb / ffmpeg）")
    tool_ok = check_tools(env)

    step("字型")
    check_font()

    step("啟動方式")
    write_launcher()
    make_shortcut(HERE / LAUNCHER)

    step("設定")
    cfg_ok = setup_config()

    # 手機放最後：前面都是電腦這邊的事，做完才輪到要動手機的那一段。
    step("手機")
    dev_ok, usable = setup_device(env)

    how = LAUNCHER if WIN else "python3 run.py"
    print("")
    if not (pkg_ok and tool_ok):
        print("還有東西沒裝成功（上面標 [!!] 的）。修好之後再跑一次：")
        print("    python setup.py")
        return 1

    if dev_ok and usable:
        print("好了，接上手機就能用。開起來：")
        print("    %s" % how)
        print("")
        print("手機上有 %d 支可以剪。選一支 → 「載入以編輯」→ 拖時間軸挑廣告"
              % usable)
        print("→ 「加入移除區間」→ 「寫入手機」。")
        return 0

    print("電腦這邊都裝好了，開起來：")
    print("    %s" % how)
    print("")
    if not dev_ok:
        print("還缺手機那一步（看上面 [--] 的說明）。弄好之後不用整個重跑，")
        print("直接開程式按「重新掃描」就會接上；或是跑 python setup.py --check。")
    elif not usable:
        print("手機連上了，但還沒有可以剪的緩存影片 ——")
        print("先在 B站 App 裡下載一支，再按「重新掃描」。")
    if not cfg_ok:
        print("備份位置還沒設好：程式裡「備份 → 變更備份位置…」可以改。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("")
        print("中斷了。再跑一次會接著做 —— 下載好的檔案留在 tools/_download。")
        sys.exit(130)
