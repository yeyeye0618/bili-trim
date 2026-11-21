# -*- coding: utf-8 -*-
"""
bili_filedlg.py — Windows 原生的「開啟」視窗（跟檔案總管同一個）。

Dear PyGui 自己畫的檔案視窗長得跟 Windows 完全不一樣：沒有側邊的「下載」
「桌面」、不能貼路徑、不認得捷徑，第一次用的人會以為程式壞了。這裡用 ctypes
直接叫 IFileOpenDialog（Vista 以後檔案總管用的那個），選檔案和選資料夾都是它。

不是 Windows、或 COM 叫不起來的時候 pick() 丟 Unavailable，呼叫端改用
Dear PyGui 的檔案視窗。

pick() 會卡住呼叫它的執行緒直到使用者按下確定或取消，所以要在背景執行緒
呼叫（介面才會繼續畫）；這裡會自己在那個執行緒初始化 COM。
"""

import ctypes
import os


class Unavailable(Exception):
    pass


_CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}"
_IID_IFileOpenDialog = "{d57c7288-d4ad-4768-be02-9d969532d960}"

FOS_PICKFOLDERS = 0x20
FOS_FORCEFILESYSTEM = 0x40
FOS_PATHMUSTEXIST = 0x800
FOS_FILEMUSTEXIST = 0x1000
SIGDN_FILESYSPATH = 0x80058000 - (1 << 32)      # 當成有號 int 傳
ERROR_CANCELLED = 0x800704C7 - (1 << 32)

# IFileDialog 的 vtable 位置（IUnknown 3 個 + IModalWindow::Show）
_SHOW, _SET_FILE_TYPES, _SET_OPTIONS, _GET_OPTIONS = 3, 4, 9, 10
_SET_FOLDER, _SET_TITLE, _GET_RESULT = 12, 17, 20
_RELEASE = 2
_SHELLITEM_GET_DISPLAY_NAME = 5


class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16),
                ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]


class _FilterSpec(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar_p), ("spec", ctypes.c_wchar_p)]


def _guid(text):
    g = _GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text),
                                             ctypes.byref(g))
    if hr:
        raise Unavailable("CLSIDFromString 0x%08X" % (hr & 0xFFFFFFFF))
    return g


def _method(obj, index, *argtypes):
    """obj 是 COM 介面指標（c_void_p），取出 vtable 第 index 個函式。"""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    fn = proto(vtbl[index])
    return lambda *a: fn(obj, *a)


def pick(owner=None, folder=False, title=None, filters=(), start_dir=None):
    """
    回傳選到的路徑（str），取消就回 None。
    filters：[("壓縮檔 (*.zip)", "*.zip"), ...]
    """
    if os.name != "nt":
        raise Unavailable("不是 Windows")
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 2)       # STA；這個執行緒已經初始化過也沒關係
    dlg = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(_guid(_CLSID_FileOpenDialog)), None, 1,   # INPROC
            ctypes.byref(_guid(_IID_IFileOpenDialog)), ctypes.byref(dlg))
        if hr or not dlg:
            raise Unavailable("CoCreateInstance 0x%08X" % (hr & 0xFFFFFFFF))

        opts = ctypes.c_uint()
        _method(dlg, _GET_OPTIONS, ctypes.POINTER(ctypes.c_uint))(
            ctypes.byref(opts))
        flags = opts.value | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
        flags |= FOS_PICKFOLDERS if folder else FOS_FILEMUSTEXIST
        _method(dlg, _SET_OPTIONS, ctypes.c_uint)(flags)

        if title:
            _method(dlg, _SET_TITLE, ctypes.c_wchar_p)(title)
        if filters and not folder:
            arr = (_FilterSpec * len(filters))(*[_FilterSpec(n, s)
                                                 for n, s in filters])
            _method(dlg, _SET_FILE_TYPES, ctypes.c_uint,
                    ctypes.c_void_p)(len(filters), ctypes.addressof(arr))
        if start_dir and os.path.isdir(str(start_dir)):
            _set_folder(dlg, str(start_dir))

        hr = _method(dlg, _SHOW, ctypes.c_void_p)(owner)
        if hr == ERROR_CANCELLED:
            return None
        if hr:
            raise Unavailable("Show 0x%08X" % (hr & 0xFFFFFFFF))

        item = ctypes.c_void_p()
        hr = _method(dlg, _GET_RESULT, ctypes.POINTER(ctypes.c_void_p))(
            ctypes.byref(item))
        if hr or not item:
            return None
        try:
            name = ctypes.c_wchar_p()
            hr = _method(item, _SHELLITEM_GET_DISPLAY_NAME, ctypes.c_int,
                         ctypes.POINTER(ctypes.c_wchar_p))(
                SIGDN_FILESYSPATH, ctypes.byref(name))
            if hr:
                return None
            path = name.value
            ole32.CoTaskMemFree(name)
            return path
        finally:
            _method(item, _RELEASE)()
    finally:
        if dlg:
            _method(dlg, _RELEASE)()


def _set_folder(dlg, path):
    """起始資料夾。只是方便，失敗就算了。"""
    item = ctypes.c_void_p()
    shell32 = ctypes.windll.shell32
    shell32.SHCreateItemFromParsingName.argtypes = [
        ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    iid_shellitem = _guid("{43826d1e-e718-42ee-bc55-a1e261c37bfe}")
    if shell32.SHCreateItemFromParsingName(path, None, ctypes.byref(iid_shellitem),
                                           ctypes.byref(item)) or not item:
        return
    try:
        _method(dlg, _SET_FOLDER, ctypes.c_void_p)(item)
    finally:
        _method(item, _RELEASE)()
