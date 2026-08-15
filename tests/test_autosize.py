# -*- coding: utf-8 -*-
"""悬浮窗自适应自测：
1) 任务多、标题长时，展开窗口高度自动适配内容（方案一）；
2) 长标题/备注自动换行，窗口拖窄时内容自动重排；
3) 点击标题展开完整内容，窗口随之自适应（方案二）。
"""
import ctypes
import os
import queue
import struct
import sys
import time
import zlib
from ctypes import wintypes

BASE = os.path.dirname(os.path.abspath(__file__))               # tests/
OUT = os.path.join(BASE, ".test_tmp")
os.makedirs(OUT, exist_ok=True)
APP_DIR = os.path.join(os.path.dirname(BASE), "app")             # 应用代码目录
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from db import DB
from floating import FloatingApp, SECTION_TITLES
from server import ApiServer

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def write_png(path, w, h, buffer_bgra, stride):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    raw = b""
    row_bytes = w * 4
    for y in range(h - 1, -1, -1):
        start = y * stride
        row = buffer_bgra[start:start + row_bytes]
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
        raise RuntimeError("empty window rect")

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
    global PASS, FAIL
    print("== 悬浮窗自适应自测 ==")
    db_path = os.path.join(OUT, "autosize.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    db = DB(db_path)
    long_note = "这是一段非常长的任务备注，用于测试备注内容在悬浮窗内自动换行显示，不会超出窗口边界。" * 3
    for i in range(12):
        db.add_task("这是一个特别长的任务标题用来测试自动换行显示完整内容第 %02d 项" % i,
                    "today", due_date="2026-08-15",
                    note=long_note if i % 2 == 0 else "",
                    deadline="2026-08-16T18:00", remind_at="2026-08-15T09:00")
    db.add_task("晨跑", "daily")
    server = ApiServer("127.0.0.1", 0, db,
                       {"host": "127.0.0.1", "port": 0, "db_path": db_path,
                        "_config_path": os.path.join(OUT, "cfg4.json")})
    server.start()
    app = FloatingApp("http://127.0.0.1:%d" % server.bound_port)
    hwnd = int(app.root.winfo_id())

    def drain():
        try:
            while True:
                it = app.q.get_nowait()
                if it[0] == "data":
                    _, groups, stats, done = it
                    app.groups = {k: groups.get(k, []) for k, _ in SECTION_TITLES}
                    app.stats = stats
                    app.done_today = done
                    app.connected = True
                elif it[0] == "error":
                    app.connected = False
        except queue.Empty:
            pass

    def step1():
        app._fetch()
        drain()
        app.set_expanded(True)
        app.root.after(700, step2)

    def step2():
        app.root.update_idletasks()
        w, h = app.root.winfo_width(), app.root.winfo_height()
        content = app._list_content_h
        cap = int(app.root.winfo_screenheight() * 0.85)
        check("窗口高度自适应内容（含85%上限）", h >= min(content + 30, cap) - 5,
              "h=%d content=%d cap=%d" % (h, content, cap))
        check("窗口高度未超过85%屏幕", h <= cap + 5, str(h))

        titles = [app.canvas.itemcget(c, "text") for c, _, _, _ in app._wrap_labels
                  if str(app.canvas.itemcget(c, "text")).startswith(("▸", "▾"))]
        full_ok = any("自动换行显示完整内容" in t and t.endswith("项") for t in titles)
        check("长标题完整显示（自动换行，未截断）", full_ok, titles[:2])

        # 窗口拖窄 → 标题自动重排（换行宽度变小），宽度不被强制弹回
        wl_before = {c: mw for c, _, mw, _ in app._wrap_labels
                     if str(app.canvas.itemcget(c, "text")).startswith("▸")}
        app.root.geometry("240x%d+200+80" % h)
        for _ in range(40):
            app.root.update()
            if app.root.winfo_width() <= 250:
                break
            time.sleep(0.02)
        app.root.update()
        win_w = app.root.winfo_width()
        wl_after = {c: mw for c, _, mw, _ in app._wrap_labels
                    if str(app.canvas.itemcget(c, "text")).startswith("▸")}
        check("窗口拖窄后宽度保持用户设置(240)", win_w <= 250, "win_w=%d" % win_w)
        narrowed = any(wl_after.get(k, wl_before.get(k, 0)) < wl_before.get(k, 1e9) for k in wl_before)
        check("窗口拖窄后标题自动重排（换行宽度变小）", narrowed,
              "before=%s after=%s" % (wl_before, wl_after))

        # 方案二：点击标题展开详情 → 窗口变高；再点收起
        h_before = app.root.winfo_height()
        first_tid = app.groups["today"][0]["id"]
        app._toggle_row_detail(first_tid)
        app.root.update_idletasks()
        h_after = app.root.winfo_height()
        check("点击标题展开详情后窗口自适应变高 (%d -> %d)" % (h_before, h_after), h_after >= h_before)
        capture(hwnd, os.path.join(OUT, "autosize_expanded_detail.png"))
        app._toggle_row_detail(first_tid)
        app.root.update_idletasks()
        h_close = app.root.winfo_height()
        check("再次点击标题详情收起", h_close <= h_after)

        app.quit()
        server.stop()
        db.close()
        print("======================")
        print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
        sys.exit(1 if FAIL else 0)

    app.root.after(800, step1)
    app.run()


if __name__ == "__main__":
    main()
