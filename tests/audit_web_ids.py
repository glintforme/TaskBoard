# -*- coding: utf-8 -*-
"""审计 web/app.js 中所有 $("id") 引用是否在 index.html 中存在对应元素。"""
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # tests/
PROJECT_ROOT = os.path.dirname(BASE_DIR)                        # 项目根目录
WEB = os.path.join(PROJECT_ROOT, "web")
JS = open(os.path.join(WEB, "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()

# 收集 HTML 中的 id
html_ids = set(re.findall(r'id="([^"]+)"', HTML))

# 收集 JS 中的 $("...") 引用
js_refs = re.findall(r'\$\("([^"]+)"\)', JS)
js_refs += re.findall(r"getElementById\(\"([^\"]+)\"\)", JS)
js_refs = list(dict.fromkeys(js_refs))  # 去重保序

# JS 中动态创建的 id（模板字符串里生成，不检查）
dynamic = re.findall(r'id="ai-chk-\$\{s\}-\$\{i\}"', JS)

missing = [r for r in js_refs if r not in html_ids]
print("JS 引用 %d 个 id，HTML 定义 %d 个 id" % (len(js_refs), len(html_ids)))
if missing:
    print("[FAIL] 以下 JS 引用的 id 在 HTML 中不存在: %s" % missing)
    sys.exit(1)
print("[PASS] 全部 JS 元素引用均存在于 HTML")
sys.exit(0)
