# -*- coding: utf-8 -*-
"""悬浮任务板入口：启动本地 HTTP 服务 + 桌面悬浮窗。

用法:
    python main.py                 # 正常启动（悬浮窗 + 后台服务 :39999）
    python main.py --no-gui        # 只启动后台服务
    python main.py --port 39999 --host 0.0.0.0
    python main.py --selftest      # GUI 构建自检（自动退出）
    python main.py --guilive       # GUI+服务联动自检（4 秒后自动退出）
"""
import argparse
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_DB = os.path.join(BASE_DIR, "taskboard.db")
DEFAULT_CONFIG = {"host": "0.0.0.0", "port": 39999, "db_path": DEFAULT_DB, "autostart": False}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
    except Exception:
        pass
    cfg["db_path"] = os.path.abspath(cfg.get("db_path") or DEFAULT_DB)
    try:
        cfg["port"] = int(cfg.get("port") or 39999)
    except (TypeError, ValueError):
        cfg["port"] = 39999
    return cfg


def main():
    parser = argparse.ArgumentParser(description="悬浮任务板：桌面悬浮窗 + 本地设置页")
    parser.add_argument("--host", default=None, help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 39999）")
    parser.add_argument("--db", default=None, help="SQLite 数据库文件路径")
    parser.add_argument("--no-gui", action="store_true", help="只启动本地服务，不显示悬浮窗")
    parser.add_argument("--selftest", action="store_true", help="GUI 构建自检（2.5 秒后自动退出）")
    parser.add_argument("--guilive", action="store_true", help="GUI+服务联动自检（4 秒后自动退出）")
    args = parser.parse_args()

    cfg = load_config()
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port
    if args.db:
        cfg["db_path"] = os.path.abspath(args.db)

    import autostart
    from db import DB
    from floating import FloatingApp
    from server import ApiServer

    cfg["autostart"] = autostart.is_enabled()
    db = DB(cfg["db_path"])
    log = print
    server = ApiServer(cfg["host"], cfg["port"], db, cfg, log=log)
    try:
        server.start()
    except RuntimeError as e:
        log("启动失败: %s" % e)
        if args.selftest or args.guilive:
            print("SELFTEST FAIL: %s" % e)
            return 1
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("悬浮任务板", str(e))
            r.destroy()
        except Exception:
            pass
        return 1

    if args.no_gui:
        print("服务运行中，按 Ctrl+C 停止")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        server.stop()
        db.close()
        return 0

    base = "http://127.0.0.1:%d" % server.bound_port
    app = FloatingApp(base, selftest=args.selftest, auto_close=(4.0 if args.guilive else 0))
    app.run()

    server.stop()
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
