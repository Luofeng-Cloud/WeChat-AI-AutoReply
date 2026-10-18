# -*- coding: utf-8 -*-
"""回归测试_防串发匹配.py — 2026-10-19 P0 修复批次永久回归表（只读测试，不改任何运行逻辑）
覆盖: ①越配防护边界 ②配置 null/脏数据防崩 ③群聊防火墙基线（P1 待修项，行为不随本批变化）
运行: python 回归测试_防串发匹配.py   全部 PASS = 零回退
"""
import ast
import os
import re
import sys
import json
import tempfile
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(BASE, "wechat_ai_bot_dev.py")
GUI = os.path.join(BASE, "wechat_bot_gui_dev.py")

def extract(path, names, ns):
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            tgts = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(t in names for t in tgts):
                exec(compile(ast.get_source_segment(src, node), path, "exec"), ns)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            exec(compile(ast.get_source_segment(src, node), path, "exec"), ns)
    return ns

ns = extract(BOT, {"normalize_for_match", "is_group_chat_name", "is_name_matched_strict"}, {"re": re})
is_group_chat_name = ns["is_group_chat_name"]
is_name_matched_strict = ns["is_name_matched_strict"]

fails = []

def check(label, got, expect):
    ok = (got == expect)
    if not ok:
        fails.append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: 实际={got!r} 期望={expect!r}")

print("=== 1. 越配防护 — 必命中（白名单=['张三']） ===")
for name in ["张三", "张三 你好啊", "张三[图片]", "3张三 你好",
             "张三 这是一条很长的消息预览内容", "张三 · 输入中"]:
    got, _ = is_name_matched_strict(name, ["张三"])
    check(f"命中 {name!r}", got, True)

print("=== 2. 越配防护 — 必拒绝（防串发核心） ===")
for name in ["张三丰", "张三丰丰", "张三丰丰丰", "张三丰大碗面", "张三丰大碗面 好的收到"]:
    got, _ = is_name_matched_strict(name, ["张三"])
    check(f"拒绝 {name!r}", got, False)

print("=== 3. 取舍用例（名+预览无空格拼接行，本批起设计为拒绝，文档化防误判为回退） ===")
for name in ["张三你好啊今天天气真好", "张三12:30"]:
    got, _ = is_name_matched_strict(name, ["张三"])
    check(f"拒绝(取舍) {name!r}", got, False)

print("=== 4. 群聊防火墙基线（P1 待修，本批不改其行为） ===")
check("真群 吃喝玩乐(25)", is_group_chat_name("吃喝玩乐(25)"), True)
check("真群 吃喝玩乐 (25) 好的", is_group_chat_name("吃喝玩乐 (25) 好的"), True)
check("正常好友行", is_group_chat_name("张三 好的收到"), False)
check("已知误伤(基线) '张三 明天群聊里说'", is_group_chat_name("张三 明天群聊里说"), True)

print("=== 5. 配置 null/脏数据防崩（GUI load_config 归一化） ===")
ns2 = {"os": os, "json": json, "print": print, "re": re, "CONFIG_FILE": None}
extract(GUI, {"DEFAULT_PROMPTS", "load_config"}, ns2)
tmp = os.path.join(tempfile.gettempdir(), "_reg_cfg_null.json")
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({"whitelist": None, "blacklist": ["微信支付", 123, None], "system_prompt": "x"}, f)
ns2["CONFIG_FILE"] = tmp
cfg = ns2["load_config"](object())
check("whitelist=null → []", cfg.get("whitelist"), [])
check("blacklist 混入数字/None → 仅留字符串", cfg.get("blacklist"), ["微信支付"])
os.remove(tmp)

print("=== 6. bot 名单读取内联语义（扫描循环同款表达式） ===")
for raw, expect in [(None, []), (["a", 5, None, "b"], ["a", "b"])]:
    wl = [w for w in (raw or []) if isinstance(w, str)]
    check(f"whitelist={raw!r}", wl, expect)

print()
if fails:
    print(f"❌ 回归失败 {len(fails)} 项: {fails}")
    sys.exit(1)
print("✅ 全部用例 PASS，零回退")
