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
# exe 版的暫存影片放在 exe 旁邊（原始碼被解到暫存資料夾，放那裡會被刪掉）
APP_DIR = (Path(sys.executable).resolve().parent
           if getattr(sys, "frozen", False) else HERE)

DEFAULT_FONT_PT = 17


def config_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "bili_trim_gui"


CONFIG_PATH = config_dir() / "settings.json"

DEFAULTS = {
    "backup_dir": str(Path.home() / "Documents" / "bili_backups"),
    "stage_dir": str(APP_DIR / "bili_gui_stage"),
    "preview_quality": "原生解析度",
    "font_pt": DEFAULT_FONT_PT,
    "snap": "outer",
    "volume": 70,
    "audio_offset_ms": 0,
    "force_stop_after_write": True,
    "window": None,
    "window_dpi": None,
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
