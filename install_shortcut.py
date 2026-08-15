# -*- coding: utf-8 -*-
"""在桌面创建「悬浮任务板」快捷方式（指向 pythonw 静默启动，无黑窗）。"""
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # 项目根目录
MAIN_PY = os.path.join(BASE_DIR, "app", "main.py")
LNK_NAME = "悬浮任务板.lnk"


def find_pythonw():
    exe = sys.executable or "pythonw"
    base, name = os.path.split(exe)
    if name.lower().startswith("python"):
        cand = os.path.join(base, "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def main():
    pyw = find_pythonw()
    ps = (
        "$ws = New-Object -ComObject WScript.Shell;"
        "$d = [Environment]::GetFolderPath('Desktop');"
        "$lnk = $d + '\\" + LNK_NAME + "';"
        "$s = $ws.CreateShortcut($lnk);"
        "$s.TargetPath = '" + pyw.replace("'", "''") + "';"
        "$s.Arguments = '\"" + MAIN_PY.replace("'", "''") + "\"';"
        "$s.WorkingDirectory = '" + BASE_DIR.replace("'", "''") + "';"
        "$s.IconLocation = '" + pyw.replace("'", "''") + ",0';"
        "$s.Description = 'TaskBoard floating task app';"
        "$s.Save();"
        "if (Test-Path $lnk) { Write-Output 'OK_SHORTCUT_CREATED' } else { Write-Output 'SHORTCUT_FAIL'; exit 1 }"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=30,
                       encoding="utf-8", errors="replace")
    print((r.stdout or r.stderr or "").strip())
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
