# -*- coding: utf-8 -*-
"""系统托盘图标（纯 ctypes + Shell_NotifyIcon，零第三方依赖）。

用法:
    tray = Tray(tooltip="悬浮任务板")
    tray.start(queue)      # 后台线程创建托盘图标；事件投递到 queue
    tray.balloon("标题", "内容")   # 气泡提示
    tray.stop()            # 移除图标并退出消息循环

事件（投递到 queue，由主线程消费）:
    ("tray_show",)             左键点击/双击 → 恢复窗口
    ("tray_menu", x, y)        右键 → 在 (x, y) 弹菜单
    ("tray_quit",)             托盘菜单退出
"""
import ctypes
import os
import sys
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

WM_USER = 0x0400
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NIF_INFO = 0x10
NIIF_INFO = 0x1


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


# ---- 函数签名（避免 64 位指针截断） ----
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.LoadIconW.restype = wintypes.HICON
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.DestroyIcon.argtypes = [wintypes.HICON]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.ExtractIconW.restype = wintypes.HICON
shell32.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_uint]

HWND_MESSAGE = wintypes.HWND(-3)


def _pythonw_path():
    exe = sys.executable or "pythonw"
    base, name = os.path.split(exe)
    if name.lower().startswith("python"):
        cand = os.path.join(base, "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


class Tray:
    def __init__(self, tooltip="悬浮任务板", icon_path=None):
        self.tooltip = tooltip[:127]
        self.icon_path = icon_path or _pythonw_path()
        self._hwnd = None
        self._icon = None
        self._thread = None
        self._proc_ref = None
        self._running = False
        self._queue = None
        self._class_name = "TaskBoardTrayWindow_%d" % id(self)

    @property
    def running(self):
        return self._running

    def start(self, queue):
        self._queue = queue
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="tray")
        self._thread.start()

    def _run(self):
        self._proc_ref = WNDPROC(self._wndproc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc_ref
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            return
        self._hwnd = user32.CreateWindowExW(
            0, self._class_name, "TaskBoardTray", 0,
            0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
        if not self._hwnd:
            return
        self._icon = self._load_icon()
        nid = self._nid()
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_USER + 1
        nid.hIcon = self._icon
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            return
        self._running = True
        msg = wintypes.MSG()
        while self._running:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _load_icon(self):
        try:
            h = shell32.ExtractIconW(None, self.icon_path, 0)
            if h:
                return h
        except Exception:
            pass
        return user32.LoadIconW(None, "IDI_APPLICATION")

    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.szTip = self.tooltip
        return nid

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == WM_USER + 1:
            mouse = lp & 0xFFFF
            if mouse in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._post(("tray_show",))
            elif mouse == WM_RBUTTONUP:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                self._post(("tray_menu", int(pt.x), int(pt.y)))
        elif msg == WM_DESTROY:
            user32.PostQuitMessage(0)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _post(self, item):
        q = self._queue
        if q is not None:
            try:
                q.put(item)
            except Exception:
                pass

    def balloon(self, title, text):
        if not self._running or not self._hwnd:
            return
        nid = self._nid()
        nid.uFlags = NIF_INFO
        nid.szInfo = (text or "")[:255]
        nid.szInfoTitle = (title or "")[:63]
        nid.dwInfoFlags = NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def stop(self):
        self._running = False
        if self._hwnd:
            nid = self._nid()
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            user32.PostMessageW(self._hwnd, WM_USER + 999, 0, 0)  # 唤醒消息循环
        if self._icon:
            try:
                user32.DestroyIcon(self._icon)
            except Exception:
                pass
            self._icon = None
