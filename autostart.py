# -*- coding: utf-8 -*-
"""开机自启动管理（Windows 注册表 HKCU\\...\\Run）。"""
import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "TaskBoardFloatingTask"

MAIN_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "main.py"))


def _pythonw():
    """优先使用 pythonw.exe，避免启动时弹出黑窗。"""
    exe = sys.executable or "pythonw"
    base, name = os.path.split(exe)
    if name.lower().startswith("python"):
        candidate = os.path.join(base, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def command_line():
    return '"%s" "%s"' % (_pythonw(), MAIN_SCRIPT)


def is_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False


def set_enabled(on):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if on:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command_line())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass
    return is_enabled()
