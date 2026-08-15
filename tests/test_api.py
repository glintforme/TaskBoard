# -*- coding: utf-8 -*-
"""端到端 API 自测：独立启动临时服务 + 临时数据库，覆盖全部接口与数据迁移。

用法: python test_api.py   （退出码 0=全部通过）
"""
import base64
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))           # tests/
APP_DIR = os.path.join(os.path.dirname(BASE_DIR), "app")         # 应用代码目录
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

os.environ["TASKBALL_AI_OFFLINE"] = "1"  # 测试强制走内置模板，避免真实 API 依赖网络

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import db as dbmod
from db import DB
from server import ApiServer

TODAY = date.today().isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()

PASS = 0
FAIL = 0
FAILS = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  [FAIL] %s  %s" % (name, detail))


def http(base, path, method="GET", body=None):
    url = base + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            return resp.status, json.loads(raw)
        except Exception:
            return resp.status, raw


def expect_http_error(base, path, method="GET", body=None):
    try:
        http(base, path, method, body)
        return None
    except urllib.error.HTTPError as e:
        return e.code


def http_bin(base, path):
    """原始字节请求（图片等二进制资源）。"""
    with urllib.request.urlopen(base + path, timeout=15) as resp:
        return resp.status, resp.read(), resp.headers.get("Content-Type")


def main():
    global PASS, FAIL
    tmp = os.path.join(BASE_DIR, ".test_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    db_path = os.path.join(tmp, "test.db")
    cfg = {"host": "127.0.0.1", "port": 0, "db_path": db_path,
           "_config_path": os.path.join(tmp, "config.json")}
    db = DB(db_path)
    server = ApiServer("127.0.0.1", 0, db, cfg)
    server.start()
    base = "http://127.0.0.1:%d" % server.bound_port
    print("== 悬浮任务板端到端自测 ==")
    print("临时服务: %s  数据库: %s\n" % (base, db_path))

    # ---------- 基础 ----------
    print("== 基础与静态资源 ==")
    s, h = http(base, "/api/health")
    check("GET /api/health", s == 200 and h.get("ok") is True, str(h))
    s, idx = http(base, "/")
    check("GET / 设置页", s == 200 and "悬浮任务板" in str(idx), "status=%s" % s)
    s, css = http(base, "/style.css")
    check("GET /style.css", s == 200 and len(css) > 500, str(s))
    s, js = http(base, "/app.js")
    check("GET /app.js", s == 200 and "悬浮任务板" in str(js), str(s))
    code = expect_http_error(base, "/api/not-exist")
    check("未知接口返回 404", code == 404, str(code))

    # ---------- 背景图片接口 ----------
    print("\n== 背景图片接口 ==")
    PNG_1PX = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    cfg_file = os.path.join(tmp, "config.json")
    bg_png = os.path.join(tmp, "bg.png")
    with open(bg_png, "wb") as f:
        f.write(PNG_1PX)
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump({"bg_image": bg_png, "bg_opacity": 0.66}, f, ensure_ascii=False)
    st, body, ctype = http_bin(base, "/api/bg")
    check("GET /api/bg 返回背景图片", st == 200 and body == PNG_1PX and ctype == "image/png",
          "status=%s type=%s" % (st, ctype))
    s, r = http(base, "/api/config")
    check("配置含背景图信息",
          s == 200 and r["config"].get("bg_image") == bg_png
          and abs(r["config"].get("bg_opacity", 0) - 0.66) < 0.001, str(r))
    bg_jpg = os.path.join(tmp, "bg.jpg")
    with open(bg_jpg, "wb") as f:
        f.write(PNG_1PX)
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump({"bg_image": bg_jpg}, f, ensure_ascii=False)
    st, body, ctype = http_bin(base, "/api/bg")
    check("GET /api/bg jpg 扩展名类型", st == 200 and ctype == "image/jpeg",
          "status=%s type=%s" % (st, ctype))
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False)
    code = expect_http_error(base, "/api/bg")
    check("未设置背景图返回 404", code == 404, str(code))
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump({"db_path": db_path}, f, ensure_ascii=False)

    # ---------- 任务 CRUD ----------
    print("\n== 任务增删改查 ==")
    s, r = http(base, "/api/tasks", "POST", {"title": "晨跑 3 公里", "scope": "daily", "note": "健康"})
    check("添加日常任务", s == 200 and r.get("ok"), str(r))
    daily_id = r["task"]["id"]

    s, r = http(base, "/api/tasks", "POST", {"title": "整理周报", "scope": "today", "due_date": TODAY})
    check("添加今日任务", s == 200, str(r))
    today_id = r["task"]["id"]

    s, r = http(base, "/api/tasks", "POST", {"title": "预约会议室", "scope": "tomorrow", "due_date": TOMORROW})
    check("添加明日任务", s == 200, str(r))

    s, r = http(base, "/api/tasks", "POST", {"title": "周末大扫除", "scope": "week", "due_date": TODAY})
    check("添加周常任务", s == 200, str(r))

    s, r = http(base, "/api/tasks", "POST", {"title": "体检预约", "scope": "month", "due_date": TODAY})
    check("添加月常任务", s == 200, str(r))

    code = expect_http_error(base, "/api/tasks", "POST", {"title": "", "scope": "today"})
    check("空标题被拒绝(400)", code == 400, "code=%s" % code)

    s, r = http(base, "/api/tasks?scope=today")
    titles = [t["title"] for t in r.get("tasks", [])]
    check("今日视图含日常+今日任务", s == 200 and "晨跑 3 公里" in titles and "整理周报" in titles,
          str(titles))

    s, r = http(base, "/api/tasks?scope=tomorrow")
    check("明日视图", s == 200 and len(r.get("tasks", [])) == 1, str(r))

    s, r = http(base, "/api/tasks?scope=all&q=" + urllib.parse.quote("体检"))
    check("全部任务搜索", s == 200 and len(r.get("tasks", [])) == 1, str(r))

    s, r = http(base, "/api/tasks/%d" % today_id, "PUT",
                {"title": "整理周报（改）", "note": "补充备注"})
    check("更新任务", s == 200 and r["task"]["title"] == "整理周报（改）", str(r))

    # ---------- 完成记录 ----------
    print("\n== 完成任务与记录 ==")
    s, r = http(base, "/api/tasks/%d/complete" % daily_id, "POST", {})
    check("完成日常任务", s == 200 and r.get("ok"), str(r))
    code = expect_http_error(base, "/api/tasks/%d/complete" % daily_id, "POST", {})
    check("同日重复完成被拒绝(409)", code == 409, str(code))

    http(base, "/api/tasks/%d/complete" % today_id, "POST", {})
    http(base, "/api/tasks/%d/complete" % today_id, "POST", {"date": "2020-01-01"})

    s, r = http(base, "/api/stats")
    st = r.get("stats", {})
    check("今日完成数=2", st.get("today_completed") == 2, str(st))
    check("总完成数=3", st.get("total_completed") == 3, str(st))
    check("日常任务数=1", st.get("daily_count") == 1, str(st))
    check("周常/月常计数", st.get("week_count") == 1 and st.get("month_count") == 1, str(st))

    s, r = http(base, "/api/tasks/float")
    g = r.get("groups", {})
    check("悬浮窗数据-今日组含日常", any(t["id"] == daily_id and t.get("completed_today") for t in g.get("today", [])),
          str(g.get("today")))
    check("悬浮窗数据-周常/月常", len(g.get("week", [])) == 1 and len(g.get("month", [])) == 1, "")

    s, r = http(base, "/api/completions")
    comps = r.get("completions", [])
    check("完成记录列表=3", len(comps) == 3, str(len(comps)))
    check("记录含任务标题", any(c.get("title") == "晨跑 3 公里" for c in comps), str(comps[:2]))

    s, r = http(base, "/api/tasks/%d/uncomplete" % daily_id, "POST", {})
    check("撤销完成", s == 200 and r.get("ok"), str(r))
    s, r = http(base, "/api/stats")
    check("撤销后今日完成=1", r["stats"]["today_completed"] == 1, str(r["stats"]))

    # ---------- AI ----------
    print("\n== AI 生成（无 Key 时应降级模板） ==")
    s, r = http(base, "/api/ai/generate", "POST",
                {"scopes": ["today", "tomorrow", "week", "month"],
                 "context": "我是一名程序员，近期在赶项目", "count": 4})
    check("AI 接口可调用", s == 200 and r.get("ok"), str(r)[:300])
    results = r.get("results", {})
    check("四类均返回任务", all(len(results.get(k, {}).get("tasks", [])) > 0
                                for k in ["today", "tomorrow", "week", "month"]),
          {k: len(v.get("tasks", [])) for k, v in results.items()})
    check("任务条目标题非空", all(t.get("title") for k, v in results.items() for t in v.get("tasks", [])))
    s, r = http(base, "/api/ai/status")
    check("AI 状态接口", s == 200 and "all" in r and "openai" in r, str(r)[:200])

    # ---------- 提醒与截止时间 ----------
    print("\n== 提醒与截止时间 ==")
    s, r = http(base, "/api/tasks", "POST", {"title": "吃药提醒", "scope": "daily",
                                             "remind_at": "2000-01-01T09:00",
                                             "deadline": "2099-01-01T18:00"})
    check("任务保存提醒/截止时间", r["task"].get("remind_at") == "2000-01-01T09:00"
          and r["task"].get("deadline") == "2099-01-01T18:00", str(r))
    rid = r["task"]["id"]

    s, r = http(base, "/api/reminders/due")
    check("已到提醒时间则返回任务", any(x["id"] == rid for x in r.get("reminders", [])),
          str([x["title"] for x in r.get("reminders", [])]))
    s, r = http(base, "/api/reminders/due")
    check("再次查询不重复提醒（当天去重）", all(x["id"] != rid for x in r.get("reminders", [])),
          str([x["title"] for x in r.get("reminders", [])]))

    s, r = http(base, "/api/tasks", "POST", {"title": "未来提醒", "scope": "today",
                                             "due_date": TODAY, "remind_at": "2999-01-01T09:00"})
    future_id = r["task"]["id"]
    s, r = http(base, "/api/reminders/due")
    check("未到提醒时间不提醒", all(x["id"] != future_id for x in r.get("reminders", [])), "")

    s, r = http(base, "/api/tasks", "POST", {"title": "已完成的提醒", "scope": "daily",
                                             "remind_at": "2000-01-01T08:00"})
    done_remind_id = r["task"]["id"]
    http(base, "/api/tasks/%d/complete" % done_remind_id, "POST", {})
    s, r = http(base, "/api/reminders/due")
    check("当天已完成的到期任务不提醒", all(x["id"] != done_remind_id for x in r.get("reminders", [])), "")

    s, r = http(base, "/api/tasks/%d" % rid, "PUT", {"deadline": "2000-01-01T08:00"})
    check("更新截止时间", s == 200 and r["task"]["deadline"] == "2000-01-01T08:00", str(r))

    # ---------- 开始时间/生效截止/未完成数/编辑 ----------
    print("\n== 开始时间/生效截止/未完成数/编辑 ==")
    s, r = http(base, "/api/tasks", "POST", {"title": "无时间任务", "scope": "today", "due_date": TODAY})
    t0 = r["task"]
    check("未设开始时间自动取当前时间", bool(t0.get("start_at")), str(t0))
    eff = t0.get("effective_deadline")
    check("未设截止时间按开始+24h", bool(eff) and eff > TODAY + "T00:00", str(eff))

    s, r = http(base, "/api/tasks", "POST", {"title": "逾期任务", "scope": "today", "due_date": TODAY,
                                             "deadline": "2000-01-01T00:00"})
    over_id = r["task"]["id"]
    http(base, "/api/tasks", "POST", {"title": "日常逾期不算", "scope": "daily",
                                      "deadline": "2000-01-01T00:00"})
    s, st = http(base, "/api/stats")
    check("未完成数=1(仅非日常逾期)", st["stats"]["overdue_count"] == 1,
          str(st["stats"]["overdue_count"]))
    http(base, "/api/tasks/%d/complete" % over_id, "POST", {})
    s, st = http(base, "/api/stats")
    check("完成后未完成数清零", st["stats"]["overdue_count"] == 0, str(st["stats"]))

    s, r = http(base, "/api/tasks/%d" % t0["id"], "PUT",
                {"title": "无时间任务改", "note": "补充内容", "deadline": "2030-01-01T12:00",
                 "start_at": "2026-08-15T09:00"})
    check("编辑标题与备注", r["task"]["title"] == "无时间任务改" and r["task"]["note"] == "补充内容", str(r))
    check("编辑截止时间生效", r["task"]["deadline"] == "2030-01-01T12:00", str(r))
    check("编辑开始时间生效", r["task"]["start_at"] == "2026-08-15T09:00", str(r))
    s, r = http(base, "/api/tasks/%d" % t0["id"], "PUT", {"deadline": None})
    check("清空截止时间恢复为开始+24h", r["task"]["deadline"] is None
          and bool(r["task"]["effective_deadline"]), str(r))
    s, r = http(base, "/api/tasks/%d" % t0["id"], "PUT", {"title": "仅改标题"})
    check("未传字段保持原值", r["task"]["note"] == "补充内容", str(r))

    s, r = http(base, "/api/tasks", "POST", {"title": "改提醒", "scope": "daily",
                                             "remind_at": "2000-01-01T07:00"})
    rid2 = r["task"]["id"]
    s, r = http(base, "/api/reminders/due")
    check("首次到期提醒返回", any(x["id"] == rid2 for x in r.get("reminders", [])),
          str([x["id"] for x in r.get("reminders", [])]))
    s, r = http(base, "/api/reminders/due")
    check("再次查询已去重", all(x["id"] != rid2 for x in r.get("reminders", [])))
    http(base, "/api/tasks/%d" % rid2, "PUT", {"remind_at": "2000-01-01T08:00"})
    s, r = http(base, "/api/reminders/due")
    check("编辑提醒时间后重置去重、重新触发", any(x["id"] == rid2 for x in r.get("reminders", [])),
          str([x["id"] for x in r.get("reminders", [])]))

    s, r = http(base, "/api/ai/generate", "POST", {"scopes": ["daily"], "context": "程序员", "count": 3})
    check("AI 可生成日常任务", s == 200 and len(r["results"]["daily"]["tasks"]) > 0, str(r)[:200])

    # ---------- 搜索接口 ----------
    print("\n== 搜索接口 ==")
    s, r = http(base, "/api/tasks/search?q=" + urllib.parse.quote("晨跑"))
    check("搜索接口按关键词", s == 200 and any("晨跑" in t["title"] for t in r["tasks"]), str(r)[:200])
    check("搜索结果含完成信息", bool(r["tasks"]) and "done_count" in r["tasks"][0]
          and "last_completed_at" in r["tasks"][0], str(r["tasks"][0])[:120])
    s, r = http(base, "/api/tasks/search?scope=done&page=1&page_size=5")
    check("搜索分类已完成+分页", r["page"] == 1 and r["page_size"] == 5 and r["pages"] >= 1
          and bool(r["tasks"]) and all(t.get("done_count", 0) > 0 for t in r["tasks"]),
          str(r)[:250])
    s, r = http(base, "/api/tasks/search?scope=undone&q=" + urllib.parse.quote("仅改标题"))
    check("搜索分类未完成+关键词", r["total"] >= 1
          and all(t.get("done_count", 0) == 0 for t in r["tasks"]), str(r)[:250])

    # ---------- 旧任务截止时间迁移 ----------
    print("\n== 旧任务截止时间迁移 ==")
    mig_db = os.path.join(tmp, "mig.db")
    if os.path.exists(mig_db):
        os.remove(mig_db)
    conn = sqlite3.connect(mig_db)
    conn.executescript(dbmod.SCHEMA)
    now_s = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute("INSERT INTO tasks(title, scope, created_at, updated_at) VALUES('旧任务', 'today', ?, ?)",
                 (now_s, now_s))
    conn.commit()
    conn.close()
    mig = dbmod.DB(mig_db)
    old = mig._one("SELECT deadline, start_at, created_at FROM tasks WHERE title='旧任务'")
    expect_dl = (datetime.fromisoformat(now_s) + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
    check("旧任务无截止时间被迁移为+24h", old["deadline"] == expect_dl, str(old))
    # 迁移只执行一次：清空后重启不会被再次设置
    conn = sqlite3.connect(mig_db)
    conn.execute("UPDATE tasks SET deadline=NULL WHERE title='旧任务'")
    conn.commit()
    conn.close()
    mig2 = dbmod.DB(mig_db)
    old2 = mig2._one("SELECT deadline FROM tasks WHERE title='旧任务'")
    check("迁移标记后清空的截止时间不被重置", old2["deadline"] is None, str(old2))
    mig2.close()

    # ---------- 删除 ----------
    print("\n== 删除 ==")
    http(base, "/api/tasks/%d/complete" % daily_id, "POST", {})  # 今天再完成一次，保证今日有记录
    s, r = http(base, "/api/tasks/%d" % today_id, "DELETE")
    check("删除任务", s == 200 and r.get("ok"), str(r))
    s, r = http(base, "/api/completions?date=%s" % TODAY)
    check("删除任务级联删除其完成记录", all(c["task_id"] != today_id for c in r.get("completions", [])), "")
    check("今日完成记录=3(晨跑+已完成的提醒+逾期任务)", len(r.get("completions", [])) == 3,
          str(len(r.get("completions", []))))
    first_cid = r["completions"][0]["id"]
    s, r = http(base, "/api/completions/%d" % first_cid, "DELETE")
    check("删除单条完成记录", s == 200, str(r))

    # ---------- 配置与迁移 ----------
    print("\n== 配置与数据库迁移 ==")
    s, r = http(base, "/api/config")
    check("读取配置", s == 200 and r["config"]["db_path"] == os.path.abspath(db_path), str(r)[:200])

    new_db = os.path.join(tmp, "moved", "task.db")
    s, r = http(base, "/api/config", "POST", {"db_path": new_db})
    check("修改数据库路径并迁移", s == 200 and r.get("db_migrated"), str(r)[:300])
    s, r = http(base, "/api/tasks?scope=all")
    check("迁移后任务仍在", any(t["title"] == "晨跑 3 公里" for t in r.get("tasks", [])),
          str([t["title"] for t in r.get("tasks", [])]))
    s, r = http(base, "/api/stats")
    check("迁移后任务总数正确", r["stats"]["total_tasks"] == 11, str(r["stats"]))
    s, r = http(base, "/api/tasks/%d/complete" % daily_id, "POST", {})
    check("迁移后可正常写入完成记录", s == 200 and r.get("ok"), str(r))
    s, r = http(base, "/api/stats")
    check("迁移后统计正确", r["stats"]["total_completed"] == 3, str(r["stats"]))

    s, r = http(base, "/api/config", "POST", {"port": 65535})
    check("修改端口(重启后生效)", s == 200 and r.get("restart_required"), str(r)[:200])
    code = expect_http_error(base, "/api/config", "POST", {"port": 99999})
    check("非法端口被拒绝(400)", code == 400, str(code))
    # 设置页保存配置时不应覆盖悬浮窗已保存的位置
    cfg_file = os.path.join(tmp, "config.json")
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump({"port": 39999, "gui_pos": [777, 333]}, f)
    s, r = http(base, "/api/config", "POST", {"port": 40001})
    with open(cfg_file, "r", encoding="utf-8") as f:
        saved = json.load(f)
    check("设置保存不覆盖悬浮窗位置", saved.get("gui_pos") == [777, 333], str(saved))

    # ---------- 收尾 ----------
    server.stop()
    db.close()
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n======================")
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    if FAILS:
        print("失败项: %s" % "; ".join(FAILS))
    print("======================")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
