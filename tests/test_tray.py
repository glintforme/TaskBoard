# -*- coding: utf-8 -*-
"""系统托盘自测：验证图标创建、点击/右键消息路径、最小化/恢复、退出清理。"""
import os
import queue
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))               # tests/
APP_DIR = os.path.join(os.path.dirname(BASE), "app")             # 应用代码目录
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from ctypes import wintypes
import ctypes

import tray as tray_mod
from floating import FloatingApp, http_json
from db import DB
from server import ApiServer

user32 = ctypes.windll.user32
WM_USER = 0x0400
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

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


def main():
    global PASS, FAIL
    print("== 系统托盘自测 ==")

    # ---- 1. 独立 Tray 对象：创建 + 消息路径 ----
    q = queue.Queue()
    t = tray_mod.Tray(tooltip="托盘自测")
    t.start(q)
    time.sleep(1.0)
    check("托盘图标创建成功", t.running, "running=%s hwnd=%s" % (t.running, t._hwnd))
    if not t.running:
        print("（当前环境可能无法创建托盘图标，跳过消息路径测试）")
    else:
        t.balloon("测试", "托盘自测气泡")
        check("气泡提示调用正常", True)
        # 模拟左键点击
        user32.PostMessageW(t._hwnd, WM_USER + 1, 1, WM_LBUTTONUP)
        got = None
        for _ in range(50):
            try:
                got = q.get(timeout=0.2)
                break
            except queue.Empty:
                time.sleep(0.02)
        check("左键点击事件送达", got == ("tray_show",), str(got))
        # 模拟右键
        user32.PostMessageW(t._hwnd, WM_USER + 1, 1, WM_RBUTTONUP)
        got = None
        for _ in range(50):
            try:
                got = q.get(timeout=0.2)
                break
            except queue.Empty:
                time.sleep(0.02)
        check("右键事件送达(含坐标)", isinstance(got, tuple) and got[0] == "tray_menu"
              and isinstance(got[1], int), str(got))
        t.stop()
        time.sleep(0.3)
        check("托盘图标已移除", not t.running)

    # ---- 2. 与悬浮窗集成：最小化/恢复 ----
    tmp = os.path.join(BASE, ".test_tmp")
    os.makedirs(tmp, exist_ok=True)
    dbp = os.path.join(tmp, "tray_gui.db")
    if os.path.exists(dbp):
        os.remove(dbp)
    db = DB(dbp)
    db.add_task("托盘集成测试任务", "daily")
    server = ApiServer("127.0.0.1", 0, db,
                       {"host": "127.0.0.1", "port": 0, "db_path": dbp,
                        "_config_path": os.path.join(tmp, "cfg3.json")})
    server.start()
    app = FloatingApp("http://127.0.0.1:%d" % server.bound_port)

    def step():
        try:
            app._minimize_to_tray()
            state = app.root.state()
            check("最小化后窗口隐藏", app._hidden_in_tray and state == "withdrawn", state)
            check("托盘已创建", app.tray is not None and app.tray.running)
            if app.tray and app.tray.running:
                # 模拟点击托盘图标恢复
                user32.PostMessageW(app.tray._hwnd, WM_USER + 1, 1, WM_LBUTTONUP)
            for _ in range(50):
                app._poll_queue_once()
                time.sleep(0.05)
            app.root.update()
            check("点击托盘后窗口恢复", not app._hidden_in_tray and app.root.state() == "normal",
                  app.root.state())
        finally:
            app.quit()
            server.stop()
            db.close()
        print("======================")
        print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
        sys.exit(1 if FAIL else 0)

    app.root.after(800, step)
    app.run()


if __name__ == "__main__":
    main()
