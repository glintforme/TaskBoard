# -*- coding: utf-8 -*-
"""悬浮窗新功能自测：
1) 折叠态显示「今日待完成任务标题」；
2) 窗口透明度默认生效（-alpha ≈ 0.72）；
3) 背景图片导入 + 图片透明度混合（像素确实改变）；
4) 底部功能按钮面板跟随悬浮窗移动、不随悬浮窗缩放改变大小；
5) 抗闪烁守卫：数据未变化不重建列表、尺寸未变不重复设置几何。
"""
import json
import os
import struct
import sys
import time
import zlib

BASE = os.path.dirname(os.path.abspath(__file__))               # tests/
ROOT = os.path.dirname(BASE)                                     # 项目根目录
OUT = os.path.join(BASE, ".test_tmp")
os.makedirs(OUT, exist_ok=True)
APP_DIR = os.path.join(os.path.dirname(BASE), "app")             # 应用代码目录
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from db import DB
from floating import FloatingApp, _apply_image_opacity, SECTION_TITLES
from server import ApiServer
import tkinter as tk

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


def write_png(path, w, h, pixel_rgb):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    raw = b""
    row = bytes(pixel_rgb) * w
    for _ in range(h):
        raw += b"\x00" + row
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8bit RGB
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
           chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def make_jpg(path, w, h, rgb):
    """用 Windows GDI+ 把纯色 PNG 转成真实 JPEG 文件（独立实现，不依赖 app.winimg）。"""
    import ctypes
    import ctypes.wintypes as wt
    try:
        g = ctypes.WinDLL("gdiplus")

        class StartupInput(ctypes.Structure):
            _fields_ = [("GdiplusVersion", wt.UINT), ("DebugEventCallback", ctypes.c_void_p),
                        ("SuppressBackgroundThread", wt.BOOL), ("SuppressExternalCodecs", wt.BOOL)]

        g.GdiplusStartup.restype = wt.UINT
        g.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t),
                                     ctypes.POINTER(StartupInput), ctypes.c_void_p]
        g.GdiplusShutdown.argtypes = [ctypes.c_size_t]
        g.GdipCreateBitmapFromFile.restype = wt.UINT
        g.GdipCreateBitmapFromFile.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
        g.GdipSaveImageToFile.restype = wt.UINT
        g.GdipSaveImageToFile.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                          ctypes.c_void_p, ctypes.c_void_p]
        g.GdipDisposeImage.restype = wt.UINT
        g.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        png_tmp = path + ".src.png"
        write_png(png_tmp, w, h, rgb)
        tok = ctypes.c_size_t()
        si = StartupInput(1, None, False, False)
        if g.GdiplusStartup(ctypes.byref(tok), ctypes.byref(si), None) != 0:
            return False
        try:
            bmp = ctypes.c_void_p()
            if g.GdipCreateBitmapFromFile(png_tmp, ctypes.byref(bmp)) != 0 or not bmp.value:
                return False
            try:
                # ImageFormatJPEG {557CF401-1A04-11D3-9A73-0000F81EF32E}
                jpeg_clsid = (ctypes.c_byte * 16)(
                    0x01, 0xf4, 0x7c, 0x55, 0x04, 0x1a, 0xd3, 0x11,
                    0x9a, 0x73, 0x00, 0x00, 0xf8, 0x1e, 0xf3, 0x2e)
                if g.GdipSaveImageToFile(bmp, path, jpeg_clsid, None) != 0:
                    return False
                return os.path.isfile(path) and os.path.getsize(path) > 0
            finally:
                g.GdipDisposeImage(bmp)
        finally:
            g.GdiplusShutdown(tok)
    except Exception:
        return False


def panel_texts(widget):
    import re
    out = []
    for w in widget.winfo_children():
        try:
            t = w.cget("text")
            if t:
                out.append(re.sub(r"\s+", "", t))  # 去除空白（个别环境返回带空格）
        except Exception:
            pass
        out.extend(panel_texts(w))
    return "".join(out)


def find_button(widget, text):
    """递归查找文本匹配的 tk.Button 控件。"""
    for w in widget.winfo_children():
        try:
            if w.winfo_class() == "Button" and str(w.cget("text")) == text:
                return w
        except Exception:
            pass
        r = find_button(w, text)
        if r is not None:
            return r
    return None


def collapsed_texts(app):
    """折叠态画布上所有文本项内容。"""
    out = []
    for i in app.collapsed.find_all():
        try:
            t = app.collapsed.itemcget(i, "text")
            if t:
                out.append(t)
        except Exception:
            pass
    return out


def collect_texts(widget):
    """递归收集控件树中所有 -text 内容（容错）。"""
    out = []
    for c in widget.winfo_children():
        try:
            t = c.cget("text")
            if t:
                out.append(t)
        except Exception:
            pass
        out.extend(collect_texts(c))
    return out


def main():
    global PASS, FAIL
    print("== 悬浮窗新功能自测 ==")
    img_path = os.path.join(OUT, "test_bg.png")
    write_png(img_path, 8, 8, (200, 100, 50))  # 纯色小图

    db_path = os.path.join(OUT, "features.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    db = DB(db_path)
    db.add_task("晨跑三公里", "daily")
    db.add_task("写完项目周报", "today", due_date="2026-08-15")
    db.add_task("给客户回邮件", "today", due_date="2026-08-15")
    db.add_task("准备演示PPT", "tomorrow", due_date="2026-08-16")
    server = ApiServer("127.0.0.1", 0, db,
                       {"host": "127.0.0.1", "port": 0, "db_path": db_path,
                        "_config_path": os.path.join(OUT, "cfgF.json")})
    server.start()
    app = FloatingApp("http://127.0.0.1:%d" % server.bound_port)
    app._fetch = lambda: None  # 禁用后台 worker 的真实拉取，避免覆盖测试注入的数据

    def step():
        try:
            # 载入数据
            import queue as qq
            app._fetch()
            try:
                while True:
                    it = app.q.get_nowait()
                    if it[0] == "data":
                        _, groups, stats, done = it
                        app.groups = {k: groups.get(k, []) for k, _ in SECTION_TITLES}
                        app.stats = stats
                        app.done_today = done
                        app.connected = True
            except qq.Empty:
                pass
            # 完成一个今日任务
            done = set(app.done_today)
            done.add(db.get_task(2)["id"])
            app.done_today = done
            app._render_collapsed()
            app.root.update()
            time.sleep(0.1)
            app.root.update()

            # ---- 1. 折叠态显示今日待完成标题（逐行 + 右侧倒计时） ----
            txts = collapsed_texts(app)
            check("折叠态含「今日待完成」标题", any("今日待完成" in t for t in txts), str(txts))
            check("折叠态显示待办任务标题", any("给客户回邮件" in t for t in txts), str(txts))
            check("已完成的今日任务不显示", not any("写完项目周报" in t for t in txts), str(txts))
            check("折叠态高度随行数自适应", app.root.winfo_height() >= 100, str(app.root.winfo_height()))
            # 两列表头：今日待完成 | 截止时间（同一行不重叠，任务行在下方）
            cd_hdr_txt = app.collapsed.itemcget(app._cd_header, "text")
            check("折叠态含「截止时间」表头", cd_hdr_txt == "截止时间", cd_hdr_txt)
            hdr_y = app.collapsed.coords(app._t_pending)[1]
            cdh_y = app.collapsed.coords(app._cd_header)[1]
            check("表头同一行不重叠", abs(hdr_y - cdh_y) < 2, "%s vs %s" % (hdr_y, cdh_y))
            if app._collapsed_cd:
                row_y = app._collapsed_cd[0][1]
                check("任务行位于表头下方", row_y > cdh_y + 10,
                      "row_y=%s hdr_y=%s" % (row_y, cdh_y))
            # 表头与首行文本 bbox 不重叠
            hdr_box = app.collapsed.bbox(app._t_pending)
            row_items = [i for i in app.collapsed.find_all()
                         if str(app.collapsed.type(i)) == "text"
                         and str(app.collapsed.itemcget(i, "text")).startswith("•")]
            if hdr_box and row_items:
                first_box = app.collapsed.bbox(row_items[0])
                check("表头与首行不重叠(bbox)", hdr_box[3] <= first_box[1] + 1,
                      "hdr_bottom=%d first_top=%d" % (hdr_box[3], first_box[1]))

            # ---- 2. 窗口透明度 ----
            alpha = app.root.attributes("-alpha")
            check("默认窗口透明度≈0.72", abs(alpha - 0.72) < 0.02, str(alpha))
            app._on_opacity_change("50")
            alpha = app.root.attributes("-alpha")
            check("透明度可调(0.5)", abs(alpha - 0.5) < 0.02, str(alpha))
            app._on_opacity_change("72")

            # ---- 3. 背景图片 ----
            app.bg_image_path = img_path
            app.bg_opacity = 1.0
            app._load_bg_image()
            check("背景图片加载成功", app._bg_display is not None and app._bg_item is not None, "")
            if app._bg_display:
                before = app._bg_display.get(0, 0)
                app.bg_opacity = 0.3
                _apply_image_opacity(app._bg_display, 0.3)
                after = app._bg_display.get(0, 0)
                changed = any(abs(a - b) > 5 for a, b in zip(after[:3], before[:3]))
                check("图片透明度混合生效(像素改变)", changed, "before=%s after=%s" % (before[:3], after[:3]))
            app.bg_opacity = 0.7  # 还原默认，避免污染配置文件
            app._remove_bg_image()
            check("移除背景图片", app._bg_display is None, "")

            # ---- 3b. 「🖼 导入背景图片」完整流程（回归：曾因 self.opt_panel 不存在而点击无反应） ----
            import tkinter.filedialog as _tfd
            import tkinter.messagebox as _tmb
            cfg_path3 = os.path.join(ROOT, "config.json")
            with open(cfg_path3, "r", encoding="utf-8") as f:
                orig_cfg3 = json.load(f)
            real_fd = _tfd.askopenfilename
            real_warn = _tmb.showwarning
            warned = []
            try:
                app.bg_image_path = ""
                app.bg_opacity = 0.7
                # ① 点面板上的「🖼 导入背景图片」按钮 → 选图 → 加载 + 保存配置
                _tfd.askopenfilename = lambda **kw: img_path
                btn = find_button(app.image_panel, "🖼 导入背景图片")
                check("导入背景图片:面板按钮存在", btn is not None, "")
                btn.invoke()
                app.root.update()
                check("导入背景图片:选图后成功加载", app._bg_display is not None and app._bg_item is not None, "")
                with open(cfg_path3, "r", encoding="utf-8") as f:
                    cfg3 = json.load(f)
                check("导入背景图片:路径已保存到配置", cfg3.get("bg_image") == img_path,
                      str(cfg3.get("bg_image")))
                # ② 取消对话框 → 状态不变
                _tfd.askopenfilename = lambda **kw: ""
                app._import_bg_image()
                app.root.update()
                check("导入背景图片:取消后保留原图", app._bg_display is not None, "")
                with open(cfg_path3, "r", encoding="utf-8") as f:
                    cfg3b = json.load(f)
                check("导入背景图片:取消后配置不变", cfg3b.get("bg_image") == img_path,
                      str(cfg3b.get("bg_image")))
                # ③ 选择损坏文件 → 警告 + 回退旧图
                bad_path = os.path.join(OUT, "bad.png")
                with open(bad_path, "wb") as f:
                    f.write(b"this is not a real png file \x00\x01\x02")
                _tmb.showwarning = lambda *a, **k: warned.append(a)
                _tfd.askopenfilename = lambda **kw: bad_path
                app._import_bg_image()
                app.root.update()
                check("导入背景图片:坏图弹出警告", len(warned) == 1, str(warned))
                check("导入背景图片:坏图后回退旧图", app.bg_image_path == img_path,
                      str(app.bg_image_path))
            finally:
                _tfd.askopenfilename = real_fd
                _tmb.showwarning = real_warn
                app.bg_image_path = ""
                app._load_bg_image()
                with open(cfg_path3, "w", encoding="utf-8") as f:
                    json.dump(orig_cfg3, f, ensure_ascii=False, indent=2)

            # ---- 3c. JPG 背景图（回归：Tk 不原生支持 JPEG，需 GDI+ 解码） ----
            jpg_path = os.path.join(OUT, "test_bg.jpg")
            jpg_ok = make_jpg(jpg_path, 8, 8, (200, 100, 50))
            check("JPG:测试图片生成成功", jpg_ok, jpg_path)
            if jpg_ok:
                with open(cfg_path3, "r", encoding="utf-8") as f:
                    orig_cfg3c = json.load(f)
                try:
                    app.bg_image_path = ""
                    app.bg_opacity = 1.0
                    _tfd.askopenfilename = lambda **kw: jpg_path
                    app._import_bg_image()
                    app.root.update()
                    check("JPG:导入后成功加载",
                          app._bg_display is not None and app._bg_item is not None, "")
                    if app._bg_display:
                        px = app._bg_display.get(0, 0)
                        check("JPG:像素色彩还原(容差±15)",
                              all(abs(a - b) <= 15 for a, b in zip(px[:3], (200, 100, 50))),
                              str(px))
                    with open(cfg_path3, "r", encoding="utf-8") as f:
                        cfg3c = json.load(f)
                    check("JPG:路径已保存到配置", cfg3c.get("bg_image") == jpg_path,
                          str(cfg3c.get("bg_image")))
                    # 大图自动降采样（>800 像素）
                    big_jpg = os.path.join(OUT, "test_big.jpg")
                    make_jpg(big_jpg, 1600, 1200, (80, 160, 240))
                    app.bg_image_path = big_jpg
                    app._load_bg_image()
                    app.root.update()
                    if app._bg_display:
                        check("JPG:大图自动降采样≤800",
                              max(app._bg_display.width(), app._bg_display.height()) <= 800,
                              str((app._bg_display.width(), app._bg_display.height())))
                    else:
                        check("JPG:大图自动降采样≤800", False, "big jpg 未加载")
                finally:
                    app.bg_image_path = ""
                    app.bg_opacity = 0.7
                    app._load_bg_image()
                    with open(cfg_path3, "w", encoding="utf-8") as f:
                        json.dump(orig_cfg3c, f, ensure_ascii=False, indent=2)

            # ---- 3d. 壁纸引擎：界面1置底不遮挡 / 界面2 / 搜索面板 / 自适应 ----
            if jpg_ok:
                big_jpg = os.path.join(OUT, "test_big.jpg")
                make_jpg(big_jpg, 1600, 1200, (80, 160, 240))
                app.bg_image_path = big_jpg
                app.bg_opacity = 0.7
                app._load_bg_image()
                app.root.update()
                # 界面1：背景图必须是最底层（不再遮挡文字/按钮）
                if app._bg_item is not None:
                    first = app.collapsed.find_all()[0]
                    check("壁纸:界面1背景图置底", first == app._bg_item,
                          "first=%s bg=%s" % (first, app._bg_item))
                    cx, cy = app.collapsed.coords(app._bg_item)
                    w, h = app.collapsed.winfo_width(), app.collapsed.winfo_height()
                    check("壁纸:界面1背景图居中",
                          abs(cx - w / 2) < 2 and abs(cy - h / 2) < 2,
                          "%s,%s vs %s,%s" % (cx, cy, w / 2, h / 2))
                    # cover 自适应：缩放后图片不小于画布（不露底）
                    p1 = app._bg_surf_collapsed.get("photo")
                    check("壁纸:背景图cover填充画布",
                          p1 is not None and p1.width() >= w and p1.height() >= h,
                          "photo=%s canvas=%s" % ((p1.width(), p1.height()) if p1 else None, (w, h)))
                else:
                    check("壁纸:界面1背景图置底", False, "bg item 未创建")
                # 界面2：展开后内层画布也有背景图且置底
                app.set_expanded(True)
                app.root.update()
                it2 = app._bg_surf_expand.get("item")
                if it2 is not None:
                    first2 = app.canvas.find_all()[0]
                    check("壁纸:界面2背景图存在且置底", first2 == it2,
                          "first=%s bg=%s" % (first2, it2))
                else:
                    check("壁纸:界面2背景图存在且置底", False, "expand bg 未创建")
                app.set_expanded(False)
                app.root.update()
                # 搜索面板：打开后结果区也有背景图且置底
                app._toggle_panel("search")
                app.root.update()
                time.sleep(0.15)
                app.root.update()
                it3 = app._bg_surf_search.get("item")
                if it3 is not None:
                    first3 = app._search_canvas.find_all()[0]
                    check("壁纸:搜索面板背景图存在且置底", first3 == it3,
                          "first=%s bg=%s" % (first3, it3))
                else:
                    check("壁纸:搜索面板背景图存在且置底", False, "search bg 未创建")
                app._toggle_panel("search")
                app.root.update()
                # 自适应：改变窗口大小 → 背景图随画布重新居中且重新降采样
                app.root.geometry("380x220")
                app.root.update()
                time.sleep(0.1)
                app.root.update()
                cx, cy = app.collapsed.coords(app._bg_item)
                w, h = app.collapsed.winfo_width(), app.collapsed.winfo_height()
                check("壁纸:窗口缩放后背景图随动",
                      abs(cx - w / 2) < 2 and abs(cy - h / 2) < 2,
                      "%s,%s vs %s,%s" % (cx, cy, w / 2, h / 2))
                p2 = app._bg_surf_collapsed.get("photo")
                check("壁纸:缩放后重新降采样",
                      p2 is not None and app._bg_surf_collapsed.get("factor", 1) >= 2,
                      "factor=%s photo=%s" % (app._bg_surf_collapsed.get("factor"),
                                              (p2.width(), p2.height()) if p2 else None))
                # 移除后各面板清空
                app._remove_bg_image()
                app.root.update()
                check("壁纸:移除后各面板清空",
                      app._bg_display is None
                      and app._bg_surf_expand.get("item") is None
                      and app._bg_surf_search.get("item") is None, "")

            # ---- 4. 功能按钮（齿轮/图片/扳手，靠右）面板跟随 ----
            app._sync_panels()
            app.root.update()
            rx, ry = app.root.winfo_x(), app.root.winfo_y()
            rw, rh = app.root.winfo_width(), app.root.winfo_height()
            bx = app.btn_panel.winfo_x()
            by = app.btn_panel.winfo_y()
            bw = app.btn_panel.winfo_width()
            check("齿轮/图片/扳手按钮均存在",
                  hasattr(app, "_gear_canvas") and hasattr(app, "_image_canvas")
                  and hasattr(app, "_wrench_canvas"), "")
            btns = [app._wrench_canvas, app._image_canvas, app._gear_canvas]
            app.root.update()
            xs = [b.winfo_rootx() for b in btns]
            check("按钮顺序：扳手-图片-齿轮（左→右）", xs[0] < xs[1] < xs[2], str(xs))
            check("按钮尺寸统一(32×32)", all(b.winfo_width() == b.winfo_height() == 32 for b in btns),
                  str([(b.winfo_width(), b.winfo_height()) for b in btns]))
            check("功能按钮出现在悬浮窗下方", by >= ry + rh - 4, "btn_y=%d win_y+h=%d" % (by, ry + rh))
            check("功能按钮靠右对齐", abs((bx + bw) - (rx + rw - 6)) <= 4,
                  "btn_right=%d win_right=%d" % (bx + bw, rx + rw - 6))
            # 移动悬浮窗 → 面板跟随
            app.root.geometry("+%d+%d" % (rx + 60, ry + 40))
            app.root.update()
            time.sleep(0.1)
            app.root.update()
            app._sync_panels()
            app.root.update()
            bx2 = app.btn_panel.winfo_x()
            by2 = app.btn_panel.winfo_y()
            rx2, ry2 = app.root.winfo_x(), app.root.winfo_y()
            check("移动后面板跟随(靠右)", abs(bx2 + bw - (rx2 + rw - 6)) <= 4,
                  "%d vs %d" % (bx2 + bw, rx2 + rw - 6))
            check("移动后面板跟随(垂直)", abs(by2 - (ry2 + rh)) <= 6, "%d vs %d" % (by2, ry2 + rh))
            # 三个面板各自开关
            app._toggle_panel("gear")
            app.root.update()
            check("点击齿轮展开齿轮面板", app._panel_open["gear"] and app.gear_panel.state() != "withdrawn", "")
            gear_text = panel_texts(app.gear_panel)
            check("齿轮面板含还原位置/自启动/游戏模式/退出",
                  all(k in gear_text for k in ("还原到原来位置", "开机自启动", "游戏模式", "退出")), gear_text)
            check("齿轮面板已移除最小化到托盘", "最小化到托盘" not in gear_text, gear_text)
            app._toggle_panel("gear")
            app.root.update()
            check("再次点击收起齿轮面板", not app._panel_open["gear"], "")
            app._toggle_panel("image")
            app.root.update()
            check("点击图片按钮展开图片面板", app.image_panel.state() != "withdrawn", "")
            img_text = panel_texts(app.image_panel)
            check("图片面板含透明/背景图选项",
                  "窗口透明度" in img_text and "背景图片透明度" in img_text
                  and "导入背景图片" in img_text and "移除背景图片" in img_text, img_text)
            app._toggle_panel("image")
            app.root.update()
            app._toggle_panel("wrench")
            app.root.update()
            wr_text = panel_texts(app.wrench_panel)
            check("扳手面板含设置页/历史",
                  "打开后台设置页面" in wr_text and "查看历史完成任务" in wr_text, wr_text)
            app._toggle_panel("wrench")
            app.root.update()
            # 面板互斥：点击一个按钮时收起其他面板
            app._toggle_panel("gear")
            app.root.update()
            app._toggle_panel("image")
            app.root.update()
            check("面板互斥：开图片收起齿轮",
                  not app._panel_open["gear"] and app._panel_open["image"], str(app._panel_open))
            app._toggle_panel("wrench")
            app.root.update()
            check("面板互斥：开扳手收起图片",
                  not app._panel_open["image"] and app._panel_open["wrench"], str(app._panel_open))
            app._toggle_panel("wrench")
            app.root.update()
            check("再次点击当前按钮全部收起", not any(app._panel_open.values()), str(app._panel_open))
            # 悬浮窗上的刷新按钮存在（折叠态与展开态）
            check("悬浮窗含刷新按钮", hasattr(app, "btn_refresh") and hasattr(app, "btn_refresh_hdr"), "")

            # ---- 5. 抗闪烁守卫 ----
            app.set_expanded(True)
            app._render_list()
            app.root.update()
            geo1 = app.root.geometry()
            app._autosize()
            app.root.update()
            geo2 = app.root.geometry()
            check("尺寸未变时 autosize 不重复设置几何", geo1 == geo2, "%s vs %s" % (geo1, geo2))
            calls = {"n": 0}

            def fake_render():
                calls["n"] += 1

            app._render_list = fake_render
            groups = dict(app.groups)
            done = set(app.done_today)
            for _ in range(2):
                app.q.put(("data", groups, app.stats, done))
                app._poll_queue_once()
            check("数据未变化不重建列表(2次轮询仅1次渲染)", calls["n"] == 1, "calls=%d" % calls["n"])
            groups2 = {k: (list(v) + [{"id": 99999, "title": "新任务", "note": "", "due_date": "2026-08-15"}]) if k == "today" else v
                       for k, v in groups.items()}
            app.q.put(("data", groups2, app.stats, done))
            app._poll_queue_once()
            check("数据变化时重建列表", calls["n"] == 2, "calls=%d" % calls["n"])
            del app._render_list  # 恢复真实的 _render_list（后续测试需要）

            # ---- 6. 手动调整尺寸不回弹 ----
            app.set_expanded(False)
            app.root.update()
            h0 = app.root.winfo_height()
            app._user_resized = True
            app.root.geometry("280x%d+200+80" % (h0 + 60))
            app.root.update()
            h_manual = app.root.winfo_height()
            # 相同数据再刷新一次 → 高度应保持
            app.q.put(("data", dict(app.groups), app.stats, set(app.done_today)))
            app._poll_queue_once()
            app.root.update()
            check("折叠态手动拖拽后刷新不回弹", app.root.winfo_height() == h_manual,
                  "h=%d expect=%d" % (app.root.winfo_height(), h_manual))
            # 数据变化 → 恢复自动适配
            app._user_resized = False
            app.q.put(("data", groups2, app.stats, set(app.done_today)))
            app._poll_queue_once()
            app.root.update()
            check("数据变化后恢复自动适配", app.root.winfo_height() >= 90, str(app.root.winfo_height()))

            # 展开态手动调整后 autosize 不回弹
            app.set_expanded(True)
            app.root.update()
            h_exp = app.root.winfo_height()
            app.root.geometry("360x%d+200+80" % (h_exp + 100))
            app.root.update()
            app._user_resized = True
            h_manual2 = app.root.winfo_height()
            app._autosize()
            app.root.update()
            check("展开态手动拖拽后 autosize 不回弹", app.root.winfo_height() == h_manual2,
                  "h=%d expect=%d" % (app.root.winfo_height(), h_manual2))

            # ---- 7. 折叠态：字号与多任务显示 ----
            app.set_expanded(False)
            # 加入 15 个今日待办
            import copy
            g3 = copy.deepcopy(app.groups)
            for i in range(15):
                g3["today"].append({"id": 2000 + i, "title": "待办任务第 %02d 项" % i, "note": "",
                                    "due_date": "2026-08-15", "remind_at": None, "deadline": None})
            app.q.put(("data", g3, app.stats, set()))
            app._poll_queue_once()
            app.root.update()
            txts7 = collapsed_texts(app)
            title_items = [i for i in app.collapsed.find_all()
                           if str(app.collapsed.type(i)) == "text" and "• " in str(app.collapsed.itemcget(i, "text"))]
            check("折叠态任务标题字号为9", bool(title_items) and "9" in str(app.collapsed.itemcget(title_items[0], "font")),
                  str([app.collapsed.itemcget(i, "font") for i in title_items[:1]]))
            check("多任务时显示前10条+剩余统计",
                  any("还有 9 项" in t for t in txts7) and any("待办任务第 05 项" in t for t in txts7),
                  str(txts7)[:160])
            check("窗口高度随多任务增高", app.root.winfo_height() >= 200, str(app.root.winfo_height()))

            # ---- 8. 折叠态吸附隐藏 / 点击恢复 / 还原位置 ----
            app._user_resized = False
            sw = app.root.winfo_screenwidth()
            x0 = sw - 400
            app.root.geometry("280x180+%d+80" % x0)
            app.root.update()
            # 模拟拖拽向右 → 窗口右缘贴近屏幕右缘（sw-13）→ 自动吸附
            y0 = app.root.winfo_y()
            app._press = (x0, y0, x0, y0)
            app._mode = "drag"
            evt = type("E", (), {"x_root": x0 + 107, "y_root": y0})()  # 右移107 → 右缘=sw-13
            app._on_b1_motion(evt)
            app.root.update()
            check("拖拽靠近屏幕右缘自动吸附隐藏", app.docked, "docked=%s" % app.docked)
            if app.docked:
                check("吸附后仅露侧边提示条", app.root.winfo_x() == sw - 8, str(app.root.winfo_x()))
                app._undock()
                app.root.update()
                check("点击隐藏位置恢复原位", not app.docked and app.root.winfo_x() == sw - 293,
                      str(app.root.winfo_x()))
            # 还原到原来位置
            app._save_pos()  # 记录当前（恢复后）位置
            app.root.geometry("280x180+60+120")
            app.root.update()
            app._restore_position()
            app.root.update()
            check("还原到原来位置", (app.root.winfo_x(), app.root.winfo_y()) == (sw - 293, 80),
                  "%s vs %s" % ((app.root.winfo_x(), app.root.winfo_y()), (sw - 293, 80)))

            # ---- 9. 折叠态截止倒计时 / 未完成数 / 详情时间 ----
            from datetime import datetime, timedelta
            now = datetime.now()
            g4 = dict(app.groups)
            g4["today"] = [
                {"id": 9001, "title": "即将截止任务", "scope": "today", "due_date": "2026-08-15",
                 "note": "", "start_at": None, "deadline": None,
                 "effective_deadline": (now + timedelta(hours=2, minutes=30)).strftime("%Y-%m-%dT%H:%M")},
            ]
            st4 = dict(app.stats or {})
            st4["overdue_count"] = 1
            app.q.put(("data", g4, st4, set()))
            app._poll_queue_once()
            app.root.update()
            app._update_collapsed_countdowns()
            app.root.update()
            cds = app._collapsed_cd
            check("折叠态逐行倒计时存在", len(cds) == 1 and cds[0][2] is not None, str(cds))
            cd_txt = app.collapsed.itemcget(cds[0][0], "text")
            import re as _re2
            check("折叠态无天数时格式 h:m:s", bool(_re2.fullmatch(r"\d{2}:\d{2}:\d{2}", cd_txt)), cd_txt)
            sum_txt = app.collapsed.itemcget(app._cd_summary_item, "text")
            check("折叠态显示未完成数", "未完成 1" in sum_txt, sum_txt)
            # 倒计时每秒刷新
            changed = False
            for _ in range(4):
                time.sleep(1.1)
                app.root.update()
                cd2 = app.collapsed.itemcget(cds[0][0], "text")
                if cd2 != cd_txt:
                    changed = True
                    break
            check("折叠态倒计时每秒刷新", changed, "%s -> %s" % (cd_txt, cd2))

            # 展开态详情显示开始/截止时间
            g5 = dict(app.groups)
            g5["today"] = [{"id": 9002, "title": "详情任务", "scope": "today",
                            "note": "备注内容", "start_at": "2026-08-15T09:00",
                            "deadline": None, "effective_deadline": "2026-08-16T09:00",
                            "remind_at": None}]
            app.q.put(("data", g5, st4, set()))
            app._poll_queue_once()
            app.set_expanded(True)
            app._row_detail[9002] = True
            app._render_list()
            app.root.update()
            labels = [c.cget("text") for c, _ in app._wrap_labels]
            check("展开详情显示开始时间", any("开始" in t for t in labels), str(labels))
            check("展开详情显示截止时间", any("截止" in t for t in labels), str(labels))
            check("展开详情显示备注", any("备注内容" in t for t in labels), str(labels))

            # ---- 10. 任务行右侧截止倒计时 / 提醒弹窗 ----
            import re as _re
            from datetime import datetime, timedelta
            now = datetime.now()
            g6 = dict(app.groups)
            g6["today"] = [
                {"id": 9101, "title": "两小时后截止", "scope": "today", "due_date": "2026-08-15",
                 "note": "", "start_at": None, "deadline": None,
                 "effective_deadline": (now + timedelta(hours=2, minutes=3, seconds=4)).strftime("%Y-%m-%dT%H:%M:%S")},
                {"id": 9102, "title": "已逾期任务", "scope": "today", "due_date": "2026-08-15",
                 "note": "", "start_at": None, "deadline": None,
                 "effective_deadline": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")},
                {"id": 9103, "title": "日常无倒计时", "scope": "daily",
                 "note": "", "start_at": None, "deadline": None, "effective_deadline": None},
            ]
            app.q.put(("data", g6, app.stats, set()))
            app._poll_queue_once()
            app.set_expanded(True)
            app.root.update()
            app._update_row_countdowns()  # 手动触发一次倒计时刷新（tick 每秒自动执行）
            app.root.update()
            cds = app._row_countdowns
            check("任务行有截止倒计时", 9101 in cds and 9102 in cds, str(list(cds.keys())))
            check("日常任务无行内倒计时", 9103 not in cds, "")
            lbl1 = cds[9101][0].cget("text")
            check("无天数时格式为 h:m:s", bool(_re.fullmatch(r"⏰ \d{2}:\d{2}:\d{2}", lbl1)), lbl1)
            lbl2 = cds[9102][0].cget("text")
            check("逾期显示已逾期", "已逾期" in lbl2, lbl2)
            time.sleep(1.1)
            app.root.update()
            lbl1b = cds[9101][0].cget("text")
            check("行倒计时每秒刷新", lbl1b != lbl1, "%s -> %s" % (lbl1, lbl1b))

            # 详情展开时倒计时不被遮挡：文本换行宽度 ≤ 倒计时左侧可用宽度
            g7 = dict(app.groups)
            g7["today"] = [{"id": 9201, "title": "长文本遮挡测试任务", "scope": "today",
                            "note": "这是一段非常长的备注内容用于验证展开详情时换行宽度会为右侧截止倒计时预留空间" * 3,
                            "start_at": None, "deadline": None,
                            "effective_deadline": (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                            "remind_at": None}]
            app.q.put(("data", g7, app.stats, set()))
            app._poll_queue_once()
            app._row_detail[9201] = True
            app._render_list()
            app.root.update()
            time.sleep(0.1)
            app.root.update()
            cd, _eff, _dt = app._row_countdowns.get(9201, (None, None, None))
            if cd:
                row = cd.master
                btns = [w for w in row.winfo_children() if isinstance(w, tk.Button)]
                check("倒计时在按钮左侧不被遮挡",
                      cd.winfo_x() + cd.winfo_width() <= btns[0].winfo_x() + 1,
                      "cd_right=%d btn_x=%d" % (cd.winfo_x() + cd.winfo_width(), btns[0].winfo_x()))
                avail = cd.winfo_x() - 8
                note_wraps = [c.cget("wraplength") for c, _ in app._wrap_labels
                              if str(c.cget("text")).startswith("💬")]
                check("详情文本换行适配（不遮挡倒计时）",
                      bool(note_wraps) and max(note_wraps) <= avail + 1,
                      "wrap=%s avail=%d" % (note_wraps, avail))
            else:
                check("倒计时在按钮左侧不被遮挡", False, "未找到倒计时")

            # 提醒弹窗：显示在悬浮窗上方 + X 关闭按钮
            app.root.geometry("280x180+300+300")
            app.root.update()
            app._show_reminders([{"title": "弹窗测试提醒"}])
            app.root.update()
            time.sleep(0.2)
            app.root.update()
            panels = {app.btn_panel, app.gear_panel, app.image_panel, app.wrench_panel}
            pops = [w for w in app.root.winfo_children()
                    if isinstance(w, tk.Toplevel) and w not in panels and w.winfo_ismapped()]
            pop = pops[-1] if pops else None
            if pop:
                ry = app.root.winfo_y()
                py = pop.winfo_y()
                check("提醒弹窗显示在悬浮窗上方", py < ry, "py=%d ry=%d" % (py, ry))

                def alltexts(w):
                    out = []
                    for c in w.winfo_children():
                        try:
                            t = c.cget("text")
                            if t:
                                out.append(t)
                        except Exception:
                            pass
                        out.extend(alltexts(c))
                    return out

                ptxt = alltexts(pop)
                check("提醒弹窗含 X 关闭按钮", any("✕" in t for t in ptxt), str(ptxt))
                check("提醒弹窗含任务标题", any("弹窗测试提醒" in t for t in ptxt), "")
                pop.destroy()
            else:
                check("提醒弹窗创建", False, "未找到弹窗")

            # ---- 11. 提醒全链路：到点 → worker 轮询 → 弹窗出现在悬浮窗上方 ----
            db.add_task("全链路提醒任务", "daily", remind_at="2000-01-01T09:00")
            app.root.geometry("280x180+400+400")
            app.root.update()
            found_pop = None
            for _ in range(16):  # worker 5 秒轮询周期，最多等 8 秒
                time.sleep(0.5)
                app.root.update()
                panels = {app.btn_panel, app.gear_panel, app.image_panel, app.wrench_panel}
                pops = [w for w in app.root.winfo_children()
                        if isinstance(w, tk.Toplevel) and w not in panels and w.winfo_ismapped()]
                if pops:
                    found_pop = pops[-1]
                    break
            if found_pop:
                ry = app.root.winfo_y()
                py = found_pop.winfo_y()
                check("提醒到达后弹窗出现且在悬浮窗上方", py < ry, "py=%d ry=%d" % (py, ry))
                found_pop.destroy()
            else:
                check("提醒到达后弹窗出现且在悬浮窗上方", False, "8 秒内未出现弹窗")

            # ---- 12b. 界面2分区折叠块 ----
            g8 = dict(app.groups)
            g8["today"] = [{"id": 9301, "title": "今日任务A", "scope": "today", "due_date": "2026-08-15",
                            "note": "", "start_at": None, "deadline": None, "effective_deadline": None}]
            g8["tomorrow"] = [{"id": 9302, "title": "明日任务B", "scope": "tomorrow", "due_date": "2026-08-16",
                               "note": "", "start_at": None, "deadline": None, "effective_deadline": None}]
            app.q.put(("data", g8, app.stats, set()))
            app._poll_queue_once()
            app.set_expanded(True)
            app.root.update()
            check("今日分区默认展开", app._section_open["today"] is True, "")
            check("明日分区默认收起", app._section_open["tomorrow"] is False, "")
            hdr_labels = [w for w in app.list_frame.winfo_children()
                          if isinstance(w, tk.Label)
                          and (w.cget("text").startswith("▸") or w.cget("text").startswith("▾"))]
            tom_hdr = next((w for w in hdr_labels if "明日任务" in w.cget("text")), None)
            if tom_hdr:
                n_before = len(app.list_frame.winfo_children())
                tom_hdr.event_generate("<Button-1>")
                app.root.update()
                check("点击分区标题展开该块", app._section_open["tomorrow"] is True
                      and len(app.list_frame.winfo_children()) > n_before,
                      "before=%d after=%d" % (n_before, len(app.list_frame.winfo_children())))
                app._toggle_section("tomorrow")
                app.root.update()
                check("再次点击分区收起", app._section_open["tomorrow"] is False)
            else:
                check("分区标题存在", False, "未找到明日任务分区标题")

            # ---- 12d. 大量任务时倒计时不卡顿（固定宽度 + 快速更新） ----
            g9 = dict(app.groups)
            g9["today"] = [{"id": 9400 + i, "title": "批量任务 %03d" % i, "scope": "today",
                            "due_date": "2026-08-15", "note": "", "start_at": None,
                            "deadline": None,
                            "effective_deadline": (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")}
                           for i in range(120)]
            app.q.put(("data", g9, app.stats, set()))
            app._poll_queue_once()
            app.set_expanded(True)
            app.root.update()
            time.sleep(0.2)
            app.root.update()
            cds120 = list(app._row_countdowns.values())
            check("大量任务均有倒计时", len(cds120) == 120, str(len(cds120)))
            w = cds120[0][0].cget("width")
            check("倒计时标签固定宽度(文本变化不回流)", bool(w) and int(w) >= 10, "width=%r" % w)
            t0 = time.time()
            app._update_row_countdowns()
            app.root.update()
            dt = time.time() - t0
            check("120行倒计时更新耗时<50ms", dt < 0.05, "%.1fms" % (dt * 1000))

            # ---- 12e. 放大镜搜索面板 ----
            check("放大镜搜索按钮存在", hasattr(app, "_search_canvas") and hasattr(app, "search_panel"), "")
            app._toggle_panel("search")  # 首次打开应自动加载全部任务
            app.root.update()
            for _ in range(30):  # 等自动加载完成
                app.root.update()
                if app._search_state["total"] > 0:
                    break
                time.sleep(0.1)
            app.root.update()
            check("点击放大镜展开搜索面板",
                  app._panel_open["search"] and app.search_panel.state() != "withdrawn", "")
            check("打开自动加载全部任务", app._search_state["total"] > 0,
                  str(app._search_state["total"]))
            rx = app.root.winfo_x()
            sx = app.search_panel.winfo_x()
            sw2 = app.search_panel.winfo_width()
            check("搜索面板显示在悬浮窗左侧", sx + sw2 <= rx + 2, "sx+sw=%d rx=%d" % (sx + sw2, rx))
            # 注入搜索结果（带当前请求序号）
            req_id = app._search_req_id
            app.q.put(("search", req_id, {"tasks": [
                {"id": 1, "title": "未完成任务A", "scope": "today", "done_count": 0, "last_completed_at": None},
                {"id": 2, "title": "已完成任务B", "scope": "week", "done_count": 1,
                 "last_completed_at": "2026-08-15T10:30:00"},
            ], "page": 1, "pages": 1, "total": 2}))
            app._poll_queue_once()
            app.root.update()
            res_texts = collect_texts(app._search_result)
            check("搜索结果显示未完成标记", any("○" in t and "未完成任务A" in t for t in res_texts), str(res_texts))
            check("搜索结果显示完成标记+时间",
                  any("✓" in t and "已完成任务B" in t for t in res_texts)
                  and any("10:30" in t for t in res_texts), str(res_texts))
            check("未完成任务不显示时间", not any("未完成任务A" in t and ":" in t for t in res_texts), str(res_texts))
            check("搜索分页信息显示", "第 1/1 页" in app._search_page_lbl.cget("text"),
                  app._search_page_lbl.cget("text"))
            # 过期响应（旧请求序号）应被丢弃
            app.q.put(("search", 999999, {"tasks": [
                {"id": 9, "title": "过期响应任务", "scope": "today", "done_count": 0,
                 "last_completed_at": None}], "page": 1, "pages": 1, "total": 1}))
            app._poll_queue_once()
            app.root.update()
            stale_texts = collect_texts(app._search_result)
            check("过期搜索响应被丢弃", "过期响应任务" not in str(stale_texts), str(stale_texts))
            # 分类筛选：点击「今日」→ 只显示今日任务
            app._search_set_cat("today")
            for _ in range(30):
                app.root.update()
                if app._search_state["total"] > 0 and "今日" in app._search_page_lbl.cget("text"):
                    break
                time.sleep(0.1)
            app.root.update()
            cat_texts = collect_texts(app._search_result)
            check("点击分类筛选生效(只显示今日)",
                  any("周报" in t for t in cat_texts) and not any("晨跑" in t for t in cat_texts),
                  str(cat_texts))
            # 即时搜索：切回「全部」分类后输入关键词（KeyRelease 防抖触发）
            app._search_set_cat("")
            for _ in range(30):
                app.root.update()
                if "全部" in app._search_page_lbl.cget("text") or app._search_state["total"] > 0:
                    break
                time.sleep(0.1)
            app.root.update()
            req_before = app._search_req_id
            app._search_input.delete(0, "end")
            app._search_input.insert(0, "晨跑")
            app._search_input.focus_force()
            app.root.update()
            app._search_input.event_generate("<KeyRelease>")
            if app._search_req_id == req_before:
                app._on_search_key(None)  # 合成事件在无焦点时可能不触发，直接模拟绑定回调
            for _ in range(40):
                app.root.update()
                if app._search_req_id > req_before and \
                        not any("周报" in t for t in collect_texts(app._search_result)):
                    break
                time.sleep(0.1)
            app.root.update()
            live_texts = collect_texts(app._search_result)
            check("搜索栏即时搜索生效（新请求已渲染、旧结果被替换）",
                  any("晨跑" in t for t in live_texts) and not any("周报" in t for t in live_texts),
                  str(live_texts))

            # ---- 12f. 面板与悬浮窗独立：拖动面板不改悬浮窗；面板边框独立缩放；X 关闭 ----
            if not app._panel_open.get("search"):
                app._toggle_panel("search")  # 确保面板处于打开状态
            app.root.update()
            time.sleep(0.1)
            app.root.update()
            win_geo_before = app.root.geometry()
            fake = type("E", (), {"widget": app._search_status,
                                  "x_root": app.root.winfo_x() - 50,
                                  "y_root": app.root.winfo_y() + 100})()
            app._mode = None
            app._on_press(fake)
            app.root.update()
            check("拖动搜索面板不改变悬浮窗(模式为空)", app._mode is None, str(app._mode))
            check("拖动搜索面板后悬浮窗几何不变", app.root.geometry() == win_geo_before,
                  "%s vs %s" % (app.root.geometry(), win_geo_before))
            # 面板自身边框缩放（独立）
            app._sync_panels()
            app.root.update()
            pw_before = app.search_panel.winfo_width()
            ev = type("E", (), {"x": 0, "y": 30,
                                "x_root": app.search_panel.winfo_x(),
                                "y_root": app.search_panel.winfo_y() + 30})()
            app._panel_press(ev)
            check("面板边缘按下进入缩放", app._panel_resize is not None, "")
            ev2 = type("E", (), {"x_root": app.search_panel.winfo_x() - 60,
                                 "y_root": app.search_panel.winfo_y() + 30})()
            app._panel_motion(ev2)
            app.search_panel.update_idletasks()
            app.root.update()
            dbg_geo = app.search_panel.geometry()
            dbg_req = app.search_panel.winfo_reqwidth()
            check("拖面板左缘改变面板宽度", app.search_panel.winfo_width() > pw_before,
                  "%d -> %d  resize=%s geo=%s req=%d" % (pw_before, app.search_panel.winfo_width(),
                                                         app._panel_resize, dbg_geo, dbg_req))
            app._panel_release(None)
            app.root.update()
            check("缩放后面板仍贴靠悬浮窗左侧",
                  app.search_panel.winfo_x() + app.search_panel.winfo_width() <= app.root.winfo_x() + 8,
                  "panel_right=%d win_x=%d" % (app.search_panel.winfo_x() + app.search_panel.winfo_width(),
                                               app.root.winfo_x()))
            # 拖动面板不会触发主窗口缩放模式
            app._panel_resize = None
            fake2 = type("E", (), {"widget": app._search_status,
                                   "x_root": app.root.winfo_x() + 5,
                                   "y_root": app.root.winfo_y() + 100})()
            app._on_press(fake2)
            app.root.update()
            check("面板区域按下不进主窗口缩放", app._mode is None and app._panel_resize is None, str(app._mode))
            # X 关闭
            app._close_search_panel()
            app.root.update()
            check("X 关闭搜索面板", not app._panel_open["search"]
                  and app.search_panel.state() == "withdrawn",
                  "open=%s state=%s" % (app._panel_open["search"], app.search_panel.state()))

            # ---- 12g. 分类自适应网格 + 结果可滚动分页 ----
            if not app._panel_open.get("search"):
                app._toggle_panel("search")
            app.root.update()
            cat_infos = [(val, b.grid_info().get("row"), b.grid_info().get("column"))
                         for val, b in app._cat_buttons.items()]
            check("分类为4×2自适应网格", len(cat_infos) == 8
                  and all(0 <= r <= 1 for _, r, _ in cat_infos)
                  and all(0 <= c <= 3 for _, _, c in cat_infos), str(cat_infos))
            cat_row = app._cat_buttons[""].master
            weights = [cat_row.grid_columnconfigure(c)["weight"] for c in range(4)]
            check("分类列等宽自适应", all(w == 1 for w in weights), str(weights))
            # 注入 12 条结果 → 全部渲染且结果区可滚动
            app._search_input.delete(0, "end")
            app._search_set_cat("")
            for _ in range(30):  # 等分类切换的真实请求完成
                app.root.update()
                if app._search_state["page"] == 1 and app._search_state["total"] > 0:
                    break
                time.sleep(0.1)
            app.root.update()
            app._search_req_id += 1  # 使此前的响应全部过期
            tasks12 = [{"id": 9500 + i, "title": "分页任务 %03d" % i, "scope": "today",
                        "done_count": 0, "last_completed_at": None} for i in range(12)]
            app.q.put(("search", app._search_req_id,
                       {"tasks": tasks12, "page": 1, "pages": 2, "total": 12}))
            app._poll_queue_once()
            app.root.update()
            app.search_panel.update_idletasks()
            rows12 = app._search_result.winfo_children()
            check("结果区渲染全部12行", len(rows12) == 12, str(len(rows12)))
            frac = app._search_canvas.yview()
            check("结果区可滚动(内容超出视口)", frac[0] == 0.0 and frac[1] < 1.0, str(frac))
            app._search_canvas.yview_moveto(1.0)
            app.root.update()
            frac_bottom = app._search_canvas.yview()
            check("滚动后可查看最后一行", frac_bottom[1] >= 0.99, str(frac_bottom))
            # 翻页：下一页 → 新结果且滚动回到顶部
            app._search_next.invoke()
            for _ in range(20):
                app.root.update()
                if app._search_state["page"] == 2:
                    break
                time.sleep(0.1)
            app.root.update()
            check("翻页到第2页", app._search_state["page"] == 2, str(app._search_state["page"]))
            app.search_panel.update_idletasks()
            frac2 = app._search_canvas.yview()
            check("翻页后滚动回到顶部", frac2[0] == 0.0, str(frac2))
            app._toggle_panel("search")
            app.root.update()
            app._toggle_panel("search")
            app.root.update()

            # ---- 12c. × 关闭确认（最小化/退出 + 下次不提醒） ----
            cfg_path = os.path.join(ROOT, "config.json")
            with open(cfg_path, "r", encoding="utf-8") as f:
                orig_cfg2 = json.load(f)
            try:
                cfg0 = dict(orig_cfg2)
                cfg0.pop("close_action", None)
                cfg0.pop("close_confirm", None)
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg0, f, ensure_ascii=False, indent=2)
                app._on_close_click()
                app.root.update()
                time.sleep(0.1)
                app.root.update()
                panels = {app.btn_panel, app.gear_panel, app.image_panel, app.wrench_panel,
                          app.search_panel}
                dlg = None
                for w in app.root.winfo_children():
                    if isinstance(w, tk.Toplevel) and w not in panels and w.winfo_ismapped():
                        try:
                            if any("关闭悬浮任务板" == str(c.cget("text"))
                                   for c in w.winfo_children() if c.cget("text")):
                                dlg = w
                        except Exception:
                            continue
                check("点击×弹出确认框", dlg is not None, "")

                def all_children(w):
                    out = []
                    for c in w.winfo_children():
                        out.append(c)
                        out.extend(all_children(c))
                    return out

                if dlg:
                    for c in all_children(dlg):
                        if isinstance(c, tk.Checkbutton):
                            c.select()
                    btns = [c for c in all_children(dlg) if isinstance(c, tk.Button)]
                    min_btn = next((b for b in btns if "最小化" in b.cget("text")), None)
                    if min_btn:
                        min_btn.invoke()
                        app.root.update()
                        time.sleep(0.2)
                        app.root.update()
                        with open(cfg_path, "r", encoding="utf-8") as f:
                            cfg2 = json.load(f)
                        check("确认框选择写入配置", cfg2.get("close_action") == "minimize"
                              and cfg2.get("close_confirm") is False,
                              "action=%s confirm=%s" % (cfg2.get("close_action"), cfg2.get("close_confirm")))
                        check("选择最小化后窗口隐藏",
                              app._hidden_in_tray and app.root.state() == "withdrawn", app.root.state())
                        app._restore_from_tray()
                        app.root.update()
                        # 已设置下次不提醒 → 点击×直接执行，不再弹框
                        app._on_close_click()
                        app.root.update()
                        time.sleep(0.2)
                        app.root.update()
                        dlg2 = None
                        for w in app.root.winfo_children():
                            if isinstance(w, tk.Toplevel) and w not in panels and w.winfo_ismapped():
                                if any("关闭悬浮任务板" == str(c.cget("text")) for c in w.winfo_children()
                                       if hasattr(c, "cget") and c.cget("text")):
                                    dlg2 = w
                        check("下次不提醒时点击×直接执行（无弹框）",
                              app._hidden_in_tray and dlg2 is None, "dlg2=%s" % (dlg2 is not None))
                        app._restore_from_tray()
                        app.root.update()
                    else:
                        check("确认框含最小化按钮", False, str([b.cget("text") for b in btns]))
            finally:
                clean = dict(orig_cfg2)
                clean.pop("close_action", None)
                clean.pop("close_confirm", None)
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(clean, f, ensure_ascii=False, indent=2)

            # ---- 12. 位置持久化：移动后保存，重启后恢复用户放置位置 ----
            cfg_path = os.path.join(ROOT, "config.json")
            with open(cfg_path, "r", encoding="utf-8") as f:
                orig_cfg = json.load(f)
            try:
                app.root.geometry("+777+333")
                app.root.update()
                app._save_pos()
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                check("移动后位置已保存到配置", cfg.get("gui_pos") == [777, 333],
                      str(cfg.get("gui_pos")))
                app2 = FloatingApp(app.base)
                app2._fetch = lambda: None
                app2.root.update()
                time.sleep(0.1)
                app2.root.update()
                check("重启后恢复用户放置位置",
                      (app2.root.winfo_x(), app2.root.winfo_y()) == (777, 333),
                      str((app2.root.winfo_x(), app2.root.winfo_y())))
                app2.quit()
                # 多显示器负坐标（左侧副屏）也应恢复
                cfg_neg = dict(orig_cfg)
                cfg_neg["gui_pos"] = [-1500, 300]
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg_neg, f, ensure_ascii=False)
                app3 = FloatingApp(app.base)
                app3._fetch = lambda: None
                app3.root.update()
                check("多显示器负坐标位置恢复",
                      (app3.root.winfo_x(), app3.root.winfo_y()) == (-1500, 300),
                      str((app3.root.winfo_x(), app3.root.winfo_y())))
                app3.quit()
            finally:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(orig_cfg, f, ensure_ascii=False, indent=2)
        finally:
            app.quit()
            server.stop()
            db.close()
        print("======================")
        print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
        sys.exit(1 if FAIL else 0)

    app.root.after(600, step)
    app.run()


if __name__ == "__main__":
    main()
