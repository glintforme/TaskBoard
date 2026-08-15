# -*- coding: utf-8 -*-
"""GUI 可视化自检：启动悬浮窗，抓取折叠态/展开态画面存为 PNG（纯 GDI + 手写 PNG 编码）。"""
import ctypes
import os
import queue
import struct
import sys
import zlib
from ctypes import wintypes

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, ".test_tmp")
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, BASE)

from db import DB
from floating import FloatingApp, SECTION_TITLES
from server import ApiServer


def write_png(path, w, h, buffer_bgra, stride):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    raw = b""
    row_bytes = w * 4
    for y in range(h - 1, -1, -1):
        start = y * stride
        row = buffer_bgra[start:start + row_bytes]
        # BGRA -> RGBA
        rgba = bytearray(row_bytes)
        for i in range(0, row_bytes, 4):
            rgba[i] = row[i + 2]
            rgba[i + 1] = row[i + 1]
            rgba[i + 2] = row[i]
            rgba[i + 3] = row[i + 3]
        raw += b"\x00" + bytes(rgba)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
           chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return path


def capture(hwnd, path):
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError("empty window rect: %r" % (rect,))

    class BMI(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    hdc_wnd = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_wnd)
    bmi = BMI()
    bmi.biSize = ctypes.sizeof(BMI)
    bmi.biWidth = w
    bmi.biHeight = -h
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0, None, None, 0)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    user32.PrintWindow(hwnd, hdc_mem, 2)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
    write_png(path, w, h, buf.raw, w * 4)
    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_wnd)
    print("captured -> %s (%dx%d)" % (path, w, h))
    return path


def main():
    db_path = os.path.join(OUT, "gui_capture.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    db = DB(db_path)
    for title, scope in [("晨跑 3 公里", "daily"), ("整理今日周报", "today"),
                         ("预约会议室", "tomorrow"), ("周末大扫除", "week"),
                         ("体检预约", "month"), ("给项目写测试用例", "today")]:
        db.add_task(title, scope, due_date="2026-08-15")
    db.complete_task(1)  # 晨跑今日已完成
    server = ApiServer("127.0.0.1", 0, db,
                       {"host": "127.0.0.1", "port": 0, "db_path": db_path,
                        "_config_path": os.path.join(OUT, "cfg.json")})
    server.start()
    base = "http://127.0.0.1:%d" % server.bound_port

    app = FloatingApp(base)
    hwnd = int(app.root.winfo_id())

    def drain():
        try:
            while True:
                item = app.q.get_nowait()
                if item[0] == "data":
                    _, groups, stats, done = item
                    app.groups = {k: groups.get(k, []) for k, _ in SECTION_TITLES}
                    app.stats = stats
                    app.done_today = done
                    app.connected = True
                elif item[0] == "error":
                    app.connected = False
                    app.last_error = item[1]
        except queue.Empty:
            pass

    def step1():
        app._fetch()
        drain()
        app._render_collapsed()
        capture(hwnd, os.path.join(OUT, "float_collapsed.png"))
        app.set_expanded(True)
        app.root.after(400, step2)

    def step2():
        capture(hwnd, os.path.join(OUT, "float_expanded.png"))
        app.set_expanded(False)
        app._dock(auto=False)
        app.root.after(400, step3)

    def step3():
        capture(hwnd, os.path.join(OUT, "float_docked.png"))
        app._undock()
        # 验证边缘缩放逻辑：直接调用 _resize 模拟向右下拖拽
        app.root.geometry("260x100+200+200")
        app.root.update_idletasks()
        app._mode = "resize"
        app._resize_start = (200, 200, 200, 200, 260, 100, "rb")
        evt = type("E", (), {"x_root": 250, "y_root": 260})()
        app._resize(evt)
        app.root.update_idletasks()
        w, h = app.root.winfo_width(), app.root.winfo_height()
        if w == 310 and h == 160:
            print("RESIZE LOGIC OK (%dx%d)" % (w, h))
        else:
            print("RESIZE LOGIC FAIL (%dx%d)" % (w, h))
        app._mode = None
        app.quit()
        server.stop()
        db.close()
        print("GUI CAPTURE OK")

    app.root.after(1200, step1)
    app.run()


if __name__ == "__main__":
    main()
