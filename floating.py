# -*- coding: utf-8 -*-
"""桌面悬浮窗（tkinter）：
- 折叠态：显示 日常/周常/月常 任务数与 今日完成/总完成 统计，并显示今日待完成任务标题；每 5 秒自动刷新；
- 透明化：整窗透明度可调（默认 72%，可透见桌面）；可导入自定义背景图片（PNG/GIF，自带透明度，默认透至桌面可见）；
- 左键单击：展开/收起任务明细（每条带「完成」按钮）；点击任务标题展开完整内容；
- 拖动：按住窗口任意位置（非按钮区域）可随意拖动；边缘 6px 内可拖拽缩放（手动调整的尺寸不会被自动适配回弹）；
  拖到屏幕右缘附近自动吸附隐藏到侧边，点击隐藏位置即可恢复原位；
- 底部功能栏：悬浮窗右下方三个统一大小的圆形按钮（**扳手**：打开设置页/查看历史；**图片**：窗口透明度/背景图片透明度/导入与移除背景图；**齿轮**：还原到原来位置/自启动/游戏模式/退出）；
  面板固定大小，不随悬浮窗缩放，始终跟随悬浮窗移动；
- 隐藏到侧边 / 游戏模式 / 系统托盘最小化 / 任务提醒响铃弹窗。
"""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import autostart
import tray as tray_mod

try:
    import winsound
except ImportError:  # 非 Windows
    winsound = None

try:
    import ctypes
    from ctypes import wintypes
except ImportError:
    ctypes = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

BG = "#20242e"
BG2 = "#2a2f3c"
FG = "#e9edf5"
FG_DIM = "#9aa4b5"
ACCENT = "#5b8cff"
DONE = "#35b26b"
WARN = "#ffb454"
RED = "#ff6b6b"

MIN_W, MIN_H = 200, 90
EDGE = 6
SLIVER = 8
COLLAPSED_W = 272
GEAR_KEY = "#010203"  # 功能按钮透明键色（圆形象四角透出桌面）


def _font(size=9, bold=False):
    fam = "Microsoft YaHei UI"
    try:
        import tkinter.font as tkfont
        root = tk._default_root
        if root is not None:
            fams = tkfont.families(root)
            if fam not in fams:
                fam = "TkDefaultFont" if fams and "TkDefaultFont" in fams else None
    except Exception:
        fam = None
    return (fam or "Microsoft YaHei UI", size, "bold" if bold else "normal")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def http_json(url, method="GET", payload=None, timeout=6):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def play_sound():
    """任务提醒响铃（Windows 系统提示音）。"""
    if winsound is None:
        return
    try:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass
    try:
        winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        pass


def foreground_fullscreen():
    """检测前台窗口是否覆盖全屏（游戏 / 全屏视频等）。"""
    if ctypes is None:
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        return w >= sw and h >= sh
    except Exception:
        return False


def _apply_image_opacity(photo, factor, base=(30, 34, 44)):
    """把图片按 factor 向面板底色混合，模拟「图片透明度」（factor=1 为原图）。
    仅当图片尺寸可控时使用（调用前先 subsample 限制尺寸）。"""
    if factor >= 0.999:
        return
    try:
        w, h = photo.width(), photo.height()
        rows = []
        for y in range(h):
            cells = []
            for x in range(w):
                px = photo.get(x, y)
                r, g, b = px[0], px[1], px[2]
                if max(r, g, b) > 255:  # 16 位通道归一化为 8 位
                    r, g, b = r * 255 // 65535, g * 255 // 65535, b * 255 // 65535
                r = int(r * factor + base[0] * (1 - factor))
                g = int(g * factor + base[1] * (1 - factor))
                b = int(b * factor + base[2] * (1 - factor))
                cells.append("#%02x%02x%02x" % (r, g, b))
            rows.append(" ".join(cells))
        # 每行单独用大括号分组（Tk photo put 的格式：{行1} {行2} ...）
        photo.put(" ".join("{%s}" % row for row in rows))
    except Exception:
        pass


SECTION_TITLES = [("today", "今日任务"), ("tomorrow", "明日任务"),
                  ("week", "周常任务"), ("month", "月常任务")]


class FloatingApp:
    def __init__(self, base_url, selftest=False, auto_close=0):
        self.base = base_url.rstrip("/")
        self.selftest = selftest
        self.auto_close = auto_close
        self.q = queue.Queue()
        self.stats = None
        self.groups = {k: [] for k, _ in SECTION_TITLES}
        self.done_today = set()
        self.expanded = False
        self.docked = False
        self._auto_docked = False
        self._hidden_in_tray = False
        self.connected = False
        self._stop = threading.Event()
        self._mode = None          # None / drag / resize
        self._press = None
        self._resize_start = None
        self._fs_state = False
        self._row_widgets = {}
        self._row_countdowns = {}  # 任务 id -> (Label, effective_deadline)，行内截止倒计时
        self._loading = False
        self._no_drag = set()
        self._row_detail = {}
        self._section_open = {"today": True, "tomorrow": False, "week": False, "month": False}
        self._close_noask_var = None
        self._wrap_labels = []
        self._last_canvas_w = 0
        self._list_sig = None
        self._pending_lines = 1
        self._collapsed_h = 0
        self._collapsed_cd = []        # 折叠态逐行倒计时: [(item_id, y, effective_deadline)]
        self._collapsed_cleanup = []   # 折叠态逐行项（重建时清理）
        self._cd_summary_item = None   # 未完成汇总行
        self._cd_header = None         # 截止时间列表头
        self._panel_open = {"gear": False, "image": False, "wrench": False, "search": False}
        self._bgop_after = None
        self._user_resized = False   # 用户手动调整过尺寸 → 暂停自动适配（直到数据变化）
        self._last_normal_pos = None # 最近一次正常位置（用于「还原到原来位置」）
        self.tray = None

        cfg = load_config()
        self.game_mode = bool(cfg.get("game_mode", False))
        self.window_opacity = float(cfg.get("window_opacity", 0.72))
        self.bg_image_path = cfg.get("bg_image") or ""
        self.bg_opacity = float(cfg.get("bg_opacity", 0.70))
        self._bg_display = None
        self._bg_item = None
        self._btn_min_win = None

        self.root = tk.Tk()
        self.root.title("悬浮任务板")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", self.window_opacity)
        except tk.TclError:
            pass
        self.root.configure(bg=BG)
        self._build_ui()
        self._restore_pos()
        self._load_bg_image()
        self._build_panels()
        self.root.after(200, self._sync_panels)

        if self.selftest:
            self.root.after(2500, self._selftest_finish)
        else:
            self._start_worker()
            if self.auto_close:
                self.root.after(int(self.auto_close * 1000), self._autoclose_finish)

        # 统一事件（bind_all 集中处理：拖动 / 点击切换 / 边缘缩放）
        self.root.bind_all("<Motion>", self._on_motion)
        self.root.bind_all("<ButtonPress-1>", self._on_press)
        self.root.bind_all("<B1-Motion>", self._on_b1_motion)
        self.root.bind_all("<ButtonRelease-1>", self._on_release)
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        self.root.bind("<Enter>", self._on_enter)
        self.root.bind("<Configure>", lambda e: self.root.after_idle(self._sync_panels))

        # 立即刷新一次
        if not self.selftest:
            self.trigger_refresh()
            self.root.after(300, self._poll_queue)
            self.root.after(1000, self._tick)

    # ---------------- UI ----------------
    def _build_ui(self):
        self.sidebar = tk.Frame(self.root, bg=ACCENT, width=6, cursor="hand2")
        self.sidebar.pack(side="right", fill="y")
        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(side="left", fill="both", expand=True)

        # ------- 折叠态：Canvas（支持背景图片 + 文字项） -------
        self.collapsed = tk.Canvas(self.body, bg=BG, highlightthickness=1,
                                   highlightbackground="#3a4152", cursor="hand2",
                                   width=COLLAPSED_W, height=120)
        self.collapsed.pack(fill="both", expand=True)
        self._t_title = self.collapsed.create_text(10, 8, anchor="w", text="☰ 悬浮任务板",
                                                   fill=ACCENT, font=_font(10, True))
        self.btn_min = tk.Label(self.collapsed, text="–", bg=BG, fg=FG_DIM, font=_font(14, True),
                                padx=8, cursor="hand2")
        self.btn_min.bind("<Button-1>", lambda e: self._minimize_to_tray())
        self.btn_min.bind("<Enter>", lambda e: self.btn_min.configure(fg=FG))
        self.btn_min.bind("<Leave>", lambda e: self.btn_min.configure(fg=FG_DIM))
        self._no_drag.add(self.btn_min)
        self._btn_min_win = self.collapsed.create_window(COLLAPSED_W - 36, 10, anchor="ne",
                                                         window=self.btn_min)
        self.btn_refresh = tk.Label(self.collapsed, text="⟳", bg=BG, fg=FG_DIM, font=_font(12, True),
                                    padx=6, cursor="hand2")
        self.btn_refresh.bind("<Button-1>", lambda e: self.trigger_refresh())
        self.btn_refresh.bind("<Enter>", lambda e: self.btn_refresh.configure(fg=FG))
        self.btn_refresh.bind("<Leave>", lambda e: self.btn_refresh.configure(fg=FG_DIM))
        self._no_drag.add(self.btn_refresh)
        self._btn_refresh_win = self.collapsed.create_window(COLLAPSED_W - 68, 10, anchor="ne",
                                                             window=self.btn_refresh)
        self.btn_exit = tk.Label(self.collapsed, text="×", bg=BG, fg=FG_DIM, font=_font(13, True),
                                 padx=8, cursor="hand2")
        self.btn_exit.bind("<Button-1>", lambda e: self._on_close_click())
        self.btn_exit.bind("<Enter>", lambda e: self.btn_exit.configure(fg=RED))
        self.btn_exit.bind("<Leave>", lambda e: self.btn_exit.configure(fg=FG_DIM))
        self._no_drag.add(self.btn_exit)
        self._btn_exit_win = self.collapsed.create_window(COLLAPSED_W - 4, 10, anchor="ne",
                                                          window=self.btn_exit)
        self._t_stat1 = self.collapsed.create_text(10, 30, anchor="w", text="", fill=FG,
                                                   font=_font(9))
        self._t_stat2 = self.collapsed.create_text(10, 50, anchor="w", text="", fill=FG_DIM,
                                                   font=_font(9))
        self._t_pending = self.collapsed.create_text(10, 72, anchor="nw", text="", fill=FG_DIM,
                                                     font=_font(8))
        self.collapsed.bind("<Configure>", self._on_collapsed_configure)

        # ------- 展开面板 -------
        self.expand_frame = tk.Frame(self.body, bg=BG, highlightthickness=1,
                                     highlightbackground="#3a4152")
        self.header = tk.Frame(self.expand_frame, bg=BG2, cursor="fleur", height=30)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        hdr = tk.Label(self.header, text="☰ 悬浮任务板", bg=BG2, fg=FG,
                       font=_font(9, True), anchor="w", padx=10)
        hdr.pack(side="left", fill="x", expand=True)
        self.btn_close = tk.Label(self.header, text="×", bg=BG2, fg=FG_DIM, font=_font(13, True),
                                  padx=10, cursor="hand2")
        self.btn_close.pack(side="right")
        self.btn_close.bind("<Button-1>", lambda e: self._on_close_click())
        self.btn_close.bind("<Enter>", lambda e: self.btn_close.configure(fg=RED))
        self.btn_close.bind("<Leave>", lambda e: self.btn_close.configure(fg=FG_DIM))
        self._no_drag.add(self.btn_close)
        self.btn_min_hdr = tk.Label(self.header, text="–", bg=BG2, fg=FG_DIM, font=_font(13, True),
                                    padx=10, cursor="hand2", takefocus=0)
        self.btn_min_hdr.pack(side="right")
        self.btn_min_hdr.bind("<Button-1>", lambda e: self._minimize_to_tray())
        self.btn_min_hdr.bind("<Enter>", lambda e: self.btn_min_hdr.configure(fg=FG))
        self.btn_min_hdr.bind("<Leave>", lambda e: self.btn_min_hdr.configure(fg=FG_DIM))
        self._no_drag.add(self.btn_min_hdr)
        self.btn_refresh_hdr = tk.Label(self.header, text="⟳", bg=BG2, fg=FG_DIM, font=_font(12, True),
                                        padx=8, cursor="hand2", takefocus=0)
        self.btn_refresh_hdr.pack(side="right")
        self.btn_refresh_hdr.bind("<Button-1>", lambda e: self.trigger_refresh())
        self.btn_refresh_hdr.bind("<Enter>", lambda e: self.btn_refresh_hdr.configure(fg=FG))
        self.btn_refresh_hdr.bind("<Leave>", lambda e: self.btn_refresh_hdr.configure(fg=FG_DIM))
        self._no_drag.add(self.btn_refresh_hdr)

        self.summary_lbl = tk.Label(self.expand_frame, text="", bg=BG, fg=FG_DIM,
                                    font=_font(9), anchor="w", padx=12, pady=4)
        self.summary_lbl.pack(fill="x")

        # 请求尺寸设小，使 canvas 始终跟随窗口大小（而非被内容撑开）
        self.canvas = tk.Canvas(self.expand_frame, bg=BG, highlightthickness=0,
                                width=10, height=10)
        self.vbar = tk.Scrollbar(self.expand_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")
        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self._list_win = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self.expand_frame.pack_forget()

    def _on_collapsed_configure(self, e):
        try:
            w = e.width or self.collapsed.winfo_width()
            h = e.height or self.collapsed.winfo_height()
            if self._bg_item is not None:
                self.collapsed.coords(self._bg_item, w / 2, h / 2)
            if self._btn_min_win is not None:
                self.collapsed.coords(self._btn_min_win, w - 36, 10)
            if self._btn_refresh_win is not None:
                self.collapsed.coords(self._btn_refresh_win, w - 68, 10)
            if self._btn_exit_win is not None:
                self.collapsed.coords(self._btn_exit_win, w - 4, 10)
            for cd, y, _eff in self._collapsed_cd:
                self.collapsed.coords(cd, w - 10, y)
            if self._cd_header is not None:
                try:
                    self.collapsed.coords(self._cd_header, w - 10, 72)
                except Exception:
                    pass
        except Exception:
            pass

    def _restore_pos(self):
        cfg = load_config()
        pos = cfg.get("gui_pos")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if isinstance(pos, list) and len(pos) == 2:
            x, y = int(pos[0]), int(pos[1])
            # 宽松校验：支持多显示器负坐标（左侧副屏），仅排除明显越界
            if -4000 <= x < sw + 1000 and -4000 <= y < sh + 1000:
                self.root.geometry("+%d+%d" % (x, y))
                self._last_normal_pos = (x, y)
                return
        self.root.geometry("+%d+%d" % (sw - 320, 60))
        self._last_normal_pos = (sw - 320, 60)

    def _save_pos(self):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self._last_normal_pos = (x, y)
        cfg = load_config()
        cfg["gui_pos"] = [x, y]
        save_config(cfg)

    def _save_game_mode(self):
        cfg = load_config()
        cfg["game_mode"] = bool(self.game_mode)
        save_config(cfg)

    def _save_bg_config(self):
        cfg = load_config()
        cfg["bg_image"] = self.bg_image_path
        cfg["bg_opacity"] = self.bg_opacity
        cfg["window_opacity"] = self.window_opacity
        save_config(cfg)

    # ---------------- 背景图片 ----------------
    def _load_bg_image(self):
        if self._bg_item is not None:
            try:
                self.collapsed.delete(self._bg_item)
            except Exception:
                pass
            self._bg_item = None
        self._bg_display = None
        path = self.bg_image_path
        if not path or not os.path.isfile(path):
            return
        try:
            photo = tk.PhotoImage(file=path)
            w, h = photo.width(), photo.height()
            maxdim = 800
            if max(w, h) > maxdim:
                k = max(1, int(max(w, h) // maxdim) + (1 if max(w, h) % maxdim else 0))
                if k > 1:
                    photo = photo.subsample(k, k)
            _apply_image_opacity(photo, max(0.0, min(1.0, self.bg_opacity)))
            self._bg_display = photo
            self._bg_item = self.collapsed.create_image(0, 0, anchor="center",
                                                        image=self._bg_display)
            self._on_collapsed_configure(type("E", (), {"width": 0, "height": 0})())
        except Exception as e:
            self.bg_image_path = ""
            self._save_bg_config()
            import tkinter.messagebox as mb
            try:
                mb.showwarning("背景图片", "图片加载失败（仅支持 PNG / GIF / PPM 格式）：\n%s" % e,
                               parent=self.image_panel if hasattr(self, "image_panel") else self.root)
            except Exception:
                pass

    def _import_bg_image(self):
        import tkinter.filedialog as fd
        path = fd.askopenfilename(parent=self.opt_panel, title="选择背景图片",
                                  filetypes=[("图片", "*.png *.gif *.ppm *.pgm"), ("所有文件", "*.*")])
        if not path:
            return
        old = self.bg_image_path
        self.bg_image_path = path
        self._load_bg_image()
        if not self._bg_display:
            self.bg_image_path = old
            return
        self._save_bg_config()

    def _remove_bg_image(self):
        self.bg_image_path = ""
        self._load_bg_image()
        self._save_bg_config()

    # ---------------- 底部功能栏（齿轮 / 图片 / 扳手） ----------------
    def _round_btn(self, parent, size, draw):
        """圆形按钮画布（四角透明键色）。draw(c, cx, cy, color) 负责绘制。"""
        c = tk.Canvas(parent, width=size, height=size, bg=GEAR_KEY, highlightthickness=0,
                      cursor="hand2")
        cx = cy = size / 2.0
        draw(c, cx, cy)
        return c

    def _draw_gear(self, c, cx, cy):
        import math
        color = ACCENT
        for i in range(8):
            ang = math.radians(i * 45)
            tx = cx + 9.5 * math.cos(ang)
            ty = cy + 9.5 * math.sin(ang)
            c.create_oval(tx - 3.2, ty - 3.2, tx + 3.2, ty + 3.2, fill=color, outline=color,
                          tags="gear")
        c.create_oval(cx - 7.5, cy - 7.5, cx + 7.5, cy + 7.5, fill=color, outline=color,
                      tags="gear")
        c.create_oval(cx - 3.2, cy - 3.2, cx + 3.2, cy + 3.2, fill=GEAR_KEY, outline=GEAR_KEY)
        c.bind("<Enter>", lambda e: c.itemconfigure("gear", fill="#8fb0ff"))
        c.bind("<Leave>", lambda e: c.itemconfigure("gear", fill=color))

    def _draw_image_icon(self, c, cx, cy):
        color = ACCENT
        c.create_rectangle(cx - 10, cy - 10, cx + 10, cy + 10, outline=color, width=2)
        c.create_oval(cx - 6.5, cy - 7, cx - 2.5, cy - 3, fill=color, outline=color,
                      tags="img")
        c.create_polygon(cx - 9, cy + 8, cx - 3, cy, cx + 3, cy + 8, cx + 9, cy + 3,
                         cx + 9, cy + 9, cx - 9, cy + 9, fill=color, outline=color, tags="img")
        c.bind("<Enter>", lambda e: c.itemconfigure("img", fill="#8fb0ff"))
        c.bind("<Leave>", lambda e: c.itemconfigure("img", fill=color))

    def _draw_search_icon(self, c, cx, cy):
        color = ACCENT
        c.create_oval(cx - 6, cy - 6, cx + 2, cy + 2, outline=color, width=2.5, tags="sr")
        c.create_line(cx + 1.5, cy + 1.5, cx + 7, cy + 7, fill=color, width=2.5, tags="sr")
        c.bind("<Enter>", lambda e: c.itemconfigure("sr", outline="#8fb0ff", fill="#8fb0ff"))
        c.bind("<Leave>", lambda e: c.itemconfigure("sr", outline=color, fill=color))

    def _draw_wrench(self, c, cx, cy):
        """标准开放扳手：头部 C 形开口朝上 + 直柄向下。"""
        color = ACCENT
        # 头部（U 形开口）
        c.create_polygon(
            cx - 8, cy - 7, cx - 8, cy + 5, cx + 8, cy + 5, cx + 8, cy - 7,
            cx + 3, cy - 7, cx + 3, cy - 2, cx - 3, cy - 2, cx - 3, cy - 7,
            fill=color, outline=color, tags="wr")
        # 直柄
        c.create_rectangle(cx - 3, cy + 4, cx + 3, cy + 11, fill=color, outline=color,
                           tags="wr")
        c.bind("<Enter>", lambda e: c.itemconfigure("wr", fill="#8fb0ff"))
        c.bind("<Leave>", lambda e: c.itemconfigure("wr", fill=color))

    def _build_panels(self):
        # 功能按钮行：搜索 / 扳手 / 图片 / 齿轮（圆形，统一 32×32，靠右，跟随悬浮窗）
        self.btn_panel = tk.Toplevel(self.root)
        self.btn_panel.overrideredirect(True)
        self.btn_panel.attributes("-topmost", True)
        try:
            self.btn_panel.attributes("-transparentcolor", GEAR_KEY)
        except tk.TclError:
            pass
        self.btn_panel.configure(bg=GEAR_KEY)
        row = tk.Frame(self.btn_panel, bg=GEAR_KEY)
        row.pack()

        def make_btn(draw, name):
            c = self._round_btn(row, 32, draw)
            c.pack(side="left", padx=2)
            c.bind("<Button-1>", lambda e, n=name: self._toggle_panel(n))
            return c

        self._search_canvas = make_btn(self._draw_search_icon, "search")
        self._wrench_canvas = make_btn(self._draw_wrench, "wrench")
        self._image_canvas = make_btn(self._draw_image_icon, "image")
        self._gear_canvas = make_btn(self._draw_gear, "gear")
        self.btn_panel.withdraw()

        # ---- 齿轮面板：常规选项 ----
        self.gear_panel = self._build_option_panel([
            ("↩ 还原到原来位置", self._restore_position),
            ("check:🚀 开机自启动", "auto"),
            ("check:🎮 游戏模式", "game"),
            ("sep",),
            ("✕ 退出", self.quit),
        ])

        # ---- 图片面板：透明与背景图片 ----
        self.image_panel = self._build_option_panel([
            ("label:窗口透明度（透见桌面）", "opacity"),
            ("label:背景图片透明度", "bgopacity"),
            ("🖼 导入背景图片", self._import_bg_image),
            ("🗑 移除背景图片", self._remove_bg_image),
        ])

        # ---- 扳手面板：设置页 / 历史 ----
        self.wrench_panel = self._build_option_panel([
            ("⚙ 打开后台设置页面", lambda: self._open_web("")),
            ("📋 查看历史完成任务", lambda: self._open_web("history")),
        ])

        self._build_search_panel()

    def _build_search_panel(self):
        """放大镜搜索面板：悬浮窗左侧子窗口（随悬浮窗移动，独立边框缩放），搜索全部任务，分类 + 分页。"""
        p = tk.Toplevel(self.root)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        try:
            p.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        p.configure(bg=BG2, highlightthickness=1, highlightbackground="#3a4152")
        body = tk.Frame(p, bg=BG2)
        body.pack(fill="both", expand=True, padx=8, pady=8)

        # 标题行 + X 关闭
        top_row = tk.Frame(body, bg=BG2)
        top_row.pack(fill="x")
        tk.Label(top_row, text="🔍 搜索任务", bg=BG2, fg=ACCENT, font=_font(9, True)).pack(side="left")
        btn_x = tk.Label(top_row, text="✕", bg=BG2, fg=FG_DIM, font=_font(10, True),
                         padx=6, cursor="hand2")
        btn_x.pack(side="right")
        btn_x.bind("<Button-1>", lambda e: self._close_search_panel())
        btn_x.bind("<Enter>", lambda e: btn_x.configure(fg=RED))
        btn_x.bind("<Leave>", lambda e: btn_x.configure(fg=FG_DIM))
        self._search_close_btn = btn_x
        self._search_input = tk.Entry(top_row, bg=BG, fg=FG, insertbackground=FG,
                                      relief="flat", font=_font(9))
        self._search_input.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self._search_input.bind("<Return>", lambda e: self._search_go())
        self._search_input.bind("<KeyRelease>", self._on_search_key)
        tk.Button(top_row, text="搜索", command=self._search_go, bg=ACCENT, fg="white",
                  relief="flat", font=_font(9), padx=8, pady=2, cursor="hand2"
                  ).pack(side="left", padx=(6, 0))

        # 分类（4×2 网格，随面板宽度自适应拉伸换行）
        cats = [("", "全部"), ("daily", "日常"), ("today", "今日"), ("tomorrow", "明日"),
                ("week", "周常"), ("month", "月常"), ("done", "已完成"), ("undone", "未完成")]
        cat_row = tk.Frame(body, bg=BG2)
        cat_row.pack(fill="x", pady=(6, 4))
        self._search_cat = ""
        self._cat_buttons = {}
        for i, (val, label) in enumerate(cats):
            b = tk.Label(cat_row, text=label, bg=ACCENT if val == "" else BG2,
                         fg="white" if val == "" else FG, font=_font(8), pady=2,
                         cursor="hand2")
            b.grid(row=i // 4, column=i % 4, sticky="ew", padx=2, pady=1)
            b.bind("<Button-1>", lambda e, v=val: self._search_set_cat(v))
            self._cat_buttons[val] = b
        for c in range(4):
            cat_row.grid_columnconfigure(c, weight=1, uniform="cat")

        # 状态标签（独立于结果区，永不销毁）
        self._search_status = tk.Label(body, text="输入关键词搜索全部任务", bg=BG2, fg=FG_DIM,
                                       font=_font(8), anchor="w", padx=4, pady=2)
        self._search_status.pack(fill="x")
        # 结果区：可滚动（内容自适应，翻页可查看全部）
        self._search_result_wrap = tk.Frame(body, bg=BG)
        self._search_result_wrap.pack(fill="both", expand=True, pady=(2, 4))
        self._search_canvas = tk.Canvas(self._search_result_wrap, bg=BG, highlightthickness=0,
                                        width=10, height=10)
        self._search_sbar = tk.Scrollbar(self._search_result_wrap, orient="vertical",
                                         command=self._search_canvas.yview)
        self._search_canvas.configure(yscrollcommand=self._search_sbar.set)
        self._search_canvas.pack(side="left", fill="both", expand=True)
        self._search_sbar.pack(side="right", fill="y")
        self._search_result = tk.Frame(self._search_canvas, bg=BG)
        self._search_result_win = self._search_canvas.create_window(
            (0, 0), window=self._search_result, anchor="nw")
        self._search_result.bind("<Configure>",
                                 lambda e: self._search_canvas.configure(scrollregion=self._search_canvas.bbox("all")))
        self._search_canvas.bind("<Configure>",
                                 lambda e: self._search_canvas.itemconfigure(self._search_result_win, width=e.width))
        self._search_canvas.bind("<MouseWheel>", self._on_search_wheel)

        # 分页
        pager = tk.Frame(body, bg=BG2)
        pager.pack(fill="x", pady=(2, 0))
        self._search_prev = tk.Button(pager, text="◀ 上一页", command=lambda: self._search_page_to(-1),
                                      bg=BG, fg=FG, relief="flat", font=_font(8), padx=6, pady=2,
                                      cursor="hand2")
        self._search_prev.pack(side="left")
        self._search_page_lbl = tk.Label(pager, text="第 1/1 页", bg=BG2, fg=FG_DIM, font=_font(8))
        self._search_page_lbl.pack(side="left", expand=True)
        self._search_next = tk.Button(pager, text="下一页 ▶", command=lambda: self._search_page_to(1),
                                      bg=BG, fg=FG, relief="flat", font=_font(8), padx=6, pady=2,
                                      cursor="hand2")
        self._search_next.pack(side="left")
        self._search_state = {"page": 1, "pages": 1, "total": 0}
        self._search_req_id = 0      # 请求序号：丢弃过期响应（快速切换分类/搜索时防串扰）
        self._search_after = None    # 即时搜索防抖
        self._panel_resize = None    # 面板自身边框缩放状态
        self._search_user_w = None   # 用户手动调整过的面板宽度（保持）
        # 冻结面板尺寸：尺寸完全由 wm geometry 控制（边框缩放可靠生效），初始取自然尺寸
        p.pack_propagate(False)
        p.update_idletasks()
        p.geometry("%dx%d" % (max(320, p.winfo_reqwidth()), max(280, p.winfo_reqheight())))
        # 面板自身边框缩放（与悬浮窗互不影响）
        p.bind("<ButtonPress-1>", self._panel_press)
        p.bind("<B1-Motion>", self._panel_motion)
        p.bind("<ButtonRelease-1>", self._panel_release)
        p.withdraw()
        self.search_panel = p

    def _close_search_panel(self):
        """搜索面板 X 关闭。"""
        self._panel_open["search"] = False
        self._sync_panels()

    def _panel_press(self, e):
        """面板边框按下：进入缩放（仅当鼠标位于面板边缘）。"""
        if self.search_panel.state() == "withdrawn":
            self.search_panel.deiconify()  # 防御：隐藏状态下按下面板先显示
        self._panel_resize = None
        x, y = e.x, e.y
        w = self.search_panel.winfo_width()
        h = self.search_panel.winfo_height()
        zone = ""
        if x <= EDGE:
            zone += "l"
        elif x >= w - EDGE:
            zone += "r"
        if y <= EDGE:
            zone += "t"
        elif y >= h - EDGE:
            zone += "b"
        if zone:
            self._panel_resize = (zone, e.x_root, e.y_root,
                                  self.search_panel.winfo_x(), self.search_panel.winfo_y(), w, h)

    def _panel_motion(self, e):
        if not self._panel_resize:
            return
        zone, x0, y0, px, py, w0, h0 = self._panel_resize
        dx = e.x_root - x0
        dy = e.y_root - y0
        x, y, w, h = px, py, w0, h0
        if "l" in zone:
            w = max(200, w0 - dx)
            x = px + dx
        if "r" in zone:
            w = max(200, w0 + dx)
        if "t" in zone:
            h = max(130, h0 - dy)
            y = py + dy
        if "b" in zone:
            h = max(130, h0 + dy)
        # 位置与尺寸一次设置（wm geometry，尺寸可靠生效）
        self.search_panel.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self._search_user_w = w

    def _panel_release(self, e):
        self._panel_resize = None
        self._sync_panels()  # 重新贴靠悬浮窗左侧

    def _on_search_wheel(self, e):
        try:
            self._search_canvas.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass

    def _on_search_key(self, e):
        """输入框即时搜索（防抖 300ms）。"""
        if self._search_after is not None:
            try:
                self.root.after_cancel(self._search_after)
            except Exception:
                pass
        self._search_after = self.root.after(300, self._search_go)

    def _search_set_cat(self, val):
        self._search_cat = val
        for v, b in self._cat_buttons.items():
            b.configure(bg=ACCENT if v == val else BG2, fg="white" if v == val else FG)
        self._search_state["page"] = 1
        self._search_go()

    def _search_go(self):
        self._search_state["page"] = 1
        self._search_fetch(self._search_input.get().strip(), 1)

    def _search_page_to(self, delta):
        page = self._search_state["page"] + delta
        if page < 1 or page > self._search_state["pages"]:
            return
        self._search_fetch(self._search_input.get().strip(), page)

    def _search_fetch(self, q, page):
        self._search_req_id += 1
        req_id = self._search_req_id
        self._search_status.configure(text="搜索中…")
        import urllib.parse as up

        def work():
            try:
                url = ("%s/api/tasks/search?q=%s&scope=%s&page=%d&page_size=10"
                       % (self.base, up.quote(q or ""), up.quote(self._search_cat or ""), page))
                r = http_json(url, timeout=8)
                self.q.put(("search", req_id, r))
            except Exception as e:
                self.q.put(("search", req_id, {"error": str(e)}))

        threading.Thread(target=work, daemon=True, name="search").start()

    def _search_render(self, r):
        try:
            if r.get("error"):
                self._search_status.configure(text="搜索失败：" + r["error"], fg=RED)
                return
            for w in self._search_result.winfo_children():
                w.destroy()
            tasks = r.get("tasks") or []
            self._search_state.update({"page": r.get("page", 1), "pages": r.get("pages", 1),
                                       "total": r.get("total", 0)})
            if not tasks:
                self._search_status.configure(text="未找到匹配任务", fg=FG_DIM)
            else:
                self._search_status.configure(text="")
                for t in tasks:
                    done = bool(t.get("done_count"))
                    row = tk.Frame(self._search_result, bg=BG)
                    row.pack(fill="x", padx=2, pady=1)
                    title = t["title"]
                    if len(title) > 14:
                        title = title[:13] + "…"
                    txt = ("✓ " if done else "○ ") + title
                    tk.Label(row, text=txt, bg=BG, fg=DONE if done else FG, font=_font(8),
                             anchor="w", justify="left").pack(side="left", fill="x", expand=True)
                    if done and t.get("last_completed_at"):
                        ct = str(t["last_completed_at"])[:16].replace("T", " ")
                        tk.Label(row, text=ct, bg=BG, fg=FG_DIM, font=_font(7)).pack(side="right")
            self._search_page_lbl.configure(text="第 %d/%d 页 · 共 %d 条" % (
                self._search_state["page"], self._search_state["pages"], self._search_state["total"]))
            self._search_prev.configure(state="normal" if self._search_state["page"] > 1 else "disabled")
            self._search_next.configure(state="normal" if self._search_state["page"] < self._search_state["pages"] else "disabled")
            try:
                self._search_canvas.yview_moveto(0)  # 翻页/搜索后回到顶部
            except Exception:
                pass
            self.search_panel.update_idletasks()
        except Exception:
            pass

    def _build_option_panel(self, items):
        """构建一个选项面板；items 支持 ('text', cmd) / ('check:文本', 键) / ('sep',) / ('label:文本', 键)。"""
        panel = tk.Toplevel(self.root)
        panel.overrideredirect(True)
        panel.attributes("-topmost", True)
        try:
            panel.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        panel.configure(bg=BG2, highlightthickness=1, highlightbackground="#3a4152")
        f = tk.Frame(panel, bg=BG2)
        f.pack(padx=6, pady=6)

        def opt_btn(text, cmd):
            b = tk.Button(f, text=text, command=cmd, bg=BG, fg=FG,
                          activebackground=ACCENT, activeforeground="white",
                          relief="flat", font=_font(9), anchor="w", padx=10, pady=3,
                          cursor="hand2")
            b.pack(fill="x", pady=1)
            return b

        for it in items:
            if it[0] == "sep":
                tk.Frame(f, bg="#3a4152", height=1).pack(fill="x", pady=4)
            elif it[0].startswith("check:"):
                text = it[0][6:]
                var = tk.BooleanVar()
                if it[1] == "auto":
                    var.set(autostart.is_enabled())
                    cb = tk.Checkbutton(f, text=text, variable=var, command=self._toggle_autostart,
                                        bg=BG2, fg=FG, selectcolor=BG, activebackground=BG2,
                                        activeforeground=FG, font=_font(9), anchor="w", padx=10)
                    self._auto_var = var
                else:
                    var.set(self.game_mode)
                    cb = tk.Checkbutton(f, text=text, variable=var, command=self._toggle_game,
                                        bg=BG2, fg=FG, selectcolor=BG, activebackground=BG2,
                                        activeforeground=FG, font=_font(9), anchor="w", padx=10)
                    self._game_var = var
                cb.pack(fill="x", pady=1)
            elif it[0].startswith("label:"):
                tk.Label(f, text=it[0][6:], bg=BG2, fg=FG_DIM, font=_font(8),
                         anchor="w").pack(fill="x")
                if it[1] == "opacity":
                    self._opacity_scale = tk.Scale(f, from_=30, to=100, orient="horizontal",
                                                   bg=BG2, fg=FG, troughcolor=BG,
                                                   highlightthickness=0, showvalue=False)
                    self._opacity_scale.set(int(self.window_opacity * 100))
                    self._opacity_scale.configure(command=self._on_opacity_change)
                    self._opacity_scale.pack(fill="x")
                elif it[1] == "bgopacity":
                    self._bgop_scale = tk.Scale(f, from_=10, to=100, orient="horizontal",
                                                bg=BG2, fg=FG, troughcolor=BG,
                                                highlightthickness=0, showvalue=False)
                    self._bgop_scale.set(int(self.bg_opacity * 100))
                    self._bgop_scale.configure(command=self._on_bgop_change)
                    self._bgop_scale.pack(fill="x")
            else:
                opt_btn(it[0], it[1])
        panel.withdraw()
        return panel

    def _toggle_panel(self, name):
        """只展开一个面板：点击其他按钮时收起已展开的。"""
        was_open = self._panel_open.get(name)
        for k in self._panel_open:
            self._panel_open[k] = False
        if not was_open:
            self._panel_open[name] = True
        self._sync_panels()
        if name == "search" and not was_open:
            # 首次打开自动加载全部任务
            if self._search_state["total"] == 0 and not self._search_input.get().strip():
                self._search_go()

    def _restore_position(self):
        """还原到原来位置：贴边隐藏时还原；否则回到最近一次正常位置。"""
        if self.docked:
            self._undock()
            return
        if self._last_normal_pos:
            x, y = self._last_normal_pos
            w = max(MIN_W, self.root.winfo_width() or 240)
            h = max(MIN_H, self.root.winfo_height() or 100)
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            x = max(0, min(x, sw - w - 4))
            y = max(0, min(y, sh - h - 4))
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _sync_panels(self):
        """功能按钮行（搜索/扳手/图片/齿轮）与各面板：固定大小，跟随悬浮窗移动；
        搜索面板显示在悬浮窗左侧。"""
        try:
            panels = (("gear", self.gear_panel), ("image", self.image_panel),
                      ("wrench", self.wrench_panel))
            if self._hidden_in_tray or self.docked:
                self.btn_panel.withdraw()
                for _, p in panels:
                    p.withdraw()
                self.search_panel.withdraw()
                return
            self.btn_panel.deiconify()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            bw = self.btn_panel.winfo_reqwidth()
            self.btn_panel.geometry("+%d+%d" % (x + w - bw - 6, y + h + 4))
            py = y + h + 4 + self.btn_panel.winfo_reqheight() + 2
            for name, panel in panels:
                if self._panel_open.get(name):
                    panel.deiconify()
                    ow = panel.winfo_reqwidth()
                    panel.geometry("+%d+%d" % (x + w - ow - 6, py))
                else:
                    panel.withdraw()
            # 搜索面板：悬浮窗左侧（保持用户手动调整的宽度，重新贴靠）
            if self._panel_open.get("search"):
                self.search_panel.deiconify()
                ow = self._search_user_w or self.search_panel.winfo_width() \
                    or self.search_panel.winfo_reqwidth()
                self.search_panel.geometry("+%d+%d" % (max(0, x - ow - 6), y))
            else:
                self.search_panel.withdraw()
        except Exception:
            pass

    def _on_opacity_change(self, v):
        try:
            self.window_opacity = max(0.3, min(1.0, int(float(v)) / 100.0))
            self.root.attributes("-alpha", self.window_opacity)
            cfg = load_config()
            cfg["window_opacity"] = self.window_opacity
            save_config(cfg)
        except Exception:
            pass

    def _on_bgop_change(self, v):
        self.bg_opacity = max(0.1, min(1.0, int(float(v)) / 100.0))
        if self._bgop_after is not None:
            try:
                self.root.after_cancel(self._bgop_after)
            except Exception:
                pass
        self._bgop_after = self.root.after(250, self._apply_bgop)

    def _apply_bgop(self):
        self._bgop_after = None
        self._load_bg_image()
        self._save_bg_config()

    # ---------------- 统一事件 ----------------
    def _edge_zone(self):
        if self.docked:
            return ""
        try:
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            x = self.root.winfo_pointerx() - self.root.winfo_rootx()
            y = self.root.winfo_pointery() - self.root.winfo_rooty()
        except Exception:
            return ""
        z = ""
        if x <= EDGE:
            z += "l"
        elif x >= w - EDGE:
            z += "r"
        if y <= EDGE:
            z += "t"
        elif y >= h - EDGE:
            z += "b"
        return z

    _CURSORS = {"l": "sb_h_double_arrow", "r": "sb_h_double_arrow",
                "t": "sb_v_double_arrow", "b": "sb_v_double_arrow",
                "lt": "size_nw_se", "tl": "size_nw_se", "rb": "size_nw_se", "br": "size_nw_se",
                "rt": "size_ne_sw", "tr": "size_ne_sw", "lb": "size_ne_sw", "bl": "size_ne_sw"}

    def _is_main_widget(self, widget):
        """事件是否来自主窗口自身的部件（面板/弹窗等独立顶层一律不算）。"""
        w = widget
        while w is not None and w is not self.root:
            if isinstance(w, tk.Toplevel):
                return False  # 属于其他顶层窗口（功能面板/搜索面板/弹窗）
            w = getattr(w, "master", None)
        return w is self.root

    def _on_motion(self, e):
        if not self._is_main_widget(e.widget):
            return
        if self._mode == "resize":
            self._resize(e)
            return
        if self._mode == "drag":
            x0, y0, wx, wy = self._press
            self.root.geometry("+%d+%d" % (wx + e.x_root - x0, wy + e.y_root - y0))
            return
        zone = self._edge_zone()
        cur = self._CURSORS.get(zone)
        if cur:
            self.root.configure(cursor=cur)
        else:
            self.root.configure(cursor=("hand2" if not self.expanded else "arrow"))

    def _on_press(self, e):
        if not self._is_main_widget(e.widget):
            return  # 面板/弹窗上的操作不影响悬浮窗
        if self.docked:
            self._undock()
            return
        self._press = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        zone = self._edge_zone()
        if zone:
            self._mode = "resize"
            self._resize_start = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(),
                                  self.root.winfo_width(), self.root.winfo_height(), zone)
        elif self._is_drag_area(e.widget):
            self._mode = "drag"
        else:
            self._mode = None

    def _on_b1_motion(self, e):
        if self._mode == "resize":
            self._resize(e)
        elif self._mode == "drag":
            x0, y0, wx, wy = self._press
            nx = wx + e.x_root - x0
            ny = wy + e.y_root - y0
            self.root.geometry("+%d+%d" % (nx, ny))
            # 拖拽靠近系统桌面右缘 → 自动吸附隐藏到侧边
            sw = self.root.winfo_screenwidth()
            w = self.root.winfo_width() or 240
            if sw - (nx + w) < 14:
                self.root.update_idletasks()  # 确保 winfo 反映最新位置（吸附前保存原位）
                self._mode = None
                self._dock(auto=True)
                self._save_pos()

    def _on_release(self, e):
        if self._mode == "drag":
            dx = abs(e.x_root - self._press[0])
            dy = abs(e.y_root - self._press[1])
            if dx < 5 and dy < 5:
                self.set_expanded(not self.expanded)
            self._save_pos()
        elif self._mode == "resize":
            # 用户手动调整过尺寸：暂停自动适配，防止回弹
            self._user_resized = True
        self._mode = None
        self._press = None
        self._resize_start = None

    def _resize(self, e):
        x0, y0, wx, wy, w0, h0, zone = self._resize_start
        dx = e.x_root - x0
        dy = e.y_root - y0
        x, y, w, h = wx, wy, w0, h0
        if "l" in zone:
            w = max(MIN_W, w0 - dx)
            x = wx + dx
        if "r" in zone:
            w = max(MIN_W, w0 + dx)
        if "t" in zone:
            h = max(MIN_H, h0 - dy)
            y = wy + dy
        if "b" in zone:
            h = max(MIN_H, h0 + dy)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _is_drag_area(self, widget):
        w = widget
        while w is not None and w is not self.root:
            if w in self._no_drag:
                return False
            if w is self.collapsed or w is self.header:
                return True
            w = getattr(w, "master", None)
        return False

    def _on_enter(self, e):
        # 贴边隐藏时鼠标悬停仅提示（点击才恢复）
        if self.docked:
            try:
                self.root.configure(cursor="hand2")
            except Exception:
                pass

    def _on_wheel(self, e):
        w = e.widget
        if self._is_main_widget(w) and self.expanded:
            self.canvas.yview_scroll(int(-e.delta / 120), "units")
            return
        # 鼠标位于搜索面板内 → 滚动搜索结果
        if w is not None:
            node = w
            while node is not None and node is not self.root:
                if node is self.search_panel:
                    try:
                        self._search_canvas.yview_scroll(int(-e.delta / 120), "units")
                    except Exception:
                        pass
                    return
                node = getattr(node, "master", None)

    # ---------------- 系统托盘（最小化） ----------------
    def _ensure_tray(self):
        if self.tray is None:
            self.tray = tray_mod.Tray(tooltip="悬浮任务板 - 任务提醒运行中")
            self.tray.start(self.q)
        return self.tray

    def _minimize_to_tray(self):
        if self._hidden_in_tray:
            return
        self._save_pos()
        t = self._ensure_tray()
        for _ in range(40):
            if t.running:
                break
            time.sleep(0.05)
        if not t.running:
            self._tray_failed()
            return
        self._hidden_in_tray = True
        self.root.withdraw()
        self._sync_panels()
        try:
            t.balloon("悬浮任务板", "已最小化到系统托盘，点击图标恢复")
        except Exception:
            pass

    def _tray_failed(self):
        import tkinter.messagebox as mb
        try:
            mb.showwarning("悬浮任务板", "系统托盘不可用（Explorer 未运行？），无法最小化到托盘。",
                           parent=self.root)
        except Exception:
            pass

    def _restore_from_tray(self):
        if not self._hidden_in_tray:
            return
        self._hidden_in_tray = False
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        self._sync_panels()
        if self.game_mode and foreground_fullscreen():
            self._dock(auto=True)

    # ---------------- 隐藏到侧边 / 游戏模式 ----------------
    def _dock(self, auto=False):
        if self.docked or self._hidden_in_tray:
            return
        sw = self.root.winfo_screenwidth()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        w = self.root.winfo_width() or 240
        h = self.root.winfo_height() or 100
        self._saved_geom = (x, y, w, h)
        self._auto_docked = auto
        self.docked = True
        self.root.geometry("%dx%d+%d+%d" % (w, h, sw - SLIVER, y))
        try:
            self.collapsed.itemconfigure(self._t_title, text="◀ 已隐藏（点击还原）", fill=FG_DIM)
        except Exception:
            pass
        self._sync_panels()
        if hasattr(self, "_dock_var"):
            self._dock_var.set(True)

    def _undock(self):
        if not self.docked:
            return
        x, y, w, h = self._saved_geom
        self.docked = False
        self._auto_docked = False
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self._last_normal_pos = (x, y)
        try:
            self.collapsed.itemconfigure(self._t_title, text="☰ 悬浮任务板", fill=ACCENT)
        except Exception:
            pass
        self._sync_panels()
        if hasattr(self, "_dock_var"):
            self._dock_var.set(False)

    def _toggle_dock(self):
        if self.docked:
            self._undock()
        else:
            self._dock(auto=False)

    def _toggle_game(self):
        self.game_mode = not self.game_mode
        self._save_game_mode()
        if hasattr(self, "_game_var"):
            self._game_var.set(self.game_mode)
        if self.game_mode and foreground_fullscreen():
            self._dock(auto=True)

    def _open_web(self, fragment):
        import webbrowser
        url = self.base + "/"
        if fragment:
            url += "#" + fragment
        webbrowser.open(url)

    def _toggle_autostart(self):
        on = self._auto_var.get()
        try:
            autostart.set_enabled(on)
            http_json(self.base + "/api/autostart", "POST", {"enabled": on})
        except Exception:
            pass
        self._auto_var.set(autostart.is_enabled())

    # ---------------- 关闭确认（× 按钮） ----------------
    def _on_close_click(self):
        """界面1/2 的 × 按钮：已设置「下次不提醒」则直接执行记住的动作，否则弹确认框。"""
        cfg = load_config()
        if cfg.get("close_confirm") is False and cfg.get("close_action") in ("minimize", "exit"):
            if cfg["close_action"] == "exit":
                self.quit()
            else:
                self._minimize_to_tray()
            return
        self._show_close_dialog()

    def _show_close_dialog(self):
        try:
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            top.configure(bg=BG2, highlightthickness=1, highlightbackground=ACCENT)
            tk.Label(top, text="关闭悬浮任务板", bg=BG2, fg=FG, font=_font(10, True),
                     padx=18, pady=8).pack(fill="x")
            tk.Label(top, text="请选择操作：", bg=BG2, fg=FG_DIM, font=_font(9),
                     anchor="w", padx=18).pack(fill="x")
            row = tk.Frame(top, bg=BG2)
            row.pack(fill="x", padx=18, pady=(4, 4))

            def act(kind):
                no_ask = self._close_noask_var.get()
                cfg = load_config()
                cfg["close_action"] = kind
                cfg["close_confirm"] = not no_ask
                save_config(cfg)
                top.destroy()
                if kind == "exit":
                    self.quit()
                else:
                    self._minimize_to_tray()

            tk.Button(row, text="➖ 最小化到托盘", command=lambda: act("minimize"),
                      bg=BG, fg=FG, activebackground=ACCENT, activeforeground="white",
                      relief="flat", font=_font(9), padx=10, pady=4, cursor="hand2"
                      ).pack(side="left", padx=(0, 8))
            tk.Button(row, text="✕ 退出程序", command=lambda: act("exit"),
                      bg="#5a2b2b", fg=FG, activebackground=RED, activeforeground="white",
                      relief="flat", font=_font(9), padx=10, pady=4, cursor="hand2"
                      ).pack(side="left")
            self._close_noask_var = tk.BooleanVar(value=False)
            tk.Checkbutton(top, text="下次不再询问，直接执行所选操作", variable=self._close_noask_var,
                           bg=BG2, fg=FG_DIM, selectcolor=BG, activebackground=BG2,
                           activeforeground=FG, font=_font(8), anchor="w", padx=18
                           ).pack(fill="x", pady=(0, 8))
            top.update_idletasks()
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            w, h = top.winfo_width(), top.winfo_height()
            x = max(0, min(self.root.winfo_x() + (self.root.winfo_width() - w) // 2, sw - w))
            y = self.root.winfo_y() - h - 10
            if y <= 0:
                y = self.root.winfo_y() + self.root.winfo_height() + 10
            top.geometry("+%d+%d" % (x, max(0, y)))
            top.lift()
            top.attributes("-topmost", True)
        except Exception:
            self.quit()

    # ---------------- 数据 ----------------
    def _start_worker(self):
        def loop():
            while not self._stop.is_set():
                try:
                    self._fetch()
                except Exception as e:
                    self.q.put(("error", str(e)))
                try:
                    self._check_reminders()
                except Exception:
                    pass
                try:
                    fs = foreground_fullscreen()
                    if fs != self._fs_state:
                        self._fs_state = fs
                        self.q.put(("fs", fs))
                except Exception:
                    pass
                self._stop.wait(5)
        t = threading.Thread(target=loop, daemon=True, name="float-worker")
        t.start()

    def trigger_refresh(self):
        if self._loading:
            return
        self._loading = True
        threading.Thread(target=self._fetch, daemon=True, name="float-refresh").start()

    def _fetch(self):
        groups = http_json(self.base + "/api/tasks/float")
        today = groups.get("stats", {}).get("today_date")
        if today:
            comps = http_json(self.base + "/api/completions?date=%s&limit=2000" % today)
            done = {c["task_id"] for c in comps.get("completions", [])}
        else:
            done = set()
        self.q.put(("data", groups.get("groups", {}), groups.get("stats", {}), done))

    def _check_reminders(self):
        r = http_json(self.base + "/api/reminders/due", timeout=5)
        items = r.get("reminders") or []
        if items:
            self.q.put(("remind", items))

    def _poll_queue_once(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "data":
                    _, groups, stats, done = item
                    self.groups = {k: groups.get(k, []) for k, _ in SECTION_TITLES}
                    self.stats = stats
                    self.done_today = done
                    self.connected = True
                    self._render_collapsed()
                    # 抗闪烁：任务数据未变化时不重建列表；变化时恢复自动适配（手动调整的尺寸随之重新适配）
                    sig = tuple(sorted(
                        (t["id"], t["title"], t["id"] in done)
                        for items in self.groups.values() for t in items))
                    if sig != self._list_sig:
                        self._list_sig = sig
                        self._user_resized = False
                        if self.expanded:
                            self._render_list()
                elif kind == "error":
                    self.connected = False
                    self.last_error = item[1]
                    self._render_collapsed()
                elif kind == "remind":
                    self._show_reminders(item[1])
                elif kind == "fs":
                    fs = item[1]
                    if self.game_mode:
                        if fs and not self.docked:
                            self._dock(auto=True)
                        elif not fs and self.docked and self._auto_docked:
                            self._undock()
                elif kind == "tray_show":
                    self._restore_from_tray()
                elif kind == "tray_menu":
                    self._panel_open["gear"] = True
                    self.gear_panel.deiconify()
                    self.gear_panel.geometry("+%d+%d" % (item[1], item[2]))
                elif kind == "tray_quit":
                    self.quit()
                elif kind == "search":
                    if item[1] == self._search_req_id:  # 丢弃过期响应
                        self._search_render(item[2])
        except queue.Empty:
            pass
        self._loading = False

    def _poll_queue(self):
        self._poll_queue_once()
        if not self.selftest:
            self.root.after(300, self._poll_queue)

    # ---------------- 提醒 ----------------
    def _show_reminders(self, items):
        play_sound()
        try:
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            top.configure(bg=BG2, highlightthickness=1, highlightbackground=WARN)
            hdr = tk.Frame(top, bg=BG2)
            hdr.pack(fill="x")
            tk.Label(hdr, text="⏰ 任务提醒", bg=BG2, fg=WARN, font=_font(11, True),
                     padx=14, pady=6).pack(side="left")
            btn_x = tk.Label(hdr, text="✕", bg=BG2, fg=FG_DIM, font=_font(11, True),
                             padx=12, cursor="hand2")
            btn_x.pack(side="right")
            btn_x.bind("<Button-1>", lambda e: top.destroy())
            btn_x.bind("<Enter>", lambda e: btn_x.configure(fg=RED))
            btn_x.bind("<Leave>", lambda e: btn_x.configure(fg=FG_DIM))
            for it in items[:4]:
                title = it.get("title", "")
                if len(title) > 36:
                    title = title[:35] + "…"
                tk.Label(top, text="• " + title, bg=BG2, fg=FG, font=_font(9),
                         anchor="w", padx=14).pack(fill="x")
            if len(items) > 4:
                tk.Label(top, text="……还有 %d 条" % (len(items) - 4), bg=BG2, fg=FG_DIM,
                         font=_font(8), anchor="w", padx=14).pack(fill="x")
            top.update_idletasks()
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            w, h = top.winfo_width(), top.winfo_height()
            # 定位：显示在悬浮窗上方居中（贴边隐藏/托盘隐藏时置顶居中）
            if not self._hidden_in_tray:
                rx, ry = self.root.winfo_x(), self.root.winfo_y()
                rw = self.root.winfo_width()
                x = max(0, min(rx + (rw - w) // 2, sw - w))
                y = ry - h - 10
                if y < 0:
                    y = ry + self.root.winfo_height() + 10  # 上方放不下 → 悬浮窗下方
            else:
                x, y = max(0, (sw - w) // 2), 10
            top.geometry("+%d+%d" % (x, y))
            top.lift()
            top.attributes("-topmost", True)
            top.bind("<Button-1>", lambda e: top.destroy())
            for c in top.winfo_children():
                c.bind("<Button-1>", lambda e: top.destroy())
            self.root.after(9000, lambda: top.destroy() if top.winfo_exists() else None)
        except Exception:
            pass

    # ---------------- 渲染：折叠态 ----------------
    def _render_collapsed(self):
        s = self.stats or {}
        if self.connected:
            self.collapsed.itemconfigure(self._t_stat1, text="日常 %d · 周常 %d · 月常 %d" % (
                s.get("daily_count", 0), s.get("week_count", 0), s.get("month_count", 0)),
                fill=FG)
            self.collapsed.itemconfigure(self._t_stat2, text="今日完成 %d · 总完成 %d" % (
                s.get("today_completed", 0), s.get("total_completed", 0)), fill=FG_DIM)
        else:
            self.collapsed.itemconfigure(self._t_stat1, text="服务未连接", fill=WARN)
            self.collapsed.itemconfigure(self._t_stat2,
                                         text="请确认程序已启动 (端口 %s)" % self.base.split(":")[-1],
                                         fill=FG_DIM)
        # 今日待完成任务：两列表头（今日待完成 | 截止时间）+ 逐行对应
        pending = [t for t in self.groups.get("today", [])
                   if t["id"] not in self.done_today]
        for it in self._collapsed_cleanup:
            try:
                self.collapsed.delete(it)
            except Exception:
                pass
        self._collapsed_cleanup = []
        self._collapsed_cd = []
        cw = self.collapsed.winfo_width() or COLLAPSED_W
        y = 72
        # 表头行：今日待完成（左）| 截止时间（右）（统一垂直居中，避免与首行重叠）
        self.collapsed.itemconfigure(self._t_pending, text="今日待完成", fill=ACCENT,
                                     font=_font(9, True), anchor="w")
        self._cd_header = self.collapsed.create_text(cw - 10, y, anchor="e", text="截止时间",
                                                     fill=ACCENT, font=_font(9, True))
        self._collapsed_cleanup.append(self._cd_header)
        y += 19
        if not pending:
            it = self.collapsed.create_text(10, y, anchor="w",
                                            text="（无，全部完成 🎉）" if self.connected else "（—）",
                                            fill=FG_DIM, font=_font(8))
            self._collapsed_cleanup.append(it)
            y += 17
        else:
            for t in pending[:10]:
                title = t["title"]
                if len(title) > 13:
                    title = title[:12] + "…"
                it = self.collapsed.create_text(10, y, anchor="w", text="• " + title,
                                                fill=FG, font=_font(9))
                self._collapsed_cleanup.append(it)
                eff = t.get("effective_deadline")
                if eff and t.get("scope") != "daily":
                    cd = self.collapsed.create_text(cw - 10, y, anchor="e", text="",
                                                    fill=WARN, font=_font(8))
                    self._collapsed_cleanup.append(cd)
                    self._collapsed_cd.append((cd, y, eff))
                else:
                    cd = self.collapsed.create_text(cw - 10, y, anchor="e", text="—",
                                                    fill=FG_DIM, font=_font(8))
                    self._collapsed_cleanup.append(cd)
                y += 17
            if len(pending) > 10:
                it = self.collapsed.create_text(10, y, anchor="w",
                                                text="…还有 %d 项" % (len(pending) - 10),
                                                fill=FG_DIM, font=_font(8))
                self._collapsed_cleanup.append(it)
                y += 17
        # 未完成汇总行（仅在有逾期时显示文本）
        self._cd_summary_item = self.collapsed.create_text(10, y, anchor="w", text="",
                                                           fill=RED, font=_font(8))
        self._collapsed_cleanup.append(self._cd_summary_item)
        y += 17
        self._pending_lines = max(1, (y - 72) // 17)
        self._update_collapsed_countdowns()
        if not self.docked:
            self.collapsed.itemconfigure(self._t_title, text="☰ 悬浮任务板", fill=ACCENT)
        if hasattr(self, "_dock_var"):
            self._dock_var.set(self.docked)
        if hasattr(self, "_game_var"):
            self._game_var.set(self.game_mode)
        self._resize_collapsed()

    def _resize_collapsed(self):
        """折叠态窗口高度随「今日待完成」行数自适应。
        用户手动调整过尺寸时保持其设置（直到任务数据变化才恢复自动适配），防止回弹。"""
        if self.expanded or self.docked or self._hidden_in_tray or self._user_resized:
            return
        try:
            sh = self.root.winfo_screenheight()
            h = 82 + max(1, self._pending_lines) * 17  # 表头 + 待办行 + 汇总行
            h = min(h, int(sh * 0.6))
            cur = self.root.winfo_height()
            if abs(h - cur) < 2:
                return
            w = max(COLLAPSED_W, self.root.winfo_width() or COLLAPSED_W)
            x, y = self.root.winfo_x(), self.root.winfo_y()
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:
            pass

    def _update_collapsed_countdowns(self):
        """折叠态逐行截止倒计时（每秒刷新）：无天数 h:m:s，有天数 N天hh:mm:ss，逾期红色。"""
        try:
            now = datetime.now()
            for cd, _y, eff in self._collapsed_cd:
                try:
                    d = datetime.fromisoformat(str(eff).replace(" ", "T"))
                except (ValueError, TypeError):
                    continue
                delta = d - now
                if delta.total_seconds() <= 0:
                    text, fg = "已逾期", RED
                else:
                    days, secs = delta.days, delta.seconds
                    hh, mm, ss = secs // 3600, (secs % 3600) // 60, secs % 60
                    if days > 0:
                        text = "%d天%02d:%02d:%02d" % (days, hh, mm, ss)
                    else:
                        text = "%02d:%02d:%02d" % (hh, mm, ss)
                    fg = WARN
                self.collapsed.itemconfigure(cd, text=text, fill=fg)
            if self._cd_summary_item is not None:
                overdue_n = int((self.stats or {}).get("overdue_count", 0) or 0)
                self.collapsed.itemconfigure(
                    self._cd_summary_item,
                    text="⚠ 未完成 %d 项" % overdue_n if overdue_n else "",
                    fill=RED)
        except Exception:
            pass

    def _tick(self):
        if not self.selftest:
            self._update_collapsed_countdowns()
            self._update_row_countdowns()
            self.root.after(1000, self._tick)

    # ---------------- 渲染：展开态 ----------------
    def set_expanded(self, on):
        if on == self.expanded:
            return
        self.expanded = on
        self._user_resized = False  # 切换布局后恢复自动适配
        sh = self.root.winfo_screenheight()
        cur_w = self.root.winfo_width() or 320
        cur_h = self.root.winfo_height() or 100
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if on:
            self.collapsed.pack_forget()
            self.expand_frame.pack(fill="both", expand=True)
            self.root.geometry("%dx%d+%d+%d" % (max(300, cur_w), min(max(cur_h, 320), int(sh * 0.7)), x, y))
            self.root.update_idletasks()
            self._render_list()
        else:
            self.expand_frame.pack_forget()
            self.collapsed.pack(fill="both", expand=True)
            self._render_collapsed()
        self.root.update_idletasks()
        self._sync_panels()

    def _wrap_width(self):
        """基础内容可换行宽度：画布宽 - 滚动条 - 行内边距 - 完成按钮。"""
        cw = self.canvas.winfo_width() or 320
        return max(80, cw - 90)

    def _row_wrap_width(self, has_cd):
        """行内文本可换行宽度：基础宽度基础上，为右侧截止倒计时预留空间（避免遮挡）。"""
        return max(80, self._wrap_width() - (95 if has_cd else 0))

    def _on_canvas_configure(self, e):
        self.canvas.itemconfigure(self._list_win, width=e.width)
        if not self.expanded:
            return
        if abs(e.width - self._last_canvas_w) < 4:
            return
        self._last_canvas_w = e.width
        self._update_wraps()
        self.root.after_idle(self._autosize)

    def _update_wraps(self):
        for lbl, has_cd in self._wrap_labels:
            try:
                lbl.configure(wraplength=self._row_wrap_width(has_cd))
            except tk.TclError:
                pass

    def _autosize(self):
        if not self.expanded or self._hidden_in_tray or self.docked or self._user_resized:
            return
        try:
            self.root.update_idletasks()
            sh = self.root.winfo_screenheight()
            w = self.root.winfo_width() or 320
            if w < 160:
                w = 160
            x, y = self.root.winfo_x(), self.root.winfo_y()
            hdr_h = 30
            sum_h = self.summary_lbl.winfo_reqheight() or 24
            content = self.list_frame.winfo_reqheight() or 100
            h = hdr_h + sum_h + content + 18
            h = min(max(h, 220), int(sh * 0.85))
            if (w, h) == (self.root.winfo_width(), self.root.winfo_height()):
                return  # 抗闪烁：尺寸未变不重复设置
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
            self._sync_panels()
        except Exception:
            pass

    def _render_list(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._row_widgets = {}
        self._row_countdowns = {}
        self._wrap_labels = []
        s = self.stats or {}
        self.summary_lbl.configure(text="今日完成 %d · 总完成 %d" % (
            s.get("today_completed", 0), s.get("total_completed", 0)))

        empty = True
        for key, title in SECTION_TITLES:
            items = self.groups.get(key, [])
            open_ = self._section_open.get(key, key == "today")
            marker = "▾ " if open_ else "▸ "
            hdr = tk.Label(self.list_frame, text="%s%s (%d)" % (marker, title, len(items)),
                           bg=BG2, fg=ACCENT, font=_font(9, True), anchor="w", padx=10, pady=4,
                           cursor="hand2")
            hdr.pack(fill="x", pady=(6, 0))
            hdr.bind("<Button-1>", lambda e, k=key: self._toggle_section(k))
            if not items:
                continue
            empty = False
            if open_:
                for t in items:
                    self._make_row(t, completed=t["id"] in self.done_today)

        if empty:
            tip = tk.Label(self.list_frame, text="还没有任务，点下方功能按钮 →「打开后台设置页面」\n"
                                                 "或在设置页用 AI 一键生成任务",
                           bg=BG, fg=FG_DIM, font=_font(9), justify="left", padx=14, pady=10)
            tip.pack(fill="x")
        self._autosize()

    def _toggle_section(self, key):
        """界面2：点击分区标题块展开/收起该分区任务。"""
        self._section_open[key] = not self._section_open.get(key, True)
        self._render_list()

    def _toggle_row_detail(self, task_id):
        self._row_detail[task_id] = not self._row_detail.get(task_id, False)
        self._render_list()

    def _make_row(self, task, completed=False):
        tid = task["id"]
        expanded = self._row_detail.get(tid, False)
        overdue = bool(task.get("effective_deadline")) and \
            task["effective_deadline"] < datetime_now_str() and not completed
        has_cd = bool(task.get("effective_deadline")) and not completed \
            and task.get("scope") != "daily"
        wrap = self._row_wrap_width(has_cd)
        row = tk.Frame(self.list_frame, bg=BG)
        row.pack(fill="x", padx=8, pady=2)
        col = tk.Frame(row, bg=BG)
        col.pack(side="left", fill="x", expand=True)

        title = task["title"]
        marker = "▾ " if expanded else "▸ "
        t_lbl = tk.Label(col, text=marker + ("⚠ " if overdue else "") + title, bg=BG,
                         fg=RED if overdue else (FG if not completed else FG_DIM),
                         font=_font(9), anchor="w", justify="left",
                         wraplength=wrap, cursor="hand2")
        t_lbl.pack(fill="x")
        t_lbl.bind("<Button-1>", lambda e, t=tid: self._toggle_row_detail(tid))
        self._wrap_labels.append((t_lbl, has_cd))

        if expanded:
            det = tk.Frame(col, bg=BG)
            det.pack(fill="x", padx=(6, 0), pady=(1, 3))
            self._make_detail(det, task, completed, wrap)

        btn = tk.Button(row, text="✓ 已完成" if completed else "完 成",
                        bg=DONE if completed else ACCENT, fg="white", relief="flat",
                        font=_font(8), bd=0, padx=8, pady=2, cursor="hand2",
                        activebackground=DONE if completed else "#3f6fd8",
                        state="disabled" if completed else "normal",
                        command=lambda t=tid: self._complete(t))
        btn.pack(side="right", padx=(6, 0), pady=2)
        # 任务行右侧：截止倒计时（h:m:s，无天数不显示天）
        if has_cd:
            # 固定宽度 + 右对齐：文本变化不触发行回流重排（大量任务时避免卡顿）
            cd = tk.Label(row, text="⏰ --:--:--", bg=BG, fg=WARN, font=_font(8),
                          padx=2, width=11, anchor="e")
            cd.pack(side="right", padx=(4, 2), pady=2)
            self._row_countdowns[tid] = (cd, task.get("effective_deadline"), None)
        self._row_widgets[tid] = (btn, row)

    def _update_row_countdowns(self):
        """任务行右侧截止倒计时（每秒刷新）：仅更新可视区标签、文本变化时才写，
        截止时间解析缓存，固定宽度标签避免重排——大量任务下不卡顿。"""
        try:
            now = datetime.now()
            for tid, entry in list(self._row_countdowns.items()):
                lbl, eff, dt = entry
                if dt is None:
                    try:
                        dt = datetime.fromisoformat(str(eff).replace(" ", "T"))
                    except (ValueError, TypeError):
                        continue
                    self._row_countdowns[tid] = (lbl, eff, dt)
                if not lbl.winfo_ismapped():  # 可视区外跳过（滚动到可见后 1 秒内自动校正）
                    continue
                delta = dt - now
                if delta.total_seconds() <= 0:
                    text, fg = "⚠ 已逾期", RED
                else:
                    days, secs = delta.days, delta.seconds
                    hh, mm, ss = secs // 3600, (secs % 3600) // 60, secs % 60
                    if days > 0:
                        text = "⏰ %d天%02d:%02d:%02d" % (days, hh, mm, ss)
                    else:
                        text = "⏰ %02d:%02d:%02d" % (hh, mm, ss)
                    fg = WARN
                if lbl.cget("text") != text:
                    lbl.configure(text=text, fg=fg)
        except Exception:
            pass

    def _make_detail(self, parent, task, completed, wrap=None):
        overdue = bool(task.get("effective_deadline")) and \
            task["effective_deadline"] < datetime_now_str() and not completed
        wrap = wrap or self._wrap_width()
        lines = []
        if task.get("note"):
            lines.append(("💬 " + task["note"], FG_DIM))
        if task.get("start_at"):
            lines.append((("▶ 开始 " + task["start_at"]).replace("T", " "), FG_DIM))
        if task.get("effective_deadline"):
            txt = ("⏰ 截止 " + task["effective_deadline"]).replace("T", " ")
            lines.append(("⚠ " + txt if overdue else txt, RED if overdue else FG_DIM))
        if task.get("remind_at"):
            lines.append((("🔔 提醒 " + task["remind_at"]).replace("T", " "), FG_DIM))
        if not lines:
            lines.append(("（无备注与时间设置）", FG_DIM))
        for text, color in lines:
            lbl = tk.Label(parent, text=text, bg=BG, fg=color, font=_font(8),
                           anchor="w", justify="left", wraplength=wrap)
            lbl.pack(fill="x", pady=1)
            self._wrap_labels.append((lbl, False))

    def _complete(self, task_id):
        def work():
            try:
                http_json(self.base + "/api/tasks/%d/complete" % task_id, "POST", {})
            except urllib.error.HTTPError as e:
                try:
                    err = json.loads(e.read().decode("utf-8", errors="replace"))
                    self.q.put(("error", err.get("error", "完成失败")))
                except Exception:
                    self.q.put(("error", "完成失败: HTTP %s" % e.code))
            except Exception as ex:
                self.q.put(("error", "完成失败: %s" % ex))
            self.trigger_refresh()
        threading.Thread(target=work, daemon=True).start()

    def quit(self):
        self._stop.set()
        if not self.docked:
            self._save_pos()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.btn_panel.destroy()
            for p in (self.gear_panel, self.image_panel, self.wrench_panel, self.search_panel):
                p.destroy()
        except Exception:
            pass
        self.root.destroy()

    # ---------------- 自检 ----------------
    def _selftest_finish(self):
        self.root.destroy()
        print("SELFTEST OK: 悬浮窗创建与渲染正常")

    def _autoclose_finish(self):
        st = self.stats or {}
        counts = {k: len(v) for k, v in self.groups.items()}
        print("GUILIVE RESULT: connected=%s today_completed=%s total_completed=%s groups=%s" % (
            self.connected, st.get("today_completed"), st.get("total_completed"), counts))
        self.quit()

    def run(self):
        self.root.mainloop()


def datetime_now_str():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%dT%H:%M")


def main():
    args = sys.argv[1:]
    selftest = "--selftest" in args
    port = 39999
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
    base = "http://127.0.0.1:%d" % port
    app = FloatingApp(base, selftest=selftest)
    app.run()


if __name__ == "__main__":
    main()
