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


def pick_font(size=17):
    """回傳 (font_tag, 路徑)；找不到中文字型就回 (None, None)。"""
    # Dear PyGui 2.x 會按需載入字符，range hint 已經是 no-op（而且它的棄用警告
    # 自己動了 warnings 的 filter，壓不掉），所以只有 1.x 才需要給。
    need_hints = _dpg_major() < 2
    for path in FONT_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            with dpg.font_registry():
                with dpg.font(path, size) as f:
                    if need_hints:
                        for hint in ("mvFontRangeHint_Chinese_Full",
                                     "mvFontRangeHint_Japanese",
                                     "mvFontRangeHint_Default"):
                            if hasattr(dpg, hint):
                                dpg.add_font_range_hint(getattr(dpg, hint))
            return f, path
        except Exception:
            continue
    return None, None


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


# 顏色
C_KEEP = (62, 134, 96, 255)
C_KEEP_HL = (84, 178, 126, 255)
C_CUT = (176, 58, 58, 255)
C_CUT_EDGE = (235, 110, 110, 255)
C_ASK = (226, 196, 86, 255)
C_BAR_BG = (46, 48, 54, 255)
C_FRAG = (104, 112, 128, 255)
C_RULER = (150, 156, 168, 255)
C_PLAY = (245, 245, 245, 255)
C_MARK = (90, 196, 220, 255)


# --------------------------------------------------------------------------- #
# 主程式
# --------------------------------------------------------------------------- #

class App(object):

    def _compute_metrics(self):
        """
        時間軸的幾何（實體像素）。DPI 與字級都會影響，所以換字級重建介面時
        要重算一次 —— 不重算的話時間軸會維持舊尺寸，字變大就擠在一起。
        """
        self.TL_H_RULER = px(18)
        self.TL_H_BAR = px(46)
        self.TL_H_FRAG = px(17)   # 要放得下「放大才看得到切點邊界」那行字
        self.TL_PAD = px(3)
        self.EDGE_GRAB = px(7)    # 抓邊緣的像素容忍度

    def __init__(self):
        self._compute_metrics()
        self.cfg = load_config()
        self.log_lines = []
        self.base_font_path = None
        self.merged_font_path = None
        self._font_regs = []
        self._adb_label = "檢查中…"
        self._adb_color = (170, 176, 188, 255)
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

    @staticmethod
    def _emit_log(stamped):
        if not dpg.does_item_exist("log_area"):
            return
        for i, line in enumerate(stamped.splitlines() or [""]):
            dpg.add_text(line if i == 0 else ("         " + line),
                         parent="log_area", wrap=0)
        dpg.set_y_scroll("log_area", -1.0)

    def progress(self, frac, text=""):
        def do():
            dpg.set_value("prog", max(0.0, min(1.0, frac)))
            dpg.configure_item("prog", overlay=text)
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

    # ======================================================================= #
    # 介面
    # ======================================================================= #

    def build(self):
        dpg.add_texture_registry(tag="tex_reg")
        with dpg.window(tag="root"):
            self._build_menu()
            self._build_toolbar()
            with dpg.group(horizontal=True):
                self._build_library()
                self._build_editor()
            self._build_bottom()
        self._build_dialogs()
        dpg.set_primary_window("root", True)

    def _build_menu(self):
        with dpg.menu_bar():
            with dpg.menu(label="緩存"):
                dpg.add_menu_item(label="重新掃描裝置與緩存",
                                  callback=lambda: self.refresh_all())
                dpg.add_menu_item(label="重新從手機拉取這支（放棄本機暫存）",
                                  callback=lambda: self.on_load_clicked(True))
            with dpg.menu(label="備份"):
                dpg.add_menu_item(label="還原備份…",
                                  callback=self.show_restore_window)
                dpg.add_menu_item(label="變更備份位置…",
                                  callback=lambda: dpg.show_item("dlg_backup_dir"))
                dpg.add_menu_item(label="開啟備份資料夾",
                                  callback=self.open_backup_dir)
            with dpg.menu(label="設定"):
                dpg.add_slider_int(label="音量", default_value=self.cfg["volume"],
                                   min_value=0, max_value=100, width=px(160),
                                   callback=self.on_volume)
                dpg.add_slider_int(label="音訊延遲 (ms)", width=px(160),
                                   default_value=self.cfg["audio_offset_ms"],
                                   min_value=-800, max_value=800,
                                   callback=self.on_audio_offset)
                dpg.add_checkbox(label="寫入後自動關閉 B站 App",
                                 default_value=self.cfg["force_stop_after_write"],
                                 callback=self.on_force_stop_toggle)
                dpg.add_combo(("原生解析度", "最高 1080p", "最高 720p",
                               "最高 480p"), label="預覽畫質", width=px(150),
                              default_value=self.cfg["preview_quality"],
                              callback=self.on_preview_quality)
                dpg.add_combo([n for n, _ in FONT_SIZES], label="介面字級",
                              width=px(150), tag="font_combo",
                              default_value=font_label_of(self.cfg["font_pt"]),
                              callback=self.on_font_size)
            with dpg.menu(label="說明"):
                dpg.add_menu_item(label="這個工具的限制",
                                  callback=lambda: dpg.show_item("win_help"))
                dpg.add_menu_item(label="環境檢查",
                                  callback=self.show_env_window)

    def _build_toolbar(self):
        with dpg.group(horizontal=True):
            dpg.add_text("adb:")
            dpg.add_text("檢查中…", tag="adb_state")
            dpg.add_spacer(width=px(12))
            dpg.add_text("裝置:")
            dpg.add_combo([], tag="device_combo", width=px(240),
                          callback=self.on_device_pick)
            dpg.add_button(label="重新掃描", callback=lambda: self.refresh_all())
            dpg.add_spacer(width=px(12))
            dpg.add_text("備份位置:")
            dpg.add_text("", tag="backup_dir_label")
            dpg.add_button(label="變更",
                           callback=lambda: dpg.show_item("dlg_backup_dir"))

    def _build_library(self):
        with dpg.child_window(width=px(430), height=px(-128), tag="lib_pane"):
            dpg.add_text("手機上的緩存")
            dpg.add_input_text(tag="lib_filter", width=-1, hint="搜尋標題…",
                               callback=self.rebuild_library)
            with dpg.child_window(height=px(-150), tag="lib_list"):
                dpg.add_text("先連好手機，按「重新掃描」。", tag="lib_empty")
            dpg.add_separator()
            dpg.add_text("", tag="sel_title", wrap=px(400))
            dpg.add_text("", tag="sel_meta", wrap=px(400))
            dpg.add_text("", tag="sel_state", wrap=px(400))
            dpg.add_button(label="載入以編輯", tag="btn_load", width=-1,
                           callback=lambda: self.on_load_clicked(False))

    def _build_editor(self):
        with dpg.child_window(height=px(-128), tag="edit_pane"):
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=px(4))
                # 影格貼圖要等載入影片、知道原生解析度之後才建得出來
                with dpg.group(tag="preview_holder"):
                    dpg.add_text("載入影片後這裡會顯示畫面",
                                 color=(120, 126, 138, 255))

            # ---- 播放控制 ----
            with dpg.group(horizontal=True):
                dpg.add_button(label="播放", tag="btn_play", width=px(70),
                               callback=self.toggle_play)
                dpg.add_button(label="|<", width=px(36),
                               callback=lambda: self.seek_to(0))
                dpg.add_button(label="-10s", width=px(52),
                               callback=lambda: self.nudge(-10))
                dpg.add_button(label="-1s", width=px(46),
                               callback=lambda: self.nudge(-1))
                dpg.add_button(label="-1格", width=px(54),
                               callback=lambda: self.nudge_frame(-1))
                dpg.add_button(label="+1格", width=px(54),
                               callback=lambda: self.nudge_frame(1))
                dpg.add_button(label="+1s", width=px(46),
                               callback=lambda: self.nudge(1))
                dpg.add_button(label="+10s", width=px(52),
                               callback=lambda: self.nudge(10))
                dpg.add_spacer(width=px(10))
                dpg.add_radio_button(("原片", "成品"), tag="mode_radio",
                                     horizontal=True, callback=self.on_mode)
                dpg.add_spacer(width=px(10))
                dpg.add_text("", tag="pos_label")

            # ---- 時間軸 ----
            dpg.add_spacer(height=px(2))
            with dpg.group(horizontal=True):
                dpg.add_text("時間軸")
                dpg.add_text("（滾輪縮放、拖空白處平移、拖紅塊邊緣調整範圍）",
                             color=(140, 146, 158, 255))
                dpg.add_spacer(width=px(8))
                dpg.add_button(label="全片", width=px(60), callback=self.zoom_all)
                dpg.add_button(label="看接縫", width=px(76),
                               callback=self.zoom_seam)
            dpg.add_drawlist(width=self.tl_w,
                             height=(self.TL_H_RULER + self.TL_H_BAR
                                     + self.TL_H_FRAG + self.TL_PAD * 3),
                             tag="timeline")

            # ---- 標記 / 區間 ----
            dpg.add_spacer(height=px(4))
            with dpg.group(horizontal=True):
                dpg.add_button(label="設為起點 (I)", width=px(118),
                               callback=self.set_mark_in)
                dpg.add_button(label="設為終點 (O)", width=px(118),
                               callback=self.set_mark_out)
                dpg.add_text("", tag="mark_label")
                dpg.add_spacer(width=px(8))
                dpg.add_button(label="加入移除區間", tag="btn_add_cut",
                               width=px(140), callback=self.add_cut_from_marks)
                dpg.add_button(label="清除標記", width=px(96),
                               callback=self.clear_marks)
                dpg.add_spacer(width=px(10))
                dpg.add_text("吸附:")
                dpg.add_radio_button(("外擴", "內縮"), tag="snap_radio",
                                     horizontal=True,
                                     default_value=("外擴"
                                                    if self.cfg["snap"] == "outer"
                                                    else "內縮"),
                                     callback=self.on_snap)
                with dpg.tooltip("snap_radio"):
                    dpg.add_text("切點只能落在 fragment 邊界（約每 5 秒一個）。\n"
                                 "外擴：往外吸，寧可多切掉一點正片。\n"
                                 "內縮：往內吸，寧可殘留一點廣告。")
            with dpg.child_window(height=px(108), tag="cuts_pane"):
                dpg.add_text("還沒有。用上面的「設為起點 / 終點」加一段。",
                             tag="cuts_empty")

            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="寫入手機", tag="btn_write", width=px(150),
                               height=px(34), callback=self.confirm_write)
                dpg.add_button(label="匯出接縫片段", tag="btn_seams",
                               width=px(160), height=px(34),
                               callback=self.export_seams)
                dpg.add_spacer(width=px(14))
                dpg.add_text("", tag="plan_label", wrap=px(640))

    def _build_bottom(self):
        dpg.add_progress_bar(tag="prog", default_value=0.0, width=-1,
                             overlay="")
        with dpg.child_window(height=-1, tag="log_area"):
            pass

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
        """（主執行緒）重建影格貼圖與畫面上的 image 元件。"""
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
                      width=self.show_w, height=self.show_h)
        # 換字級會重建貼圖，把最後那張影格補回去，免得預覽空一塊
        if (self._last_frame is not None
                and self._last_frame.size == self.tex_buf.size):
            np.multiply(self._last_frame, np.float32(1.0 / 255.0),
                        out=self.tex_buf, casting="unsafe")
            dpg.set_value("preview_tex", self.tex_buf)
        self.fit_preview()

    def fit_preview(self):
        """
        顯示尺寸：塞得進編輯區就用原生大小（1:1 最清楚），塞不進才等比縮小
        —— 縮小是 GPU 取樣，比先把貼圖做小再放大好得多。
        """
        if not dpg.does_item_exist("preview_img") or not self.tex_h:
            return
        try:
            pane_w, pane_h = dpg.get_item_rect_size("edit_pane")
            avail_w = pane_w - px(24)
            # 預覽底下那一整疊（播放列到寫入按鈕）有多高 —— 量出來而不是用猜的，
            # 不然換字型、換縮放、區間列數變了就會把按鈕擠出畫面
            below = (dpg.get_item_rect_max("btn_write")[1]
                     - dpg.get_item_rect_min("btn_play")[1])
        except Exception:
            return
        if avail_w < px(200) or pane_h <= 0:
            return
        if not (px(260) <= below <= px(760)):
            below = self._below_h or px(460)
        self._below_h = below
        avail_h = max(pane_h - below - px(28), px(160))
        k = min(1.0, avail_w / float(self.tex_w), avail_h / float(self.tex_h))
        w, h = max(int(self.tex_w * k), 16), max(int(self.tex_h * k), 16)
        if (w, h) != (self.show_w, self.show_h):
            self.show_w, self.show_h = w, h
            dpg.configure_item("preview_img", width=w, height=h)

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
            dpg.add_file_extension(BK.EXT, color=(140, 220, 160, 255))
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
                dpg.add_button(label="還原到手機", tag="btn_do_restore",
                               width=px(150), callback=self.do_restore_selected)
                dpg.add_button(label="只核對備份完整性", width=px(180),
                               callback=self.verify_selected)
                dpg.add_button(label="關閉", width=px(96),
                               callback=lambda: dpg.hide_item("win_restore"))

        with dpg.window(label="確認寫入", tag="win_confirm", show=False,
                        modal=True, width=px(720), height=px(420)):
            dpg.add_text("", tag="confirm_text", wrap=px(690))
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="備份並寫入", width=px(160),
                               callback=self.do_write)
                dpg.add_button(label="取消", width=px(96),
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
                dpg.add_button(label="把結果寫進訊息區", width=px(190),
                               callback=self.log_env_check)
                dpg.add_button(label="關閉", width=px(96),
                               callback=lambda: dpg.hide_item("win_env"))

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
                ("mode_radio", loaded),
                ("device_combo", not self.busy),
        ):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=bool(on))

    # ======================================================================= #
    # 裝置與清單
    # ======================================================================= #

    def startup(self):
        dpg.set_value("backup_dir_label", self.cfg["backup_dir"])
        ok, msg = DEV.check_adb()
        self._adb_label = "OK" if ok else "找不到"
        self._adb_color = (120, 220, 140, 255) if ok else (240, 120, 120, 255)
        dpg.set_value("adb_state", self._adb_label)
        dpg.configure_item("adb_state", color=self._adb_color)
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

    STATE_COLOR = {"ok": (120, 220, 140, 255),
                   "warn": (235, 200, 110, 255),
                   "bad": (240, 120, 120, 255)}

    def show_env_window(self):
        self.fill_env_window()
        # DPG 的 modal 預設貼在左上角，開在畫面中間比較像一回事
        try:
            cfg = dpg.get_item_configuration("win_env")
            vw = dpg.get_viewport_client_width()
            vh = dpg.get_viewport_client_height()
            dpg.configure_item("win_env",
                               pos=[max((vw - cfg["width"]) // 2, 0),
                                    max((vh - cfg["height"]) // 2, 0)])
        except Exception:
            pass
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
        rows = self.env_rows()
        for item, state, note in rows:
            with dpg.group(horizontal=True, parent="env_list"):
                dpg.add_text(ENV.MARK.get(state, "     ").strip(),
                             color=self.STATE_COLOR.get(state))
                dpg.add_text("%-10s" % item)
                dpg.add_text(note, wrap=px(620))

        missing = [n for n in ("adb", "ffmpeg", "ffplay", "ffprobe")
                   if not ENV.have(n)]
        if not missing:
            dpg.add_separator(parent="env_list")
            dpg.add_text("兩個外部程式都在，接上手機按「重新掃描」就可以開始。",
                         parent="env_list", color=self.STATE_COLOR["ok"],
                         wrap=px(860))
            return

        # 同一包的只講一次（ffmpeg / ffplay / ffprobe 是一起下載的）
        shown = set()
        for name in missing:
            label, url, howto = ENV.DOWNLOADS[name]
            if label in shown:
                continue
            shown.add(label)
            dpg.add_separator(parent="env_list")
            with dpg.group(parent="env_list"):
                dpg.add_text("缺 %s" % label, color=self.STATE_COLOR["bad"])
                same = [n for n in missing if ENV.DOWNLOADS[n][0] == label]
                dpg.add_text("少了：%s" % "、".join(same), wrap=px(860))
                for n in same:
                    dpg.add_text("    %s —— %s" % (n, ENV.TOOLS[n][2]),
                                 wrap=px(840))
                dpg.add_text("下載：%s" % url, wrap=px(860))
                dpg.add_text("放法：%s" % howto, wrap=px(860))
                with dpg.group(horizontal=True):
                    dpg.add_button(label="開啟下載頁", width=px(140),
                                   callback=lambda s_=None, a_=None, u=url:
                                   self.open_url(u))
                    dpg.add_button(label="複製網址", width=px(120),
                                   callback=lambda s_=None, a_=None, u=url:
                                   self.copy_text(u))

    def open_tools_dir(self):
        d = ENV.TOOLS_DIR
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.log("建不出 %s：%s" % (d, e))
            return
        self.open_path(d)

    def open_url(self, url):
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception as e:
            self.log("打不開 %s：%s" % (url, e))
            self.copy_text(url)

    def copy_text(self, text):
        try:
            dpg.set_clipboard_text(text)
            self.log("已複製：%s" % text)
        except Exception:
            # 舊版 Dear PyGui 沒有剪貼簿 API，至少把字留在訊息區讓人自己複製
            self.log("複製不了，網址在這裡：%s" % text)

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

    def refresh_all(self):
        def work():
            devs = DEV.list_devices()
            if not devs:
                raise DEV.AdbError(
                    "adb 看不到裝置。確認手機已用 USB 連接、開啟「USB 偵錯」，"
                    "並在手機上允許這台電腦。")
            dev = devs[0]
            self.log("裝置：%s" % dev.label)
            self.progress(0.1, "找 B站 的緩存目錄")
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
            devs, dev, roots, items = res
            self.devices, self.device, self.roots, self.items = \
                devs, dev, roots, items
            dpg.configure_item("device_combo",
                               items=[d.label for d in devs],
                               default_value=dev.label)
            self.log("共 %d 支緩存影片，其中 %d 支可以處理。"
                     % (len(items),
                        sum(1 for i in items if i.unsupported_reason() is None)))
            self.rebuild_library()
            self.progress(0.0, "")

        self.run_worker("掃描", work, done)

    def on_device_pick(self, sender, app_data):
        for d in self.devices:
            if d.label == app_data:
                self.device = d
                self.log("改用裝置 %s" % d.label)
                self.refresh_all()
                return

    def rebuild_library(self):
        needle = (dpg.get_value("lib_filter") or "").strip().lower()
        self.filtered = [it for it in self.items
                         if not needle or needle in it.display_name.lower()
                         or needle in (it.owner or "").lower()
                         or needle in str(it.cid)]
        dpg.delete_item("lib_list", children_only=True)
        if not self.filtered:
            dpg.add_text("沒有符合的影片。" if self.items
                         else "這台手機上沒有找到緩存影片。", parent="lib_list")
            return
        for n, it in enumerate(self.filtered):
            bad = it.unsupported_reason()
            with dpg.group(parent="lib_list"):
                dpg.add_selectable(label=it.display_name, user_data=n,
                                   callback=self.on_item_pick,
                                   tag="sel_%d" % n)
                meta = "    %s · %s · %s" % (
                    it.quality_desc or ("q" + it.quality_dir),
                    fmt_time(it.duration_s), fmt_bytes(it.total_bytes))
                dpg.add_text(meta, color=(146, 152, 164, 255))
                if bad:
                    dpg.add_text("    ⚠ " + bad, color=(226, 170, 90, 255),
                                 wrap=px(400))
        if self.item in self.filtered:
            tag = "sel_%d" % self.filtered.index(self.item)
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, True)

    def on_item_pick(self, sender, app_data, user_data):
        for n in range(len(self.filtered)):
            tag = "sel_%d" % n
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, n == user_data)
        self.select_item(self.filtered[user_data])

    def select_item(self, item):
        self.item = item
        self.unload_video()
        self.show_selection()

    def show_selection(self):
        """只更新右下角那幾行說明。重建介面時也要用，所以不能順手卸載影片。"""
        item = self.item
        if item is None:
            return
        dpg.set_value("sel_title", item.display_name)
        dpg.set_value("sel_meta", "%s · %s · %s · UP %s\ncid %s · %s"
                      % (item.quality_desc or ("q" + item.quality_dir),
                         fmt_time(item.duration_s), fmt_bytes(item.total_bytes),
                         item.owner or "?", item.cid, item.root.label))
        bad = item.unsupported_reason()
        if bad:
            dpg.set_value("sel_state", "不能處理：" + bad)
            dpg.configure_item("sel_state", color=(240, 150, 100, 255))
            self.sync_enabled()
            return
        dpg.configure_item("sel_state", color=(170, 176, 188, 255))
        local = self.stage_dir / item.device_path.rsplit("/", 1)[-1]
        st = DEV.stage_state(local)
        hist = st.get("history") or []
        if st.get("orig"):
            note = "本機已有暫存副本，載入很快。"
        else:
            note = "本機還沒有這支，載入要從手機傳 %s。" % fmt_bytes(
                item.total_bytes)
        if hist:
            note += "　這支在這台電腦上剪過 %d 次。" % len(hist)
        dpg.set_value("sel_state", note)
        self.sync_enabled()

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
            if MED.have_ffmpeg():
                info = MED.probe(q / "video.m4s")
                tw, th = self.texture_size_for(info["width"], info["height"])
                self.log("畫面 %dx%d @ %.3g fps（%s），預覽用 %dx%d"
                         % (info["width"], info["height"], info["fps"],
                            info["codec"], tw, th))
                src = MED.FrameSource(q / "video.m4s", tw, th)
            self.progress(1.0, "完成")
            return local, q, vt, src

        def done(res):
            local, q, vt, src = res
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

    def refresh_cuts_ui(self):
        dpg.delete_item("cuts_pane", children_only=True)
        if not self.cuts:
            dpg.add_text("還沒有。用上面的「設為起點 / 終點」加一段。",
                         parent="cuts_pane", color=(146, 152, 164, 255))
        snap, rows = self.snapped()
        if snap is None:
            dpg.add_text("吸附失敗：%s" % rows, parent="cuts_pane",
                         color=(240, 150, 100, 255), wrap=px(640))
            rows = []
        for i, cut in enumerate(self.cuts):
            row = rows[i] if i < len(rows) else None
            with dpg.group(parent="cuts_pane", horizontal=True):
                dpg.add_text("%d." % (i + 1))
                dpg.add_input_text(default_value=fmt_time(cut[0], True),
                                   width=px(100), user_data=(i, 0),
                                   callback=self.on_cut_edit,
                                   on_enter=True)
                dpg.add_text("→")
                dpg.add_input_text(default_value=fmt_time(cut[1], True),
                                   width=px(100), user_data=(i, 1),
                                   callback=self.on_cut_edit,
                                   on_enter=True)
                dpg.add_text("%5.1f s" % (cut[1] - cut[0]),
                             color=(170, 176, 188, 255))
                dpg.add_button(label="看", width=px(38), user_data=i,
                               callback=self.goto_cut)
                dpg.add_button(label="移除", width=px(60), user_data=i,
                               callback=self.del_cut)
                if row is not None:
                    _, _, sa, sb = row
                    dpg.add_text("實際切 %s - %s（%+.2f / %+.2f s）"
                                 % (fmt_time(sa, True), fmt_time(sb, True),
                                    sa - cut[0], sb - cut[1]),
                                 color=(200, 180, 110, 255))

        total_cut = sum(b - a for a, b in (snap or []))
        if self.vtrack is not None:
            dpg.set_value("plan_label",
                          "原片 %s → 成品 %s（移除 %d 段，共 %s）"
                          % (fmt_time(self.duration),
                             fmt_time(self.duration - total_cut),
                             len(snap or []), fmt_time(total_cut)))
        else:
            dpg.set_value("plan_label", "")
        marks = []
        if self.mark_in is not None:
            marks.append("起 %s" % fmt_time(self.mark_in, True))
        if self.mark_out is not None:
            marks.append("迄 %s" % fmt_time(self.mark_out, True))
        dpg.set_value("mark_label", "   ".join(marks))
        self.apply_segments()
        self.request_redraw()
        self.sync_enabled()

    def on_snap(self, sender, app_data):
        self.cfg["snap"] = "outer" if app_data.startswith("外擴") else "inner"
        save_config(self.cfg)
        self.refresh_cuts_ui()

    # ======================================================================= #
    # 播放
    # ======================================================================= #

    def on_mode(self, sender, app_data):
        self.preview_mode = "result" if app_data == "成品" else "source"
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

    @property
    def tl_y_ruler(self):
        return self.TL_PAD

    @property
    def tl_y_bar(self):
        return self.TL_PAD + self.TL_H_RULER + self.TL_PAD

    @property
    def tl_y_frag(self):
        return self.tl_y_bar + self.TL_H_BAR + self.TL_PAD

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

    def resize_timeline(self):
        try:
            w = dpg.get_item_rect_size("edit_pane")[0] - px(24)
        except Exception:
            return
        w = max(int(w), px(320))
        if w != self.tl_w:
            self.tl_w = w
            dpg.configure_item("timeline", width=w)
            self.request_redraw()
        self.fit_preview()

    def redraw_timeline(self):
        self._pending_redraw = False
        if not dpg.does_item_exist("timeline"):
            return
        dpg.delete_item("timeline", children_only=True)
        W = self.tl_w
        yb, hb = self.tl_y_bar, self.TL_H_BAR
        dpg.draw_rectangle((0, yb), (W, yb + hb), fill=C_BAR_BG,
                           color=(28, 30, 34, 255), parent="timeline")
        if self.vtrack is None:
            dpg.draw_text((px(8), yb + hb / 2 - px(8)), "還沒載入影片", size=px(14),
                          color=(120, 126, 138, 255), parent="timeline")
            return

        snap, rows = self.snapped()
        snap = snap or []

        # 保留的部分
        for a, b in complement(snap, 0.0, self.duration):
            x0, x1 = self.t2x(a), self.t2x(b)
            if x1 < 0 or x0 > W:
                continue
            dpg.draw_rectangle((max(x0, 0), yb + 1), (min(x1, W), yb + hb - 1),
                               fill=C_KEEP, color=C_KEEP, parent="timeline")

        # 要移除的部分（吸附後的實際範圍）
        for a, b in snap:
            x0, x1 = self.t2x(a), self.t2x(b)
            if x1 < 0 or x0 > W:
                continue
            dpg.draw_rectangle((max(x0, 0), yb + 1), (max(min(x1, W),
                                                          max(x0, 0) + 2),
                                                      yb + hb - 1),
                               fill=C_CUT, color=C_CUT_EDGE, thickness=max(SCALE, 1.0),
                               parent="timeline")

        # 使用者要求的範圍（還沒吸附）畫成細框，看得出吸附差多少
        for a, b, sa, sb in rows or []:
            if abs(sa - a) < 1e-6 and abs(sb - b) < 1e-6:
                continue
            x0, x1 = self.t2x(a), self.t2x(b)
            if x1 < 0 or x0 > W:
                continue
            dpg.draw_rectangle((max(x0, 0), yb + 3), (min(x1, W), yb + hb - 3),
                               color=C_ASK, thickness=max(SCALE, 1.0), parent="timeline")

        self._draw_ruler()
        self._draw_frag_strip()

        # 標記
        for t, lab in ((self.mark_in, "I"), (self.mark_out, "O")):
            if t is None:
                continue
            x = self.t2x(t)
            if -10 <= x <= W + 10:
                dpg.draw_line((x, yb - 2), (x, yb + hb + 2), color=C_MARK,
                              thickness=1.6 * SCALE, parent="timeline")
                dpg.draw_text((x + px(2), yb - px(1)), lab, size=px(13), color=C_MARK,
                              parent="timeline")

        x = self.t2x(self.playhead)
        dpg.draw_line((x, self.tl_y_ruler), (x, yb + hb + self.TL_H_FRAG),
                      color=C_PLAY, thickness=1.4 * SCALE, parent="timeline",
                      tag="tl_playhead")

    def _draw_ruler(self):
        W, y = self.tl_w, self.tl_y_ruler
        span = self.view_b - self.view_a
        steps = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)
        step = steps[-1]
        for s in steps:
            if span / s <= 11:
                step = s
                break
        t = (int(self.view_a / step)) * step
        while t <= self.view_b:
            x = self.t2x(t)
            if 0 <= x <= W:
                dpg.draw_line((x, y + px(12)), (x, y + px(17)), color=C_RULER,
                              parent="timeline")
                dpg.draw_text((x + px(3), y), fmt_time(t), size=px(13), color=C_RULER,
                              parent="timeline")
            t += step

    def _draw_frag_strip(self):
        """fragment 邊界 —— 視訊只能切在這些位置上。"""
        W, y = self.tl_w, self.tl_y_frag
        lo = bisect_left(self.bounds, self.view_a)
        hi = bisect_right(self.bounds, self.view_b)
        n = hi - lo
        spacing = (self.t2x(self.bounds[lo + 1]) - self.t2x(self.bounds[lo])) \
            if n > 1 else 0
        if n > 1 and spacing >= px(4) and n <= 500:
            for i in range(lo, hi):
                x = self.t2x(self.bounds[i])
                dpg.draw_line((x, y), (x, y + self.TL_H_FRAG), color=C_FRAG,
                              parent="timeline")
        else:
            avg = (sum(self.vtrack.fragment_seconds())
                   / max(len(self.vtrack.frags), 1))
            dpg.draw_text((px(4), y),
                          "fragment 約 %.1f s —— 放大才看得到切點邊界" % avg,
                          size=px(12), color=(126, 132, 144, 255),
                          parent="timeline")

    def update_playhead(self):
        if dpg.does_item_exist("tl_playhead"):
            x = self.t2x(self.playhead)
            yb, hb = self.tl_y_bar, self.TL_H_BAR
            dpg.configure_item("tl_playhead", p1=(x, self.tl_y_ruler),
                               p2=(x, yb + hb + self.TL_H_FRAG))

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
        self.resize_timeline()
        self.tick_drag()

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

        if self.vtrack is not None:
            if self.preview_mode == "result" and self.player is not None:
                dpg.set_value("pos_label",
                              "原片 %s / %s      成品 %s / %s"
                              % (fmt_time(self.playhead, True),
                                 fmt_time(self.duration),
                                 fmt_time(self.player.source_to_output(
                                     self.playhead), True),
                                 fmt_time(self.player.total_playable())))
            else:
                dpg.set_value("pos_label", "%s / %s"
                              % (fmt_time(self.playhead, True),
                                 fmt_time(self.duration)))

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
            "要移除 %d 段，共 %s（原片 %s → 成品 %s）："
            % (len(snap), fmt_time(total), fmt_time(self.duration),
               fmt_time(self.duration - total)),
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
                self.log("⚠ 音畫差超過 50 ms，可能聽得出來不同步。")

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
        dpg.set_value("backup_dir_label", self.cfg["backup_dir"])
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
                             color=(146, 152, 164, 255))
        for p, why in bad:
            dpg.add_text("✗ %s — %s" % (p.name, why), parent="restore_list",
                         color=(226, 170, 90, 255), wrap=px(880))

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
                lines.append("⚠ 手機上已經沒有這個目錄了。單純把檔案放回去，"
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

    def apply_font(self):
        """
        用目前的字級重新載入字型並綁定。

        舊的字型不刪：drawlist 裡用 draw_text 畫出來的文字會記住它建立當下
        綁的那個字型，事後把那個字型刪掉，下一次 render 就會踩到已經釋放的
        記憶體（實測是直接 segfault）。字型留著不礙事 —— Dear PyGui 2.x 的
        字形是用到才載入，而且一次工作階段也換不了幾次字級。
        """
        size = px(BASE_FONT_PT)
        for path in [p for p in (self.merged_font_path, self.base_font_path)
                     if p]:
            try:
                with dpg.font_registry() as reg:
                    f = dpg.add_font(str(path), size)
                dpg.bind_font(f)
            except Exception as e:
                self.log("字型：%s 載入失敗（%s）" % (Path(path).name, e))
                continue
            self._font_regs.append(reg)
            return path
        return None

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
        字級變了要連版面一起重做（按鈕寬度、時間軸高度這些都是建立時就定死的，
        不重建的話字變大就會被切掉）。狀態都在 self 上，所以重建完補回去就好。
        """
        was_playing = self.player is not None and self.player.playing
        if was_playing:
            self.player.pause()
        needle = (dpg.get_value("lib_filter") or "") \
            if dpg.does_item_exist("lib_filter") else ""

        for tag in ("root", "dlg_backup_dir", "dlg_backup_file", "win_restore",
                    "win_confirm", "win_help", "win_env", "preview_tex",
                    "tex_reg"):
            if dpg.does_item_exist(tag):
                dpg.delete_item(tag)

        # 先換字型再建介面：drawlist 的文字會記住建立當下綁的字型，
        # 順序反過來的話時間軸上的字會留在舊尺寸。
        self.apply_font()
        self._compute_metrics()
        self._below_h = 0          # 版面換了，重新量一次預覽底下那疊的高度
        self.tl_w = px(1000)
        self.build()

        dpg.set_value("lib_filter", needle)
        dpg.set_value("backup_dir_label", self.cfg["backup_dir"])
        dpg.set_value("adb_state", self._adb_label)
        dpg.configure_item("adb_state", color=self._adb_color)
        for line in self.log_lines:
            self._emit_log(line)
        self.rebuild_library()
        self.show_selection()

        if self.src is not None:
            self.set_preview_texture(self.src.out_w, self.src.out_h)
        if self.vtrack is not None:
            dpg.set_value("mode_radio",
                          "成品" if self.preview_mode == "result" else "原片")
            self.refresh_cuts_ui()
            self.request_redraw()
            if was_playing:
                self.player.play(self.playhead)
                dpg.set_item_label("btn_play", "暫停")
            elif self.src is not None:
                self.scrub.request(self.playhead)
        self.sync_enabled()

    def start_font_upgrade(self, base_path, size):
        """
        單一中文字型補不齊 B站 的標題（♡ ❤ ┊ ⌇、韓文、💓 這些都缺），
        缺的字 Dear PyGui 會畫成「?」。所以背景把韓文與符號從別的系統字型
        合併進來，好了再換上去 —— 介面照樣先用底字型開起來，不卡啟動。
        """
        if not base_path:
            return
        ready = BF.cached(base_path)
        if ready:
            self.apply_merged_font(ready, size)
            return
        problem = BF.fonttools_problem()
        if problem is not None:
            self.log("提醒：標題裡的 ♡ ❤ 韓文、表情符號這個字型沒有，會顯示成"
                     "「?」。要自動補起來需要 fonttools，但它現在不能用：")
            self.log("      %s" % problem)
            self.log('      裝法（用這個直譯器，別直接打 pip）：'
                     '"%s" -m pip install fonttools' % sys.executable)
            self.log("      裝好後重開程式。診斷指令："
                     "python bili_gui.py --check-font")
            return

        def body():
            self.log("字型：正在補齊缺字（韓文與符號），只有第一次要做，"
                     "大約半分鐘。這期間標題可能先顯示成「?」。")
            try:
                path = BF.build(base_path, log=self.log)
            except BF.FontError as e:
                self.log("字型：補字失敗（%s）。標題裡的 ♡ ❤ 韓文、表情會顯示"
                         "成「?」，其他功能不受影響。" % e)
                return
            self.post(lambda: self.apply_merged_font(path, size))

        threading.Thread(target=body, name="font-merge", daemon=True).start()

    def apply_merged_font(self, path, size=None):
        """（主執行緒）換上補好字的字型。size 保留只為相容舊呼叫。"""
        before = self.merged_font_path
        self.merged_font_path = Path(path)
        if self.apply_font() is None:
            self.merged_font_path = before
            self.log("字型：載入 %s 失敗，繼續用原本的。" % Path(path).name)
            return
        self.log("字型：已套用補字版 %s" % Path(path).name)
        if self.items:
            self.rebuild_library()

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
        font, fpath = pick_font(px(BASE_FONT_PT))
        self.base_font_path = fpath
        self.build()
        if font is not None:
            dpg.bind_font(font)
        with dpg.handler_registry():
            dpg.add_mouse_down_handler(callback=self.on_mouse_down)
            dpg.add_mouse_release_handler(callback=self.on_mouse_release)
            dpg.add_mouse_wheel_handler(callback=self.on_wheel)
            dpg.add_key_press_handler(callback=self.on_key)

        w, h = self.window_size()
        # 見 WINDOW_TITLE 的註解：非 ASCII 的標題會讓第一次 render 直接 segfault，
        # 所以這裡再擋一次，免得將來有人改成中文又踩到。
        title = WINDOW_TITLE if WINDOW_TITLE.isascii() else "bili-trim"
        dpg.create_viewport(title=title, width=int(w), height=int(h),
                            min_width=px(1100), min_height=px(700))
        dpg.setup_dearpygui()
        dpg.show_viewport()
        if font is None:
            self.log("找不到中文字型，介面上的中文會顯示不出來。")
        else:
            self.log("字型：%s（%d pt）" % (fpath, px(BASE_FONT_PT)))
        self.start_font_upgrade(fpath, px(BASE_FONT_PT))
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

    base = None
    for p in FONT_CANDIDATES:
        if os.path.isfile(p):
            base = p
            break
    print("底字型   : %s" % (base or "找不到任何中文字型！"))
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
    App().run()


if __name__ == "__main__":
    main()
