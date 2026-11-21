# -*- coding: utf-8 -*-
"""
bili_gui.py — B站 緩存去工商：圖形介面。

流程就是螢幕上從左到右：

    選裝置 -> 選影片 -> 載入（從手機拉下來）-> 拖時間軸挑廣告
    -> 切到「成品」模式邊播邊確認 -> 寫入手機

幾件這個工具沒辦法繞過的事實（都是實測出來的，細節看 bili_trim.py 的開頭）：

  1. 視訊只能整個 fragment 移除，所以切點精度是 GOP 長度（B站 大約 5 秒）。
     時間軸上那排細刻度就是 fragment 邊界，放大就看得到切點會落在哪。
     音訊可以精切（約 23 ms），所以音畫不會飄。
  2. 檔案不能比原本小，不然 B站 會以為沒下載完、跑去續傳過期的網址，
     結果就是永遠卡在「正在緩衝」。所以剪掉的位元組會用 free box 補回去
     —— 廣告真的消失了，但手機容量不會變少。
  3. 不能用 ffmpeg 重新封裝，連「完全不剪」的重封裝都會被 App 拒絕。

備份是強制的，而且是一個檔案（.bilibak），檔名就是影片標題。
"""

import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from bisect import bisect_left, bisect_right
from pathlib import Path

import numpy as np
import dearpygui.dearpygui as dpg

import bili_backup as BK
import bili_config as CFG
import bili_device as DEV
import bili_env as ENV
import bili_font as BF
import bili_look as LOOK
import bili_media as MED
import bili_trim as TRIM

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

APP_NAME = "B站 緩存去工商"

# 視窗標題（作業系統那一條）只能用 ASCII：Dear PyGui 2.3.1 拿到非 ASCII 的
# viewport title 時，create/setup/show 都不會出錯，但第一次 render 就會
# segfault（整個行程直接消失，連 traceback 都沒有）。介面裡面的標籤用中文沒事，
# 只有這一個字串要避開。
WINDOW_TITLE = "bili-trim"

HERE = Path(__file__).resolve().parent

# 視窗標題列與工作列的圖示（exe 版封在 exe 裡，執行時解到 sys._MEIPASS）
ICON_PATH = Path(getattr(sys, "_MEIPASS", HERE)) / "assets" / "bili-trim.ico"


# --------------------------------------------------------------------------- #
# 高 DPI
# --------------------------------------------------------------------------- #
#
# 不宣告自己懂 DPI 的話，Windows 會讓程式以為螢幕只有 2048x1152（125% 縮放下
# 的邏輯像素），畫完再把整張畫面放大 1.25 倍貼上去 —— 那是點陣放大，文字跟
# 影片全部一起變糊。宣告之後拿到的就是實體像素，但介面不會自己變大，所以
# 字型大小與所有寫死的尺寸都要自己乘上縮放倍率（下面的 px()）。

SCALE = 1.0          # 螢幕的 DPI 縮放，由系統決定
UI_SCALE = 1.0       # 使用者自己調的字級倍率（設定選單裡的「介面字級」）

BASE_FONT_PT = CFG.DEFAULT_FONT_PT
FONT_SIZES = (("小", 14), ("標準", 17), ("大", 20),
              ("特大", 23), ("超大", 27))


def font_pt_of(label):
    for name, pt in FONT_SIZES:
        if name == label:
            return pt
    return BASE_FONT_PT


def font_label_of(pt):
    for name, size in FONT_SIZES:
        if size == pt:
            return name
    return "標準"


def set_taskbar_identity():
    """
    讓工作列顯示 bili-trim 的印章，而不是 Python 的圖示。

    用 python run.py 開的時候，Windows 會把這個視窗算成 python.exe 的一份，
    工作列就拿 python.exe 的圖示、跟其他 Python 視窗擠在同一格。給行程一個
    自己的 AppUserModelID，工作列才會改用視窗本身的圖示（create_viewport 的
    large_icon）。exe 版不需要，但設了也無害。必須在建立任何視窗之前呼叫。
    """
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p("yeyeye0618.bili-trim"))
    except (AttributeError, OSError):
        pass


def enable_dpi_awareness():
    """
    回傳螢幕的縮放倍率（125% -> 1.25）。必須在建立任何視窗之前呼叫。

    這三個 API 的「失敗」都是看回傳值而不是丟例外，而且三者的慣例還不一樣
    （BOOL / HRESULT / BOOL），所以得一個一個判，不能呼叫完就當它成功。
    """
    if os.name != "nt":
        return 1.0
    u32 = ctypes.windll.user32
    ok = False

    try:        # Windows 10 1703+：per-monitor v2，(HANDLE)-4
        fn = u32.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_bool
        ok = bool(fn(ctypes.c_void_p(-4)))
    except (AttributeError, OSError):
        pass
    if not ok:  # Windows 8.1+：PROCESS_PER_MONITOR_DPI_AWARE = 2，S_OK = 0
        try:
            ok = ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0
        except (AttributeError, OSError):
            pass
    if not ok:  # 更舊的系統：只有 system-wide
        try:
            ok = bool(u32.SetProcessDPIAware())
        except (AttributeError, OSError):
            pass
    if not ok:
        return 1.0

    try:
        dpi = u32.GetDpiForSystem()
    except (AttributeError, OSError):
        dpi = 0
    if not dpi:
        try:
            dc = u32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)   # LOGPIXELSX
            u32.ReleaseDC(0, dc)
        except (AttributeError, OSError):
            dpi = 0
    return max(dpi / 96.0, 1.0) if dpi else 1.0


def px(n):
    """
    邏輯像素 -> 實體像素。負數是 Dear PyGui 的「填滿／保留」語意，照比例縮。
    字級倍率也乘進來 —— 字變大時按鈕、欄位、時間軸要跟著變大，不然會切字。
    """
    return int(round(n * SCALE * UI_SCALE))


# --------------------------------------------------------------------------- #
# 設定檔
# --------------------------------------------------------------------------- #

# 設定檔的讀寫在 bili_config 裡 —— setup.py 要在使用者第一次開 GUI 之前
# 就把備份位置寫好，而它不能 import 這個檔。
CONFIG_PATH = CFG.CONFIG_PATH
DEFAULTS = CFG.DEFAULTS
load_config = CFG.load_config
save_config = CFG.save_config


# --------------------------------------------------------------------------- #
# 字型（預設字型沒有中日文字，不載就是一排豆腐）
# --------------------------------------------------------------------------- #

# 候選清單在 bili_font 裡 —— setup.py 也要用它，而 setup.py 不能 import 這個檔。
FONT_CANDIDATES = BF.FONT_CANDIDATES


def _dpg_major():
    """要先 create_context 才能問，所以這個只在建介面之後呼叫。"""
    try:
        return int(dpg.get_dearpygui_version().split(".")[0])
    except Exception:
        return 2


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def fmt_time(t, ms=False):
    t = max(float(t), 0.0)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    if ms:
        return "%d:%02d:%06.3f" % (h, m, s)
    return "%d:%02d:%02d" % (h, m, int(s))


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.1f GB" % n


def parse_time(text):
    """吃 '195'、'3:15'、'1:02:03.5' 這幾種寫法。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("空的")
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return total


def complement(segments, lo, hi):
    """[lo,hi] 扣掉 segments 之後剩下的區段。"""
    out, cur = [], lo
    for a, b in sorted(segments):
        a, b = max(a, lo), min(b, hi)
        if b <= cur:
            continue
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
    if cur < hi:
        out.append((cur, hi))
    return out


def fmt_clock(t):
    """播放位置的大字：一律兩位數，數字寬度才不會跳。"""
    t = max(float(t), 0.0)
    return "%02d:%02d:%02d" % (int(t // 3600), int((t % 3600) // 60), int(t % 60))


def fmt_dur(t):
    """影片長度：四捨五入到秒。原長、新長、移除的秒數才對得起來（不要一個取整一個截斷）。"""
    return fmt_time(round(float(t)))


def fmt_delta(d):
    """吸附挪了多少秒：0 就寫 0，不要出現 -0.0。"""
    if abs(d) < 0.05:
        return "0"
    return "%+.1f" % d


def fmt_ruler(t):
    t = int(round(t))
    if t >= 3600:
        return "%d:%02d:%02d" % (t // 3600, (t % 3600) // 60, t % 60)
    return "%d:%02d" % (t // 60, t % 60)


def short_path(p, keep=2):
    """C:\\Users\\x\\Documents\\bili_backups -> Documents\\bili_backups"""
    parts = Path(p).parts
    if len(parts) <= keep + 1:
        return str(p)
    return os.sep.join(parts[-keep:])


def crisp(v):
    """1 px 的線要落在像素中間，不然會糊成兩條半透明的線。"""
    return int(v) + 0.5


# 不能出現在行首的標點（中文排版的避頭規則）
_NO_LINE_START = set("，。、；：？！」』）】〉》…—,.;:?!)]}")
_ASCII_RUN = re.compile(r"[\x00-\x7f]+|[^\x00-\x7f]+")
_WRAP_TOKEN = re.compile(r"[A-Za-z0-9_.,:;!?'\"()\[\]/%+\-@#&*=~]+|\s|.")


# 版面尺寸（邏輯像素，以「標準」字級為準；實際大小一律過 px()）
LIB_W = 298        # 左：快取清單
INSP_W = 324       # 右：這一支
BAR_H = 48         # 頂列
FOOT_H = 30        # 底列

# 時間軸的尺度：一格要多寬才看得出是一格
CELL_MIN = 9
RULER_MINOR = {1: 5, 2: 4, 5: 5, 10: 5, 15: 3, 30: 3, 60: 4, 120: 4, 300: 5,
               600: 5, 900: 3, 1800: 3, 3600: 4, 7200: 4}


# --------------------------------------------------------------------------- #
# 主程式
# --------------------------------------------------------------------------- #

class App(object):

    def _compute_metrics(self):
        """
        時間軸的幾何（實體像素）。DPI 與字級都會影響，所以換字級重建介面時
        要重算一次 —— 不重算的話時間軸會維持舊尺寸，字變大就擠在一起。
        """
        # 時間軸由上而下：刻度、稿紙格（一格一個 fragment）、標記帶、說明列
        self.TL_RULER = px(28)
        self.TL_CELLS = px(50)
        self.TL_BAND = px(26)
        self.TL_FOOT = px(22)
        self.TL_H = self.TL_RULER + self.TL_CELLS + self.TL_BAND + self.TL_FOOT
        self.EDGE_GRAB = px(7)    # 抓刪除號兩端的像素容忍度
        self.ROW_H_HEAD = px(30)
        self.ROW_H = px(36)
        # 區間表固定高度：表頭 + 三列（兩段 + 草稿列，再多就捲動）
        self.CUTS_H = self.ROW_H_HEAD + 4 * (self.ROW_H + 1) + 2
        self._cuts_compact = False

    def __init__(self):
        self._compute_metrics()
        self.cfg = load_config()
        self.log_lines = []
        self.font_paths = {}
        self.font_files = {}
        self.F = {}               # 字型角色 -> Dear PyGui 的 font id
        self.T = {}               # 主題名稱 -> theme id
        self._font_regs = []
        self._wcache = {}
        self._aligns = []
        self._hrules = []
        self._vrules = []
        self._segs = {}
        self._later = []
        self._hover = None
        self._foot_text = ""
        self._prefer_serial = None
        self._wifi_busy = False
        self._grid_k = 1
        self._native = (0, 0)
        self._adb_label = "adb 檢查中…"
        self._ff_label = ""
        self._stage_note = ""
        self.ui_q = queue.Queue()
        self.busy = False
        self.busy_name = ""

        # 裝置 / 清單
        self.device = None
        self.devices = []
        self.roots = []
        self.items = []
        self.filtered = []
        self.item = None

        # 編輯中的影片
        self.local = None          # 本機暫存（鏡像手機上現在那一份）
        self.q_dir = None
        self.vtrack = None
        self.duration = 0.0
        self.bounds = []
        self.cuts = []             # [[a, b], ...] 使用者要求的（還沒吸附）
        self.mark_in = None
        self.mark_out = None

        # 預覽
        self.src = None
        self.player = None
        self.scrub = MED.Scrubber()
        self.playhead = 0.0
        self.preview_mode = "source"
        self.tex_buf = None
        self.tex_w = 0            # 影格貼圖的尺寸，載入影片時才知道（取原生解析度）
        self.tex_h = 0
        self.show_w = 0           # 畫面上實際顯示的大小（照編輯區寬度縮）
        self.show_h = 0
        self._last_frame = None   # 重建貼圖時用來把畫面補回去
        self._fit_key = None

        # 時間軸視窗
        self.view_a = 0.0
        self.view_b = 1.0
        self.tl_w = px(1000)
        self._below_h = 0
        self._drag = None
        self._pending_redraw = False

    # ======================================================================= #
    # 主執行緒信箱：工作執行緒只能丟 callable 進來，不能直接碰 UI
    # ======================================================================= #

    def post(self, fn):
        self.ui_q.put(fn)

    def pump(self):
        for _ in range(64):
            try:
                fn = self.ui_q.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                self.log("UI 回呼出錯：\n%s" % traceback.format_exc())

    def log(self, msg):
        # 主控台也留一份：視窗裡的訊息區會捲動，出問題時用終端機看最直接
        text = str(msg).rstrip()
        try:
            print(text, flush=True)
        except (OSError, ValueError, UnicodeError):
            pass
        # 存起來，換字級要重建介面時才能把訊息區原樣補回去
        stamped = "[%s] %s" % (time.strftime("%H:%M:%S"), text)
        self.log_lines.append(stamped)
        del self.log_lines[:-300]
        if threading.current_thread() is threading.main_thread():
            self._emit_log(stamped)
        else:
            self.post(lambda: self._emit_log(stamped))

    def _emit_log(self, stamped):
        if not dpg.does_item_exist("log_area"):
            return
        loud = ("失敗" in stamped or "出錯" in stamped or "警告" in stamped
                or "不符" in stamped)
        for i, line in enumerate(stamped.splitlines() or [""]):
            t = dpg.add_text(line if i == 0 else ("    " + line),
                             parent="log_area", wrap=0,
                             color=LOOK.INK if loud else LOOK.INK_2)
            self.font(t, self.role_for(line, "strong_s") if loud else "small")
        dpg.set_y_scroll("log_area", -1.0)
        if dpg.does_item_exist("last_msg"):
            first = stamped.split("] ", 1)[-1].splitlines()[0] if stamped else ""
            line = self.wrap(first, "small", px(560), 1)[0]
            dpg.set_value("last_msg", line)
            dpg.configure_item("last_msg",
                               color=LOOK.INK if loud else LOOK.INK_2)

    def progress(self, frac, text=""):
        def do():
            if not dpg.does_item_exist("prog"):
                return
            dpg.set_value("prog", max(0.0, min(1.0, frac)))
            dpg.set_value("prog_label", text)
            busy = bool(text) or frac > 0
            dpg.configure_item("prog", show=busy)
            dpg.configure_item("prog_label", show=busy)
            dpg.configure_item("last_msg", show=not busy)
        self.post(do)

    def _mk_progress(self, prefix=""):
        """給 bili_device / bili_backup 用的 progress callback（會節流）。"""
        last = [0.0]

        def cb(done, total, label=""):
            now = time.perf_counter()
            if now - last[0] < 0.1 and done < total:
                return
            last[0] = now
            frac = (done / float(total)) if total else 0.0
            self.progress(frac, "%s%s  %s / %s"
                          % (prefix, label, fmt_bytes(done), fmt_bytes(total)))
        return cb

    # ---- 工作執行緒 ----

    def run_worker(self, name, fn, then=None):
        if self.busy:
            self.log("還在忙（%s），等它跑完。" % self.busy_name)
            return False
        self.busy = True
        self.busy_name = name
        self.post(self.sync_enabled)

        def body():
            try:
                result = fn()
            except Exception as e:
                tb = traceback.format_exc()
                self.log("【%s】失敗：%s" % (name, e))
                if isinstance(e, DEV.AdbError) and self.device is not None \
                        and self.device.wireless:
                    self.log("    用 Wi-Fi 連著：手機螢幕鎖住之後，有些手機（例如 vivo）會停掉"
                             "無線偵錯。把螢幕打開，按「重新掃描手機」讓程式重新找到它；"
                             "還是不行就在手機上把無線偵錯關掉再打開。")
                self.post(self.show_log)      # 失敗一定要讓人看到整段說明
                if not isinstance(e, (DEV.AdbError, BK.BackupError,
                                      MED.MediaError, ValueError)):
                    self.log(tb)
                self.progress(0.0, "")
            else:
                if then is not None:
                    self.post(lambda: then(result))
            finally:
                self.busy = False
                self.busy_name = ""
                self.post(self.sync_enabled)

        threading.Thread(target=body, name=name, daemon=True).start()
        return True

    # ---- 小工具：字型、量字、對齊、線 ----

    def font(self, item, role):
        f = self.F.get(role)
        if f is not None and item:
            dpg.bind_item_font(item, f)
        return item

    def theme(self, item, name):
        t = self.T.get(name)
        if t is not None and item:
            dpg.bind_item_theme(item, t)
        return item

    # 粗體、襯線、數字字型都沒補字：遇到它畫不出來的字就退回補過字的內文
    PLAIN_ROLE = {"strong": "body", "strong_s": "small", "action": "body",
                  "title": "body", "head": "body", "num": "body",
                  "num_s": "small", "num_l": "lead", "num_v": "body",
                  "clock": "body",
                  "mark": "body"}

    def role_for(self, text, role):
        if role not in self.PLAIN_ROLE:
            return role
        face = LOOK.FONT_ROLES[role][0]
        path = self.font_files.get(face)
        if path is None or path == self.font_files.get("body"):
            return role
        if BF.covers(path, text):
            return role
        return self.PLAIN_ROLE[role]

    def _exact_width(self, text, role):
        f = self.F.get(role)
        try:
            sz = dpg.get_text_size(text, font=f) if f else dpg.get_text_size(text)
        except Exception:
            return None
        if sz and sz[0] > 0:
            return float(sz[0])
        return None

    def measure(self, text, role="body"):
        """文字寬度（實體像素）。字型還沒載好時用估的，寧可估寬一點。"""
        if not text:
            return 0.0
        w = self._exact_width(text, role)
        if w is not None:
            return w
        size = px(LOOK.SIZES[LOOK.FONT_ROLES[role][1]])
        return sum(size * (0.86 if ord(c) > 0x2E7F else 0.56) for c in text)

    def wrap(self, text, role, width, max_lines):
        """手動斷行（中文可以任意斷），超過行數就在最後一行補「…」。"""
        cache = self._wcache.setdefault(role, {})

        def cw(ch):
            w = cache.get(ch)
            if w is None:
                w = self._exact_width(ch, role)
                if w is None:
                    return self.measure(ch, role)
                cache[ch] = w
            return w

        # 英數字串當成一個詞（不要把 bytes 斷成 byt / es），中文一個字一個單位
        tokens = []
        for tok in _WRAP_TOKEN.findall(text):
            if len(tok) > 1 and sum(cw(c) for c in tok) > width:
                tokens.extend(tok)        # 一個詞比整行還長，只好逐字斷
            else:
                tokens.append(tok)
        lines, cur, cur_w, i, n = [], "", 0.0, 0, len(tokens)
        while i < n:
            tok = tokens[i]
            w = sum(cw(c) for c in tok)
            # 句讀不放行首：逗號、句號、右括號這些超出一點也掛在上一行行尾
            if cur and cur_w + w > width and not (len(tok) == 1 and tok in _NO_LINE_START):
                lines.append(cur.rstrip())
                cur, cur_w = "", 0.0
                if len(lines) == max_lines:
                    break
                if tok.isspace():
                    i += 1
                    continue
            cur += tok
            cur_w += w
            i += 1
        if cur and len(lines) < max_lines:
            lines.append(cur)
        if i < n and lines:
            last, ell = lines[-1], "…"
            while last and sum(cw(c) for c in last) + cw(ell) > width:
                last = last[:-1]
            lines[-1] = last.rstrip() + ell
        return lines or [""]

    def later(self, fn, frames=2):
        """過幾個畫格再做 —— 剛載入的字型要畫過一次才量得到寬度。"""
        self._later.append([frames, fn])

    def _tick_later(self):
        if not self._later:
            return
        due = [item for item in self._later if item[0] <= 0]
        for item in self._later:
            item[0] -= 1
        self._later = [item for item in self._later if item[0] >= 0]
        for _, fn in due:
            try:
                fn()
            except Exception:
                self.log("UI 回呼出錯：\n%s" % traceback.format_exc())

    def align_right(self, spacer, block, container, pad, gap):
        """讓 block 貼齊 container 的右邊：每一格調一次中間 spacer 的寬度。"""
        self._aligns.append((spacer, block, container, pad, gap))

    def _tick_aligns(self):
        # spacer 自己沒有矩形可以問，所以看 block 的右緣差多少，就把 spacer 加寬多少
        for spacer, block, container, pad, gap in self._aligns:
            try:
                # 子視窗沒有 rect_min/max 可問，只有位置（在最外層視窗裡，
                # 也就是螢幕座標）和大小
                right = (dpg.get_item_state(container)["pos"][0]
                         + dpg.get_item_rect_size(container)[0] - pad)
                bx = dpg.get_item_rect_max(block)[0]
                bw = dpg.get_item_rect_size(block)[0]
                cur = dpg.get_item_configuration(spacer)["width"]
            except Exception:
                continue
            if bw <= 0:
                continue
            w = max(int(cur + right - bx), 1)
            if abs(w - cur) >= 1:
                dpg.configure_item(spacer, width=w)

    def hrule(self, color, h, width=None, parent=0, track=True, pos=None):
        """水平線。用一張 1x1 白圖染色畫：Dear PyGui 的子視窗最矮 4 px。"""
        kw = {"parent": parent} if parent else {}
        if pos is not None:
            kw["pos"] = pos
        t = dpg.add_image("tex_white", width=width or px(1600), height=max(h, 1),
                          tint_color=color, **kw)
        if width is None and track:
            self._hrules.append(t)
        return t

    def vrule(self, color=LOOK.PAPER_3):
        t = dpg.add_image("tex_white", width=1, height=px(400), tint_color=color)
        self._vrules.append(t)
        return t

    def seg(self, key, labels, value, callback, width=0, height=0):
        """兩段式切換鈕（原片／成品、外擴／內縮）。選中的是墨底白字。"""
        tags = []
        with dpg.group(horizontal=True, horizontal_spacing=0):
            for i, lab in enumerate(labels):
                tag = "seg_%s_%d" % (key, i)
                dpg.add_button(label=lab, tag=tag, width=width, height=height,
                               user_data=(key, lab), callback=self._on_seg)
                tags.append(tag)
        self._segs[key] = {"tags": tags, "labels": labels, "value": value,
                           "cb": callback}
        self._paint_seg(key)

    def _paint_seg(self, key):
        s = self._segs.get(key)
        if not s:
            return
        for tag, lab in zip(s["tags"], s["labels"]):
            if dpg.does_item_exist(tag):
                on = lab == s["value"]
                self.theme(tag, "seg_on" if on else "seg_off")
                self.font(tag, "strong" if on else "body")

    def _on_seg(self, sender, app_data, user_data):
        key, lab = user_data
        s = self._segs.get(key)
        if not s or lab == s["value"]:
            return
        s["value"] = lab
        self._paint_seg(key)
        s["cb"](lab)

    def set_seg(self, key, lab):
        if key in self._segs:
            self._segs[key]["value"] = lab
            self._paint_seg(key)

    @staticmethod
    def _reserve():
        """主體三欄底下要留給底列的高度（含那條 2 px 的黑線）。"""
        return px(FOOT_H) + max(px(2), 1)

    # ======================================================================= #
    # 介面
    # ======================================================================= #

    def build(self):
        dpg.add_texture_registry(tag="tex_reg")
        self._aligns, self._hrules, self._vrules = [], [], []
        with dpg.window(tag="root", no_scrollbar=True):
            self._build_topbar()
            self.hrule(LOOK.INK, max(px(2), 1))
            with dpg.group(horizontal=True, horizontal_spacing=0):
                self._build_library()
                self.vrule()
                self._build_editor()
                self.vrule()
                self._build_inspector()
            self.hrule(LOOK.INK, max(px(2), 1))
            self._build_footer()
        self.theme("root", "root")
        self._build_popups()
        self._build_dialogs()
        dpg.set_primary_window("root", True)

    # ---- 頂列：裝訂邊 ----

    def _build_topbar(self):
        with dpg.child_window(tag="topbar", height=px(BAR_H), border=False,
                              no_scrollbar=True, always_use_window_padding=True):
            with dpg.group(horizontal=True, horizontal_spacing=px(12)):
                with dpg.group() as _g:
                    self.theme(_g, "tight")
                    dpg.add_spacer(height=px(3))
                    self.font(dpg.add_text("BILI-TRIM"), "mark")
                with dpg.group() as _g:
                    self.theme(_g, "tight")
                    dpg.add_spacer(height=px(11))
                    self.font(dpg.add_text("v%s" % ENV.VERSION,
                                           color=LOOK.INK_2), "num_s")
                dpg.add_spacer(width=px(6))
                with dpg.group(horizontal=True, horizontal_spacing=0):
                    dpg.add_drawlist(width=px(24), height=px(32), tag="chip_dot")
                    self.theme(dpg.add_button(label="沒有連線的手機", tag="chip_dev",
                                              height=px(32)), "chip")
                with dpg.group() as _g:
                    self.theme(_g, "tight")
                    dpg.add_spacer(height=px(8))
                    self.font(dpg.add_text("", tag="roots_label",
                                           color=LOOK.INK_2), "small")
                dpg.add_spacer(width=10, tag="bar_sp")
                with dpg.group(horizontal=True, horizontal_spacing=px(2),
                               tag="bar_right"):
                    for tag, label in (("bar_backup", "備份"),
                                       ("bar_settings", "設定"),
                                       ("bar_help", "說明")):
                        b = dpg.add_button(label=label, tag=tag, height=px(32))
                        self.theme(b, "flat")
        self.align_right("bar_sp", "bar_right", "topbar", px(20) - px(8),
                         px(12))
        self.theme("topbar", "bar")

    def _draw_chip_dot(self):
        if not dpg.does_item_exist("chip_dot"):
            return
        dpg.delete_item("chip_dot", children_only=True)
        w, h = px(24), px(32)
        dpg.draw_rectangle((0, 0), (w, h), fill=LOOK.PAPER_2, color=LOOK.PAPER_2,
                           parent="chip_dot")
        s = px(7)
        x0, y0 = px(12) - s / 2.0, h / 2.0 - s / 2.0
        on = self.device is not None
        dpg.draw_rectangle((x0, y0), (x0 + s, y0 + s),
                           fill=LOOK.OK if on else LOOK.PAPER_2,
                           color=LOOK.OK if on else LOOK.INK_2,
                           parent="chip_dot")

    def _sync_device_ui(self):
        """頂列的裝置晶片、快取來源，和裝置選單。"""
        dev = self.device
        if dpg.does_item_exist("chip_dev"):
            if dev is None:
                label = "沒有連線的手機"
            elif dev.wireless:
                label = dev.label.replace("（", " · ").rstrip("）")
            else:
                label = ("%s · %s" % (dev.model, dev.serial) if dev.model
                         else dev.serial)
            dpg.set_item_label("chip_dev", label)
        if dpg.does_item_exist("roots_label"):
            if self.roots:
                dpg.set_value("roots_label", "%s · %d 支" % (
                    "、".join(r.label for r in self.roots), len(self.items)))
            else:
                dpg.set_value("roots_label", "")
        self._draw_chip_dot()
        if dpg.does_item_exist("pop_dev_list"):
            dpg.delete_item("pop_dev_list", children_only=True)
            for n, d in enumerate(self.devices):
                dpg.add_menu_item(label=d.label, parent="pop_dev_list",
                                  check=True, default_value=d is dev,
                                  user_data=n, callback=self.on_device_pick)
            if not self.devices:
                self.font(dpg.add_text("adb 看不到裝置。接上 USB、開啟「USB 偵錯」，"
                                       "並在手機上允許這台電腦；沒有線的話用下面的"
                                       "「用 Wi-Fi 連線」。",
                                       parent="pop_dev_list", wrap=px(300),
                                       color=LOOK.INK_2), "small")
            if dev is not None and dev.wireless:
                dpg.add_menu_item(label="中斷這支的 Wi-Fi 連線",
                                  parent="pop_dev_list",
                                  callback=self.disconnect_wifi)

    def _build_popups(self):
        """頂列的四個選單。點一下就開，不是右鍵。"""
        left = dpg.mvMouseButton_Left
        with dpg.popup("chip_dev", mousebutton=left, tag="pop_dev",
                       min_size=(px(280), px(20))):
            dpg.add_group(tag="pop_dev_list")
            dpg.add_separator()
            dpg.add_menu_item(label="重新掃描裝置與快取",
                              callback=lambda: self.refresh_all())
            dpg.add_menu_item(label="用 Wi-Fi 連線…",
                              callback=self.show_wifi_window)
        with dpg.popup("bar_backup", mousebutton=left, tag="pop_backup",
                       min_size=(px(280), px(20))):
            self.font(dpg.add_text("", tag="backup_dir_label", wrap=px(320),
                                   color=LOOK.INK_2), "small")
            dpg.add_separator()
            dpg.add_menu_item(label="還原備份…", callback=self.show_restore_window)
            dpg.add_menu_item(label="開啟備份資料夾", callback=self.open_backup_dir)
            dpg.add_menu_item(label="變更備份位置…",
                              callback=lambda: dpg.show_item("dlg_backup_dir"))
        with dpg.popup("bar_settings", mousebutton=left, tag="pop_settings",
                       min_size=(px(300), px(20))):
            dpg.add_slider_int(label="音量", default_value=self.cfg["volume"],
                               min_value=0, max_value=100, width=px(170),
                               callback=self.on_volume)
            dpg.add_slider_int(label="音訊延遲 (ms)", width=px(170),
                               default_value=self.cfg["audio_offset_ms"],
                               min_value=-800, max_value=800,
                               callback=self.on_audio_offset)
            dpg.add_combo(("原生解析度", "最高 1080p", "最高 720p", "最高 480p"),
                          label="預覽畫質", width=px(170),
                          default_value=self.cfg["preview_quality"],
                          callback=self.on_preview_quality)
            dpg.add_combo([n for n, _ in FONT_SIZES], label="介面字級",
                          width=px(170), tag="font_combo",
                          default_value=font_label_of(self.cfg["font_pt"]),
                          callback=self.on_font_size)
            dpg.add_checkbox(label="寫入後自動關閉 B站 App",
                             default_value=self.cfg["force_stop_after_write"],
                             callback=self.on_force_stop_toggle)
        with dpg.popup("bar_help", mousebutton=left, tag="pop_help",
                       min_size=(px(220), px(20))):
            dpg.add_menu_item(label="這個工具的限制",
                              callback=lambda: (self.center_window("win_help"),
                                                dpg.show_item("win_help")))
            dpg.add_menu_item(label="環境檢查", callback=self.show_env_window)
        self._sync_device_ui()
        self._sync_backup_label()

    def _sync_backup_label(self):
        d = self.cfg["backup_dir"]
        if dpg.does_item_exist("bar_backup"):
            dpg.set_item_label("bar_backup", "備份 → %s" % short_path(d))
        if dpg.does_item_exist("backup_dir_label"):
            dpg.set_value("backup_dir_label", "寫入前的原檔都收在：\n%s" % d)

    # ---- 左：手機上的快取 ----

    def _build_library(self):
        W = px(LIB_W)
        with dpg.child_window(tag="lib_pane", width=W, height=-self._reserve(),
                              border=False, no_scrollbar=True):
            dpg.add_spacer(height=px(13))
            with dpg.group(horizontal=True, horizontal_spacing=0, indent=px(16)):
                self.font(dpg.add_text("手機上的快取"), "head")
                dpg.add_spacer(width=10, tag="lib_count_sp")
                with dpg.group(tag="lib_count_grp"):
                    dpg.add_spacer(height=px(5))
                    self.font(dpg.add_text("", tag="lib_count",
                                           color=LOOK.INK_2), "num_l")
            dpg.add_spacer(height=px(12))
            f = dpg.add_input_text(tag="lib_filter", width=W - px(32),
                                   indent=px(16), hint="搜尋標題、作者或 cid…",
                                   callback=self.rebuild_library)
            self.theme(f, "search")
            dpg.add_spacer(height=px(10))
            self.hrule(LOOK.PAPER_3, 1, width=W)
            with dpg.child_window(tag="lib_list", border=False,
                                  height=-(px(58) + 1)):
                pass
            self.hrule(LOOK.PAPER_3, 1, width=W)
            dpg.add_spacer(height=px(11))
            b = dpg.add_button(label="重新掃描手機", tag="btn_rescan",
                               width=W - px(32), height=px(36), indent=px(16),
                               callback=lambda: self.refresh_all())
            self.theme(b, "outline")
        self.align_right("lib_count_sp", "lib_count_grp", "lib_pane", px(16), 0)
        self.theme("lib_pane", "pane")

    # ---- 中：墨井、播放、時間軸、區間 ----

    def _build_editor(self):
        with dpg.child_window(tag="edit_pane", width=-(px(INSP_W) + 1),
                              height=-self._reserve(), border=False,
                              no_scrollbar=True, always_use_window_padding=True):
            with dpg.child_window(tag="well", height=px(300), border=True,
                                  no_scrollbar=True):
                self.font(dpg.add_text("載入影片後，畫面會出現在這裡",
                                       tag="well_empty", color=LOOK.ON_WELL_2,
                                       pos=(px(20), px(20))), "body")
                with dpg.group(tag="preview_holder"):
                    pass
                with dpg.child_window(tag="well_tag", border=False, show=False,
                                      no_scrollbar=True, auto_resize_x=True,
                                      auto_resize_y=True, pos=(0, 0)):
                    with dpg.group(horizontal=True, horizontal_spacing=px(5)):
                        for i, role in enumerate(("small", "num_s", "small",
                                                  "num_s", "small", "num_s")):
                            t = dpg.add_text("", tag="well_t%d" % i,
                                             color=(LOOK.ON_WELL_2 if role == "small"
                                                    else LOOK.ON_WELL))
                            self.font(t, role)
                self.theme("well_tag", "well_tag")
            self.theme("well", "well")
            # 墨井自己的主題把間距設成 0，連帶吃掉它下面那一格間距，這裡補回來
            dpg.add_spacer(height=1)

            # ---- 播放 ----
            # 寬度夠就一列；不夠的話「原片／成品」和時間碼換到第二列（見 _tick_transport）
            with dpg.group(tag="transport"):
              with dpg.group(horizontal=True, horizontal_spacing=px(14),
                             tag="tp_row1"):
                with dpg.group(tag="tp_a") as _g:
                    self.theme(_g, "tight")
                    dpg.add_spacer(height=px(4))
                    with dpg.group(horizontal=True, horizontal_spacing=px(6)):
                        b = dpg.add_button(label="播放", tag="btn_play",
                                           width=px(66), height=px(32),
                                           callback=self.toggle_play)
                        self.theme(b, "ink")
                        self.font(b, "strong")
                        for i, (label, cb) in enumerate((
                                ("開頭", lambda: self.seek_to(0)),
                                ("−10s", lambda: self.nudge(-10)),
                                ("−1s", lambda: self.nudge(-1)),
                                ("−格", lambda: self.nudge_frame(-1)),
                                ("格+", lambda: self.nudge_frame(1)),
                                ("+1s", lambda: self.nudge(1)),
                                ("+10s", lambda: self.nudge(10)))):
                            dpg.add_button(label=label, tag="tp_%d" % i,
                                           height=px(32), callback=cb)
                with dpg.group(horizontal=True, horizontal_spacing=px(14),
                               tag="tp_b"):
                  with dpg.group(tag="tp_seg") as _g:
                    self.theme(_g, "tight")
                    dpg.add_spacer(height=px(4))
                    self.seg("mode", ("原片", "成品"),
                             "成品" if self.preview_mode == "result" else "原片",
                             self.on_mode, height=px(32))
                  dpg.add_spacer(width=10, tag="tp_sp")
                  with dpg.group(horizontal=True, horizontal_spacing=px(6),
                                 tag="tp_clock"):
                    self.font(dpg.add_text("00:00:00", tag="clock"), "clock")
                    with dpg.group() as _g:
                        self.theme(_g, "tight")
                        # 小字的基線對齊大字：大字在一般群組裡，被 Dear PyGui
                        # 往下推了 FramePadding.y（px(6)），小字在 tight 裡沒有
                        dpg.add_spacer(height=max(int(round(
                            self._baseline_px("clock") - self._baseline_px("num_l")
                            + px(6))), 0))
                        self.font(dpg.add_text("", tag="clock_sub",
                                               color=LOOK.INK_2), "num_l")
                    # 成品模式的註記是中文，數字字型（Barlow）沒有中文字，
                    # 所以分成另一個字串、用內文字型
                    with dpg.group() as _g:
                        self.theme(_g, "tight")
                        dpg.add_spacer(height=max(int(round(
                            self._baseline_px("clock") - self._baseline_px("small")
                            + px(6))), 0))
                        self.font(dpg.add_text("", tag="clock_note",
                                               color=LOOK.INK_2), "small")
              dpg.add_group(horizontal=True, tag="tp_row2", show=False)
            self._tp_narrow = False
            self.align_right("tp_sp", "tp_clock", "edit_pane", px(19), px(14))

            # ---- 時間軸 ----
            with dpg.group(horizontal=True, horizontal_spacing=px(12),
                           tag="tl_head"):
                self.font(dpg.add_text("時間軸", tag="tl_h"), "head")
                with dpg.group() as _g:
                    self.theme(_g, "tight")
                    dpg.add_spacer(height=px(12))
                    self.font(dpg.add_text("", tag="tl_desc",
                                           color=LOOK.INK_2), "small")
                dpg.add_spacer(width=10, tag="tlh_sp")
                with dpg.group(horizontal=True, horizontal_spacing=px(6),
                               tag="tlh_right"):
                    for tag, label, cb in (("btn_zoom_all", "全片", self.zoom_all),
                                           ("btn_zoom_seam", "看接縫",
                                            self.zoom_seam)):
                        self.theme(dpg.add_button(label=label, tag=tag,
                                                  height=px(30), callback=cb),
                                   "ghost")
            self.align_right("tlh_sp", "tlh_right", "edit_pane", px(19), px(12))
            dpg.add_drawlist(width=self.tl_w, height=self.TL_H, tag="timeline")

            # ---- 要移除的區間 ----
            # 跟左邊的清單一樣自己排列：Dear PyGui 的表格會把每一列撐高，
            # 而且輸入框和文字的基線對不齊
            with dpg.child_window(tag="cuts_list", height=self.CUTS_H,
                                  border=True):
                pass
            self.theme("cuts_list", "cuts")
        self.theme("edit_pane", "work")

    # ---- 右：這一支 ----

    def _build_inspector(self):
        """
        上面是這一支的資料（太長就自己捲），下面固定是動作（吸附、寫入、匯出）——
        視窗再矮，「備份並寫入手機」都不會被擠到看不到的地方。
        """
        with dpg.child_window(tag="insp", width=-1, height=-self._reserve(),
                              border=False, no_scrollbar=True):
            with dpg.child_window(tag="insp_info", height=-1, border=False,
                                  always_use_window_padding=True):
                dpg.add_group(tag="insp_dyn")
            self.theme("insp_info", "insp_info")
            with dpg.child_window(tag="insp_actc", height=-1, border=False,
                                  no_scrollbar=True,
                                  always_use_window_padding=True, show=False):
                dpg.add_group(tag="insp_act")
            self.theme("insp_actc", "insp_act")
        self.theme("insp", "insp")

    def _tick_inspector(self):
        try:
            h = dpg.get_item_rect_size("insp_act")[1]
            total = dpg.get_item_rect_size("insp")[1]
            box = dpg.get_item_rect_size("insp_info")[1]
            info = dpg.get_item_rect_size("insp_dyn")[1]
            over = dpg.get_y_scroll_max("insp_info")
        except Exception:
            return
        has = bool(dpg.get_item_children("insp_act", 1))
        if not has:
            want = -1
        elif h > 0 and dpg.is_item_shown("insp_actc"):
            # 動作緊跟在資料後面；只有放不下的時候才釘在底部、讓資料自己捲
            need = int(box + over) if over > 0 else int(info) + 2 * px(13)
            room = int(total - h - px(16))
            want = need if need <= room else self._whole_rows(room)
        else:
            want = -px(330)      # 先給一塊地方讓它畫出來，下一格再量實際高度
        if dpg.get_item_configuration("insp_info")["height"] != want:
            dpg.configure_item("insp_info", height=want)
        if dpg.is_item_shown("insp_actc") != has:
            dpg.configure_item("insp_actc", show=has)

    def _whole_rows(self, room):
        """
        資料區放不下、要捲的時候，把它的高度切在某一列的下緣：
        最後一列要嘛整列看得到，要嘛整列在捲軸下面，不要切半個字。
        """
        try:
            top = dpg.get_item_state("insp_info")["pos"][1]
            scroll = dpg.get_y_scroll("insp_info")
        except Exception:
            return room
        items = []
        for c in dpg.get_item_children("insp_dyn", 1) or []:
            kids = dpg.get_item_children(c, 1)
            items.extend(kids if kids else [c])     # 資料列包在一個群組裡
        limit = room - px(13)                       # 下方內距
        best = 0
        for it in items:
            try:
                bottom = dpg.get_item_rect_max(it)[1] - top + scroll
            except Exception:
                continue
            if best < bottom <= limit:
                best = bottom
        return int(best + px(13)) if best > 0 else room

    # ---- 底列 ----

    def _build_footer(self):
        with dpg.child_window(tag="foot", height=-1, border=False,
                              no_scrollbar=True, always_use_window_padding=True):
            with dpg.group(horizontal=True, horizontal_spacing=px(14)):
                for tag in ("adb_state", "ff_state", "stage_state"):
                    self.font(dpg.add_text("", tag=tag, color=LOOK.INK_2),
                              "small")
                dpg.add_spacer(width=10, tag="foot_sp")
                with dpg.group(horizontal=True, horizontal_spacing=px(10),
                               tag="foot_right"):
                    # 平常顯示最後一則訊息；忙的時候換成進度
                    self.font(dpg.add_text("", tag="last_msg"), "small")
                    self.font(dpg.add_text("", tag="prog_label"), "small")
                    with dpg.group() as g:
                        self.theme(g, "tight")
                        dpg.add_spacer(height=px(4))
                        p = dpg.add_progress_bar(tag="prog", default_value=0.0,
                                                 width=px(220), height=px(10),
                                                 show=False)
                        self.theme(p, "progress")
                    b = dpg.add_button(label="訊息", tag="btn_log",
                                       callback=self.toggle_log)
                    self.theme(b, "link")
                    self.font(b, "small")
        self.align_right("foot_sp", "foot_right", "foot", px(20), px(14))
        self.theme("foot", "foot")
        self._sync_footer()

    def toggle_log(self):
        if dpg.is_item_shown("win_log"):
            dpg.hide_item("win_log")
        else:
            self.show_log()

    def show_log(self):
        """訊息視窗：開在畫面中間，捲到最底。"""
        if not dpg.does_item_exist("win_log"):
            return
        if not dpg.is_item_shown("win_log"):
            self.center_window("win_log")
        dpg.show_item("win_log")
        dpg.set_y_scroll("log_area", -1.0)

    def copy_log(self):
        self.copy_text("\n".join(self.log_lines))

    # ======================================================================= #
    # 用 Wi-Fi 連線（Android 11 以上的「無線偵錯」）
    # ======================================================================= #

    def _build_wifi_window(self):
        W = px(780)

        def section(title, note):
            dpg.add_spacer(height=px(4))
            self.font(dpg.add_text(title), "strong")
            self.font(dpg.add_text(note, wrap=W, color=LOOK.INK_2), "small")

        self.font(dpg.add_text(
            "不用接線，改走 Wi-Fi：手機要 Android 11 以上，跟電腦在同一個區網（電腦用"
            "網路線也行）。手機上：開發人員選項 →「無線偵錯」打開。找手機和傳檔的時候，"
            "手機螢幕要亮著。", wrap=W), "body")

        section("找手機",
                "程式會找用 Wi-Fi 連過的手機，和在網路上廣播的手機，並自己試出它現在的"
                "埠號（約 3 秒）。找到的列在下面，選一支按「連線」。")
        with dpg.group(horizontal=True):
            b = dpg.add_button(label="找手機", tag="btn_wifi_scan", width=px(110),
                               callback=self.wifi_search)
            self.theme(b, "ink")
            self.font(b, "strong")
            with dpg.group() as g:
                self.theme(g, "tight")
                dpg.add_spacer(height=px(11))
                p_ = dpg.add_progress_bar(tag="wifi_prog", width=px(240),
                                          height=px(10), show=False)
                self.theme(p_, "progress")
        dpg.add_group(tag="wifi_results")
        self.font(dpg.add_text("", tag="wifi_status", wrap=W,
                               color=LOOK.INK_2), "small")

        section("只知道手機的 IP",
                "填「無線偵錯」畫面上的 IP（冒號前面那段），程式會幫你試出埠號。")
        with dpg.group(horizontal=True):
            t = dpg.add_input_text(tag="wifi_ip", width=px(270), hint="192.168.1.23",
                                   on_enter=True, callback=self.wifi_find_ip)
            self.font(t, "num")
            dpg.add_button(label="找埠號", tag="btn_wifi_ip", width=px(110),
                           callback=self.wifi_find_ip)

        section("第一次：配對",
                "在手機上點「使用配對碼配對裝置」，把跳出來的視窗裡的「IP 位址和通訊埠」"
                "和 6 位數配對碼填在這裡。配對只要做一次。")
        with dpg.group(horizontal=True):
            t = dpg.add_input_text(tag="wifi_pair_addr", width=px(270),
                                   hint="192.168.1.23:37165")
            self.font(t, "num")
            t = dpg.add_input_text(tag="wifi_code", width=px(150),
                                   hint="6 位數配對碼", decimal=True,
                                   on_enter=True, callback=self.wifi_pair)
            self.font(t, "body")          # 提示字是中文，數字字型畫不出來
            dpg.add_button(label="配對", tag="btn_wifi_pair", width=px(110),
                           callback=self.wifi_pair)

        section("手動連線",
                "直接填「無線偵錯」畫面上的「IP 位址和通訊埠」（跟配對用的埠號不一樣）。")
        with dpg.group(horizontal=True):
            t = dpg.add_input_text(tag="wifi_conn_addr", width=px(270),
                                   hint="192.168.1.23:41235", on_enter=True,
                                   callback=self.wifi_connect)
            self.font(t, "num")
            dpg.add_button(label="連線", tag="btn_wifi_conn", width=px(110),
                           callback=self.wifi_connect)

        dpg.add_spacer(height=px(4))
        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_button(label="關閉", width=px(96),
                           callback=lambda: dpg.hide_item("win_wifi"))
            with dpg.group() as g:
                self.theme(g, "tight")
                dpg.add_spacer(height=px(7))
                self.font(dpg.add_text(
                    "藍芽沒辦法：adb 只能走 USB 或網路，藍芽傳檔也進不去 B站 的快取資料夾。",
                    color=LOOK.INK_2), "small")

    def show_wifi_window(self):
        self.center_window("win_wifi")
        dpg.show_item("win_wifi")
        self.wifi_search()

    def _wifi_status(self, text, kind="info"):
        if not dpg.does_item_exist("wifi_status"):
            return
        color = {"ok": LOOK.OK, "bad": LOOK.INK}.get(kind, LOOK.INK_2)
        dpg.set_value("wifi_status", text)
        dpg.configure_item("wifi_status", color=color)
        self.font("wifi_status", "strong_s" if kind == "bad" else "small")

    def _wifi_job(self, busy_text, fn):
        """在背景跑一個 adb 動作，按鈕先停用，結果寫回視窗裡那一行。"""
        if getattr(self, "_wifi_busy", False):
            return
        self._wifi_busy = True
        tags = ("btn_wifi_pair", "btn_wifi_conn", "btn_wifi_scan",
                "btn_wifi_ip")
        for t in tags:
            if dpg.does_item_exist(t):
                dpg.configure_item(t, enabled=False)
        self._wifi_status(busy_text)

        def body():
            try:
                result = fn()
            except (DEV.AdbError, ValueError) as e:
                result = (str(e), "bad", None)
            except Exception as e:
                result = ("出錯了：%s" % e, "bad", None)

            def done():
                self._wifi_busy = False
                if dpg.does_item_exist("wifi_prog"):
                    dpg.configure_item("wifi_prog", show=False)
                for t in tags:
                    if dpg.does_item_exist(t):
                        dpg.configure_item(t, enabled=True)
                text, kind, after = result
                self._wifi_status(text, kind)
                if kind == "bad":
                    self.log("Wi-Fi：%s" % text)
                if after:
                    after()
            self.post(done)
        threading.Thread(target=body, name="wifi", daemon=True).start()

    def _fill(self, tag, value):
        if value and dpg.does_item_exist(tag) and not (dpg.get_value(tag) or "").strip():
            dpg.set_value(tag, value)

    # ---- 找手機 ----

    NOT_LISTENING = ("手機在網路上，但無線偵錯沒有在聽。螢幕鎖過的話，有些手機（例如 vivo）"
                     "會悄悄停掉無線偵錯，畫面上卻還寫著開著、還顯示舊的埠號 —— "
                     "在手機上把無線偵錯關掉再打開，再按一次「找手機」。")

    def _wifi_prog(self, done, total, host):
        if dpg.does_item_exist("wifi_prog"):
            dpg.configure_item("wifi_prog", show=True)
            dpg.set_value("wifi_prog", done / float(max(total, 1)))
        self._wifi_status("正在試 %s 的埠號…" % host)

    def _probe_host(self, host, source):
        """（背景執行緒）一台主機：在不在網路上、無線偵錯在哪個埠。回傳一列結果。"""
        row = {"host": host, "source": source, "addrs": [], "note": ""}
        if not DEV.host_reachable(host):
            row["note"] = "不在網路上（手機沒連這個網路，或 IP 換了）"
            return row
        ports = DEV.find_adb_ports(
            host, progress=lambda d, n: self.post(
                lambda d=d, n=n: self._wifi_prog(d, n, host)))
        row["addrs"] = ["%s:%d" % (host, p) for p, _ in ports]
        if not row["addrs"]:
            row["note"] = self.NOT_LISTENING
        return row

    def _show_wifi_results(self, rows):
        if not dpg.does_item_exist("wifi_results"):
            return
        dpg.delete_item("wifi_results", children_only=True)
        names = self.cfg.get("wifi_names") or {}
        W = px(780)
        for row in rows:
            model = names.get(row["host"], "")
            title = "%s（%s）" % (model, row["host"]) if model else row["host"]
            for addr in row["addrs"] or [None]:
                with dpg.group(horizontal=True, parent="wifi_results"):
                    if addr:
                        b = dpg.add_button(label="連線", width=px(90), user_data=addr,
                                           callback=self._wifi_connect_to)
                        self.theme(b, "ink")
                    with dpg.group() as g:
                        self.theme(g, "tight")
                        dpg.add_spacer(height=px(6))
                        self.font(dpg.add_text(
                            "%s　埠號 %s　· %s" % (title, addr.rsplit(":", 1)[1], row["source"])
                            if addr else "%s　· %s" % (title, row["source"])), "body")
                if not addr and row["note"]:
                    self.font(dpg.add_text(row["note"], wrap=W, color=LOOK.INK_2,
                                           parent="wifi_results"), "small")

    def wifi_search(self, *_):
        hosts = list(self.cfg.get("wifi_hosts") or [])

        def work():
            rows, seen = [], set()
            found = DEV.discover(timeout=5)
            pair = found["pair"][0][1] if found["pair"] else ""
            for _, addr in found["connect"]:
                h = DEV.host_of(addr)
                if h not in seen:
                    seen.add(h)
                    rows.append({"host": h, "source": "網路上廣播的", "addrs": [addr],
                                 "note": ""})
            for h in hosts:
                if h not in seen:
                    seen.add(h)
                    rows.append(self._probe_host(h, "用 Wi-Fi 連過"))

            def show():
                self._fill("wifi_pair_addr", pair)
                self._show_wifi_results(rows)
            if any(r["addrs"] for r in rows):
                text, kind = "找到了，選一支按「連線」。", "ok"
            elif rows:
                text, kind = "沒找到開著無線偵錯的手機，看上面每一支的說明。", "info"
            else:
                text, kind = ("還沒用 Wi-Fi 連過手機，網路上也沒有手機在廣播。"
                              "在下面填手機的 IP，程式會幫你試出埠號；第一次還要先配對。"), "info"
            if pair:
                text += "另外找到手機的配對視窗，配對位址已經填好。"
            return text, kind, show
        self._wifi_job("找手機…", work)

    def wifi_find_ip(self, *_):
        text = (dpg.get_value("wifi_ip") or "").strip().replace("：", ":")

        def work():
            host = text.rsplit(":", 1)[0] if ":" in text else text
            DEV.parse_addr(host + ":1")              # 只是借來檢查 IP 的格式
            row = self._probe_host(host, "剛剛填的 IP")

            def show():
                self._show_wifi_results([row])
            if row["addrs"]:
                return "找到了，按「連線」。沒配對過的話，連線會告訴你要先配對。", "ok", show
            return "這個 IP 沒找到無線偵錯，看上面的說明。", "info", show
        self._wifi_job("找埠號…", work)

    def _wifi_connect_to(self, sender, app_data, addr):
        def work():
            DEV.drop_stale(DEV.host_of(addr))
            serial = DEV.connect(addr)
            return self._wifi_connected(serial, DEV.host_of(serial))
        self._wifi_job("連線中…", work)

    def wifi_pair(self, *_):
        addr = dpg.get_value("wifi_pair_addr")
        code = dpg.get_value("wifi_code")

        def work():
            DEV.pair(addr, code)
            host = DEV.host_of(DEV.parse_addr(addr))
            # 配對完手機會改廣播「連線」那一組；adb 有時也會自己連上
            for _ in range(8):
                for d in DEV.list_devices():
                    if d.wireless and (DEV.host_of(d.serial) == host
                                       or not DEV._ADDR.match(d.serial)):
                        return self._wifi_connected(d.serial, host)
                conns = [a for _, a in DEV.discover(timeout=5)["connect"]
                         if DEV.host_of(a) == host]
                if conns:
                    return self._wifi_connected(DEV.connect(conns[0]), host)
                time.sleep(1.0)

            def ask():
                if dpg.does_item_exist("wifi_conn_addr") and \
                        not (dpg.get_value("wifi_conn_addr") or "").strip():
                    dpg.set_value("wifi_conn_addr", host + ":")
            return ("配對好了。現在填「連線」那一格（無線偵錯主畫面上的 IP 位址和通訊埠），"
                    "按「連線」。", "ok", ask)
        self._wifi_job("配對中…", work)

    def wifi_connect(self, *_):
        addr = dpg.get_value("wifi_conn_addr")

        def work():
            DEV.drop_stale(DEV.host_of(DEV.parse_addr(addr)))
            serial = DEV.connect(addr)
            return self._wifi_connected(serial, DEV.host_of(serial))
        self._wifi_job("連線中…", work)

    def _wifi_connected(self, serial, host):
        """（背景執行緒）記住這支手機，回傳讓視窗關掉並重新掃描的結果。"""
        hosts = [h for h in (self.cfg.get("wifi_hosts") or []) if h != host]
        self.cfg["wifi_hosts"] = ([host] + hosts)[:5]
        if DEV._ADDR.match(serial):
            self.cfg["wifi_last"] = serial
        save_config(self.cfg)
        self.log("Wi-Fi：連上 %s" % serial)

        def after():
            dpg.hide_item("win_wifi")
            self._prefer_serial = serial
            self.refresh_all()
        return "連上了。", "ok", after

    def _reconnect_wifi(self, hosts):
        """
        （背景執行緒）幫用 Wi-Fi 連過的手機重新連線。依序：區網廣播 -> 上次那組
        IP:埠號 -> 自己試出埠號（手機重開、無線偵錯關掉再開之後埠號會變）。
        已經有手機連著（USB 或 Wi-Fi）就不花這個時間。
        """
        DEV.drop_stale()
        try:
            if DEV.list_devices():
                return
        except DEV.AdbError:
            return

        def done(addr, how):
            self.cfg["wifi_last"] = addr
            save_config(self.cfg)
            self.log("Wi-Fi：重新連上 %s（%s）" % (addr, how))
            return True

        for _, addr in DEV.discover(timeout=5)["connect"]:
            if DEV.host_of(addr) in hosts:
                try:
                    DEV.connect(addr, timeout=10)
                    return done(addr, "網路廣播")
                except (DEV.AdbError, ValueError):
                    pass
        last = self.cfg.get("wifi_last") or ""
        if last and DEV.host_of(last) in hosts:
            try:
                DEV.connect(last, timeout=6)
                return done(last, "上次的埠號")
            except (DEV.AdbError, ValueError):
                pass
        for host in hosts:
            if not DEV.host_reachable(host):
                continue
            label = "找 %s 的無線偵錯埠號" % host
            self.progress(0.02, label)
            ports = DEV.find_adb_ports(
                host, progress=lambda d, n: self.progress(0.02 + 0.06 * d / n, label))
            for port, _ in ports:
                addr = "%s:%d" % (host, port)
                try:
                    DEV.connect(addr, timeout=10)
                    return done(addr, "試出新的埠號")
                except (DEV.AdbError, ValueError):
                    pass
            if not ports:
                self.log("Wi-Fi：" + self.NOT_LISTENING)

    def disconnect_wifi(self, *_):
        dev = self.device
        if dev is None or not dev.wireless:
            return
        host = DEV.host_of(dev.serial)
        self.cfg["wifi_hosts"] = [h for h in (self.cfg.get("wifi_hosts") or [])
                                  if h != host]
        if DEV.host_of(self.cfg.get("wifi_last") or "") == host:
            self.cfg["wifi_last"] = ""
        save_config(self.cfg)

        def work():
            DEV.disconnect(dev.serial)
            self.log("Wi-Fi：中斷 %s（之後重新掃描不會再自動連它）" % dev.label)
            return DEV.list_devices()

        def done(devs):
            if self.item is not None:
                self.select_item(None)
            self.devices, self.device = devs, None
            self.items, self.roots = [], []
            self._sync_device_ui()
            self.rebuild_library()
            if devs:                      # 還有別支（例如 USB 接著的）就換過去
                self.refresh_all()
        self.run_worker("中斷 Wi-Fi", work, done)

    def _sync_footer(self):
        for tag, text in (("adb_state", self._adb_label),
                          ("ff_state", self._ff_label),
                          ("stage_state", self._stage_note)):
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, text)

    # ---- 預覽貼圖 ----

    def preview_cap(self):
        """設定裡的預覽畫質上限（影格高度）。原生就回 None。"""
        return {"原生解析度": None, "最高 1080p": 1080,
                "最高 720p": 720, "最高 480p": 480}.get(
                    self.cfg["preview_quality"], 720)

    def texture_size_for(self, native_w, native_h):
        """
        影格貼圖取原生解析度（必要時依設定降階），不要先縮小再放大 ——
        縮到比顯示尺寸還小再放大就是把畫面弄糊。寬高都取偶數，給 ffmpeg 好過。
        """
        w, h = int(native_w or 0), int(native_h or 0)
        if w < 16 or h < 16:
            w, h = 1280, 720
        cap = self.preview_cap()
        if cap and h > cap:
            w = int(round(w * cap / float(h)))
            h = cap
        # 太大的貼圖沒意義（記憶體與上傳頻寬），硬上限 1920x1080
        if h > 1080:
            w, h = int(round(w * 1080.0 / h)), 1080
        if w > 1920:
            w, h = 1920, int(round(h * 1920.0 / w))
        return max(w - w % 2, 16), max(h - h % 2, 16)

    def set_preview_texture(self, w, h):
        """（主執行緒）重建影格貼圖與墨井裡的 image 元件。"""
        self.tex_w, self.tex_h = int(w), int(h)
        self.tex_buf = np.zeros(self.tex_w * self.tex_h * 4, dtype=np.float32)
        if not dpg.does_item_exist("tex_reg"):
            dpg.add_texture_registry(tag="tex_reg")
        dpg.delete_item("preview_holder", children_only=True)
        if dpg.does_item_exist("preview_tex"):
            dpg.delete_item("preview_tex")
        dpg.add_raw_texture(width=self.tex_w, height=self.tex_h,
                            default_value=self.tex_buf,
                            format=dpg.mvFormat_Float_rgba,
                            tag="preview_tex", parent="tex_reg")
        self.show_w, self.show_h = self.tex_w, self.tex_h
        dpg.add_image("preview_tex", tag="preview_img", parent="preview_holder",
                      width=self.show_w, height=self.show_h, pos=(0, 0))
        dpg.configure_item("well_empty", show=False)
        dpg.configure_item("well_tag", show=True)
        # 換字級會重建貼圖，把最後那張影格補回去，免得預覽空一塊
        if (self._last_frame is not None
                and self._last_frame.size == self.tex_buf.size):
            np.multiply(self._last_frame, np.float32(1.0 / 255.0),
                        out=self.tex_buf, casting="unsafe")
            dpg.set_value("preview_tex", self.tex_buf)
        self._fit_key = None
        self.fit_preview()

    def clear_preview(self):
        if dpg.does_item_exist("preview_holder"):
            dpg.delete_item("preview_holder", children_only=True)
        if dpg.does_item_exist("well_empty"):
            dpg.configure_item("well_empty", show=True)
        if dpg.does_item_exist("well_tag"):
            dpg.configure_item("well_tag", show=False)
        self._fit_key = None

    def fit_preview(self):
        """
        墨井的高度 = 編輯欄扣掉底下那一疊（播放列到區間表）—— 量出來而不是猜，
        換字型、換縮放都不會把區間表擠出畫面。
        影格：塞得進墨井就用原生大小（1:1 最清楚），塞不進才等比縮小，置中。
        """
        try:
            pane_h = dpg.get_item_rect_size("edit_pane")[1]
            th = dpg.get_item_rect_size("transport")[1]
            hh = dpg.get_item_rect_size("tl_head")[1]
            ww, wh = dpg.get_item_rect_size("well")
        except Exception:
            return
        if pane_h > 0 and th > 0 and hh > 0:
            pad, gap = px(15), px(10)
            want = int(pane_h - 2 * pad - 4 * gap - th - hh - self.TL_H
                       - self.CUTS_H - px(4))
            want = max(want, px(120))
            # 視窗很矮的時候區間表少一列，把高度讓給畫面
            compact = want < px(200) or (self._cuts_compact and want < px(250))
            if compact != self._cuts_compact:
                self._cuts_compact = compact
                self.CUTS_H = self.ROW_H_HEAD + (3 if compact else 4) * (
                    self.ROW_H + 1) + 2
                dpg.configure_item("cuts_list", height=self.CUTS_H)
                return
            if want != self._below_h:
                self._below_h = want
                dpg.configure_item("well", height=want)

        if not dpg.does_item_exist("preview_img") or not self.tex_h:
            if dpg.does_item_exist("well_empty"):
                tw = self.measure(dpg.get_value("well_empty"), "body")
                dpg.configure_item("well_empty", pos=(
                    max(int((ww - tw) / 2), px(10)),
                    max(int((wh - px(17)) / 2), px(10))))
            return
        m = px(12)
        try:
            tag_h = dpg.get_item_rect_size("well_tag")[1] or px(26)
        except Exception:
            tag_h = px(26)
        # 解析度那行字放在畫面下方留給它的一條黑邊裡，不疊在畫面上；
        # 畫面小到放不下（寬不到 300）就不顯示那行字，黑邊也不留
        strip = tag_h + px(10)

        def fit(bottom):
            k = min(1.0, (ww - 2 * m) / float(self.tex_w),
                    (wh - m - bottom) / float(self.tex_h))
            return k, max(int(self.tex_w * k), 16), max(int(self.tex_h * k), 16)

        k, w, h = fit(strip)
        show_tag = w >= px(300)
        if not show_tag:
            k, w, h = fit(m)
            strip = m
        key = (w, h, int(ww), int(wh))
        if key != self._fit_key:
            self._fit_key = key
            self.show_w, self.show_h = w, h
            dpg.configure_item("preview_img", width=w, height=h,
                               pos=(int((ww - w) / 2),
                                    int(m + (wh - m - strip - h) / 2)))
            nw, nh = self._native if self._native[0] else (self.tex_w, self.tex_h)
            scale = ("×1" if k >= 0.999 else "×%.2f" % (w / float(nw)))
            for i, text in enumerate(("原生", "%d×%d" % (nw, nh), "· 顯示",
                                      "%d×%d" % (w, h), "· 縮放", scale)):
                dpg.set_value("well_t%d" % i, text)
        try:
            tw, th = dpg.get_item_rect_size("well_tag")
        except Exception:
            return
        if tw > 0:
            pos, show = (int(ww - tw - px(10)), int(wh - th - px(5))), \
                show_tag
            if dpg.is_item_shown("well_tag") != show:
                dpg.configure_item("well_tag", show=show)
            if tuple(dpg.get_item_pos("well_tag")) != pos:
                dpg.configure_item("well_tag", pos=pos)

    def on_preview_quality(self, sender, app_data):
        self.cfg["preview_quality"] = app_data
        save_config(self.cfg)
        if self.src is None:
            return
        self.log("預覽畫質改成 %s，重新載入這支才會生效。" % app_data)

    # ---- 對話框 ----

    def _build_dialogs(self):
        dpg.add_file_dialog(directory_selector=True, show=False,
                            tag="dlg_backup_dir", width=px(760), height=px(460),
                            callback=self.on_pick_backup_dir)

        with dpg.file_dialog(directory_selector=False, show=False,
                             tag="dlg_backup_file", width=px(760), height=px(460),
                             callback=self.on_pick_backup_file):
            dpg.add_file_extension(BK.EXT, color=LOOK.OK)
            dpg.add_file_extension(".*")

        with dpg.window(label="還原備份", tag="win_restore", show=False,
                        modal=True, width=px(940), height=px(520)):
            with dpg.group(horizontal=True):
                dpg.add_text("", tag="restore_dir_label", wrap=px(700))
                dpg.add_button(label="瀏覽其他檔案…",
                               callback=lambda: dpg.show_item("dlg_backup_file"))
                dpg.add_button(label="重新整理", callback=self.fill_restore_list)
            dpg.add_separator()
            with dpg.child_window(height=px(-104), tag="restore_list"):
                pass
            dpg.add_separator()
            dpg.add_text("", tag="restore_detail", wrap=px(890))
            with dpg.group(horizontal=True):
                b = dpg.add_button(label="還原到手機", tag="btn_do_restore",
                                   width=px(150), height=px(36),
                                   callback=self.do_restore_selected)
                self.theme(b, "write")
                self.font(b, "strong")
                dpg.add_button(label="只核對備份完整性", width=px(180),
                               callback=self.verify_selected)
                dpg.add_button(label="關閉", width=px(96),
                               callback=lambda: dpg.hide_item("win_restore"))

        with dpg.window(label="確認寫入", tag="win_confirm", show=False,
                        modal=True, width=px(720), height=px(420)):
            dpg.add_text("", tag="confirm_text", wrap=px(690))
            dpg.add_separator()
            with dpg.group(horizontal=True):
                b = dpg.add_button(label="備份並寫入", width=px(160),
                                   height=px(40), callback=self.do_write)
                self.theme(b, "write")
                self.font(b, "strong")
                dpg.add_button(label="取消", width=px(96), height=px(40),
                               callback=lambda: dpg.hide_item("win_confirm"))

        with dpg.window(label="環境檢查", tag="win_env", show=False,
                        modal=True, width=px(900), height=px(560)):
            dpg.add_text(
                "這個程式需要兩個外部程式：adb（連手機）和 ffmpeg（預覽畫面）。"
                "把它們放進專案的 tools/ 資料夾就好，不用改系統的 PATH；"
                "已經加進 PATH 的也找得到。", wrap=px(870))
            dpg.add_separator()
            with dpg.child_window(height=px(-96), tag="env_list"):
                pass
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="重新檢查", width=px(120),
                               callback=self.fill_env_window)
                dpg.add_button(label="開啟 tools 資料夾", width=px(190),
                               callback=self.open_tools_dir)
                dpg.add_button(label="把結果寫進訊息", width=px(190),
                               callback=self.log_env_check)
                dpg.add_button(label="關閉", width=px(96),
                               callback=lambda: dpg.hide_item("win_env"))

        with dpg.window(label="用 Wi-Fi 連線", tag="win_wifi", show=False,
                        modal=True, width=px(820), height=px(720)):
            self._build_wifi_window()

        with dpg.window(label="訊息", tag="win_log", show=False,
                        width=px(860), height=px(520), no_collapse=True):
            with dpg.child_window(tag="log_area", border=False, height=-px(46)):
                pass
            self.theme("log_area", "log")
            with dpg.group(horizontal=True):
                dpg.add_button(label="全部複製", width=px(120),
                               callback=self.copy_log)
                dpg.add_button(label="關閉", width=px(96),
                               callback=lambda: dpg.hide_item("win_log"))

        with dpg.window(label="這個工具的限制", tag="win_help", show=False,
                        modal=True, width=px(760), height=px(460)):
            dpg.add_text(HELP_TEXT, wrap=px(730))
            dpg.add_button(label="知道了", width=px(106),
                           callback=lambda: dpg.hide_item("win_help"))

        self._restore_sel = None
        self._restore_items = []

    # ======================================================================= #
    # 啟用 / 停用
    # ======================================================================= #

    def sync_enabled(self):
        loaded = self.vtrack is not None
        has_cuts = bool(self.cuts)
        for tag, on in (
                ("btn_load", not self.busy and self.item is not None
                 and self.item.unsupported_reason() is None),
                ("btn_play", loaded),
                ("btn_write", loaded and has_cuts and not self.busy),
                ("btn_seams", loaded and has_cuts and not self.busy),
                ("btn_add_cut", loaded and self.mark_in is not None
                 and self.mark_out is not None),
                ("btn_mark_in", loaded),
                ("btn_mark_out", loaded),
                ("btn_clear_marks", loaded and (self.mark_in is not None
                                                or self.mark_out is not None)),
                ("seg_mode_0", loaded),
                ("seg_mode_1", loaded),
                ("chip_dev", not self.busy),
                ("btn_zoom_all", loaded),
                ("btn_zoom_seam", loaded and has_cuts),
                ("btn_rescan", not self.busy),
        ) + tuple(("tp_%d" % i, loaded) for i in range(7)):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=bool(on))

    # ======================================================================= #
    # 裝置與清單
    # ======================================================================= #

    def startup(self):
        self._sync_backup_label()
        ok, msg = DEV.check_adb()
        m = re.search(r"version\s+([\d.]+)", msg)
        if ok:
            self._adb_label = "adb %s" % m.group(1) if m else "adb 可用"
        else:
            self._adb_label = "找不到 adb"
        self._ff_label = "ffmpeg 檢查中…" if MED.have_ffmpeg() else "找不到 ffmpeg"
        self._sync_footer()
        self._probe_ffmpeg()
        self.log("%s v%s 啟動" % (APP_NAME, ENV.VERSION))
        self.log("adb：%s" % msg)
        if not MED.have_ffmpeg() or not MED.have_ffplay():
            for name in ("ffmpeg", "ffprobe", "ffplay"):
                if not ENV.have(name):
                    self.log(ENV.missing_hint(name))
        self.log("備份位置：%s" % self.cfg["backup_dir"])
        if ok:
            self.refresh_all()
        else:
            self.log("補好之後按「重新掃描」。")
        self.sync_enabled()
        # 連不到手機（adb）或看不到畫面（ffmpeg/ffprobe）就等於不能用。
        # 這種時候不要只在訊息區留一行 —— 直接把「去哪裡拿、放哪裡」
        # 的視窗開出來，不然使用者只會看到一個什麼都不能按的介面。
        if not ok or not MED.have_ffmpeg():
            self.show_env_window()

    def _probe_ffmpeg(self):
        """底列要寫 ffmpeg 的版本；問一次要幾十毫秒，丟到背景。"""
        if not MED.have_ffmpeg():
            return

        def body():
            label = "ffmpeg 可用"
            try:
                out = subprocess.run([ENV.path("ffmpeg"), "-version"],
                                     capture_output=True, timeout=20,
                                     creationflags=getattr(
                                         subprocess, "CREATE_NO_WINDOW", 0))
                first = out.stdout.decode("utf-8", "replace").splitlines()[0]
                # 正式版是 8.0 這種號碼；每日建置（gyan.dev 的 git 版）是日期
                m = (re.search(r"version\s+(\d{4}-\d{2}-\d{2})", first)
                     or re.search(r"version\s+n?([\d.]+)", first))
                if m:
                    label = "ffmpeg %s" % m.group(1)
            except (OSError, IndexError, subprocess.SubprocessError):
                pass

            def done():
                self._ff_label = label
                self._sync_footer()
            self.post(done)
        threading.Thread(target=body, name="ffmpeg-version", daemon=True).start()

    # 環境檢查視窗裡的狀態標記。紅色留給刪除號，缺東西用黑字加粗表示。
    STATE_COLOR = {"ok": LOOK.OK, "warn": LOOK.WARN, "bad": LOOK.INK}

    @staticmethod
    def center_window(tag):
        """DPG 的視窗預設貼在左上角；開在畫面中間比較像一回事。"""
        try:
            cfg = dpg.get_item_configuration(tag)
            vw = dpg.get_viewport_client_width()
            vh = dpg.get_viewport_client_height()
            dpg.configure_item(tag, pos=[max((vw - cfg["width"]) // 2, 0),
                                         max((vh - cfg["height"]) // 2, 0)])
        except Exception:
            pass

    def show_env_window(self):
        self.fill_env_window()
        self.center_window("win_env")
        dpg.show_item("win_env")

    def env_rows(self):
        rows = ENV.check()
        rows.append(("字型", ) + ENV.font_state())
        return rows

    def fill_env_window(self):
        """
        把 ENV.check() 的結果畫出來，缺的東西再補一段「去哪裡拿、放哪裡」。
        看程式碼的人不會知道要點什麼，所以這裡要把下一步講完 ——
        連結、資料夾、重新檢查都做成按鈕。
        """
        dpg.delete_item("env_list", children_only=True)

        missing = [n for n in ("adb", "ffmpeg", "ffplay", "ffprobe")
                   if not ENV.have(n)]
        if not missing:
            dpg.add_text("兩個外部程式都在，接上手機按「重新掃描」就可以開始。",
                         parent="env_list", color=self.STATE_COLOR["ok"],
                         wrap=px(860))

        # 同一包的只講一次（ffmpeg / ffplay / ffprobe 是一起下載的）
        shown = set()
        for name in missing:
            label, url, howto = ENV.DOWNLOADS[name]
            if label in shown:
                continue
            if shown:
                dpg.add_separator(parent="env_list")
            shown.add(label)
            with dpg.group(parent="env_list"):
                dpg.add_text("缺 %s" % label, color=self.STATE_COLOR["bad"])
                same = [n for n in missing if ENV.DOWNLOADS[n][0] == label]
                dpg.add_text("少了：%s" % "、".join(same), wrap=px(860))
                for n in same:
                    dpg.add_text("    %s —— %s" % (n, ENV.TOOLS[n][2]),
                                 wrap=px(840))
                # 網址放在唯讀輸入框裡：按鈕都失靈的時候，還能自己框選、Ctrl+C
                with dpg.group(horizontal=True):
                    dpg.add_text("下載：")
                    t = dpg.add_input_text(default_value=url, readonly=True,
                                           width=px(780))
                    self.font(t, "small")
                dpg.add_text("放法：%s" % howto, wrap=px(860))
                dpg.add_text("tools 資料夾在：%s（按下面的「開啟 tools 資料夾」，"
                             "沒有的話會幫你建）" % ENV.TOOLS_DIR, wrap=px(860))
                status = dpg.generate_uuid()
                # Dear PyGui 呼叫按鈕回呼時固定傳 (sender, app_data, user_data)，
                # 網址要放 user_data —— 寫成 lambda 的預設參數會被第三個引數蓋成 None
                with dpg.group(horizontal=True):
                    dpg.add_button(label="開啟下載頁", width=px(140),
                                   user_data=(url, status),
                                   callback=lambda s_, a_, u:
                                   self.open_url(*u))
                    dpg.add_button(label="複製網址", width=px(120),
                                   user_data=(url, status),
                                   callback=lambda s_, a_, u:
                                   self.copy_text(*u))
                self.font(dpg.add_text("", tag=status, wrap=px(860),
                                       color=LOOK.INK_2), "small")

        # 缺的東西放最上面：第一次用的人一打開就要看到按鈕，不用往下捲
        dpg.add_separator(parent="env_list")
        self.font(dpg.add_text("全部的檢查結果", parent="env_list"), "strong")
        rows = self.env_rows()
        for item, state, note in rows:
            with dpg.group(horizontal=True, parent="env_list"):
                dpg.add_text(ENV.MARK.get(state, "     ").strip(),
                             color=self.STATE_COLOR.get(state))
                dpg.add_text("%-10s" % item)
                dpg.add_text(note, wrap=px(620))

    def open_tools_dir(self):
        d = ENV.TOOLS_DIR
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.log("建不出 %s：%s" % (d, e))
            return
        self.open_path(d)

    def _say_near(self, status, msg, bad=False):
        """把結果寫在按鈕旁邊（status 是那行字的 tag）；沒有的話寫進訊息區。"""
        self.log(msg)
        if status and dpg.does_item_exist(status):
            dpg.set_value(status, msg)
            dpg.configure_item(status, color=self.STATE_COLOR["bad"] if bad
                               else LOOK.INK_2)

    def open_url(self, url, status=None):
        err = None
        try:
            if os.name == "nt":
                # 直接交給 Windows 用預設瀏覽器開，失敗會丟 OSError 而不是默默回 False
                os.startfile(url)
                opened = True
            else:
                import webbrowser
                opened = webbrowser.open(url)
        except Exception as e:
            opened, err = False, e
        if opened:
            self._say_near(status, "已經用瀏覽器打開：%s" % url)
            return
        self._say_near(status, "打不開瀏覽器（%s），網址已經複製，貼到瀏覽器就好。"
                       % (err or "找不到預設瀏覽器"), bad=True)
        self.copy_text(url, status, quiet=True)

    def copy_text(self, text, status=None, quiet=False):
        try:
            dpg.set_clipboard_text(text)
        except Exception as e:
            self._say_near(status, "複製不了（%s），請直接框選上面的網址按 Ctrl+C。"
                           % e, bad=True)
            return False
        if not quiet:
            first = text.splitlines()[0] if text else ""
            self._say_near(status, "已複製：%s%s" % (
                first, " …（共 %d 行）" % len(text.splitlines())
                if "\n" in text else ""))
        return True

    def log_env_check(self):
        """
        在應用裡跑一次 setup.py --check 的那份檢查。使用者回報「不能用」時，
        叫他截這段訊息比問十個問題快 —— 它會直接寫出每個東西到底在不在、
        在哪裡、是從 tools/ 還是 PATH 找到的。
        """
        self.log("—— 環境檢查 ——")
        rows = self.env_rows()
        for item, state, note in rows:
            self.log("%s%-10s %s" % (ENV.MARK.get(state, "     "), item, note))
        if ENV.is_fatal(rows):
            self.log("有必要的東西缺了 —— 看「說明 → 環境檢查」那個視窗，"
                     "裡面有下載連結與放法。")
        self.log("—— 檢查完畢 ——")
        self.show_log()

    def refresh_all(self):
        def work():
            hosts = list(self.cfg.get("wifi_hosts") or [])
            if hosts:
                self._reconnect_wifi(hosts)
            devs = DEV.list_devices()
            if not devs:
                # 還沒接手機是正常狀態，不是錯誤：不要因此跳出訊息視窗，
                # 左邊清單會寫下一步怎麼做
                self.log("還沒看到手機：接上 USB 並開啟「USB 偵錯」，或用頂端裝置"
                         "選單的「用 Wi-Fi 連線」。")
                return None
            dev = self.device if self.device in devs else devs[0]
            for d in devs:
                if self.device is not None and d.serial == self.device.serial:
                    dev = d
            prefer, self._prefer_serial = self._prefer_serial, None
            if prefer:
                # 剛用 Wi-Fi 連上的那支優先；adb 自己用 mDNS 連上時序號不是
                # IP:埠號，找不到就退而選第一支用 Wi-Fi 連著的
                match = [d for d in devs if d.serial == prefer] or \
                    [d for d in devs if d.wireless]
                if match:
                    dev = match[0]
            self.log("裝置：%s" % dev.label)
            self.progress(0.1, "找 B站 的快取目錄")
            roots = dev.find_roots()
            if not roots:
                raise DEV.AdbError(
                    "這台手機上找不到 B站 的緩存目錄。已經找過：\n"
                    + "\n".join("  %s" % t.format(pkg=p)
                                for p, _ in DEV.PACKAGES
                                for t in DEV.ROOT_TEMPLATES[:1])
                    + "\n（三種 B站 都找過了。確認 App 裡真的有下載好的影片。）")
            for r in roots:
                self.log("找到 %s：%s" % (r.label, r.path))
            self.progress(0.3, "讀取影片清單")
            items = dev.list_caches(roots, progress=self._mk_progress())
            self.progress(1.0, "完成")
            return devs, dev, roots, items

        def done(res):
            if res is None:
                self.devices, self.device, self.roots, self.items = [], None, [], []
                self._sync_device_ui()
                self.rebuild_library()
                self.progress(0.0, "")
                return
            devs, dev, roots, items = res
            self.devices, self.device, self.roots, self.items = \
                devs, dev, roots, items
            if dev.wireless and DEV._ADDR.match(dev.serial) and dev.model:
                names = dict(self.cfg.get("wifi_names") or {})
                if names.get(DEV.host_of(dev.serial)) != dev.model:
                    names[DEV.host_of(dev.serial)] = dev.model
                    self.cfg["wifi_names"] = names
                    save_config(self.cfg)
            self.log("共 %d 支緩存影片，其中 %d 支可以處理。"
                     % (len(items),
                        sum(1 for i in items if i.unsupported_reason() is None)))
            self._sync_device_ui()
            self.rebuild_library()
            self.progress(0.0, "")

        self.run_worker("掃描", work, done)

    def on_device_pick(self, sender, app_data, user_data):
        if not (0 <= user_data < len(self.devices)):
            return
        d = self.devices[user_data]
        if self.device is not None and d.serial == self.device.serial:
            return
        self.device = d
        self.log("改用裝置 %s" % d.label)
        self._sync_device_ui()
        self.refresh_all()

    # ---- 清單 ----

    def rebuild_library(self, *_):
        """
        每一列的家具固定：標題最多兩行、時長、畫質、大小；不能剪的印灰並寫出原因。
        列是自己排的（絕對座標）：Dear PyGui 的 selectable 只有一行字，
        要做出兩行標題加一行說明、整列都能點，只能把字疊在一個等高的 selectable 上。
        """
        if not dpg.does_item_exist("lib_list"):
            return
        needle = (dpg.get_value("lib_filter") or "").strip().lower()
        self.filtered = [it for it in self.items
                         if not needle or needle in it.display_name.lower()
                         or needle in (it.owner or "").lower()
                         or needle in str(it.cid)]
        dpg.set_value("lib_count", str(len(self.items)) if self.items else "")
        dpg.delete_item("lib_list", children_only=True)
        W = px(LIB_W)
        x = px(16)
        text_w = W - px(32) - px(11)          # 右邊留給捲軸
        if not self.filtered:
            if self.items:
                msg = "沒有符合「%s」的影片。" % needle
            elif self.device is None:
                msg = ("還沒連上手機。接上 USB、開啟「USB 偵錯」，再按下面的「重新掃描手機」。"
                       "沒有線的話，頂端的裝置選單裡有「用 Wi-Fi 連線」。")
            else:
                msg = "這台手機上沒有找到快取影片。先在 B站 App 裡下載幾支。"
            for i, line in enumerate(self.wrap(msg, "small", text_w, 4)):
                self.font(dpg.add_text(line, parent="lib_list", color=LOOK.INK_2,
                                       pos=(x, px(14) + i * px(21))), "small")
            return

        lh, lh_s = px(LOOK.SIZES["body"]), px(LOOK.SIZES["small"])
        y = 0
        for n, it in enumerate(self.filtered):
            bad = it.unsupported_reason()
            sel = it is self.item
            role = self.role_for(it.display_name, "strong" if sel else "body")
            lines = self.wrap(it.display_name, role, text_w, 2)
            why = self.wrap("不能剪：" + bad, "small", text_w, 2) if bad else []
            h = (px(10) + len(lines) * (lh + px(4)) + px(2) + lh_s
                 + (px(5) + len(why) * (lh_s + px(3)) if why else 0) + px(10))
            dpg.add_selectable(label="", tag="sel_%d" % n, parent="lib_list",
                               pos=(0, y), height=h, default_value=sel,
                               user_data=n, callback=self.on_item_pick)
            ty = y + px(10)
            for line in lines:
                self.font(dpg.add_text(line, parent="lib_list", pos=(x, ty),
                                       color=LOOK.INK_2 if bad else LOOK.INK),
                          role)
                ty += lh + px(4)
            ty += px(2)
            mx = x
            quality = it.quality_desc or ("q" + it.quality_dir)
            for text, r, col in ((fmt_time(it.duration_s), "num_s", LOOK.INK_2),
                                 (quality, "num_s" if quality.isascii()
                                  else "strong_s", LOOK.INK),
                                 (fmt_bytes(it.total_bytes), "num_s", LOOK.INK_2)):
                r = self.role_for(text, r)
                self.font(dpg.add_text(text, parent="lib_list", pos=(mx, ty),
                                       color=col), r)
                mx += int(self.measure(text, r)) + px(12)
            ty += lh_s
            if why:
                ty += px(5)
                for line in why:
                    self.font(dpg.add_text(line, parent="lib_list", pos=(x, ty),
                                           color=LOOK.INK_2), "small")
                    ty += lh_s + px(3)
            y += h
            self.hrule(LOOK.PAPER_3, 1, width=W, parent="lib_list", track=False,
                       pos=(0, y))
            y += 1

    def on_item_pick(self, sender, app_data, user_data):
        item = self.filtered[user_data]
        for n in range(len(self.filtered)):
            tag = "sel_%d" % n
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, n == user_data)
        if item is not self.item:
            self.select_item(item)
        # 選中的那一列標題換粗體：要重排，而且不要在這個 selectable 自己的
        # 回呼裡把它刪掉，留到下一格做
        self.post(self.rebuild_library)

    def select_item(self, item):
        self.item = item
        self.unload_video()
        self.show_selection()
        # 底列那則訊息講的是上一支，換了就不要再掛著
        if dpg.does_item_exist("last_msg"):
            dpg.set_value("last_msg", "")

    def show_selection(self):
        """重畫右邊「這一支」。重建介面時也要用，所以不能順手卸載影片。"""
        self.refresh_inspector()
        self.sync_enabled()

    # ---- 右：這一支 ----

    def _insp_width(self):
        try:
            w = dpg.get_item_rect_size("insp")[0]
        except Exception:
            w = 0
        if w <= 0:
            w = px(INSP_W)
        return int(w - 2 * px(16) - px(11) - 1)

    def kv(self, parent, label, value, color=LOOK.INK, width=0):
        """
        資訊欄的一列：左邊名稱、右邊數值（靠右）。
        數值裡的英數字用數字字型、中文用內文，兩者都是一般體 —— 「2 段 · 120.0 s」
        這種混排的值才不會一半粗一半細。
        """
        lw = self.measure(label, "small")
        room = width - lw - px(12)
        runs = [(r, "num_v" if r.isascii() else "body")
                for r in _ASCII_RUN.findall(value)]
        runs = [(r, self.role_for(r, role)) for r, role in runs]
        vw = sum(self.measure(r, role) for r, role in runs)
        if vw > room:                  # 太長（通常是作者名）：整串用內文截短
            value = self.wrap(value, "body", room, 1)[0]
            runs = [(value, "body")]
            vw = self.measure(value, "body")
        parts = [(label, "small", LOOK.INK_2)] + [(r, role, color)
                                                  for r, role in runs]
        base = {role: self._baseline_px(role) for _, role, _ in parts}
        top = max(base.values())
        with dpg.group(horizontal=True, horizontal_spacing=0, parent=parent):
            for i, (text, role, col) in enumerate(parts):
                if i == 1:
                    dpg.add_spacer(width=max(int(width - lw - vw), px(8)))
                drop = int(round(top - base[role]))
                if drop > 0:          # 基線對齊：基線比較高的往下推
                    with dpg.group() as g:
                        self.theme(g, "tight")
                        dpg.add_spacer(height=drop)
                        self.font(dpg.add_text(text, color=col), role)
                else:
                    self.font(dpg.add_text(text, color=col), role)

    def _baseline_px(self, role):
        """這個字型角色的基線離行頂幾個像素。"""
        face, size_key = LOOK.FONT_ROLES[role]
        size = px(LOOK.SIZES[size_key])
        ratio = BF.baseline(self.font_files.get(face)) if \
            self.font_files.get(face) else None
        return size * (ratio if ratio else 0.8)

    def media_sizes(self):
        """(視訊位元組, 音訊位元組)；沒載入就是 (None, None)。"""
        if self.q_dir is None:
            return None, None
        out = []
        for name in ("video.m4s", "audio.m4s"):
            try:
                out.append(os.path.getsize(str(Path(self.q_dir) / name)))
            except OSError:
                out.append(None)
        return tuple(out)

    def refresh_inspector(self):
        if not dpg.does_item_exist("insp_dyn"):
            return
        P = "insp_dyn"
        dpg.delete_item(P, children_only=True)
        dpg.delete_item("insp_act", children_only=True)
        A = "insp_act"
        W = self._insp_width()
        self.font(dpg.add_text("這一支", parent=P), "head")
        it = self.item
        if it is None:
            self.font(dpg.add_text("從左邊的清單挑一支影片。", parent=P,
                                   wrap=W, color=LOOK.INK_2), "body")
            return

        title_role = self.role_for(it.display_name, "title")
        self.font(dpg.add_text(it.display_name, parent=P, wrap=W), title_role)
        dpg.add_spacer(height=px(2), parent=P)
        self.hrule(LOOK.PAPER_3, 1, width=W, parent=P, track=False)

        loaded = self.vtrack is not None
        quality = it.quality_desc or ("q" + it.quality_dir)
        outer, P = P, dpg.add_group(parent=P)
        self.theme(P, "kv")
        self.kv(P, "作者", it.owner or "—", width=W)
        self.kv(P, "cid", str(it.cid), width=W)
        self.kv(P, "畫質", quality, width=W)
        if not loaded:
            self.kv(P, "長度", fmt_dur(it.duration_s), width=W)
            self.kv(P, "大小", fmt_bytes(it.total_bytes), width=W)
        else:
            snap, _ = self.snapped()
            snap = snap or []
            removed = sum(b - a for a, b in snap)
            red = LOOK.VERMILION if snap else LOOK.INK
            self.kv(P, "原長", fmt_dur(self.duration), width=W)
            self.kv(P, "新長", fmt_dur(self.duration - removed),
                    color=red, width=W)
            self.kv(P, "移除", ("%d 段 · %.1f s" % (len(snap), removed))
                    if snap else "還沒有", color=red, width=W)
            vb, ab = self.media_sizes()
            if vb is not None:
                self.kv(P, "視訊", "{:,}".format(vb), width=W)
            if ab is not None:
                self.kv(P, "音訊", "{:,}".format(ab), width=W)
            total = len(self.vtrack.frags)
            gone = 0
            for a, b in snap:
                gone += max(bisect_right(self.bounds, b + 1e-6)
                            - bisect_left(self.bounds, a - 1e-6) - 1, 0)
            self.kv(P, "fragment", "%d → %d" % (total, total - gone)
                    if gone else str(total), width=W)

        P = outer

        bad = it.unsupported_reason()
        if bad:
            self.hrule(LOOK.PAPER_3, 1, width=W, parent=A, track=False)
            self.font(dpg.add_text("不能剪：" + bad, parent=A, wrap=W), "body")
            return

        if not loaded:
            local = self.stage_dir / it.device_path.rsplit("/", 1)[-1]
            st = DEV.stage_state(local)
            hist = st.get("history") or []
            if st.get("orig"):
                note = "本機已有暫存副本，載入很快。"
            else:
                note = "本機還沒有這支，載入要從手機傳 %s。" % fmt_bytes(
                    it.total_bytes)
            if hist:
                note += "這支在這台電腦上剪過 %d 次。" % len(hist)
            self.hrule(LOOK.PAPER_3, 1, width=W, parent=A, track=False)
            self.font(dpg.add_text(note, parent=A, wrap=W, color=LOOK.INK_2),
                      "small")
            b = dpg.add_button(label="載入以編輯", tag="btn_load", parent=A,
                               width=W, height=px(44),
                               callback=lambda: self.on_load_clicked(False))
            self.theme(b, "ink")
            self.font(b, "strong")
            if st.get("orig"):
                b = dpg.add_button(label="放棄暫存，重新從手機拉", parent=A,
                                   width=W, height=px(34),
                                   callback=lambda: self.on_load_clicked(True))
                self.theme(b, "flat")
                self.font(b, "small")
            return

        self.hrule(LOOK.PAPER_3, 1, width=W, parent=A, track=False)
        self.font(dpg.add_text("吸附方向", parent=A, color=LOOK.INK_2), "small")
        with dpg.group(parent=A):
            self.seg("snap", ("外擴", "內縮"),
                     "外擴" if self.cfg["snap"] == "outer" else "內縮",
                     self.on_snap, width=W // 2, height=px(32))
        with dpg.tooltip(self._segs["snap"]["tags"][0]):
            dpg.add_text("切點只能落在 fragment 邊界（約每 5 秒一個）。\n"
                         "外擴：往外吸，寧可多切掉一點正片。")
        with dpg.tooltip(self._segs["snap"]["tags"][1]):
            dpg.add_text("切點只能落在 fragment 邊界（約每 5 秒一個）。\n"
                         "內縮：往內吸，寧可殘留一點廣告。")
        dpg.add_spacer(height=px(2), parent=A)
        b = dpg.add_button(label="備份並寫入手機", tag="btn_write", parent=A,
                           width=W, height=px(50), callback=self.confirm_write)
        self.theme(b, "write")
        self.font(b, "action")
        b = dpg.add_button(label="匯出接縫片段", tag="btn_seams", parent=A,
                           width=W, height=px(36), callback=self.export_seams)
        self.theme(b, "ghost")
        vb, _ = self.media_sizes()
        warn = ("寫入前一定會先存一份乾淨原檔到 %s。" % short_path(
            self.cfg["backup_dir"], 1))
        if vb is not None:
            warn += "檔案大小維持 {:,} bytes —— 手機容量不會變少。".format(vb)
        self.font(dpg.add_text(warn, parent=A, wrap=W, color=LOOK.INK_2),
                  "small")

    @property
    def stage_dir(self):
        return Path(self.cfg["stage_dir"])

    # ======================================================================= #
    # 載入一支影片
    # ======================================================================= #

    def on_load_clicked(self, refresh):
        if self.item is None:
            self.log("先在左邊選一支影片。")
            return
        item = self.item
        if item.unsupported_reason():
            self.log("這支不能處理：%s" % item.unsupported_reason())
            return
        if self.device is None:
            self.log("沒有連上裝置。")
            return

        device = self.device
        stage = self.stage_dir

        def work():
            local = self.ensure_local(device, item, stage, refresh)
            self.progress(0.9, "解析視訊 fragment")
            q = self.quality_dir_of(local)
            vt = TRIM.Track(q / "video.m4s")
            self.log("video：%d 個 fragment，平均 %.2f s，共 %s"
                     % (len(vt.frags),
                        sum(vt.fragment_seconds()) / len(vt.frags),
                        fmt_time(vt.duration_s())))
            self.log(self.describe_version(local, vt))
            src = None
            native = (0, 0)
            if MED.have_ffmpeg():
                info = MED.probe(q / "video.m4s")
                native = (int(info["width"] or 0), int(info["height"] or 0))
                tw, th = self.texture_size_for(info["width"], info["height"])
                self.log("畫面 %dx%d @ %.3g fps（%s），預覽用 %dx%d"
                         % (info["width"], info["height"], info["fps"],
                            info["codec"], tw, th))
                src = MED.FrameSource(q / "video.m4s", tw, th)
            self.progress(1.0, "完成")
            return local, q, vt, src, native

        def done(res):
            local, q, vt, src, native = res
            self._native = native
            self.local, self.q_dir, self.vtrack = local, q, vt
            self.duration = vt.duration_s()
            self.bounds = vt.boundaries()
            self.cuts = []
            self.mark_in = self.mark_out = None
            self.playhead = 0.0
            self.src = src
            if src is not None:
                self.set_preview_texture(src.out_w, src.out_h)
                self.scrub.set_source(src)
                self.player = MED.Player(src, q / "audio.m4s")
                self.player.volume = self.cfg["volume"]
                self.player.audio_offset = self.cfg["audio_offset_ms"] / 1000.0
                self.apply_segments()
                self.scrub.request(0.0)
            else:
                self.player = None
                self.clear_preview()
            self._sync_footer()
            self.zoom_all()
            self.refresh_cuts_ui()
            self.progress(0.0, "")
            self.log("載入完成，可以開始挑廣告了。")

        self.run_worker("載入", work, done)

    def quality_dir_of(self, local):
        cand = [p for p in Path(local).iterdir()
                if p.is_dir() and (p / "video.m4s").is_file()]
        if not cand:
            raise ValueError("%s 裡找不到含 video.m4s 的畫質目錄。" % local)
        if len(cand) > 1:
            raise ValueError("%s 裡有多個畫質目錄：%s"
                             % (local, [p.name for p in cand]))
        return cand[0]

    # ---- 暫存：永遠保存「乾淨的原檔」 ----

    def ensure_local(self, device, item, stage, refresh=False):
        """
        回傳本機暫存目錄（內容是手機上現在那一份的副本）。
        暫存裡那份只要驗得過（大小 + md5 都對得上側記檔），就直接重用，
        不用再從手機拉一次 —— 一支影片動輒好幾百 MB。
        """
        local = Path(stage) / item.device_path.rsplit("/", 1)[-1]
        st = DEV.stage_state(local)

        if refresh and local.exists():
            self.log("放棄本機暫存，重新從手機拉。")
            shutil.rmtree(local, ignore_errors=True)
            st = {}

        if local.is_dir() and st.get("orig"):
            self.progress(0.0, "核對本機暫存")
            ok, why = self.verify_staged(local, st["orig"])
            if ok:
                self.log("重用本機暫存 %s（與手機上那一份一致）" % local)
                self._stage_note = "暫存已驗證 · 重用"
                return local
            self.log("本機暫存不能用：%s —— 重新拉一次。" % why)
            shutil.rmtree(local, ignore_errors=True)
        elif local.is_dir():
            self.log("本機暫存沒有側記檔，判不出是不是原檔 —— 重新拉一次。")
            shutil.rmtree(local, ignore_errors=True)

        self.log("從手機拉取 %s（%s）" % (item.device_path,
                                        fmt_bytes(item.total_bytes)))
        local = device.pull_cache(item, stage, progress=self._mk_progress())

        self.progress(0.0, "計算 md5（之後重用暫存時用來驗證）")
        q = self.quality_dir_of(local)
        orig = {}
        for rel in item.remote_files():
            p = Path(local) / rel
            if p.is_file():
                orig[rel] = {"size": p.stat().st_size,
                             "md5": TRIM.md5_file(p)}
        DEV.write_stage_state(local, {
            "orig": orig, "quality_dir": q.name, "cid": item.cid,
            "device_path": item.device_path,
            "pulled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self.log("已拉到 %s" % local)
        self._stage_note = "剛從手機拉取 · 已記下 md5"
        return Path(local)

    def verify_staged(self, local, orig):
        local = Path(local)
        for rel, want in orig.items():
            p = local / rel
            if not p.is_file():
                return False, "缺少 %s" % rel
            if p.stat().st_size != want["size"]:
                return False, "%s 大小不符" % rel
        for rel, want in orig.items():
            p = local / rel
            self.progress(0.0, "核對 %s" % rel)
            if TRIM.md5_file(p) != want["md5"]:
                return False, "%s 的 md5 與側記不符（手機上那份已經換過了）" % rel
        return True, "一致"

    def describe_version(self, local, track):
        """
        這支之前被剪過沒有。判斷不必額外讀手機 —— track 解析的時候就知道了
        （尾端有補大小用的 free box = 剪過），本機側記再補上剪了幾次、剪了什麼。
        """
        hist = DEV.stage_state(local).get("history") or []
        if not track.trimmed and not hist:
            return "這是原檔，沒有被剪過。"
        if not hist:
            return ("這支之前被剪過（尾端有 %s bytes 補大小用的 free box），"
                    "但不是在這台電腦上剪的，所以查不到剪了哪些段。\n"
                    "時間軸已經是剪完的樣子，現在標的區間就是以目前這一版為準。"
                    % TRIM.human(track.tail_pad))
        lines = ["這支之前剪過 %d 次（時間軸已經是剪完的樣子，"
                 "現在標的區間就是以目前這一版為準）：" % len(hist)]
        for n, h in enumerate(hist, 1):
            cuts = h.get("cuts") or []
            lines.append("  第 %d 次 %s：移除 %d 段，共 %s%s"
                         % (n, h.get("at", "?"), len(cuts),
                            fmt_time(sum(b - a for a, b in cuts)),
                            "；備份 %s" % Path(h["backup"]).name
                            if h.get("backup") else ""))
        return "\n".join(lines)

    def unload_video(self):
        if self.player is not None:
            self.player.shutdown()
        self.player = None
        self.src = None
        self.scrub.set_source(None)
        self.vtrack = None
        self.local = self.q_dir = None
        self.duration = 0.0
        self.bounds = []
        self.cuts = []
        self.mark_in = self.mark_out = None
        self.playhead = 0.0
        if self.tex_buf is not None and dpg.does_item_exist("preview_tex"):
            self.tex_buf[:] = 0.0
            dpg.set_value("preview_tex", self.tex_buf)
        self._last_frame = None
        self.tex_h = 0
        self._native = (0, 0)
        self._stage_note = ""
        self._sync_footer()
        self.clear_preview()
        self.preview_mode = "source"
        self.set_seg("mode", "原片")
        self.refresh_cuts_ui()

    # ======================================================================= #
    # 剪輯區間
    # ======================================================================= #

    def snapped(self):
        """回傳 (吸附後的區間, 說明列)；吸附不出來就回 (None, 錯誤字串)。"""
        if not self.cuts or not self.bounds:
            return [], []
        try:
            return TRIM.snap_to_boundaries(self.bounds, [tuple(c) for c in self.cuts],
                                           self.cfg["snap"])
        except ValueError as e:
            return None, str(e)

    def keep_segments(self):
        snap, _ = self.snapped()
        if snap is None:
            snap = []
        return complement(snap, 0.0, self.duration) or [(0.0, self.duration)]

    def apply_segments(self):
        if self.player is None:
            return
        if self.preview_mode == "result":
            self.player.set_segments(self.keep_segments())
        else:
            self.player.set_segments([(0.0, self.duration)])

    def set_mark_in(self):
        if self.vtrack is None:
            return
        self.mark_in = self.playhead
        if self.mark_out is not None and self.mark_out <= self.mark_in:
            self.mark_out = None
        self.refresh_cuts_ui()

    def set_mark_out(self):
        if self.vtrack is None:
            return
        self.mark_out = self.playhead
        if self.mark_in is not None and self.mark_in >= self.mark_out:
            self.mark_in = None
        self.refresh_cuts_ui()

    def clear_marks(self):
        self.mark_in = self.mark_out = None
        self.refresh_cuts_ui()

    def add_cut_from_marks(self):
        if self.mark_in is None or self.mark_out is None:
            self.log("先設起點與終點。")
            return
        a, b = sorted((self.mark_in, self.mark_out))
        if b - a < 0.05:
            self.log("這段太短了。")
            return
        self.cuts.append([a, b])
        self.cuts.sort()
        self.mark_in = self.mark_out = None
        self.apply_segments()
        self.refresh_cuts_ui()

    def del_cut(self, sender, app_data, user_data):
        if 0 <= user_data < len(self.cuts):
            self.cuts.pop(user_data)
            self.apply_segments()
            self.refresh_cuts_ui()

    def on_cut_edit(self, sender, app_data, user_data):
        idx, side = user_data
        try:
            t = parse_time(app_data)
        except ValueError:
            self.log("時間格式看不懂：%r（可以寫 195、3:15 或 1:02:03.5）"
                     % app_data)
            self.refresh_cuts_ui()
            return
        t = max(0.0, min(t, self.duration))
        cut = self.cuts[idx]
        cut[side] = t
        if cut[1] <= cut[0]:
            self.log("終點必須大於起點，改回來了。")
            self.refresh_cuts_ui()
            return
        self.cuts.sort()
        self.apply_segments()
        self.refresh_cuts_ui()

    def goto_cut(self, sender, app_data, user_data):
        snap, _ = self.snapped()
        idx = user_data
        if snap and idx < len(snap):
            a, b = snap[idx]
        else:
            a, b = self.cuts[idx]
        self.seek_to(max(a - 3, 0))
        self.zoom_to(a - 10, b + 10)

    def _cut_cols(self, W):
        """區間表的欄位：(左緣, 寬)。起、迄兩欄平分剩下的寬度。"""
        pad, gap = px(13), px(12)
        fixed = (("tick", px(16)), ("len", px(80)), ("snap", px(116)),
                 ("look", px(36)), ("del", px(52)))
        half = int((W - 2 * pad - sum(w for _, w in fixed) - 6 * gap) / 2)
        if half < px(112):
            # 窄的時候先拿掉「吸附」那欄（滑到長度上看得到），間距也收緊，
            # 起迄要放得下到毫秒的時間
            pad, gap = px(10), px(7)
            fixed = (("tick", px(14)), ("len", px(66)), ("look", px(30)),
                     ("del", px(46)))
            half = max(int((W - 2 * pad - sum(w for _, w in fixed)
                            - 5 * gap) / 2), px(96))
        order = (fixed[0], ("a", half), ("b", half)) + fixed[1:]
        cols, x = {}, pad
        for key, w in order:
            cols[key] = (x, w)
            x += w + gap
        return cols

    def refresh_cuts_ui(self):
        self._fill_cuts_table()
        self.refresh_inspector()
        self.apply_segments()
        self.request_redraw()
        self.sync_enabled()

    def _fill_cuts_table(self):
        L = "cuts_list"
        if not dpg.does_item_exist(L):
            return
        dpg.delete_item(L, children_only=True)
        W = int(self.tl_w) - px(11) - 2          # 右邊留給捲軸
        C = self._cut_cols(W)
        HH, RH = self.ROW_H_HEAD, self.ROW_H
        lh = px(LOOK.SIZES["body"])
        lh_s = px(LOOK.SIZES["small"])

        def text(s, key, y, role="num", color=LOOK.INK, h=RH):
            x = C[key][0]
            t = dpg.add_text(s, parent=L, color=color,
                             pos=(x, y + (h - (lh_s if role in ("small", "num_s")
                                               else lh)) // 2))
            self.font(t, role)
            return t

        def button(label, key, y, theme, cb, tag=0, user_data=None, role="body",
                   width=0, h=px(26)):
            kw = {"tag": tag} if tag else {}
            b = dpg.add_button(label=label, parent=L, height=h,
                               width=width, user_data=user_data, callback=cb,
                               pos=(C[key][0], y + (RH - h) // 2), **kw)
            self.theme(b, theme)
            self.font(b, role)
            return b

        def tick(y, color, h):
            dpg.add_image("tex_white", parent=L, width=px(14), height=h,
                          tint_color=color,
                          pos=(C["tick"][0], y + (RH - h) // 2))

        # 表頭
        self.hrule(LOOK.PAPER_2, HH, width=W + px(11), parent=L, track=False,
                   pos=(0, 0))
        for key, label in (("a", "起"), ("b", "迄"), ("len", "實際刪"),
                           ("snap", "吸附挪動")):
            if key in C:
                text(label, key, 0, "small", LOOK.INK_2, h=HH)
        self.hrule(LOOK.PAPER_3, 1, width=W + px(11), parent=L, track=False,
                   pos=(0, HH))
        y = HH + 1

        loaded = self.vtrack is not None
        snap, rows = self.snapped()
        if snap is None:
            text("吸附失敗：%s" % rows, "a", y, "small")
            y += RH + 1
            rows = []
        ih = lh + 2 * px(3)                          # 輸入框的高度
        for i, cut in enumerate(self.cuts):
            row = rows[i] if i < len(rows) else None
            tick(y, LOOK.VERMILION, max(px(3), 2))
            for side, key in ((0, "a"), (1, "b")):
                # 顯示實際會刪的範圍（吸附之後），跟長度、刪除號、確認視窗一致；
                # 改這格設定的是你標的點，存回去之後會再吸附一次
                shown = cut[side] if row is None else row[2 + side]
                t = dpg.add_input_text(default_value=fmt_time(shown, True),
                                       parent=L, width=C[key][1] + px(8),
                                       user_data=(i, side), on_enter=True,
                                       callback=self.on_cut_edit,
                                       pos=(C[key][0] - px(4),
                                            y + (RH - ih) // 2))
                self.theme(t, "cell")
                self.font(t, "num")
                with dpg.tooltip(t):
                    dpg.add_text("實際從這裡切（已吸附到 fragment 邊界）。你標的是 %s。\n"
                                 "可以直接改：打 195、3:15 或 1:02:03.5，按 Enter，"
                                 "會再吸附一次。" % fmt_time(cut[side], True))
            # 長度寫「實際會刪掉的」（吸附之後），跟刪除號、資訊欄、確認視窗同一個數
            real = (row[3] - row[2]) if row is not None else (cut[1] - cut[0])
            d = text("%.1f s" % real, "len", y)
            if row is not None:
                _, _, sa, sb = row
                if "snap" in C:
                    d = text("%s / %s" % (fmt_delta(sa - cut[0]),
                                          fmt_delta(sb - cut[1])), "snap", y,
                             color=LOOK.INK_2)
                with dpg.tooltip(d):
                    dpg.add_text("標的是 %.1f 秒，實際會刪 %s – %s（%.1f 秒）。\n"
                                 "切點只能落在 fragment 邊界，所以起訖各挪了一點。"
                                 % (cut[1] - cut[0], fmt_time(sa, True),
                                    fmt_time(sb, True), sb - sa))
            b = button("看", "look", y, "link", self.goto_cut, user_data=i)
            with dpg.tooltip(b):
                dpg.add_text("跳到這段前面，放大看接縫")
            b = button("取消", "del", y, "link", self.del_cut, user_data=i)
            with dpg.tooltip(b):
                dpg.add_text("從清單拿掉這一段（還沒寫入，不會動到影片）")
            y += RH
            self.hrule(LOOK.PAPER_3, 1, width=W + px(11), parent=L,
                       track=False, pos=(0, y))
            y += 1

        # 草稿列：正在標的這一段。I / O 是同一件事的快速鍵。
        tick(y, LOOK.INK_3, 1)
        for key, tag, empty, t, cb in (
                ("a", "btn_mark_in", "設為起點  I", self.mark_in,
                 self.set_mark_in),
                ("b", "btn_mark_out", "設為終點  O", self.mark_out,
                 self.set_mark_out)):
            button(fmt_time(t, True) if t is not None else empty, key, y,
                   "ghost_s", cb, tag=tag, width=C[key][1],
                   role="num" if t is not None else "body")
        if self.mark_in is not None and self.mark_out is not None:
            text("%.1f s" % abs(self.mark_out - self.mark_in), "len", y)
        button("加入", "look", y, "ink_s", self.add_cut_from_marks,
               tag="btn_add_cut", width=C["look"][1] + px(8))
        button("清除", "del", y, "link", self.clear_marks, tag="btn_clear_marks")
        y += RH
        dpg.add_spacer(parent=L, height=1, pos=(0, y))
        if not loaded:
            for tag in ("btn_mark_in", "btn_mark_out", "btn_add_cut",
                        "btn_clear_marks"):
                dpg.configure_item(tag, enabled=False)

    def on_snap(self, value):
        self.cfg["snap"] = "outer" if value == "外擴" else "inner"
        save_config(self.cfg)
        self.refresh_cuts_ui()

    # ======================================================================= #
    # 播放
    # ======================================================================= #

    def on_mode(self, value):
        self.preview_mode = "result" if value == "成品" else "source"
        was = self.player is not None and self.player.playing
        if self.player is not None:
            self.player.pause()
        self.apply_segments()
        if self.preview_mode == "result":
            # 落在被剪掉的區間裡就往後挪到保留處
            for a, b in (self.snapped()[0] or []):
                if a <= self.playhead < b:
                    self.playhead = min(b, self.duration)
                    break
        if was:
            self.player.play(self.playhead)
        else:
            self.scrub.request(self.playhead)
        self.request_redraw()

    def toggle_play(self):
        if self.player is None:
            return
        if self.player.playing:
            self.player.pause()
            dpg.set_item_label("btn_play", "播放")
            self.scrub.request(self.playhead)
        else:
            self.player.volume = self.cfg["volume"]
            self.player.audio_offset = self.cfg["audio_offset_ms"] / 1000.0
            self.player.play(self.playhead)
            dpg.set_item_label("btn_play", "暫停")

    def seek_to(self, t):
        if self.vtrack is None:
            return
        t = max(0.0, min(float(t), self.duration))
        if self.preview_mode == "result":
            # 成品模式下落進被剪掉的區間沒有意義，挪到該段結束處
            for a, b in (self.snapped()[0] or []):
                if a <= t < b:
                    t = min(b, self.duration)
                    break
        self.playhead = t
        if self.player is not None:
            if self.player.playing:
                self.player.seek(self.playhead)
            else:
                self.player.pos = self.playhead
                self.scrub.request(self.playhead)
        self.request_redraw()

    def nudge(self, dt):
        self.seek_to(self.playhead + dt)

    def nudge_frame(self, n):
        fps = self.src.fps if self.src else 30.0
        self.seek_to(self.playhead + n / fps)

    def on_volume(self, sender, app_data):
        self.cfg["volume"] = int(app_data)
        save_config(self.cfg)
        if self.player is not None:
            self.player.volume = int(app_data)

    def on_audio_offset(self, sender, app_data):
        self.cfg["audio_offset_ms"] = int(app_data)
        save_config(self.cfg)
        if self.player is not None:
            self.player.audio_offset = int(app_data) / 1000.0

    def on_force_stop_toggle(self, sender, app_data):
        self.cfg["force_stop_after_write"] = bool(app_data)
        save_config(self.cfg)

    # ======================================================================= #
    # 時間軸
    # ======================================================================= #

    # 由上而下的四條帶子（drawlist 內的 y 座標）
    @property
    def y_cells(self):
        return self.TL_RULER

    @property
    def y_band(self):
        return self.TL_RULER + self.TL_CELLS

    @property
    def y_foot(self):
        return self.TL_RULER + self.TL_CELLS + self.TL_BAND

    def t2x(self, t):
        span = max(self.view_b - self.view_a, 1e-6)
        return (t - self.view_a) / span * self.tl_w

    def x2t(self, x):
        span = max(self.view_b - self.view_a, 1e-6)
        return self.view_a + (x / max(self.tl_w, 1)) * span

    def zoom_all(self):
        self.view_a, self.view_b = 0.0, max(self.duration, 1.0)
        self.request_redraw()

    def zoom_to(self, a, b):
        a, b = max(0.0, a), min(max(self.duration, 1.0), b)
        if b - a < 0.5:
            b = a + 0.5
        self.view_a, self.view_b = a, b
        self.request_redraw()

    def zoom_seam(self):
        """跳到第一個接縫附近，看切點落在哪。"""
        snap, _ = self.snapped()
        if not snap:
            self.log("還沒有要移除的區間。")
            return
        a, b = snap[0]
        self.zoom_to(a - 12, b + 12)
        self.seek_to(max(a - 4, 0))

    def request_redraw(self):
        self._pending_redraw = True

    def resize_all(self):
        """視窗大小變了：滿版的線、欄位之間的直線、時間軸寬度、墨井。"""
        try:
            vw = dpg.get_viewport_client_width()
            body_h = dpg.get_item_rect_size("lib_pane")[1]
        except Exception:
            return
        for t in self._hrules:
            if dpg.does_item_exist(t) and \
                    dpg.get_item_configuration(t)["width"] != vw:
                dpg.configure_item(t, width=vw)
        for t in self._vrules:
            if body_h > 0 and dpg.does_item_exist(t) and \
                    dpg.get_item_configuration(t)["height"] != body_h:
                dpg.configure_item(t, height=int(body_h))
        self.resize_timeline()

    def resize_timeline(self):
        try:
            w = dpg.get_item_rect_size("edit_pane")[0] - 2 * px(19)
        except Exception:
            return
        w = max(int(w), px(320))
        if w != self.tl_w:
            self.tl_w = w
            dpg.configure_item("timeline", width=w)
            self.request_redraw()
            self._fill_cuts_table()       # 欄寬跟著編輯欄走
        self.fit_preview()

    def _set_desc(self, desc, foot):
        self._desc_full = desc
        self._foot_text = foot

    def _inner_w(self):
        try:
            return dpg.get_item_rect_size("edit_pane")[0] - 2 * px(19)
        except Exception:
            return 0

    def _tick_transport(self):
        """播放列放不下就拆成兩列：按鈕一列，原片／成品和時間碼一列。"""
        inner = self._inner_w()
        try:
            a = dpg.get_item_rect_size("tp_a")[0]
            seg = dpg.get_item_rect_size("tp_seg")[0]
            clk = dpg.get_item_rect_size("tp_clock")[0]
        except Exception:
            return
        if inner <= 0 or a <= 0 or clk <= 0:
            return
        narrow = a + seg + clk + 3 * px(14) + px(10) > inner
        if narrow != self._tp_narrow:
            self._tp_narrow = narrow
            dpg.move_item("tp_b", parent="tp_row2" if narrow else "tp_row1")
            dpg.configure_item("tp_row2", show=narrow)

    def _tick_desc(self):
        """時間軸標題旁的說明：放不下就截短，不要撞到右邊的按鈕。"""
        desc = getattr(self, "_desc_full", "")
        try:
            room = (self._inner_w() - dpg.get_item_rect_size("tl_h")[0]
                    - dpg.get_item_rect_size("tlh_right")[0] - 3 * px(12))
        except Exception:
            return
        if room <= 0:
            return
        text = desc if self.measure(desc, "small") <= room else \
            self.wrap(desc, "small", room, 1)[0]
        if dpg.get_value("tl_desc") != text:
            dpg.set_value("tl_desc", text)

    def redraw_timeline(self):
        """
        稿紙：上緣刻度、中間一格一個 fragment 的格子、下面一條標記帶、最下面說明。
        要移除的段落把格子染成淡硃砂，上面畫一條帶回鉤的硃砂橫線（刪除號），
        正中央寫「刪 52.0 s」。線永遠落在格線上 —— 切點只能在 fragment 邊界，
        不用另外解釋。
        """
        self._pending_redraw = False
        D = "timeline"
        if not dpg.does_item_exist(D):
            return
        dpg.delete_item(D, children_only=True)
        W, H = self.tl_w, self.TL_H
        yc, yb, yf = self.y_cells, self.y_band, self.y_foot
        dpg.draw_rectangle((0, 0), (W, H), fill=LOOK.PAPER, color=LOOK.PAPER,
                           parent=D)

        if self.vtrack is None:
            self._set_desc("滾輪縮放 · 拖空白處平移 · 拖刪除號的兩端調整範圍",
                           "切點只能落在格線上（fragment 邊界，約每 5 秒一格）")
            dpg.draw_rectangle((0, yc), (W, yb), fill=LOOK.PAPER_2,
                               color=LOOK.PAPER_2, parent=D)
            t = dpg.draw_text((px(14), yc + (self.TL_CELLS - px(16)) / 2),
                              "還沒載入影片", size=px(16), color=LOOK.INK_2,
                              parent=D)
            self.font(t, "small")
            self._draw_frame(W, H)
            return

        snap, rows = self.snapped()
        snap = snap or []

        # 格子的底色：留下的是紙的陰面，要刪的是淡硃砂
        for a, b in complement(snap, 0.0, self.duration):
            x0, x1 = self.t2x(a), self.t2x(b)
            if x1 < 0 or x0 > W:
                continue
            dpg.draw_rectangle((max(x0, 0), yc), (min(x1, W), yb),
                               fill=LOOK.PAPER_2, color=LOOK.PAPER_2, parent=D)
        for a, b in snap:
            x0, x1 = self.t2x(a), self.t2x(b)
            if x1 < 0 or x0 > W:
                continue
            dpg.draw_rectangle((max(x0, 0), yc), (max(min(x1, W), x0 + 1), yb),
                               fill=LOOK.VERMILION_PALE,
                               color=LOOK.VERMILION_PALE, parent=D)

        self._draw_cells(yc, yb)
        self._draw_ruler(0, yc)

        # 標記帶：使用者原本標的範圍（吸附前），和正在標的起訖
        for a, b, sa, sb in rows or []:
            if abs(sa - a) < 1e-6 and abs(sb - b) < 1e-6:
                continue
            x0, x1 = self.t2x(a), self.t2x(b)
            if x1 < 0 or x0 > W or x1 - x0 < px(16):
                continue
            y = yb + px(7)
            dpg.draw_line((x0, y), (x1, y), color=LOOK.INK_2, thickness=1,
                          parent=D)
            for x in (x0, x1):
                dpg.draw_line((crisp(x), y), (crisp(x), y + px(5)),
                              color=LOOK.INK_2, thickness=1, parent=D)
        if self.mark_in is not None and self.mark_out is not None:
            x0, x1 = sorted((self.t2x(self.mark_in), self.t2x(self.mark_out)))
            y = yb + px(17)
            dpg.draw_rectangle((x0, y), (x1, y + px(2)), fill=LOOK.INK,
                               color=LOOK.INK, parent=D)
        for t, lab in ((self.mark_in, "起"), (self.mark_out, "迄")):
            if t is None:
                continue
            x = self.t2x(t)
            if -10 <= x <= W + 10:
                dpg.draw_line((crisp(x), yc), (crisp(x), yf), color=LOOK.INK,
                              thickness=1, parent=D)
                tt = dpg.draw_text((x + px(4), yb + px(3)), lab, size=px(16),
                                   color=LOOK.INK, parent=D)
                self.font(tt, "strong_s")

        # 刪除號
        last_label_end = -1e9
        for i, (a, b) in enumerate(snap):
            x0, x1 = self.t2x(a), self.t2x(b)
            if x1 < 0 or x0 > W:
                continue
            self._draw_strike(i, x0, x1, b - a, yc)

        # 播放頭：黑色細線，頂端一個小方塊壓在刻度帶底部。先畫它，
        # 「刪 xx s」的紙色底才蓋得過去，字不會被線劃過
        x = self.t2x(self.playhead)
        dpg.draw_rectangle((x - px(1), yc), (x + px(1), yf), fill=LOOK.INK,
                           color=LOOK.INK, parent=D, tag="tl_playhead")
        dpg.draw_rectangle((x - px(6), yc - px(8)), (x + px(6), yc),
                           fill=LOOK.INK, color=LOOK.INK, parent=D,
                           tag="tl_playhead_head")

        for i, (a, b) in enumerate(snap):
            x0, x1 = self.t2x(a), self.t2x(b)
            if x1 < 0 or x0 > W:
                continue
            last_label_end = self._draw_strike_label(x0, x1, b - a, yc,
                                                     last_label_end)

        # 說明列（太長就截短，不要被框切掉一半）
        y = yf + (self.TL_FOOT - px(16)) / 2.0
        foot = self._foot_text
        if self.measure(foot, "small") > W - px(20):
            foot = self.wrap(foot, "small", W - px(20), 1)[0]
        t = dpg.draw_text((px(10), y), foot, size=px(16),
                          color=LOOK.INK_2, parent=D)
        self.font(t, "small")

        self._draw_frame(W, H)

    def _draw_frame(self, W, H):
        D = "timeline"
        for y in (self.y_cells, self.y_foot):
            dpg.draw_line((0, crisp(y)), (W, crisp(y)), color=LOOK.GRID,
                          thickness=1, parent=D)
        dpg.draw_rectangle((0.5, 0.5), (W - 0.5, H - 0.5), color=LOOK.GRID,
                           thickness=1, parent=D)

    def _draw_cells(self, y0, y1):
        """一格一個 fragment。太擠就每 k 個畫一條，並在說明裡寫清楚一格是幾個。"""
        D, W = "timeline", self.tl_w
        b = self.bounds
        lo = max(bisect_left(b, self.view_a) - 1, 0)
        hi = min(bisect_right(b, self.view_b) + 1, len(b))
        avg_s = (sum(self.vtrack.fragment_seconds())
                 / max(len(self.vtrack.frags), 1))
        per_px = avg_s / max(self.view_b - self.view_a, 1e-6) * W
        k = 1
        for k in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000):
            if per_px * k >= px(CELL_MIN):
                break
        self._grid_k = k
        snap_word = "外擴" if self.cfg["snap"] == "outer" else "內縮"
        if k == 1:
            self._set_desc("一格＝一個 fragment＝%.3f 秒 · 滾輪縮放 · 拖空白處平移"
                           % avg_s,
                           "切點只能落在格線上 · %s吸附 · 音訊精切 ±23 ms"
                           % snap_word)
        else:
            self._set_desc("一格＝%d 個 fragment（約 %.0f 秒）· 滾輪放大到一格一個"
                           % (k, avg_s * k),
                           "切點只能落在 fragment 邊界 · 放大才看得到每一格 · "
                           "%s吸附 · 音訊精切 ±23 ms" % snap_word)
        start = lo - lo % k
        for i in range(start, hi, k):
            x = self.t2x(b[i])
            if 0 < x < W:
                dpg.draw_line((crisp(x), y0 + 1), (crisp(x), y1), parent=D,
                              color=LOOK.GRID_SOFT, thickness=1)

    def _draw_ruler(self, y0, y1):
        D, W = "timeline", self.tl_w
        span = self.view_b - self.view_a
        steps = sorted(RULER_MINOR)
        fit = max(W / float(px(96)), 1.0)
        step = steps[-1]
        for s in steps:
            if span / s <= fit:
                step = s
                break
        minor = step / float(RULER_MINOR[step])
        t = int(self.view_a / minor) * minor
        n = 0
        while t <= self.view_b + 1e-9 and n < 2000:
            n += 1
            x = self.t2x(t)
            if 0 <= x <= W:
                major = abs(t / step - round(t / step)) < 1e-6
                # 刻度從格子的上緣往上長、數字在最上面：播放頭的小方塊壓在
                # 刻度帶底部，不會蓋到數字
                h = px(8) if major else px(4)
                dpg.draw_line((crisp(x), y1 - h), (crisp(x), y1),
                              color=LOOK.INK_2 if major
                              else LOOK.alpha(LOOK.INK_2, 120),
                              thickness=1, parent=D)
                if major:
                    text = fmt_ruler(t)
                    w = self.measure(text, "num_s")
                    lx = min(max(x - w / 2.0, px(4)), W - w - px(4))
                    tt = dpg.draw_text((lx, y0 + px(2)), text, size=px(16),
                                       color=LOOK.INK_2, parent=D)
                    self.font(tt, "num_s")
            t += minor

    def _draw_strike(self, i, x0, x1, secs, yc):
        D = "timeline"
        ym = yc + self.TL_CELLS * 0.5
        th = max(px(3), 2)
        x1 = max(x1, x0 + 2 * th)
        hook = self.TL_CELLS * 0.42
        if x1 - x0 < hook * 1.2:
            # 全片看的時候一段廣告只有幾個像素寬，橫線加回鉤會擠成一個 H；
            # 這時候直接塗成一塊硃砂，放大之後才畫成刪除號
            dpg.draw_rectangle((x0, ym - hook / 2.0), (x1, ym + hook / 2.0),
                               fill=LOOK.VERMILION, color=LOOK.VERMILION,
                               parent=D)
            return
        dpg.draw_rectangle((x0, ym - th / 2.0), (x1, ym + th / 2.0),
                           fill=LOOK.VERMILION, color=LOOK.VERMILION, parent=D)
        for side, x in ((0, x0), (1, x1 - th)):
            hot = self._hover == ("edge", i, side) or (
                self._drag is not None and self._drag.get("kind") == "edge"
                and self._drag.get("idx") == i and self._drag.get("side") == side)
            col = LOOK.INK if hot else LOOK.VERMILION
            w = th * 2 if hot else th
            xx = x - (w - th) / 2.0
            dpg.draw_rectangle((xx, ym - hook / 2.0), (xx + w, ym + hook / 2.0),
                               fill=col, color=col, parent=D)

    def _draw_strike_label(self, x0, x1, secs, yc, prev_end):
        """「刪 52.0 s」：放在刪除號正上方，紙色底。跟前一個重疊就不畫。"""
        D, W = "timeline", self.tl_w
        num = " %.1f s" % secs
        w1, w2 = self.measure("刪", "strong_s"), self.measure(num, "num_s")
        tw = w1 + w2
        pad = px(5)
        lx = min(max((x0 + x1) / 2.0 - tw / 2.0, pad + 1), W - tw - pad - 1)
        if lx - pad < prev_end + px(4):
            return prev_end
        ly = yc + px(2)
        dpg.draw_rectangle((lx - pad, ly), (lx + tw + pad, ly + px(18)),
                           fill=LOOK.PAPER, color=LOOK.PAPER, parent=D)
        t = dpg.draw_text((lx, ly + px(1)), "刪", size=px(16),
                          color=LOOK.VERMILION, parent=D)
        self.font(t, "strong_s")
        t = dpg.draw_text((lx + w1, ly + px(1)), num, size=px(16),
                          color=LOOK.VERMILION, parent=D)
        self.font(t, "num_s")
        return lx + tw + pad

    def update_playhead(self):
        if dpg.does_item_exist("tl_playhead"):
            x = self.t2x(self.playhead)
            yc, yf = self.y_cells, self.y_foot
            dpg.configure_item("tl_playhead", pmin=(x - px(1), yc),
                               pmax=(x + px(1), yf))
            dpg.configure_item("tl_playhead_head", pmin=(x - px(6), yc - px(8)),
                               pmax=(x + px(6), yc))

    def _tick_hover(self):
        """滑到刪除號的兩端時把回鉤加粗變黑：那裡可以拖。"""
        hover = None
        if self._drag is None and self.vtrack is not None:
            loc = self.tl_local()
            if loc is not None and self.y_cells <= loc[1] <= self.y_band:
                kind, idx, side = self.tl_hit(loc[0])
                if kind == "edge":
                    hover = ("edge", idx, side)
        if hover != self._hover:
            self._hover = hover
            self.request_redraw()

    # ---- 滑鼠 ----

    def tl_local(self):
        """滑鼠在時間軸上的本地座標，不在上面就回 None。"""
        if not dpg.does_item_exist("timeline"):
            return None
        mx, my = dpg.get_mouse_pos(local=False)
        x0, y0 = dpg.get_item_rect_min("timeline")
        w, h = dpg.get_item_rect_size("timeline")
        lx, ly = mx - x0, my - y0
        if -2 <= lx <= w + 2 and -2 <= ly <= h + 2:
            return lx, ly
        return None

    def tl_hit(self, lx):
        """(種類, 索引, 邊) —— 種類是 'edge' / 'body' / 'empty'。"""
        snap, _ = self.snapped()
        for i, (a, b) in enumerate(snap or []):
            x0, x1 = self.t2x(a), self.t2x(b)
            if abs(lx - x0) <= self.EDGE_GRAB:
                return "edge", i, 0
            if abs(lx - x1) <= self.EDGE_GRAB:
                return "edge", i, 1
            if x0 < lx < x1:
                return "body", i, None
        return "empty", None, None

    def on_mouse_down(self, sender, app_data):
        if app_data[0] != 0 or self.vtrack is None:
            return
        loc = self.tl_local()
        if loc is None:
            return
        lx, _ = loc
        kind, idx, side = self.tl_hit(lx)
        t = self.x2t(lx)
        if kind == "edge":
            # 吸附後的邊緣對應的是「使用者要求的那一段」，所以改的是 self.cuts
            self._drag = {"kind": "edge", "idx": idx, "side": side}
        elif kind == "body":
            self._drag = {"kind": "body", "idx": idx, "t0": t,
                          "orig": list(self.cuts[idx])}
        else:
            self._drag = {"kind": "pan", "t0": t, "va": self.view_a,
                          "vb": self.view_b, "moved": False, "x0": lx}

    def on_mouse_release(self, sender, app_data):
        drag = self._drag
        self._drag = None
        if drag is None:
            return
        if drag["kind"] == "pan" and not drag.get("moved"):
            self.seek_to(drag["t0"])          # 只是點一下 -> 跳到那裡
        elif drag["kind"] in ("edge", "body"):
            self.cuts.sort()
            self.refresh_cuts_ui()

    def tick_drag(self):
        if self._drag is None:
            return
        if not dpg.is_mouse_button_down(dpg.mvMouseButton_Left):
            self.on_mouse_release(None, [0])
            return
        loc = self.tl_local()
        if loc is None:
            return
        lx, _ = loc
        t = self.x2t(lx)
        d = self._drag
        if d["kind"] == "pan":
            if abs(lx - d["x0"]) > 3:
                d["moved"] = True
            if d["moved"]:
                span = d["vb"] - d["va"]
                shift = d["t0"] - self.x2t(lx)
                a = d["va"] + shift
                a = max(0.0, min(a, max(self.duration - span, 0.0)))
                self.view_a, self.view_b = a, a + span
                self.request_redraw()
            return
        if d["kind"] == "edge":
            cut = self.cuts[d["idx"]]
            t = max(0.0, min(t, self.duration))
            if d["side"] == 0:
                cut[0] = min(t, cut[1] - 0.05)
            else:
                cut[1] = max(t, cut[0] + 0.05)
        elif d["kind"] == "body":
            a0, b0 = d["orig"]
            shift = t - d["t0"]
            shift = max(-a0, min(shift, self.duration - b0))
            self.cuts[d["idx"]] = [a0 + shift, b0 + shift]
        self.request_redraw()

    def on_wheel(self, sender, app_data):
        loc = self.tl_local()
        if loc is None or self.vtrack is None:
            return
        lx, _ = loc
        anchor = self.x2t(lx)
        span = self.view_b - self.view_a
        factor = 0.8 if app_data > 0 else 1.25
        new = max(min(span * factor, max(self.duration, 1.0)), 0.5)
        frac = (anchor - self.view_a) / max(span, 1e-6)
        a = anchor - frac * new
        a = max(0.0, min(a, max(self.duration - new, 0.0)))
        self.view_a, self.view_b = a, a + new
        self.request_redraw()

    @staticmethod
    def _typing():
        """游標在輸入框裡的時候，快速鍵要讓位給打字。"""
        try:
            focused = dpg.get_focused_item()
            if not focused:
                return False
            kind = dpg.get_item_info(focused).get("type", "")
        except Exception:
            return False
        return "Input" in kind

    def on_key(self, sender, app_data):
        if self.vtrack is None or self._typing():
            return
        key = app_data
        if key == dpg.mvKey_Spacebar:
            self.toggle_play()
        elif key == getattr(dpg, "mvKey_I", -1):
            self.set_mark_in()
        elif key == getattr(dpg, "mvKey_O", -1):
            self.set_mark_out()
        elif key == dpg.mvKey_Left:
            self.nudge_frame(-1)
        elif key == dpg.mvKey_Right:
            self.nudge_frame(1)
        elif key == dpg.mvKey_Home:
            self.seek_to(0)

    # ======================================================================= #
    # 每一幀
    # ======================================================================= #

    def tick(self):
        self.pump()
        self._tick_later()
        self.resize_all()
        self._tick_transport()
        self._tick_desc()
        self._tick_inspector()
        self._tick_aligns()
        self.tick_drag()
        self._tick_hover()

        frame = None
        if self.player is not None and self.player.playing:
            pos, frame = self.player.tick()
            self.playhead = pos
            if not self.player.playing:
                dpg.set_item_label("btn_play", "播放")
        else:
            # 沒在播的時候，畫面來自拖時間軸抓的單張影格
            got = self.scrub.take()
            if got is not None:
                _, frame = got

        if self.player is not None:
            err = self.player.take_audio_error()
            if err:
                self.log("預覽沒有聲音 —— ffplay 一開就結束了：%s" % err)

        if frame is not None and self.tex_buf is not None:
            self._last_frame = frame
            np.multiply(frame, np.float32(1.0 / 255.0), out=self.tex_buf,
                        casting="unsafe")
            dpg.set_value("preview_tex", self.tex_buf)

        if self._pending_redraw:
            self.redraw_timeline()
        else:
            self.update_playhead()

        self._sync_play_label()
        self._tick_clock()

    def _sync_play_label(self):
        """
        按鈕上的字跟著「現在到底有沒有在播」走，不靠每個地方記得去改。
        以前只在按下去的那一刻改字：播著的時候換一支影片，舊的播放器被關掉了，
        按鈕卻還寫著「暫停」，新的那支要按兩下字才對得上。
        """
        want = "暫停" if (self.player is not None and self.player.playing) \
            else "播放"
        if dpg.does_item_exist("btn_play") and \
                dpg.get_item_label("btn_play") != want:
            dpg.set_item_label("btn_play", want)

    def _tick_clock(self):
        """大字是播放位置；成品模式下是剪完之後的時間，後面註明原片在哪。"""
        note = ""
        if self.vtrack is None:
            big, sub = "00:00:00", ""
        elif self.preview_mode == "result" and self.player is not None:
            out = self.player.source_to_output(self.playhead)
            big = fmt_clock(out)
            # 總長跟資訊欄的「新長」用同一種進位，不然會差一秒
            sub = ".%d / %s" % (int((out % 1) * 10),
                                fmt_dur(self.player.total_playable()))
            note = "成品 · 原片在 %s" % fmt_time(self.playhead)
        else:
            big = fmt_clock(self.playhead)
            sub = ".%d / %s" % (int((self.playhead % 1) * 10),
                                fmt_dur(self.duration))
        for tag, value in (("clock", big), ("clock_sub", sub),
                           ("clock_note", note)):
            if dpg.get_value(tag) != value:
                dpg.set_value(tag, value)

    # ======================================================================= #
    # 寫入
    # ======================================================================= #

    def confirm_write(self):
        if self.vtrack is None or not self.cuts:
            return
        snap, rows = self.snapped()
        if snap is None:
            self.log("吸附失敗：%s" % rows)
            return
        total = sum(b - a for a, b in snap)
        bdir = Path(self.cfg["backup_dir"])
        dest = BK.unique_path(bdir, self.item.backup_stem)
        lines = [
            "影片：%s" % self.item.display_name,
            "手機路徑：%s" % self.item.device_path,
            "",
            "要移除 %d 段，共 %.1f 秒（原片 %s → 成品 %s）："
            % (len(snap), total, fmt_dur(self.duration),
               fmt_dur(self.duration - total)),
        ]
        for i, (a, b) in enumerate(snap, 1):
            lines.append("    %d. %s - %s   （%.1f s）"
                         % (i, fmt_time(a, True), fmt_time(b, True), b - a))
        lines += [
            "",
            "寫入前會先備份成一個檔案：",
            "    %s" % dest,
            "",
            "注意：檔案大小會維持原樣（尾端補 free box），所以手機容量不會變少"
            " —— B站 不接受變小的檔案，會判定下載不完整而卡在「正在緩衝」。",
        ]
        dpg.set_value("confirm_text", "\n".join(lines))
        self.center_window("win_confirm")
        dpg.show_item("win_confirm")

    def do_write(self):
        dpg.hide_item("win_confirm")
        snap, why = self.snapped()
        if not snap:
            self.log("沒有可用的移除區間：%s" % (why or "還沒加"))
            return
        item, device, local = self.item, self.device, self.local
        q_name = self.q_dir.name
        vtrack = self.vtrack
        bdir = Path(self.cfg["backup_dir"])
        force_stop = self.cfg["force_stop_after_write"]

        def work():
            # 1) 備份（強制；備的是「動刀之前」的樣子，也就是手機上現在那一份）
            dest = BK.unique_path(bdir, item.backup_stem)
            self.log("備份成 %s" % dest)
            man = BK.create(local, dest, item=item,
                            extra={"cuts": [list(c) for c in snap]},
                            progress=self._mk_progress("備份 "))
            self.log("備份完成（%s，%d 個檔案）"
                     % (fmt_bytes(dest.stat().st_size), len(man["files"])))

            # 2) 在另一個目錄做手術，暫存那份原檔保持乾淨
            out = Path(local).parent / (Path(local).name + ".out")
            shutil.rmtree(out, ignore_errors=True)
            (out / q_name).mkdir(parents=True)
            for rel in ("entry.json", "%s/index.json" % q_name):
                shutil.copy2(Path(local) / rel, out / rel)
            for p in Path(local).glob("*.xml"):
                shutil.copy2(p, out / p.name)

            self.progress(0.0, "計算視訊")
            v_src = Path(local) / q_name / "video.m4s"
            a_src = Path(local) / q_name / "audio.m4s"
            at = TRIM.Track(a_src)

            vplan = TRIM.Plan(vtrack, "video")
            vplan.cut_aligned([(int(round(a * vtrack.timescale)),
                                int(round(b * vtrack.timescale)))
                               for a, b in snap])
            aplan = TRIM.Plan(at, "audio")
            aplan.cut_exact([(int(round(a * at.timescale)),
                              int(round(b * at.timescale)))
                             for a, b in snap])
            self.progress(0.3, "產生新的 video.m4s")
            v_new, v_info = vplan.render(os.path.getsize(v_src))
            self.progress(0.5, "產生新的 audio.m4s")
            a_new, a_info = aplan.render(os.path.getsize(a_src))

            drift = abs(v_info["removed_s"] - a_info["removed_s"])
            self.log("video：移除 %d 個 fragment，新時長 %s"
                     % (v_info["ndrop"], fmt_time(v_info["new_dur_s"])))
            self.log("audio：移除 %d 個、精修 %d 個，新時長 %s"
                     % (a_info["ndrop"], a_info["ntrim"],
                        fmt_time(a_info["new_dur_s"])))
            self.log("音畫差 %.3f s；尾端補回 video %s / audio %s bytes"
                     % (drift, TRIM.human(v_info["tail_pad"]),
                        TRIM.human(a_info["tail_pad"])))
            if drift > 0.05:
                self.log("警告：音畫差超過 50 ms，可能聽得出來不同步。")

            self.progress(0.6, "寫入本機")
            changed = TRIM.write_back(out, out / q_name, v_new, a_new,
                                      v_info, a_info, snap, log=self.log)

            # 3) 推回手機
            report = device.push_cache(out, item, rels=changed,
                                       progress=self._mk_progress("推送 "))
            bad = [r for r in report if not r[3]]
            for rel, mine, theirs, ok in report:
                self.log("  %-16s 本機 %s / 裝置 %s  %s"
                         % (rel, mine[:12], theirs[:12] or "(讀不到)",
                            "相符" if ok else "不符！"))
            if bad:
                raise DEV.AdbError(
                    "裝置上的檔案與本機不一致，傳輸可能中斷了。"
                    "請用「備份 → 還原備份」把原檔放回去再重試。")

            # 暫存的角色是「手機上現在那一份」，所以推完要跟著變成新版本。
            # 這樣下次編輯同一支不用重拉幾百 MB，而且標的時間跟手機上看到的
            # 是同一條時間軸 —— 要再剪一段廣告不必先還原原檔。
            self.progress(0.0, "更新本機暫存")
            for rel in changed:
                src = out / rel
                if src.is_file():
                    dst = Path(local) / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            mirror = {}
            for rel in item.remote_files():
                f = Path(local) / rel
                if f.is_file():
                    mirror[rel] = {"size": f.stat().st_size,
                                   "md5": TRIM.md5_file(f)}
            hist = DEV.stage_state(local).get("history") or []
            hist.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "cuts": [list(c) for c in snap],
                         "backup": str(dest)})
            DEV.write_stage_state(local, {"orig": mirror, "history": hist})
            shutil.rmtree(out, ignore_errors=True)

            if force_stop:
                try:
                    device.force_stop(item.root.package)
                    self.log("已關閉 %s，下次打開會重新讀緩存。"
                             % item.root.package)
                except DEV.AdbError as e:
                    self.log("關閉 App 失敗（不影響結果）：%s" % e)
            self.progress(1.0, "完成")
            return dest

        def done(dest):
            self.log("寫入完成。備份在 %s" % dest)
            self.log("去 B站 App 播一下確認。如果不滿意，用「備份 → 還原備份」"
                     "把原檔放回去。")
            self.progress(0.0, "")

        self.run_worker("寫入", work, done)

    # ======================================================================= #
    # 匯出接縫片段（檔案形式的對照）
    # ======================================================================= #

    def export_seams(self):
        if self.vtrack is None or not self.cuts:
            return
        if not MED.have_ffmpeg():
            self.log("這個功能需要 ffmpeg。")
            return
        local, q_name = self.local, self.q_dir.name
        snap, why = self.snapped()
        if not snap:
            self.log("沒有可用的移除區間：%s" % (why or "還沒加"))
            return
        vtrack = self.vtrack
        out_dir = Path(self.cfg["backup_dir"]) / "preview"
        tag = Path(local).name

        def work():
            v_src = Path(local) / q_name / "video.m4s"
            a_src = Path(local) / q_name / "audio.m4s"
            at = TRIM.Track(a_src)
            vplan = TRIM.Plan(vtrack, "video")
            vplan.cut_aligned([(int(round(a * vtrack.timescale)),
                                int(round(b * vtrack.timescale)))
                               for a, b in snap])
            aplan = TRIM.Plan(at, "audio")
            aplan.cut_exact([(int(round(a * at.timescale)),
                              int(round(b * at.timescale)))
                             for a, b in snap])
            self.progress(0.3, "產生對照用的檔案")
            v_new, _ = vplan.render(os.path.getsize(v_src))
            a_new, _ = aplan.render(os.path.getsize(a_src))
            self.progress(0.6, "擷取片段（會重編碼，稍等）")
            TRIM.make_previews(v_new, a_new, v_src, a_src, snap, 6.0,
                               out_dir, tag)
            self.progress(1.0, "完成")
            return out_dir

        def done(d):
            self.log("接縫片段在 %s" % d)
            self.progress(0.0, "")
            self.open_path(d)

        self.run_worker("匯出接縫", work, done)

    # ======================================================================= #
    # 備份位置 / 還原
    # ======================================================================= #

    def on_pick_backup_dir(self, sender, app_data):
        path = app_data.get("file_path_name") or app_data.get("current_path")
        if not path:
            return
        self.cfg["backup_dir"] = str(Path(path))
        save_config(self.cfg)
        self._sync_backup_label()
        self.refresh_inspector()
        self.log("備份位置改成 %s" % self.cfg["backup_dir"])

    def open_backup_dir(self):
        d = Path(self.cfg["backup_dir"])
        d.mkdir(parents=True, exist_ok=True)
        self.open_path(d)

    def open_path(self, p):
        try:
            if os.name == "nt":
                os.startfile(str(p))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except OSError as e:
            self.log("打不開 %s：%s" % (p, e))

    def show_restore_window(self):
        self.center_window("win_restore")
        dpg.show_item("win_restore")
        self.fill_restore_list()

    def fill_restore_list(self):
        bdir = Path(self.cfg["backup_dir"])
        dpg.set_value("restore_dir_label", "備份位置：%s" % bdir)
        dpg.delete_item("restore_list", children_only=True)
        dpg.set_value("restore_detail", "")
        self._restore_sel = None
        good, bad = BK.list_backups(bdir)
        self._restore_items = good
        if not good and not bad:
            dpg.add_text("這個資料夾裡沒有 %s 備份。可以用「瀏覽其他檔案…」"
                         "指定別的位置。" % BK.EXT, parent="restore_list",
                         wrap=px(880))
        for i, b in enumerate(good):
            with dpg.group(parent="restore_list"):
                dpg.add_selectable(label=b.display_name, user_data=i,
                                   callback=self.on_restore_pick,
                                   tag="rst_%d" % i)
                dpg.add_text("    %s · %s · %s · cid %s"
                             % (b.created_at, fmt_time(b.duration_ms / 1000.0),
                                fmt_bytes(b.total_bytes), b.cid),
                             color=LOOK.INK_2)
        for p, why in bad:
            dpg.add_text("讀不了：%s —— %s" % (p.name, why),
                         parent="restore_list", color=LOOK.WARN, wrap=px(880))

    def on_restore_pick(self, sender, app_data, user_data):
        for i in range(len(self._restore_items)):
            tag = "rst_%d" % i
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, i == user_data)
        self._restore_sel = self._restore_items[user_data]
        self.describe_restore(self._restore_sel)

    def on_pick_backup_file(self, sender, app_data):
        path = app_data.get("file_path_name")
        if not path:
            return
        try:
            bi = BK.info(path)
        except BK.BackupError as e:
            self.log(str(e))
            return
        self._restore_sel = bi
        self._restore_items = [bi]
        dpg.delete_item("restore_list", children_only=True)
        dpg.add_text("（指定的檔案）%s" % bi.path, parent="restore_list",
                     wrap=px(880))
        dpg.show_item("win_restore")
        self.describe_restore(bi)

    def describe_restore(self, bi):
        lines = ["%s" % bi.display_name,
                 "備份時間 %s · %s · %d 個檔案"
                 % (bi.created_at, fmt_bytes(bi.total_bytes), len(bi.files)),
                 "會放回手機：%s" % (bi.device_path or "(備份裡沒記路徑)")]
        if bi.m.get("cuts"):
            lines.append("這是在剪掉 %d 段之前備下來的原檔。"
                         % len(bi.m["cuts"]))
        if self.device is not None and bi.device_path:
            try:
                there = self.device.exists(bi.device_path)
            except DEV.AdbError:
                there = None
            if there is False:
                lines.append("注意：手機上已經沒有這個目錄了。單純把檔案放回去，"
                             "B站 的資料庫裡沒有這筆記錄，App 不會看到它 —— "
                             "要先在 App 裡重新下載這支影片。")
        dpg.set_value("restore_detail", "\n".join(lines))

    def verify_selected(self):
        bi = self._restore_sel
        if bi is None:
            self.log("先選一個備份。")
            return

        def work():
            info, report = BK.verify(bi.path, progress=self._mk_progress("核對 "))
            return report

        def done(report):
            for rel, ok, why in report:
                self.log("  %-16s %s" % (rel, why))
            self.log("核對完成：%s"
                     % ("全部相符" if all(r[1] for r in report) else "有問題！"))
            self.progress(0.0, "")

        self.run_worker("核對備份", work, done)

    def do_restore_selected(self):
        bi = self._restore_sel
        if bi is None:
            self.log("先選一個備份。")
            return
        if not bi.device_path:
            self.log("這個備份裡沒記手機路徑，沒辦法自動放回去。")
            return
        if self.device is None:
            self.log("沒有連上裝置。")
            return
        dpg.hide_item("win_restore")
        device = self.device
        stage = self.stage_dir

        def work():
            tmp = stage / ("restore_%s" % (bi.cid or "x"))
            shutil.rmtree(tmp, ignore_errors=True)
            self.log("解開並核對 %s" % bi.path.name)
            info, out = BK.extract(bi.path, tmp,
                                   progress=self._mk_progress("還原 "))
            self.log("備份內容核對通過，開始推回手機。")

            class _Target(object):
                device_path = bi.device_path
                quality_dir = bi.quality_dir
                cid = bi.cid

                def remote_files(self):
                    return [f["rel"] for f in bi.files]

            report = device.push_cache(out, _Target(),
                                       rels=[f["rel"] for f in bi.files],
                                       progress=self._mk_progress("推送 "))
            bad = [r for r in report if not r[3]]
            for rel, mine, theirs, ok in report:
                self.log("  %-16s %s" % (rel, "相符" if ok else "不符！"))
            if bad:
                raise DEV.AdbError("推回去的檔案與備份不一致，請重試。")

            # 手機上換回備份那一版了，本機暫存要跟著回到同一版，不然下次
            # 載入會因為對不上而整支重拉。直接把解出來的內容搬過去最省事。
            local = stage / bi.device_path.rsplit("/", 1)[-1]
            shutil.rmtree(local, ignore_errors=True)
            shutil.move(str(out), str(local))
            mirror = {}
            for f in bi.files:
                fp = local / f["rel"]
                if fp.is_file():
                    mirror[f["rel"]] = {"size": f["size"], "md5": f["md5"]}
            DEV.write_stage_state(local, {
                "orig": mirror, "history": [],
                "restored_from": str(bi.path),
                "restored_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            shutil.rmtree(tmp, ignore_errors=True)

            if self.cfg["force_stop_after_write"] and bi.package:
                try:
                    device.force_stop(bi.package)
                except DEV.AdbError:
                    pass
            self.progress(1.0, "完成")
            return bi

        def done(_):
            self.log("已還原到手機。")
            self.progress(0.0, "")

        self.run_worker("還原", work, done)

    # ======================================================================= #
    # 跑起來
    # ======================================================================= #

    # ======================================================================= #
    # 補字字型
    # ======================================================================= #

    def load_fonts(self, paths):
        """
        依字型組（BF.resolve 的結果）和目前的字級，載入每個字型角色。

        舊的字型不刪：drawlist 裡用 draw_text 畫出來的文字會記住它建立當下
        綁的那個字型，事後把那個字型刪掉，下一次 render 就會踩到已經釋放的
        記憶體（實測是直接 segfault）。字型留著不礙事 —— Dear PyGui 2.x 的
        字形是用到才載入，而且一次工作階段也換不了幾次字級。
        """
        body = paths.get("body")
        files = {
            "body": body,
            "strong": paths.get("strong") or body,
            "serif": paths.get("serif") or paths.get("strong") or body,
            # 影片標題什麼字都有：襯線體只在畫得出每個字時才用，見 refresh_inspector
            "title": paths.get("serif") or body,
            "num": paths.get("num") or body,
            "numr": paths.get("numr") or paths.get("num") or body,
        }
        need_hints = _dpg_major() < 2
        F = {}
        with dpg.font_registry() as reg:
            for role, (face, size_key) in LOOK.FONT_ROLES.items():
                path = files.get(face)
                if not path:
                    continue
                try:
                    with dpg.font(str(path), px(LOOK.SIZES[size_key])) as f:
                        if need_hints:
                            for hint in ("mvFontRangeHint_Chinese_Full",
                                         "mvFontRangeHint_Japanese",
                                         "mvFontRangeHint_Default"):
                                if hasattr(dpg, hint):
                                    dpg.add_font_range_hint(getattr(dpg, hint))
                    F[role] = f
                except Exception as e:
                    self.log("字型：%s 載入失敗（%s）" % (Path(path).name, e))
        self._font_regs.append(reg)
        for role in LOOK.FONT_ROLES:
            if role not in F and "body" in F:
                F[role] = F["body"]
        self.F = F
        self.font_files = files
        self._wcache = {}
        if "body" in F:
            dpg.bind_font(F["body"])

    def on_font_size(self, sender, app_data):
        global UI_SCALE
        pt = font_pt_of(app_data)
        if pt == self.cfg.get("font_pt"):
            return
        self.cfg["font_pt"] = pt
        save_config(self.cfg)
        UI_SCALE = pt / float(BASE_FONT_PT)
        self.rebuild_ui()
        self.log("介面字級改成「%s」（%d pt）。" % (app_data, pt))

    def rebuild_ui(self):
        """
        字級或字型變了要連版面一起重做（按鈕寬度、時間軸高度、間距都是建立時
        就定死的，不重建的話字變大就會被切掉）。狀態都在 self 上，重建完補回去。
        """
        was_playing = self.player is not None and self.player.playing
        if was_playing:
            self.player.pause()
        needle = (dpg.get_value("lib_filter") or "") \
            if dpg.does_item_exist("lib_filter") else ""

        for tag in ("root", "dlg_backup_dir", "dlg_backup_file", "win_restore",
                    "win_confirm", "win_help", "win_env", "win_log", "win_wifi",
                    "pop_dev",
                    "pop_backup", "pop_settings", "pop_help", "preview_tex",
                    "tex_reg"):
            if dpg.does_item_exist(tag):
                dpg.delete_item(tag)
        for t in self.T.values():
            if dpg.does_item_exist(t):
                dpg.delete_item(t)
        self._segs = {}
        self._later = []

        # 先換字型再建介面：drawlist 的文字會記住建立當下綁的字型
        self.load_fonts(self.font_paths)
        self.T = LOOK.make_themes(px)
        dpg.bind_theme(self.T["global"])
        self._compute_metrics()
        self._below_h = 0          # 版面換了，重新量一次墨井底下那疊的高度
        self._fit_key = None
        self.tl_w = px(1000)
        self.build()

        dpg.set_value("lib_filter", needle)
        self._sync_footer()
        for line in self.log_lines:
            self._emit_log(line)
        self._fill_cuts_table()

        if self.src is not None:
            self.set_preview_texture(self.src.out_w, self.src.out_h)
        if self.vtrack is not None:
            self.set_seg("mode",
                         "成品" if self.preview_mode == "result" else "原片")
            self.request_redraw()
            if was_playing:
                self.player.play(self.playhead)
                dpg.set_item_label("btn_play", "暫停")
            elif self.src is not None:
                self.scrub.request(self.playhead)
        self.sync_enabled()
        # 新字型要畫過一次才量得到字寬：清單和資訊欄晚兩格再排
        self.later(self.rebuild_library)
        self.later(self.refresh_cuts_ui)

    def start_font_upgrade(self):
        """
        介面的字型組（Noto 的固定字重、Bahnschrift 的等寬數字、補過字的內文）
        第一次要在背景做大約一分鐘。setup.py 平常已經先做好了，這裡是給
        沒跑 setup 的人的保險。做好就重建介面換上去。
        """
        pending = self.font_paths.get("pending") or []
        if not pending:
            return
        problem = BF.fonttools_problem()
        if problem is not None:
            self.log("提醒：介面字型和標題裡的 ♡ ❤ 韓文、表情需要 fonttools 才做得"
                     "出來，但它現在不能用：")
            self.log("      %s" % problem)
            self.log('      裝法（用這個直譯器，別直接打 pip）：'
                     '"%s" -m pip install fonttools' % sys.executable)
            self.log("      裝好後重開程式。診斷指令："
                     "python bili_gui.py --check-font")
            return

        def body():
            self.log("字型：第一次要準備介面字型（約一分鐘），這段時間先用系統字型。")
            res = BF.resolve(make=True, log=self.log)
            self.post(lambda: self.on_fonts_built(res))

        threading.Thread(target=body, name="font-build", daemon=True).start()

    def on_fonts_built(self, res):
        keys = ("body", "strong", "serif", "num", "numr")
        same = all(str(res.get(k)) == str(self.font_paths.get(k)) for k in keys)
        self.font_paths = res
        if same:
            return
        self.log("字型：介面字型準備好了，換上去。")
        self.rebuild_ui()

    def window_size(self):
        """
        記下來的視窗大小是實體像素，所以換了縮放比例（或接上不同 DPI 的螢幕）
        就要換算回來，不然在高 DPI 機器上會開出一個很小的窗。
        """
        saved = self.cfg.get("window")
        if not saved or len(saved) != 2 or min(saved) < 320:
            w, h = px(1500), px(950)
        else:
            w, h = int(saved[0]), int(saved[1])
            old = self.cfg.get("window_dpi")
            if old and abs(float(old) - SCALE) > 0.01:
                w = int(round(w * SCALE / float(old)))
                h = int(round(h * SCALE / float(old)))
        # 不要開得比螢幕還大
        try:
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            if sw > 0 and sh > 0:
                w, h = min(w, sw - px(40)), min(h, sh - px(80))
        except (AttributeError, OSError):
            pass
        return max(w, px(1000)), max(h, px(640))

    def run(self):
        global UI_SCALE
        UI_SCALE = self.cfg.get("font_pt", BASE_FONT_PT) / float(BASE_FONT_PT)
        dpg.create_context()
        # 一張 1x1 的白圖：所有細線都是它染色拉長的（重建介面時不刪）
        with dpg.texture_registry(tag="ui_tex"):
            dpg.add_static_texture(1, 1, [1.0, 1.0, 1.0, 1.0], tag="tex_white")
        self.font_paths = BF.resolve(make=False)
        self.load_fonts(self.font_paths)
        self.T = LOOK.make_themes(px)
        dpg.bind_theme(self.T["global"])
        self.build()
        self._fill_cuts_table()
        with dpg.handler_registry():
            dpg.add_mouse_down_handler(callback=self.on_mouse_down)
            dpg.add_mouse_release_handler(callback=self.on_mouse_release)
            dpg.add_mouse_wheel_handler(callback=self.on_wheel)
            dpg.add_key_press_handler(callback=self.on_key)

        w, h = self.window_size()
        # 見 WINDOW_TITLE 的註解：非 ASCII 的標題會讓第一次 render 直接 segfault，
        # 所以這裡再擋一次，免得將來有人改成中文又踩到。
        title = WINDOW_TITLE if WINDOW_TITLE.isascii() else "bili-trim"
        icon = str(ICON_PATH) if ICON_PATH.is_file() else ""
        dpg.create_viewport(title=title, width=int(w), height=int(h),
                            min_width=px(1180), min_height=px(700),
                            small_icon=icon, large_icon=icon)
        dpg.set_viewport_clear_color(LOOK.PAPER)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        if not self.font_files.get("body"):
            self.log("找不到中文字型，介面上的中文會顯示不出來。")
        else:
            self.log("字型：%s（%d pt）" % (Path(self.font_files["body"]).name,
                                          px(BASE_FONT_PT)))
        self.start_font_upgrade()
        self.later(self.refresh_inspector)
        self.later(self.rebuild_library)
        self.startup()

        try:
            while dpg.is_dearpygui_running():
                try:
                    self.tick()
                except Exception:
                    self.log("主迴圈出錯：\n%s" % traceback.format_exc())
                dpg.render_dearpygui_frame()
                # 沒在播也沒在拖的時候不必全速空轉，省點 CPU
                if self._drag is None and not (
                        self.player is not None and self.player.playing):
                    time.sleep(0.008)
        finally:
            try:
                self.cfg["window"] = [dpg.get_viewport_client_width(),
                                      dpg.get_viewport_client_height()]
                self.cfg["window_dpi"] = SCALE
            except Exception:
                pass
            save_config(self.cfg)
            if self.player is not None:
                self.player.shutdown()
            self.scrub.shutdown()
            dpg.destroy_context()


HELP_TEXT = """這個工具直接在 B站 的緩存檔上動刀，所以產物還是 B站 App 認得的檔案，
剪完照樣在 App 裡播、彈幕在、進度記憶也在。代價是幾個繞不過去的限制：

1. 切點只能落在 fragment 邊界（大約每 5 秒一個）
   視訊的每個 fragment 開頭才是關鍵影格，只能整塊移除，不然後面的畫面會解不開。
   時間軸下面那排細刻度就是邊界，放大就看得到你的切點會被吸附到哪裡。
   「外擴」會寧可多切掉一點正片，「內縮」會寧可殘留一點廣告。
   音訊可以精確切（約 23 毫秒），所以音畫不會飄。

2. 手機容量不會變少
   實測 B站 App 只要發現檔案比它資料庫裡記的小，就判定下載不完整、
   跑去向 CDN 續傳 —— 而那些網址早就過期了，結果就是永遠卡在「正在緩衝」。
   所以剪掉多少位元組，就在檔尾補一個同樣大的 free box 回去。
   廣告是真的被移除了（時間軸連續接起來），但檔案大小一模一樣。

3. 不能用 ffmpeg 重新封裝
   試過了，即使完全不剪，只是讓 ffmpeg 重封一次容器，App 就不認。
   所以這個工具完全不靠 ffmpeg 改檔案，只拿它來做預覽畫面。

4. 剪過的還是可以再剪
   剪完時間軸就接起來了，原本 10:00 的畫面現在在 8:00。所以再剪的時候，
   你標的時間是以「手機上目前這一版」為準 —— 在這個介面裡拖時間軸挑不會
   搞錯，因為畫面上播的就是目前那一版。
   唯一要小心的是命令列：別把上次用過的切點時間再貼一次，那些位置現在
   已經是正片了。

5. 備份是強制的，但它是後悔藥，不是編輯的前提
   寫入前一定會先把動刀前的樣子收成一個 .bilibak 檔（檔名就是影片標題）。
   不滿意就用「備份 → 還原備份」放回去。
   備份不見了也還是可以繼續剪，只是回不去而已 —— 不需要重新下載。
   注意：如果你在 App 裡把這支影片刪掉了，光把檔案放回去是沒用的 ——
   B站 的資料庫裡沒有那筆記錄，要先在 App 裡重新下載。
"""


def font_report():
    """
    `python bili_gui.py --check-font`
    印出字型實際的狀況：用的是哪個 python、補字字型建好了沒、
    手機上每一筆標題還有沒有字形缺。缺字的時候用這個看最快。
    """
    import site
    print("python   : %s" % sys.executable)
    print("版本     : %s" % sys.version.split()[0])
    print("工作目錄 : %s" % os.getcwd())
    problem = BF.fonttools_problem()
    print("fonttools: %s" % ("有" if problem is None else "不能用 —— %s" % problem))
    if problem is not None:
        print("  使用者套件目錄啟用：%s" % site.ENABLE_USER_SITE)
        try:
            print("  使用者套件目錄    ：%s" % site.getusersitepackages())
        except Exception as e:
            print("  使用者套件目錄    ：讀不到（%s）" % e)
        for k in ("PYTHONNOUSERSITE", "PYTHONPATH", "PYTHONHOME"):
            if os.environ.get(k):
                print("  環境變數 %s = %s" % (k, os.environ[k]))
        print("  sys.path：")
        for p in sys.path:
            print("      %s" % (p or "(空字串 = 目前目錄)"))
        print("  用這個直譯器安裝（別直接打 pip，那可能是另一個 Python）：")
        print('      "%s" -m pip install fonttools' % sys.executable)

    res = BF.resolve(make=False)
    for role, label in (("body", "內文"), ("strong", "粗體"), ("serif", "標題"),
                        ("num", "數字")):
        print("%-8s : %s" % (label, res[role] or "（沒有，用內文代替）"))
    if res["pending"]:
        print("還沒做好 : %s —— 下次開 GUI 會在背景做（約一分鐘），"
              "或現在跑 python bili_font.py" % "、".join(res["pending"]))
    base = BF.face_ready("body") or BF.base_font()
    print("補字的底 : %s" % (base or "找不到任何中文字型！"))
    if base is None:
        return 1

    out, chosen = BF.plan(base)
    print("補字來源 : %s"
          % ("、".join(Path(p).name for p, _ in chosen) or "（找不到）"))
    print("快取位置 : %s" % (out or "（無）"))
    ready = BF.cached(base)
    if ready:
        print("合併字型 : 已建好，%.1f MB" % (ready.stat().st_size / 1e6))
    else:
        print("合併字型 : 還沒建 —— 下次啟動會在背景建，約半分鐘。")
        print("           （還沒建好之前，♡ ❤ 韓文、表情會顯示成「?」）")
        if out is not None:
            try:
                st = os.stat(str(out))
                print("           注意：那個路徑其實有檔案（%d bytes），"
                      "但程式判定不可用 —— 這不正常，請回報。" % st.st_size)
            except FileNotFoundError:
                print("           （確認過：那個路徑目前沒有檔案）")
            except OSError as e:
                print("           讀那個路徑出錯：%s" % e)
        if problem is None:
            print("\n現在就建（會顯示進度，約半分鐘）：")
            print("      python bili_font.py")
            print("   或直接開 GUI，它會在背景自己建好再換上去。")

    font_for_check = ready or base
    cover = None
    if BF.have_fonttools():
        try:
            from fontTools.ttLib import TTFont
            f = TTFont(str(font_for_check),
                       fontNumber=0 if str(font_for_check).lower()
                       .endswith(".ttc") else -1, lazy=True)
            cover = set()
            for tb in f["cmap"].tables:
                cover |= set(tb.cmap.keys())
            print("涵蓋字數 : %d" % len(cover))
        except Exception as e:
            print("涵蓋字數 : 讀不出來（%s）" % e)

    if cover is None:
        return 0
    print("\n檢查手機上的標題…")
    try:
        devs = DEV.list_devices()
        if not devs:
            print("  （adb 看不到裝置，跳過）")
            return 0
        dev = devs[0]
        items = dev.list_caches(dev.find_roots())
    except DEV.AdbError as e:
        print("  （%s）" % e)
        return 0

    missing = {}
    for it in items:
        for text in (it.display_name, it.owner, it.quality_desc):
            for ch in text or "":
                if ord(ch) not in cover:
                    missing.setdefault(ch, []).append(it.display_name[:30])
    if not missing:
        print("  %d 支影片的標題／作者，每個字元都有字形 —— 不會出現「?」"
              % len(items))
        return 0
    print("  還有 %d 個字元沒有字形：" % len(missing))
    for ch, where in sorted(missing.items()):
        print("    U+%04X %r  出現在：%s" % (ord(ch), ch, where[0]))
    print("\n  把上面這段貼回來，就知道要從哪個字型補。")
    return 1


def main():
    global SCALE
    if "--check-font" in sys.argv:
        sys.exit(font_report())
    # 一定要在建立任何視窗之前，不然 Windows 會用點陣放大整張畫面（全糊）
    SCALE = enable_dpi_awareness()
    set_taskbar_identity()
    App().run()


if __name__ == "__main__":
    main()
