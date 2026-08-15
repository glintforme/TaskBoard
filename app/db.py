# -*- coding: utf-8 -*-
"""SQLite 数据层：任务表、完成记录表、设置表。"""
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta

SCOPES = ("daily", "today", "tomorrow", "week", "month")
SCOPE_LABELS = {
    "daily": "日常任务",
    "today": "今日任务",
    "tomorrow": "明日任务",
    "week": "周常任务",
    "month": "月常任务",
}

_KEEP = object()  # update_task 哨兵：未提供的字段保持原值（显式 None 表示清空）

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  scope TEXT NOT NULL,
  due_date TEXT,
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  sort_order INTEGER DEFAULT 0,
  remind_at TEXT,
  deadline TEXT,
  start_at TEXT
);
CREATE TABLE IF NOT EXISTS completions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  completed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_completions_task_date ON completions(task_id, date);
CREATE INDEX IF NOT EXISTS idx_completions_date ON completions(date);
CREATE TABLE IF NOT EXISTS reminders (
  task_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY (task_id, date)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

TASK_COLUMNS = ("id", "title", "scope", "due_date", "note", "created_at",
                "updated_at", "sort_order", "remind_at", "deadline", "start_at")


def today_str(d=None):
    d = d or date.today()
    return d.isoformat()


def parse_date(s, default=None):
    if not s:
        return default
    try:
        return date.fromisoformat(str(s))
    except (ValueError, TypeError):
        return default


def week_range(d=None):
    """ISO 周（周一起）范围。"""
    d = d or date.today()
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def month_range(d=None):
    d = d or date.today()
    start = d.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1) - timedelta(days=1)
    else:
        end = start.replace(month=start.month + 1) - timedelta(days=1)
    return start, end


class DB:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            self._conn.executescript(SCHEMA)
            self._migrate_columns()
            self._migrate_deadlines()
            self._conn.commit()

    def _migrate_deadlines(self):
        """一次性迁移：以前部署的没有截止时间的任务，统一设置为「开始（或创建）时间 + 24 小时」。
        用 settings 标志位保证只执行一次，避免覆盖用户后续手动清空的截止时间。"""
        if self.get_setting("deadlines_migrated") == "1":
            return
        rows = self._rows("SELECT id, start_at, created_at FROM tasks WHERE deadline IS NULL")
        for r in rows:
            base = r["start_at"] or r["created_at"]
            try:
                d = datetime.fromisoformat(str(base))
                dl = (d + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
                self._conn.execute("UPDATE tasks SET deadline=? WHERE id=?", (dl, r["id"]))
            except (ValueError, TypeError):
                pass
        self.set_setting("deadlines_migrated", "1")

    def _migrate_columns(self):
        """旧库自动补列（remind_at / deadline / start_at）。"""
        try:
            cols = {r["name"] for r in self._rows("PRAGMA table_info(tasks)")}
        except Exception:
            cols = set()
        for col in ("remind_at", "deadline", "start_at"):
            if col not in cols:
                try:
                    self._conn.execute("ALTER TABLE tasks ADD COLUMN %s TEXT" % col)
                except sqlite3.Error:
                    pass

    # ---------- 基础 ----------
    def _rows(self, sql, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]
        return rows

    def _one(self, sql, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            r = cur.fetchone()
        return dict(r) if r else None

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ---------- 任务 ----------
    def add_task(self, title, scope, due_date=None, note="", sort_order=0,
                 remind_at=None, deadline=None, start_at=None):
        title = (title or "").strip()
        if not title:
            raise ValueError("任务标题不能为空")
        if scope not in SCOPES:
            raise ValueError("无效的任务类型: %s" % scope)
        now = datetime.now().isoformat(timespec="seconds")
        start_at = start_at or now  # 未设定开始时间 → 按创建（编辑）时间开始
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks(title, scope, due_date, note, created_at, updated_at,"
                " sort_order, remind_at, deadline, start_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (title, scope, due_date, note or "", now, now, int(sort_order),
                 remind_at or None, deadline or None, start_at),
            )
            self._conn.commit()
            return self.decorate(self.get_task(cur.lastrowid))

    def update_task(self, task_id, title=None, scope=None, due_date=_KEEP, note=None,
                    sort_order=None, remind_at=_KEEP, deadline=_KEEP, start_at=_KEEP):
        task = self.get_task(task_id)
        if not task:
            return None
        now = datetime.now().isoformat(timespec="seconds")
        if start_at is not _KEEP and not start_at:
            start_at = now  # 显式清空开始时间 → 按编辑时间开始

        def keep(cur, new):
            return cur if new is _KEEP else new

        fields = {
            "title": title if title is not None else task["title"],
            "scope": scope if scope is not None else task["scope"],
            "due_date": keep(task.get("due_date"), due_date),
            "note": note if note is not None else task.get("note", ""),
            "sort_order": int(sort_order) if sort_order is not None else task.get("sort_order", 0),
            "remind_at": keep(task.get("remind_at"), remind_at),
            "deadline": keep(task.get("deadline"), deadline),
            "start_at": keep(task.get("start_at"), start_at),
            "updated_at": now,
        }
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET title=?, scope=?, due_date=?, note=?, sort_order=?,"
                " remind_at=?, deadline=?, start_at=?, updated_at=? WHERE id=?",
                (fields["title"], fields["scope"], fields["due_date"], fields["note"],
                 fields["sort_order"], fields["remind_at"], fields["deadline"],
                 fields["start_at"], now, task_id),
            )
            # 提醒时间变化时，重置当天已提醒记录（否则新的提醒时间会被旧记录挡住）
            if remind_at is not _KEEP and fields["remind_at"] != task.get("remind_at"):
                self._conn.execute("DELETE FROM reminders WHERE task_id=?", (task_id,))
            self._conn.commit()
        return self.decorate(self.get_task(task_id))

    def delete_task(self, task_id):
        with self._lock:
            self._conn.execute("DELETE FROM completions WHERE task_id=?", (task_id,))
            self._conn.execute("DELETE FROM reminders WHERE task_id=?", (task_id,))
            self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._conn.commit()
        return True

    def get_task(self, task_id):
        return self.decorate(self._one("SELECT * FROM tasks WHERE id=?", (task_id,)))

    # ---------- 生效截止时间 / 逾期 ----------
    @staticmethod
    def effective_deadline(task):
        """生效截止时间：设置了 deadline 用之；否则按开始时间 +24 小时（未设开始则无）。"""
        if not task:
            return None
        dl = task.get("deadline")
        if dl:
            return str(dl)
        st = task.get("start_at")
        try:
            d = datetime.fromisoformat(str(st))
            return (d + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
        except (ValueError, TypeError):
            return None

    @staticmethod
    def decorate(task):
        """为任务补充 start_at（兜底）与 effective_deadline 字段。"""
        if task is None:
            return None
        task = dict(task)
        task.setdefault("start_at", None)
        task["effective_deadline"] = DB.effective_deadline(task)
        return task

    def list_tasks(self, scope=None, due_date=None, q=None, include_daily=False, limit=500):
        """按条件列出任务。scope='today' 表示查看“今日”（含日常任务）。"""
        sql = "SELECT * FROM tasks WHERE 1=1"
        args = []
        if scope == "today":
            if include_daily:
                sql += " AND (scope='daily' OR (scope='today' AND due_date=?) OR (scope='today' AND due_date IS NULL))"
                args.append(due_date or today_str())
            else:
                sql += " AND (scope='today' AND (due_date=? OR due_date IS NULL))"
                args.append(due_date or today_str())
        elif scope == "tomorrow":
            sql += " AND scope='tomorrow' AND (due_date=? OR due_date IS NULL)"
            args.append(due_date or (date.today() + timedelta(days=1)).isoformat())
        elif scope == "week":
            sql += " AND scope='week'"
            if due_date:
                sql += " AND (due_date=? OR due_date IS NULL)"
                args.append(due_date)
        elif scope == "month":
            sql += " AND scope='month'"
            if due_date:
                sql += " AND (due_date=? OR due_date IS NULL)"
                args.append(due_date)
        elif scope:
            sql += " AND scope=?"
            args.append(scope)
        if q:
            sql += " AND (title LIKE ? OR note LIKE ?)"
            args += ["%" + q + "%", "%" + q + "%"]
        sql += " ORDER BY sort_order ASC, id ASC LIMIT ?"
        args.append(limit)
        return [self.decorate(r) for r in self._rows(sql, args)]

    def tasks_for_float(self):
        """悬浮窗数据：今日/明日/周常/月常 分组。"""
        t = today_str()
        tmr = (date.today() + timedelta(days=1)).isoformat()
        ws, we = week_range()
        ms, me = month_range()
        today_tasks = self._rows(
            "SELECT * FROM tasks WHERE scope='daily' OR (scope='today' AND (due_date=? OR due_date IS NULL))"
            " ORDER BY sort_order, id", (t,))
        tomorrow_tasks = self._rows(
            "SELECT * FROM tasks WHERE scope='tomorrow' AND (due_date=? OR due_date IS NULL)"
            " ORDER BY sort_order, id", (tmr,))
        week_tasks = self._rows(
            "SELECT * FROM tasks WHERE scope='week' AND (due_date IS NULL OR (due_date>=? AND due_date<=?))"
            " ORDER BY sort_order, id", (ws.isoformat(), we.isoformat()))
        month_tasks = self._rows(
            "SELECT * FROM tasks WHERE scope='month' AND (due_date IS NULL OR (due_date>=? AND due_date<=?))"
            " ORDER BY sort_order, id", (ms.isoformat(), me.isoformat()))
        return {
            "today": [self.decorate(t) for t in today_tasks],
            "tomorrow": [self.decorate(t) for t in tomorrow_tasks],
            "week": [self.decorate(t) for t in week_tasks],
            "month": [self.decorate(t) for t in month_tasks],
        }

    # ---------- 完成记录 ----------
    def complete_task(self, task_id, on_date=None):
        """标记完成。同一天同一任务只记录一次。"""
        task = self.get_task(task_id)
        if not task:
            return {"ok": False, "reason": "任务不存在"}
        on_date = on_date or today_str()
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO completions(task_id, date, completed_at) VALUES(?,?,?)",
                (task_id, on_date, now))
            self._conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "reason": "该任务今天已完成", "task_id": task_id, "date": on_date}
        return {"ok": True, "task_id": task_id, "date": on_date,
                "completion": self._one(
                    "SELECT * FROM completions WHERE task_id=? AND date=?", (task_id, on_date))}

    def uncomplete_task(self, task_id, on_date=None):
        on_date = on_date or today_str()
        with self._lock:
            self._conn.execute(
                "DELETE FROM completions WHERE task_id=? AND date=?", (task_id, on_date))
            self._conn.commit()
        return {"ok": True, "task_id": task_id, "date": on_date}

    def delete_completion(self, completion_id):
        with self._lock:
            self._conn.execute("DELETE FROM completions WHERE id=?", (completion_id,))
            self._conn.commit()
        return True

    def list_completions(self, on_date=None, limit=500):
        sql = ("SELECT c.id AS cid, c.task_id, c.date, c.completed_at, t.title, t.scope"
               " FROM completions c LEFT JOIN tasks t ON c.task_id=t.id WHERE 1=1")
        args = []
        if on_date:
            sql += " AND c.date=?"
            args.append(on_date)
        sql += " ORDER BY c.completed_at DESC, c.id DESC LIMIT ?"
        args.append(limit)
        rows = self._rows(sql, args)
        for r in rows:
            r["id"] = r.pop("cid")
            r.setdefault("title", "(已删除的任务)")
        return rows

    # ---------- 搜索 ----------
    def search_tasks(self, q="", scope=None, page=1, page_size=10):
        """搜索全部任务：按关键词/分类过滤，分页返回，并附带完成次数与最近完成时间。"""
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 10), 100))
        where = " WHERE 1=1"
        args = []
        if q:
            where += " AND (t.title LIKE ? OR t.note LIKE ?)"
            args += ["%" + q + "%", "%" + q + "%"]
        if scope == "done":
            where += " AND EXISTS (SELECT 1 FROM completions c WHERE c.task_id = t.id)"
        elif scope == "undone":
            where += " AND NOT EXISTS (SELECT 1 FROM completions c WHERE c.task_id = t.id)"
        elif scope in SCOPES:
            where += " AND t.scope = ?"
            args.append(scope)
        total = self._one("SELECT COUNT(*) AS n FROM tasks t" + where, args)["n"]
        rows = self._rows(
            "SELECT t.*,"
            " (SELECT COUNT(*) FROM completions c WHERE c.task_id = t.id) AS done_count,"
            " (SELECT MAX(c.completed_at) FROM completions c WHERE c.task_id = t.id) AS last_completed_at"
            " FROM tasks t" + where + " ORDER BY t.id DESC LIMIT ? OFFSET ?",
            args + [page_size, (page - 1) * page_size])
        return {
            "tasks": [self.decorate(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
        }

    # ---------- 提醒 ----------
    def due_reminders(self, now_str=None):
        """返回已到提醒时间、今天尚未提醒、且今天未完成的任务；同时标记已提醒（当天去重）。

        now_str 形如 'YYYY-MM-DDTHH:MM'，ISO 格式可直接按字典序比较。
        """
        now_str = now_str or datetime.now().strftime("%Y-%m-%dT%H:%M")
        t = today_str()
        due = []
        with self._lock:
            rows = self._rows(
                "SELECT id, title, note, scope, due_date, remind_at, deadline FROM tasks"
                " WHERE remind_at IS NOT NULL AND remind_at != '' AND remind_at <= ?"
                " AND id NOT IN (SELECT task_id FROM reminders WHERE date=?)"
                " AND id NOT IN (SELECT task_id FROM completions WHERE date=?)"
                " ORDER BY remind_at ASC",
                (now_str, t, t))
            for r in rows:
                due.append(r)
                self._conn.execute(
                    "INSERT OR IGNORE INTO reminders(task_id, date, sent_at) VALUES(?,?,?)",
                    (r["id"], t, datetime.now().isoformat(timespec="seconds")))
            self._conn.commit()
        return due

    def completed_dates(self, task_id):
        return [r["date"] for r in self._rows(
            "SELECT date FROM completions WHERE task_id=?", (task_id,))]

    # ---------- 统计 ----------
    def stats(self):
        t = today_str()
        ws, we = week_range()
        ms, me = month_range()
        def cnt(sql, args=()):
            return self._one(sql, args)["n"]
        daily_count = cnt("SELECT COUNT(*) AS n FROM tasks WHERE scope='daily'")
        today_count = cnt(
            "SELECT COUNT(*) AS n FROM tasks WHERE scope='today' AND (due_date=? OR due_date IS NULL)", (t,))
        tomorrow_count = cnt(
            "SELECT COUNT(*) AS n FROM tasks WHERE scope='tomorrow' AND (due_date=? OR due_date IS NULL)",
            ((date.today() + timedelta(days=1)).isoformat(),))
        week_count = cnt(
            "SELECT COUNT(*) AS n FROM tasks WHERE scope='week' AND (due_date IS NULL OR (due_date>=? AND due_date<=?))",
            (ws.isoformat(), we.isoformat()))
        month_count = cnt(
            "SELECT COUNT(*) AS n FROM tasks WHERE scope='month' AND (due_date IS NULL OR (due_date>=? AND due_date<=?))",
            (ms.isoformat(), me.isoformat()))
        today_completed = cnt("SELECT COUNT(*) AS n FROM completions WHERE date=?", (t,))
        week_completed = cnt(
            "SELECT COUNT(*) AS n FROM completions WHERE date>=? AND date<=?", (ws.isoformat(), we.isoformat()))
        month_completed = cnt(
            "SELECT COUNT(*) AS n FROM completions WHERE date>=? AND date<=?", (ms.isoformat(), me.isoformat()))
        total_completed = cnt("SELECT COUNT(*) AS n FROM completions")
        total_tasks = cnt("SELECT COUNT(*) AS n FROM tasks")
        # 未完成数：非日常任务到达（生效）截止时间仍未完成
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
        overdue_count = 0
        for r in self._rows("SELECT id, scope, deadline, start_at FROM tasks"):
            if r["scope"] == "daily":
                continue
            eff = self.effective_deadline(r)
            if not eff or eff >= now:
                continue
            if self._one("SELECT 1 AS x FROM completions WHERE task_id=?", (r["id"],)) is None:
                overdue_count += 1
        return {
            "today_date": t,
            "daily_count": daily_count,
            "today_count": today_count,
            "tomorrow_count": tomorrow_count,
            "week_count": week_count,
            "month_count": month_count,
            "today_completed": today_completed,
            "week_completed": week_completed,
            "month_completed": month_completed,
            "total_completed": total_completed,
            "total_tasks": total_tasks,
            "overdue_count": overdue_count,
        }

    # ---------- 设置 ----------
    def set_setting(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
            self._conn.commit()

    def get_setting(self, key, default=None):
        r = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def all_settings(self):
        return {r["key"]: r["value"] for r in self._rows("SELECT key, value FROM settings")}

    # ---------- 迁移 ----------
    def migrate_to(self, new_path):
        """把当前数据库完整复制到新路径（含 schema 与数据）。"""
        new_path = os.path.abspath(new_path)
        if os.path.normcase(new_path) == os.path.normcase(self.path):
            return {"ok": True, "path": new_path, "moved": False}
        os.makedirs(os.path.dirname(new_path) or ".", exist_ok=True)
        with self._lock:
            src = self._conn
            dst = sqlite3.connect(new_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
            self._conn.close()
        self.path = new_path
        self._conn = sqlite3.connect(new_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
        return {"ok": True, "path": new_path, "moved": True}
