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
    2. %LOCALAPPDATA%\bili_trim_gui\tools（exe 和原始碼版共用這一份）
    3. PATH

把下載好的 zip 拖進程式視窗（install_from），或跑 setup.py，工具就會放進
第 2 步那個資料夾，PATH 完全不用動。已經把 adb／ffmpeg 加進 PATH 的人
也不受影響 —— 第 3 步就會找到。
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# 版本號放這裡：bili_env 是唯一一個每個模組都會載入的共用檔案。
# 回報問題時附上的 --check 輸出第一行就要寫明是哪一版。
VERSION = "0.2.2"

HERE = Path(__file__).resolve().parent

# 打包成 exe（PyInstaller）的時候，原始碼被解到一個暫存資料夾，每次開都不一樣、
# 關掉就刪掉，所以 tools 不能跟著 __file__。exe 和原始碼版都用
# %LOCALAPPDATA%\bili_trim_gui\tools（見 bili_config.tools_dir）。
FROZEN = bool(getattr(sys, "frozen", False))

from bili_config import tools_dir as _tools_dir  # noqa: E402

TOOLS_DIR = _tools_dir()

# 舊版把工具放在專案裡的 tools/（setup.py 下載的、--pack 帶過來的）。
# 開程式時會把它複製進 TOOLS_DIR（adopt_legacy_tools），之後就只看 TOOLS_DIR。
LEGACY_TOOLS_DIR = HERE / "tools"

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

# 缺工具時要告訴使用者去哪裡拿、拿到之後放哪裡。adb 指向官方頁面（要先同意
# 條款才能下載）。ffmpeg 官網沒有下載按鈕、只有一排第三方建置的連結，第一次
# 用的人會迷路，所以 Windows 直接給 gyan.dev 的 release-essentials.zip
# （ffmpeg.org 列的建置；這個檔名永遠指向最新的正式版，不會過期）。
DOWNLOADS = {
    "adb": ("Android SDK Platform-Tools",
            "https://developer.android.com/tools/releases/platform-tools",
            "下載好的 zip 拖進程式視窗就會裝好（不用解壓縮）"),
    "ffmpeg": ("ffmpeg",
               "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
               if os.name == "nt" else "https://ffmpeg.org/download.html",
               "下載好的 zip 拖進程式視窗就會裝好（不用解壓縮）"),
}
DOWNLOADS["ffplay"] = DOWNLOADS["ffmpeg"]
DOWNLOADS["ffprobe"] = DOWNLOADS["ffmpeg"]

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
    label, url, howto = DOWNLOADS.get(name, (name, "", ""))
    text = ("找不到 %s（%s 這一包）—— %s。\n"
            "到 %s 下載，%s —— PATH 不用改。"
            % (name, label, why, url, howto))
    if not FROZEN:      # exe 版旁邊沒有 setup.py，也不一定有 Python
        text += "\n（也可以跑 python setup.py，它會問要不要幫你下載。）"
    return text


# --------------------------------------------------------------------------- #
# 從使用者下載好的壓縮檔（或解開的資料夾）裝工具
# --------------------------------------------------------------------------- #

_WIN = os.name == "nt"

# 一包要拿哪些檔案。adb.exe 不能單獨搬 —— 兩個 dll 要躺在它旁邊，不然一執行
# 就是缺模組；ffmpeg 那包只要三個執行檔，其他（文件、授權、dll 都沒有）不用。
PACKS = (
    ("adb", ["adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"] if _WIN
     else ["adb"], ["version"]),
    ("ffmpeg", ["ffmpeg.exe", "ffplay.exe", "ffprobe.exe"] if _WIN
     else ["ffmpeg", "ffplay", "ffprobe"], ["-version"]),
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _WIN else 0


class InstallError(Exception):
    pass


def _index_dir(root):
    """資料夾裡 {小寫檔名: Path}，同名取路徑最短的；最多往下 4 層。"""
    out = {}
    base = len(root.parts)
    for r, dirs, files in os.walk(root):
        rp = Path(r)
        if len(rp.parts) - base >= 4:
            dirs[:] = []
        for f in files:
            k = f.lower()
            cand = rp / f
            if k not in out or len(cand.parts) < len(out[k].parts):
                out[k] = cand
    return out


def install_from(path, log=lambda s: None):
    """
    把拖進來的東西裝進 tools/，回傳裝好的 [(工具, 版本那一行)]。
    可以是：下載好的 zip、已經解開的資料夾、或資料夾裡的 adb.exe 本身。
    出錯丟 InstallError，訊息是直接給使用者看的一句話。

    只挑需要的檔案平鋪寫進 tools/（不整包解開）：輸出路徑是這裡自己組的，
    壓縮檔裡寫 ../../ 也影響不到別的地方。zipfile 讀每個檔案時會核對 CRC，
    沒下載完或壞掉的檔案在這一步就會被擋下來。
    """
    src = Path(path)
    if not src.exists():
        raise InstallError("找不到這個檔案：%s" % src)
    names = set(n.lower() for _, files, _ in PACKS for n in files)
    if src.is_file() and src.name.lower() in names:
        src = src.parent            # 拖的是 adb.exe 本身：拿它旁邊的檔案

    z = None
    if src.is_dir():
        index = _index_dir(src)
    elif zipfile.is_zipfile(str(src)):
        z = zipfile.ZipFile(str(src))
        index = {}
        for info in z.infolist():
            if info.filename.endswith("/"):
                continue
            k = info.filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
            depth = info.filename.count("/")
            if k not in index or depth < index[k].filename.count("/"):
                index[k] = info
    elif src.suffix.lower() == ".zip":
        raise InstallError("%s 壞了或還沒下載完，重新下載一次。" % src.name)
    elif src.suffix.lower() in (".7z", ".rar", ".xz", ".gz"):
        raise InstallError("%s 是 %s 格式，請改下載 .zip 的版本。"
                           % (src.name, src.suffix))
    else:
        raise InstallError("%s 不是壓縮檔。請拖下載好的 .zip。" % src.name)

    try:
        todo = [(kind, files, args) for kind, files, args in PACKS
                if files[0].lower() in index]
        if not todo:
            raise InstallError("%s 裡面沒有 adb 也沒有 ffmpeg，是不是拖錯檔案了？"
                               % src.name)
        try:
            TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise InstallError("建不出 tools 資料夾（%s）：%s" % (TOOLS_DIR, e))

        done = []
        for kind, files, args in todo:
            lack = [f for f in files if f.lower() not in index]
            if kind == "adb" and lack:
                raise InstallError("這包 adb 不完整，少了 %s。重新下載一次。"
                                   % "、".join(lack))
            log("正在放進 tools：%s" % kind)
            for f in files:
                if f.lower() not in index:
                    continue        # ffplay 缺了只是預覽沒聲音，不擋
                _write_one(z, index[f.lower()], TOOLS_DIR / f, kind)
            exe = TOOLS_DIR / files[0]
            try:
                r = subprocess.run([str(exe)] + args, capture_output=True,
                                   timeout=30, creationflags=_NO_WINDOW)
                line = r.stdout.decode("utf-8", "replace").strip().splitlines()
            except (OSError, subprocess.SubprocessError) as e:
                raise InstallError("放好了，但 %s 跑不起來：%s" % (kind, e))
            if r.returncode or not line:
                raise InstallError("放好了，但 %s 跑不起來（結束碼 %s）。"
                                   % (kind, r.returncode))
            done.append((kind, line[0]))
    finally:
        if z is not None:
            z.close()
    for name in TOOLS:
        find(name, refresh=True)
    return done


def adopt_legacy_tools():
    """
    專案 tools/ 裡有、TOOLS_DIR 裡還沒有的工具，複製一份過去（原本的不刪）。
    回傳裝好的 [(工具, 版本那一行)]；沒有要搬的就是 []。
    """
    old = LEGACY_TOOLS_DIR
    if FROZEN or not old.is_dir() or _under(old, TOOLS_DIR) \
            or _under(TOOLS_DIR, old):
        return []
    lacking = [kind for kind, files, _ in PACKS
               if not (TOOLS_DIR / files[0]).is_file()]
    if not lacking:
        return []
    have = _index_dir(old)
    if not any(files[0].lower() in have for kind, files, _ in PACKS
               if kind in lacking):
        return []
    try:
        return install_from(old)
    except InstallError:
        return []           # 舊的那份壞了就算了，照一般「缺工具」的流程走


def _write_one(z, item, out, kind):
    tmp = out.with_name(out.name + ".part")
    try:
        if z is not None:
            with z.open(item) as src, open(str(tmp), "wb") as dst:
                shutil.copyfileobj(src, dst)
        else:
            shutil.copyfile(str(item), str(tmp))
    except zipfile.BadZipFile:
        _unlink(tmp)
        raise InstallError("壓縮檔壞了（%s 核對不過），重新下載一次。" % out.name)
    except OSError as e:
        _unlink(tmp)
        raise InstallError("寫不進 tools 資料夾：%s" % e)
    try:
        os.replace(str(tmp), str(out))
    except PermissionError:
        # 舊的 adb.exe 還在跑（adb 會留一個背景 server），先請它停下來再換
        if kind == "adb" and out.is_file():
            try:
                subprocess.run([str(out), "kill-server"], capture_output=True,
                               timeout=15, creationflags=_NO_WINDOW)
                os.replace(str(tmp), str(out))
                return
            except (OSError, subprocess.SubprocessError):
                pass
        _unlink(tmp)
        raise InstallError("%s 正在使用中，換不掉。關掉用到它的程式再試一次。"
                           % out.name)
    if not _WIN:
        try:
            os.chmod(str(out), 0o755)
        except OSError:
            pass


def _unlink(p):
    try:
        p.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 環境自我檢查
# --------------------------------------------------------------------------- #

PACKAGES = (
    ("dearpygui", "圖形介面", True),
    ("numpy", "預覽影格的像素搬運", True),
    ("fontTools", "補字型（缺了標題會出現「?」）", False),
)


def probe_modules():
    """
    開一個新的行程去 import，而不是在這裡 import，回傳 {套件: 版本或 "!錯誤"}。

    理由是 pip --user 的目標目錄如果在啟動時還不存在，Python 就不會把它
    放進 sys.path；同一個行程裝完照樣 import 不到，於是明明裝好了卻一路
    往下報「沒有」。新行程看到的才是使用者開程式時看到的那一份。
    """
    if FROZEN:
        # exe 裡的 sys.executable 就是 exe 本身，拿它去跑 -c 只會再開一個程式。
        # 套件都封在 exe 裡了，直接在這裡問就好。
        out = {}
        for m, _, _ in PACKAGES:
            try:
                out[m] = getattr(__import__(m), "__version__", "") or "yes"
            except Exception as e:
                out[m] = "!" + type(e).__name__
        return out
    code = ("import json, sys\n"
            "out = {}\n"
            "for m in ('dearpygui', 'numpy', 'fontTools'):\n"
            "    try:\n"
            "        out[m] = getattr(__import__(m), '__version__', '') or 'yes'\n"
            "    except Exception as e:\n"
            "        out[m] = '!' + type(e).__name__\n"
            "sys.stdout.write(json.dumps(out))\n")
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(HERE),
                           capture_output=True, timeout=180)
        return json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except Exception as e:
        return {m: "!" + type(e).__name__ for m, _, _ in PACKAGES}


def refresh_site():
    """把 pip --user 剛建好的目錄補進 sys.path，不然這個行程一直看不到。"""
    import importlib
    import site
    try:
        site.main()
    except Exception:
        pass
    try:
        up = site.getusersitepackages()
        if isinstance(up, str) and os.path.isdir(up) and up not in sys.path:
            sys.path.append(up)
    except Exception:
        pass
    importlib.invalidate_caches()


def pip_install(args, log=print):
    """跑 pip，失敗就退回裝到使用者目錄。回傳成功與否。"""
    def run(extra):
        try:
            return subprocess.run([sys.executable, "-m", "pip", "install"]
                                  + extra + list(args)).returncode == 0
        except Exception as e:
            log("跑 pip 出錯：%s" % e)
            return False

    if run([]):
        return True
    # pip 根本不在？先補起來再試
    try:
        subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    if run([]):
        return True
    # 系統 Python 被發行版鎖住時（Debian/Ubuntu 的 externally-managed），
    # 裝到使用者目錄通常就過了。
    log("再試一次，裝到使用者目錄")
    return run(["--user"])


def check():
    """
    回傳 [(項目, 狀態, 說明)]，狀態是 "ok" / "warn" / "bad"。
    setup.py、run.py 和 GUI 都用這一份，不要各寫一套判斷。
    """
    rows = []

    rows.append(("bili-trim", "ok", "v%s  %s%s" % (
        VERSION, sys.executable if FROZEN else HERE, "（exe）" if FROZEN else "")))

    v = sys.version_info
    ok = v >= (3, 8)
    rows.append(("Python", "ok" if ok else "bad",
                 "%d.%d.%d  %s" % (v[0], v[1], v[2],
                                   "封在 exe 裡" if FROZEN else sys.executable)))

    have = probe_modules()
    for mod, why, need in PACKAGES:
        v = have.get(mod, "!unknown")
        if v.startswith("!"):
            rows.append((mod, "bad" if need else "warn",
                         "沒有 —— %s（%s）" % (why, v[1:])))
        else:
            rows.append((mod, "ok", why if v == "yes" else v))

    for name, (_, need, why) in TOOLS.items():
        p = find(name, refresh=True)
        if p:
            where = "tools" if _under(p, TOOLS_DIR) else "PATH"
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
    try:
        res = bili_font.resolve(make=False)
    except Exception as e:
        return "warn", "檢查字型失敗：%s" % e
    if res["body"] is None:
        return "bad", "找不到任何中文字型，標題會整排都是「?」"
    if res["pending"]:
        return "warn", ("介面字型還沒做好（%s）—— 第一次開 GUI 會自己做，"
                        "或跑 python setup.py" % "、".join(res["pending"]))
    faces = [p for p in (res["body"], res["serif"], res["num"]) if p]
    return "ok", "、".join(p.name for p in faces)


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
