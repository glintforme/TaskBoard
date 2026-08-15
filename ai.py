# -*- coding: utf-8 -*-
"""AI 任务生成：从系统环境变量读取 API Key，按可用性选择模型。

- openai-apikey     : 暂不可用（按需求跳过）
- gemini-apikey     : 模型 gemini-3.7flash
- kimik3-apikey     : 模型 kimi-k3
- deepseek-apikey   : 模型 deepseek-v4-flash

未配置任何 Key 或调用失败时，自动降级为内置模板，保证功能可用。
"""
import json
import os
import re
import urllib.error
import urllib.request

try:
    import winreg
except ImportError:  # 非 Windows 环境
    winreg = None

# 自动选择顺序：DeepSeek → Kimi → Gemini
# （DeepSeek / Kimi(月之暗面) 国内可直连；Gemini 可能受网络环境影响，可在设置页手动指定）
PROVIDERS = [
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "env": ["DEEPSEEK_API_KEY", "deepseek-apikey"],
        "model": "deepseek-v4-flash",
        "url": "https://api.deepseek.com/chat/completions",
    },
    {
        "id": "kimi",
        "label": "Kimi",
        "env": ["MOONSHOT_API_KEY", "KIMIK3_API_KEY", "KIMI_K3_API_KEY", "kimik3-apikey"],
        "model": "kimi-k3",
        "url": "https://api.moonshot.cn/v1/chat/completions",
    },
    {
        "id": "gemini",
        "label": "Gemini",
        "env": ["GOOGLE_API_KEY", "GEMINI_API_KEY", "gemini-apikey"],
        "model": "gemini-3.7flash",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    },
]

OPENAI = {
    "id": "openai",
    "label": "OpenAI",
    "env": ["OPENAI_API_KEY", "openai-apikey"],
    "model": "",
    "url": "",
    "unavailable": "openai-apikey / OPENAI_API_KEY 暂不可用，请改用 GOOGLE_API_KEY(gemini) / MOONSHOT_API_KEY(kimi) / DEEPSEEK_API_KEY(deepseek)",
}

SCOPE_DESC = {
    "daily": "每日日常",
    "today": "今日",
    "tomorrow": "明日",
    "week": "本周内",
    "month": "本月内",
}


def _offline():
    """测试用：TASKBALL_AI_OFFLINE=1 时强制走内置模板，不发起真实 API 调用。"""
    return os.environ.get("TASKBALL_AI_OFFLINE") == "1"


def _registry_env():
    """从注册表读取用户/系统环境变量（无需重启进程即可感知 setx/系统属性 的新变量）。"""
    if winreg is None:
        return {}
    vals = {}
    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    paths = (r"Environment", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
    for root in roots:
        for key_path in paths:
            try:
                with winreg.OpenKey(root, key_path) as k:
                    i = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(k, i)
                            if isinstance(value, str):
                                vals[name] = value
                            i += 1
                        except OSError:
                            break
            except OSError:
                continue
    return vals


def _find_env(names):
    """优先进程环境变量，其次注册表（用户级 -> 系统级）。"""
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    reg = _registry_env()
    for n in names:
        v = reg.get(n)
        if v and v.strip():
            return v.strip()
    return None


def detect_providers():
    """检测当前环境可用的 Key。"""
    out = []
    for p in PROVIDERS:
        key = None if _offline() else _find_env(p["env"])
        out.append({
            "id": p["id"], "label": p["label"], "model": p["model"],
            "has_key": bool(key),
            "key_masked": (key[:6] + "****" + key[-4:]) if key and len(key) > 10 else (key[:6] + "****" if key else ""),
        })
    return out


def default_provider():
    for p in detect_providers():
        if p["has_key"]:
            return p["id"]
    return None


def ai_status():
    return {
        "available": [p for p in detect_providers() if p["has_key"]],
        "all": detect_providers(),
        "openai": {**OPENAI, "has_key": bool(_find_env(OPENAI["env"]))},
        "default": default_provider(),
    }


# ---------------- 调用各模型 ----------------

def _http_json(url, payload, headers=None, timeout=25):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _call_gemini(provider, key, prompt):
    url = provider["url"].format(model=provider["model"]) + "?key=" + key
    data = _http_json(url, {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
    })
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Gemini 返回异常: %s" % json.dumps(data, ensure_ascii=False)[:300])
    return text


def _call_openai_compat(provider, key, prompt):
    url = provider["url"]
    data = _http_json(url, {
        "model": provider["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9,
    }, headers={"Authorization": "Bearer " + key})
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("%s 返回异常: %s" % (provider["label"], json.dumps(data, ensure_ascii=False)[:300]))


# ---------------- 提示词与解析 ----------------

def _build_prompt(scope, context, count):
    ctx = (context or "").strip() or "用户未提供背景信息（按一般职场人士的日常节奏生成）"
    return (
        "你是一名高效的个人任务规划助手。\n"
        "用户背景：%s\n"
        "请为「%s」生成 %d 条具体、可执行、简洁的中文任务，符合现实生活与工作场景，不要泛泛而谈。\n"
        "严格只输出 JSON 数组，不要任何解释、不要 Markdown 代码块标记，格式如下：\n"
        '[{"title": "任务标题", "note": "一句话说明（可省略）"}]\n'
        "注意：title 必须是一句动宾短语，例如「整理本周工作周报」。" % (ctx, SCOPE_DESC.get(scope, scope), int(count))
    )


def _extract_json(text):
    text = text or ""
    text = re.sub(r"```(?:json)?", "", text).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("AI 输出中未找到 JSON 数组: %s" % text[:200])
    return json.loads(text[start:end + 1])


def _sanitize(items, count):
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title or len(title) > 120:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        note = str(it.get("note") or "").strip()
        out.append({"title": title, "note": note})
        if len(out) >= count:
            break
    return out


# ---------------- 降级模板 ----------------

TEMPLATES = {
    "daily": [
        {"title": "起床后喝一杯温水", "note": "唤醒身体"},
        {"title": "晨间拉伸 10 分钟", "note": "活动筋骨"},
        {"title": "整理床铺与桌面", "note": "整洁环境"},
        {"title": "喝水 8 杯并定时起身活动", "note": "久坐提醒"},
        {"title": "阅读或学习 30 分钟", "note": "每天进步一点"},
        {"title": "睡前复盘今日并写下明日计划", "note": "坚持记录"},
    ],
    "today": [
        {"title": "晨间拉伸与 10 分钟冥想", "note": "唤醒身体，调整状态"},
        {"title": "整理今日待办并设定优先级", "note": "用 5 分钟规划今天"},
        {"title": "完成一项最重要的工作任务", "note": "先啃最硬的骨头"},
        {"title": "喝水 8 杯并起来走动", "note": "久坐提醒"},
        {"title": "阅读或学习 30 分钟", "note": "碎片时间充电"},
        {"title": "复盘今日并记录心得", "note": "睡前简单总结"},
    ],
    "tomorrow": [
        {"title": "提前准备明天要用的材料", "note": "文件、衣物、会议资料"},
        {"title": "梳理明天三个核心目标", "note": "写进明日清单"},
        {"title": "预约/确认明天的会议", "note": "避免时间冲突"},
        {"title": "明天早睡，定好闹钟", "note": "保证精力"},
    ],
    "week": [
        {"title": "整理房间并做一次大扫除", "note": "每周一次"},
        {"title": "采购一周生活必需品", "note": "列出清单再买"},
        {"title": "联系家人或朋友", "note": "保持联系"},
        {"title": "完成本周工作复盘", "note": "总结得失"},
        {"title": "运动 3 次（每次 30 分钟）", "note": "保持健康"},
        {"title": "学习一项新技能 2 小时", "note": "每周进步一点"},
    ],
    "month": [
        {"title": "核对本月账单并记账", "note": "掌握收支"},
        {"title": "预约一次体检", "note": "关注健康"},
        {"title": "月度目标回顾与下月规划", "note": "写一篇月总结"},
        {"title": "清理手机/电脑存储空间", "note": "卸载无用应用"},
        {"title": "读完一本书", "note": "月度阅读计划"},
        {"title": "给家里做一次安全检查", "note": "水电燃气"},
    ],
}


def _fallback(scope, count, context):
    base = list(TEMPLATES.get(scope, TEMPLATES["today"]))
    items = []
    for i in range(max(1, int(count))):
        items.append(base[i % len(base)])
    return items


def fallback_tasks(scope, count=5, context=""):
    """公开的模板兜底入口。"""
    return _fallback(scope, count, context)


# ---------------- 对外入口 ----------------

def generate(scope, context="", count=5, provider=None):
    """生成任务。返回 {source, provider, model, message, tasks}。"""
    count = max(1, min(int(count or 5), 20))
    if scope not in SCOPE_DESC:
        scope = "today"

    provs = {p["id"]: p for p in PROVIDERS}
    if provider and provider in provs:
        picked = provs[provider]
    else:
        did = default_provider()
        picked = provs[did] if did else None

    if picked is None:
        return {
            "source": "fallback", "provider": None, "model": None,
            "message": "未检测到 gemini-apikey / kimik3-apikey / deepseek-apikey 环境变量，"
                       "已使用内置模板生成示例任务（openai-apikey 暂不可用）。",
            "tasks": _fallback(scope, count, context),
        }

    key = _find_env(picked["env"])
    prompt = _build_prompt(scope, context, count)
    try:
        if picked["id"] == "gemini":
            text = _call_gemini(picked, key, prompt)
        else:
            text = _call_openai_compat(picked, key, prompt)
        tasks = _sanitize(_extract_json(text), count)
        if not tasks:
            raise ValueError("AI 返回的任务列表为空")
        return {
            "source": "api", "provider": picked["id"], "model": picked["model"],
            "message": "已由 %s (%s) 生成" % (picked["label"], picked["model"]),
            "tasks": tasks,
        }
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return {
            "source": "error", "provider": picked["id"], "model": picked["model"],
            "message": "%s 调用失败 (HTTP %s): %s" % (picked["label"], e.code, body or e.reason),
            "error": True,
            "tasks": _fallback(scope, count, context),
        }
    except Exception as e:
        return {
            "source": "error", "provider": picked["id"], "model": picked["model"],
            "message": "%s 调用失败: %s" % (picked["label"], e),
            "error": True,
            "tasks": _fallback(scope, count, context),
        }
