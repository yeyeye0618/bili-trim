# -*- coding: utf-8 -*-
"""
bili_drop.py — 讓檔案總管裡的檔案可以拖進視窗（只有 Windows）。

Dear PyGui 的拖放只在介面元件之間，收不到從檔案總管拖進來的檔案。
這裡直接跟 Windows 要：對視窗呼叫 DragAcceptFiles，再把視窗的訊息處理
函式換成自己的，攔下 WM_DROPFILES，其他訊息原封不動交還給 Dear PyGui。

攔到的訊息是在主執行緒的 render_dearpygui_frame() 裡處理的，所以
on_files 可以直接碰介面。
"""

import ctypes
import os

WM_DROPFILES = 0x0233
WM_COPYDATA = 0x004A
WM_COPYGLOBALDATA = 0x0049
GWLP_WNDPROC = -4
MSGFLT_ALLOW = 1

# 換上去的回呼要一直活著：被回收的話 Windows 下一次呼叫就是當機
_keep = []


def install(title, on_files):
    """對這個行程裡標題是 title 的視窗開啟拖放。成功回 True。"""
    if os.name != "nt":
        return False
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    sh = ctypes.windll.shell32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    hwnd = _find_window(title)
    if not hwnd:
        return False

    u32.SetWindowLongPtrW.restype = ctypes.c_void_p
    u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                      ctypes.c_void_p]
    u32.CallWindowProcW.restype = LRESULT
    u32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                    wintypes.UINT, wintypes.WPARAM,
                                    wintypes.LPARAM]
    sh.DragQueryFileW.restype = wintypes.UINT
    sh.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                  ctypes.c_wchar_p, wintypes.UINT]
    sh.DragFinish.argtypes = [ctypes.c_void_p]
    sh.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]

    old = [None]

    def proc(h, msg, wp, lp):
        if msg == WM_DROPFILES:
            files = []
            try:
                n = sh.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
                for i in range(n):
                    size = sh.DragQueryFileW(wp, i, None, 0) + 1
                    buf = ctypes.create_unicode_buffer(size)
                    sh.DragQueryFileW(wp, i, buf, size)
                    files.append(buf.value)
            finally:
                sh.DragFinish(wp)
            try:
                on_files(files)
            except Exception:
                pass            # 例外不能穿過 Windows 的回呼
            return 0
        return u32.CallWindowProcW(old[0], h, msg, wp, lp)

    cb = WNDPROC(proc)
    _keep.append(cb)
    old[0] = u32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC,
                                   ctypes.cast(cb, ctypes.c_void_p))
    if not old[0]:
        return False
    # 用系統管理員身分執行時，Windows 預設擋掉一般權限的檔案總管拖進來的訊息
    try:
        for m in (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA):
            u32.ChangeWindowMessageFilterEx(hwnd, m, MSGFLT_ALLOW, None)
    except (AttributeError, OSError):
        pass
    sh.DragAcceptFiles(hwnd, True)
    return True


def _find_window(title):
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    pid = os.getpid()
    hit = []
    ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(h, _):
        owner = wintypes.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(owner))
        if owner.value != pid:
            return True
        n = u32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(h, buf, n + 1)
        if buf.value == title:
            hit.append(h)
            return False
        return True

    u32.EnumWindows(ENUM(each), 0)
    return hit[0] if hit else None
