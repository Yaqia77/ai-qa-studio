# -*- coding: utf-8 -*-
"""UI 自动化能力扩展端到端冒烟（iframe / 多窗口 / 上传 / 等待 / 富断言 / 定位方言 / dialog）。

用法：先启动后端（须用装了 playwright 的 venv 解释器），再执行本脚本
    .venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
    .venv\Scripts\python.exe tools\smoke_ui_actions.py

设计：本脚本自带一个本地 http 服务提供 tests/fixtures 静态页（http origin 才能可靠验证
localStorage 提取 / 原生 dialog / 多标签窗口），用例 goto 用绝对 http URL（含 ://，执行器原样导航）。
只在临时 UI 项目造数据、收尾必删，零副作用；浏览器不可用时执行段合理 skip 不判失败。

覆盖门禁第 5 项：CRUD+meta 常跑；执行段逐能力建用例并断言每步 ok。
"""
from __future__ import annotations

import functools
import http.server
import json
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ui import (  # noqa: E402  复用同一套 HTTP 原语与 check 计数
    BASE, ROOT, ADMIN, check, login, req, wait_run_terminal, browser_ready, project_count,
)
import smoke_ui as _smoke  # noqa: E402  取其模块级 ADMIN_TOK/计数器做收尾汇总

FIXTURES = (ROOT / "tests" / "fixtures").resolve()
UPLOAD_REL = "data/secret_vars.xlsx"  # 相对项目根：执行器按 _PROJECT_ROOT join，文件确存

_passed_holder = {"v": 0}


def serve_fixtures():
    """本地线程 http 服务提供 fixture 页，返回 base_url（含端口）。"""

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # 静音
            pass

    handler = functools.partial(Quiet, directory=str(FIXTURES))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", srv


def case(cid, name, steps, extract=None):
    return {
        "case_id": cid, "module": "五期冒烟", "name": name, "desc": "能力扩展端到端",
        "priority": "P1", "run": True, "browser": "chromium", "headless": True,
        "steps": steps, "extract": extract or [],
    }


def run_one(pfile, tok, c):
    """建单条用例 → 单独批次（keyword 精确命中）→ 轮询终态 → 返回 (batch_status, steps)。"""
    code, resp = req("POST", "/api/ui-cases" + _smoke.qf(pfile), c, tok)
    if code not in (200, 201):
        return f"create:{code}", []
    cid = c["case_id"]
    code, resp = req("POST", "/api/runs", {"file": pfile, "keyword": cid}, tok)
    run_id = resp.get("run_id") if code == 202 else None
    if not run_id:
        return f"run:{code}", []
    final = wait_run_terminal(run_id)
    code, detail = req("GET", f"/api/runs/{urllib.parse.quote(run_id, safe='')}/cases/"
                       + urllib.parse.quote(cid, safe=""), token=tok)
    steps = (detail or {}).get("ui_steps") or []
    req("DELETE", f"/api/runs/{urllib.parse.quote(run_id, safe='')}", token=tok)
    return final.get("status", "unknown"), steps


def all_ok(steps):
    return bool(steps) and all(s.get("ok") for s in steps)


def check_case(tag, status, steps):
    bad = [s for s in steps if not s.get("ok")]
    detail = "" if not bad else f" 首个失败步={bad[0].get('action')}:{bad[0].get('error')}"
    check(f"{tag} 通过", status == "passed" and all_ok(steps), f"status={status}{detail}")


def main():
    print(f"目标：{BASE}")
    code, _ = req("GET", "/api/health")
    if code != 200:
        print(f"后端不可达：{BASE}", file=sys.stderr)
        return 2
    tok = login(*ADMIN)
    _smoke.ADMIN_TOK = tok  # wait_run_terminal 内部引用模块级 ADMIN_TOK 轮询，需注入本脚本登录态
    base_proj = project_count(tok)

    print("== meta 下发新动作与 hint（常跑，不依赖浏览器） ==")
    uniq = uuid.uuid4().hex[:6]
    code, proj = req("POST", "/api/projects", {"name": f"五期冒烟_{uniq}", "kind": "ui"}, tok)
    pfile = proj.get("file")
    check("建临时 UI 项目", code == 201 and pfile, f"{code} {proj}")
    code, meta = req("GET", "/api/ui-cases/meta" + _smoke.qf(pfile), token=tok)
    by = {a["action"]: a for a in meta.get("actions", [])}
    check("meta 含全部新增动作", all(a in by for a in
          ("upload", "drag_to", "switch_frame", "switch_window", "expect_attr", "expect_count")), f"{code}")
    check("meta 动作带 hint 字段（upload/switch_frame 非空）",
          "hint" in by.get("upload", {}) and by["upload"].get("hint") and by["switch_frame"].get("hint"))

    if not pfile:
        print("项目创建失败，中止", file=sys.stderr)
        return 1

    print("== 执行链路（需 chromium） ==")
    srv = None
    if browser_ready():
        base_url, srv = serve_fixtures()
        demo = f"{base_url}/ui_demo.html"
        host = f"{base_url}/ui_frame_host.html"

        # A. 交互 + 断言 + 定位方言 + dialog + 等待（作用主文档）
        a_steps = [
            {"action": "goto", "value": demo},
            {"action": "expect_count", "locator": "css=.item", "value": "3"},
            {"action": "fill", "locator": "placeholder=搜索关键词", "value": "kw"},
            {"action": "expect_value", "locator": "placeholder=搜索关键词", "value": "kw"},
            {"action": "expect_visible", "locator": "testid=profile-card"},
            {"action": "expect_visible", "locator": "alt=用户头像"},
            {"action": "check", "locator": "css=#agree"},
            {"action": "uncheck", "locator": "css=#agree"},
            {"action": "dblclick", "locator": "css=#dbl-target"},
            {"action": "expect_text", "locator": "css=#dbl-target", "value": "已双击"},
            {"action": "upload", "locator": "css=#file", "value": UPLOAD_REL},
            {"action": "expect_text", "locator": "css=#file-name", "value": "secret_vars.xlsx"},
            {"action": "expect_attr", "locator": "css=#link", "value": "href=/login"},
            {"action": "expect_hidden", "locator": "css=#lazy"},
            {"action": "click", "locator": "css=#reveal-btn"},
            {"action": "wait_state", "locator": "css=#lazy", "value": "visible"},
            {"action": "expect_visible", "locator": "css=#lazy"},
            {"action": "click", "locator": "css=#disable-btn"},
            {"action": "click", "locator": "css=#enable-btn"},
            {"action": "expect_enabled", "locator": "css=#toggle-btn"},
            {"action": "scroll_to", "locator": "css=#drag-src"},
            {"action": "drag_to", "locator": "css=#drag-src", "value": "#drop-zone"},
            {"action": "expect_text", "locator": "css=#drop-zone", "value": "已接收"},
            {"action": "click", "locator": "css=#store-btn"},
            {"action": "click", "locator": "css=#confirm-btn"},
            {"action": "expect_text", "locator": "css=#dialog-result", "value": "已放行"},
            {"action": "wait_load", "value": "load"},
            {"action": "sleep", "value": "200"},
            {"action": "navigate", "value": "reload"},
            {"action": "expect_title", "value": "演示"},
        ]
        st, steps = run_one(pfile, tok, case(f"a_{uniq}", "交互断言方言dialog", a_steps,
                                             extract=[{"var": "tk", "source": "storage:ui_token"}]))
        check_case("A 交互/断言/定位方言/dialog", st, steps)

        # B. iframe：主文档→切入帧断言→回主文档
        b_steps = [
            {"action": "goto", "value": host},
            {"action": "expect_text", "locator": "css=#host-msg", "value": "host-main-content"},
            {"action": "switch_frame", "locator": "css=#app-frame"},
            {"action": "expect_text", "locator": "css=#inner-msg", "value": "frame-inner-content"},
            {"action": "expect_count", "locator": "css=.inner-item", "value": "2"},
            {"action": "switch_main"},
            {"action": "expect_visible", "locator": "css=#host-title"},
        ]
        st, steps = run_one(pfile, tok, case(f"b_{uniq}", "iframe 切换", b_steps))
        check_case("B iframe 切入/回主文档", st, steps)

        # C. 多窗口：新标签→切到最后一个→在末页断言（弹窗异步注册，先等页面数增加再切）
        c_steps = [
            {"action": "goto", "value": demo},
            {"action": "click", "locator": "css=#open-new"},
            {"action": "sleep", "value": "1200"},
            {"action": "switch_window", "value": "last"},
            {"action": "wait_load", "value": "load"},
            {"action": "expect_title", "value": "Frame 内页"},
            {"action": "expect_visible", "locator": "testid=inner-title"},
        ]
        st, steps = run_one(pfile, tok, case(f"c_{uniq}", "多标签窗口", c_steps))
        check_case("C switch_window 新窗口断言", st, steps)
    else:
        print("  （执行链路已跳过；装好 chromium 后重跑本脚本即可覆盖）")

    print("== 收尾与零副作用核验 ==")
    code, resp = req("DELETE", "/api/projects/" + urllib.parse.quote(proj.get("id", ""), safe=""), token=tok)
    check("删除临时 UI 项目", code == 200 and resp.get("deleted"), f"{code} {resp}")
    after = project_count(tok)
    check("项目数回到基线", after == base_proj, f"{base_proj} -> {after}")
    if srv:
        srv.shutdown()
        srv.server_close()

    print(f"\n结果：通过 {check.__globals__['_passed']} / 失败 {check.__globals__['_failed']}")
    return 1 if check.__globals__["_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
