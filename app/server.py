# -*- coding: utf-8 -*-
"""本地 HTTP 服务：设置页面 + JSON API（纯标准库实现，端口默认 39999）。"""
import json
import os
import re
import socket
import threading
import traceback
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import ai
import autostart
from db import DB, SCOPES, SCOPE_LABELS, month_range, today_str, week_range

BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # app/
PROJECT_ROOT = os.path.dirname(BASE_DIR)                        # 项目根目录
WEB_DIR = os.path.join(PROJECT_ROOT, "web")


def get_lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


class ApiServer:
    def __init__(self, host, port, db, config, log=None):
        self.host = host
        self.port = int(port)
        self.db = db
        self.config = config
        self.log = log or (lambda *a: None)
        self.httpd = None
        self.thread = None
        self.config_path = config.get("_config_path") or os.path.join(PROJECT_ROOT, "config.json")

    # ---------- 生命周期 ----------
    def start(self):
        handler = self._make_handler()
        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as e:
            raise RuntimeError("无法监听 %s:%s —— %s（端口可能被占用）" % (self.host, self.port, e))
        self.bound_port = self.httpd.server_address[1]
        self.host = self.httpd.server_address[0]
        self.httpd.daemon_threads = True
        self.httpd.allow_reuse_address = True
        self.httpd.app = self
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="httpd")
        self.thread.start()
        self.log("HTTP 服务已启动: http://127.0.0.1:%d  (局域网: %s)" % (
            self.port, " ".join("http://%s:%d" % (i, self.port) for i in get_lan_ips()) or "无"))

    def stop(self):
        if self.httpd:
            t = threading.Thread(target=self.httpd.shutdown, daemon=True)
            t.start()
            t.join(timeout=3)
            self.httpd.server_close()
            self.httpd = None

    def open_page(self, fragment=""):
        url = "http://127.0.0.1:%d/" % self.port
        if fragment:
            url = url.rstrip("/") + "/#%s" % fragment
        webbrowser.open(url)

    # ---------- 路由 ----------
    def _make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                app.log("http %s" % (fmt % args))

            # -- 基础工具 --
            def _send(self, code, body, ctype="application/json; charset=utf-8"):
                if isinstance(body, (dict, list)):
                    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                elif isinstance(body, str):
                    data = body.encode("utf-8")
                else:
                    data = body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, obj, code=200):
                self._send(code, obj)

            def _ok(self, **kw):
                self._json({"ok": True, **kw})

            def _err(self, msg, code=400, **kw):
                self._json({"ok": False, "error": str(msg), **kw}, code)

            def _body(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    length = 0
                if length <= 0:
                    return {}
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                try:
                    return json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    return {}

            def _q(self, name, default=None):
                vals = parse_qs(urlparse(self.path).query).get(name)
                return vals[0] if vals else default

            # -- 静态页 --
            def _serve_static(self, rel):
                target = os.path.normpath(os.path.join(WEB_DIR, rel))
                if not target.startswith(WEB_DIR) or not os.path.isfile(target):
                    return False
                ctype = {
                    ".html": "text/html; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".png": "image/png",
                    ".svg": "image/svg+xml",
                }.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
                with open(target, "rb") as f:
                    self._send(200, f.read(), ctype)
                return True

            # -- 路由分发 --
            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/", "/index.html", ""):
                    if self._serve_static("index.html"):
                        return
                    return self._err("设置页面缺失", 404)
                if path in ("/style.css", "/app.js") or path.startswith("/static/"):
                    rel = path[1:] if not path.startswith("/static/") else path[len("/static/"):]
                    if self._serve_static(rel):
                        return
                    return self._err("资源缺失", 404)
                if path == "/api/health":
                    return self._ok(service="taskboard", time=today_str())
                if path == "/api/stats":
                    return self._ok(stats=app.db.stats())
                if path == "/api/config":
                    return self._ok(config=self._public_config())
                if path == "/api/tasks":
                    scope = self._q("scope")
                    due = self._q("due_date")
                    q = self._q("q")
                    if scope == "all":
                        return self._ok(tasks=app.db.list_tasks(q=q))
                    return self._ok(tasks=app.db.list_tasks(scope=scope, due_date=due, q=q, include_daily=True))
                if path == "/api/tasks/float":
                    groups = app.db.tasks_for_float()
                    done = {c["task_id"] for c in app.db.list_completions(on_date=today_str(), limit=10000)}
                    for items in groups.values():
                        for it in items:
                            it["completed_today"] = it["id"] in done
                    return self._ok(groups=groups, stats=app.db.stats())
                if path == "/api/tasks/search":
                    q = self._q("q", "")
                    scope = self._q("scope", "")
                    try:
                        page = int(self._q("page", 1) or 1)
                        page_size = int(self._q("page_size", 10) or 10)
                    except (TypeError, ValueError):
                        page, page_size = 1, 10
                    return self._ok(**app.db.search_tasks(q, scope, page, page_size))
                if path == "/api/completions":
                    return self._ok(completions=app.db.list_completions(
                        on_date=self._q("date"), limit=int(self._q("limit", 500))))
                if path == "/api/ai/status":
                    return self._ok(**ai.ai_status())
                if path == "/api/reminders/due":
                    return self._ok(reminders=app.db.due_reminders())
                if path == "/api/autostart":
                    return self._ok(enabled=autostart.is_enabled())
                if path == "/api/bg":
                    return self._serve_bg()
                m = re.fullmatch(r"/api/tasks/(\d+)", path)
                if m:
                    t = app.db.get_task(int(m.group(1)))
                    return self._ok(task=t) if t else self._err("任务不存在", 404)
                return self._err("接口不存在: %s" % path, 404)

            def do_POST(self):
                path = urlparse(self.path).path
                body = self._body()
                if path == "/api/tasks":
                    try:
                        t = app.db.add_task(
                            title=body.get("title", ""),
                            scope=body.get("scope", "today"),
                            due_date=body.get("due_date"),
                            note=body.get("note", ""),
                            sort_order=body.get("sort_order", 0),
                            remind_at=body.get("remind_at"),
                            deadline=body.get("deadline"),
                            start_at=body.get("start_at"))
                        return self._ok(task=t, message="已添加任务")
                    except ValueError as e:
                        return self._err(e)
                m = re.fullmatch(r"/api/tasks/(\d+)/complete", path)
                if m:
                    r = app.db.complete_task(int(m.group(1)), body.get("date"))
                    if r["ok"]:
                        return self._ok(message="已完成 ✓", **r)
                    return self._err(r["reason"], 409, task_id=r.get("task_id"), date=r.get("date"))
                m = re.fullmatch(r"/api/tasks/(\d+)/uncomplete", path)
                if m:
                    return self._ok(**app.db.uncomplete_task(int(m.group(1)), body.get("date")))
                if path == "/api/completions" and body.get("action") == "uncomplete":
                    return self._ok(**app.db.uncomplete_task(int(body.get("task_id", 0)), body.get("date")))
                if path == "/api/ai/generate":
                    scopes = body.get("scopes") or ["today"]
                    if isinstance(scopes, str):
                        scopes = [s for s in re.split(r"[,，\s]+", scopes) if s]
                    count = int(body.get("count", 5))
                    context = body.get("context", "")
                    provider = body.get("provider")
                    targets = [s for s in scopes if s in SCOPES]
                    if not targets:
                        return self._err("请选择要生成的类型（日常/今日/明日/周常/月常）")
                    results = {}
                    lock = threading.Lock()

                    def _gen(s):
                        r = ai.generate(s, context=context, count=count, provider=provider)
                        with lock:
                            results[s] = r

                    ts = [threading.Thread(target=_gen, args=(s,), daemon=True) for s in targets]
                    for t in ts:
                        t.start()
                    for t in ts:
                        t.join(timeout=30)
                    for s in targets:
                        if s not in results:  # 超时/失败兜底：用内置模板
                            results[s] = {
                                "source": "error", "provider": provider or "auto", "model": None,
                                "message": "AI 调用超时（请检查网络或该模型是否可访问），已用内置模板生成",
                                "error": True,
                                "tasks": ai.fallback_tasks(s, count, context),
                            }
                    first = next(iter(results.values()))
                    return self._ok(results=results, provider=first["provider"],
                                    model=first["model"], source=first["source"],
                                    message=first["message"])
                if path == "/api/config":
                    return self._apply_config(body)
                if path == "/api/autostart":
                    enabled = bool(body.get("enabled"))
                    autostart.set_enabled(enabled)
                    app.config["autostart"] = enabled
                    return self._ok(enabled=autostart.is_enabled(),
                                    message="已%s开机自启动" % ("开启" if enabled else "关闭"))
                return self._err("接口不存在: %s" % path, 404)

            def do_PUT(self):
                path = urlparse(self.path).path
                m = re.fullmatch(r"/api/tasks/(\d+)", path)
                if not m:
                    return self._err("接口不存在", 404)
                body = self._body()
                kwargs = {}
                for k in ("title", "scope", "note", "sort_order"):
                    if k in body:
                        kwargs[k] = body[k]
                for k in ("due_date", "remind_at", "deadline", "start_at"):
                    if k in body:
                        kwargs[k] = body[k]  # 显式 None 表示清空（start_at 清空 → 编辑时间开始）
                t = app.db.update_task(int(m.group(1)), **kwargs)
                if not t:
                    return self._err("任务不存在", 404)
                return self._ok(task=t, message="已更新")

            def do_DELETE(self):
                path = urlparse(self.path).path
                m = re.fullmatch(r"/api/tasks/(\d+)", path)
                if m:
                    app.db.delete_task(int(m.group(1)))
                    return self._ok(message="已删除任务")
                m = re.fullmatch(r"/api/completions/(\d+)", path)
                if m:
                    app.db.delete_completion(int(m.group(1)))
                    return self._ok(message="已删除完成记录")
                return self._err("接口不存在", 404)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def _apply_config(self, body):
                changed_db = False
                msg = []
                db_path = str(body.get("db_path") or "").strip()
                if db_path:
                    cur = os.path.normcase(os.path.abspath(app.db.path))
                    new = os.path.normcase(os.path.abspath(db_path))
                    if new != cur:
                        try:
                            r = app.db.migrate_to(db_path)
                            app.config["db_path"] = os.path.abspath(db_path)
                            changed_db = True
                            msg.append("数据库已保存到: %s" % app.db.path)
                        except Exception as e:
                            return self._err("数据库路径切换失败: %s" % e, 500)
                port = body.get("port")
                host = str(body.get("host") or "").strip() or app.config.get("host", "0.0.0.0")
                restart = False
                if port is not None:
                    try:
                        port = int(port)
                        if port < 1 or port > 65535:
                            raise ValueError()
                    except (TypeError, ValueError):
                        return self._err("端口必须是 1-65535 之间的数字")
                    if port != app.port:
                        app.config["port"] = port
                        msg.append("端口已改为 %d，重启应用后生效" % port)
                        restart = True
                if host != app.config.get("host", "0.0.0.0"):
                    app.config["host"] = host
                    msg.append("监听地址已改为 %s，重启应用后生效" % host)
                    restart = True
                if body.get("autostart") is not None:
                    autostart.set_enabled(bool(body["autostart"]))
                    app.config["autostart"] = bool(body["autostart"])
                    msg.append("开机自启动已%s" % ("开启" if body["autostart"] else "关闭"))
                self._save_config()
                self._sync_settings()
                return self._ok(message="；".join(msg) or "已保存", db_migrated=changed_db,
                                restart_required=restart, config=self._public_config())

            def _public_config(self):
                # 背景图/透明度从当前配置文件读取（悬浮窗运行时写入，启动时可能已变化）
                cur = {}
                try:
                    with open(app.config_path, "r", encoding="utf-8") as f:
                        cur = json.load(f)
                except Exception:
                    pass
                return {
                    "host": app.config.get("host", "0.0.0.0"),
                    "port": app.port,
                    "db_path": os.path.abspath(app.db.path),
                    "autostart": autostart.is_enabled(),
                    "lan_ips": get_lan_ips(),
                    "today": today_str(),
                    "bg_image": cur.get("bg_image") or "",
                    "bg_opacity": float(cur.get("bg_opacity", 0.7)),
                }

            def _serve_bg(self):
                """后台设置页背景图片：按配置读取当前文件，返回图片字节。"""
                cur = {}
                try:
                    with open(app.config_path, "r", encoding="utf-8") as f:
                        cur = json.load(f)
                except Exception:
                    pass
                p = cur.get("bg_image") or ""
                ext = os.path.splitext(p)[1].lower()
                mime = {".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg", ".jfif": "image/jpeg", ".webp": "image/webp"}
                if p and os.path.isfile(p) and ext in mime:
                    try:
                        with open(p, "rb") as f:
                            data = f.read()
                        return self._send(200, data, mime[ext])
                    except OSError:
                        pass
                return self._err("未设置背景图片", 404)

            def _save_config(self):
                """保存配置：与当前文件合并，避免用启动时的旧配置覆盖悬浮窗已保存的位置等。"""
                try:
                    cur = {}
                    try:
                        with open(app.config_path, "r", encoding="utf-8") as f:
                            cur = json.load(f)
                    except Exception:
                        pass
                    cfg = dict(cur)
                    cfg.update(app.config)
                    cfg["autostart"] = autostart.is_enabled()
                    with open(app.config_path, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    app.log("保存 config.json 失败: %s" % e)

            def _sync_settings(self):
                app.db.set_setting("db_path", os.path.abspath(app.db.path))
                app.db.set_setting("port", str(app.port))

            def handle_error(self, *a):
                app.log("handler error: %s" % traceback.format_exc())

        return Handler
