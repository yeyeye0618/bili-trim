# -*- coding: utf-8 -*-
"""
bili_config.py — GUI 的設定檔（%APPDATA%/bili_trim_gui/settings.json）。

為什麼單獨一個檔
----------------
這段本來在 bili_gui.py 裡。但 setup.py 要在「剛裝完、使用者還沒開過 GUI」
的時候先把備份位置之類的東西寫好，讓第一次啟動就是設定好的狀態；
而 setup.py 不能 import bili_gui（它連 dearpygui 都還不一定裝好）。

所以搬到這裡。純標準庫，誰都能 import。
"""

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, "frozen", False))

DEFAULT_FONT_PT = 17


def config_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "bili_trim_gui"


def local_dir():
    """
    大的、不用跟著帳號漫遊的東西：%LOCALAPPDATA%\\bili_trim_gui。
    exe 可能就躺在桌面或「下載」，不能在它旁邊長出一堆資料夾；
    原始碼被解到暫存資料夾，也不能放那裡。
    """
    if os.name == "nt":
        base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                or str(Path.home()))
    else:
        base = (os.environ.get("XDG_DATA_HOME")
                or str(Path.home() / ".local" / "share"))
    return Path(base) / "bili_trim_gui"


def tools_dir():
    """
    adb / ffmpeg 放這裡。exe 和原始碼版共用同一份：在哪個版本裝的，
    另一個版本都找得到。BILI_TOOLS 可以改指別的資料夾。
    """
    return Path(os.environ.get("BILI_TOOLS") or (local_dir() / "tools"))


CONFIG_PATH = config_dir() / "settings.json"

DEFAULTS = {
    "backup_dir": str(Path.home() / "Documents" / "bili_backups"),
    # 暫存影片：原始碼版照舊放在專案裡，exe 版放 %LOCALAPPDATA%
    "stage_dir": str(local_dir() / "stage" if FROZEN
                     else HERE / "bili_gui_stage"),
    "preview_quality": "原生解析度",
    "font_pt": DEFAULT_FONT_PT,
    "snap": "outer",
    "volume": 70,
    "audio_offset_ms": 0,
    "force_stop_after_write": True,
    "window": None,
    "window_dpi": None,
    # 用 Wi-Fi 連上過的手機 IP：重新掃描時會順手幫它們重新連線
    "wifi_hosts": [],
    # 最後一次用 Wi-Fi 連上的 IP:埠號。網路上找不到手機（有些網路或防火牆擋掉
    # 區網廣播）的時候，至少先試這一組 —— 手機沒重開的話埠號不會變
    "wifi_last": "",
    # IP -> 手機型號，「找手機」列表顯示用
    "wifi_names": {},
    # 無線偵錯 guid（adb-<序號>-xxx）-> 手機型號：配對過但不在網路上的手機，
    # 清單裡也寫得出名字
    "wifi_models": {},
    # IP -> guid：自己試埠號找到的手機只知道 IP，靠這個對回是哪支配對過的手機
    "wifi_guids": {},
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg, strict=False):
    """
    GUI 存設定失敗沒必要打斷使用者（strict=False，預設）；
    但 setup.py 把設定寫進去是整個步驟的重點，失敗一定要說（strict=True）。
    """
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        return True
    except OSError:
        if strict:
            raise
        return False
