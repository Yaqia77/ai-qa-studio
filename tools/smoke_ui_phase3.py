# -*- coding: utf-8 -*-
"""UI 自动化端到端冒烟：浏览器复用（执行提速）+ 用例级失败重试（稳定性）。

用法（须用装了 playwright 的 venv 解释器启动后端，再执行本脚本）：
    .venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
    .venv\Scripts\python.exe tools\smoke_ui_phase3.py

覆盖验证门禁的两项新证据（既有冒烟只守存量，不含这两条）：
1. 复用：一个 3 条用例的批次（同 chromium/headless 分组），执行日志里「新启浏览器束」应恰为 1 次
   （分组数），而非 3 次（用例数）；批次结束统一回收 1 束。
2. 重试：一条 flaky 用例（本地站点首次访问给 loading、第二次才给 ready）配 retry=1 应转绿，
   且用例列表摘要带「重试 1 次后通过」、详情含 ui_retry（attempts=2、首次失败留痕）。
3. 存量形状：未重试用例详情 ui_retry 为 None（报告结构向后兼容）。

浏览器未就绪时执行段整体跳过（与本仓其它冒烟同一口径）。脚本只在临时 UI 项目造数据，收尾必删。
"""
import functools
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = os.environ.get("IA_BASE", "http://127.0.0.1:8000").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
# 执行日志共享文件（与 core/log_setup 同源）：全进程 append 同一文件，
# 冒烟按发起前偏移量读增量窗口，跨批次不串计。
LOG_FILE = ROOT / "logs" / "interface-auto_runtime.log"
FIXTURE_URI = (ROOT / "tests" / "fixtures" / "ui_demo.html").as_uri()
ADMIN = ("admin", "admin123")

_passed = _failed = 0


def check(name, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [OK] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name} {extra}")


def req(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw.decode()[:200]}
    except urllib.error.URLError as e:
        print(f"后端不可达：{BASE}（{e.reason}）", file=sys.stderr)
        sys.exit(2)


def login(u, p):
    code, resp = req("POST", "/api/auth/login", {"username": u, "password": p})
    assert code == 200, f"登录失败 {u}: {code} {resp}"
    return resp["token"]


def qf(file, **extra):
    q = {"file": file}
    q.update({k: v for k, v in extra.items() if v is not None})
    return "?" + urllib.parse.urlencode(q)


# ==================== flaky 站点：第 1 次访问 loading，之后 ready ====================

_flaky_lock = threading.Lock()
_flaky_hits = {"n": 0}


class FlakyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/flaky"):
            with _flaky_lock:
                _flaky_hits["n"] += 1
                n = _flaky_hits["n"]
            msg = "loading" if n == 1 else "ready"
            html = f"<html><body><div id=\"msg\">{msg} (hit {n})</div></body></html>"
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


def serve_flaky():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}/flaky"


def browser_ready():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            try:
                b = p.chromium.launch(headless=True)
            except Exception:  # noqa: BLE001
                b = p.chromium.launch(headless=True, channel="chrome")
            b.close()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] 浏览器不可用，执行段跳过：{str(e).splitlines()[0]}")
        return False


def wait_run_terminal(run_id, timeout=180):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        code, rec = req("GET", "/api/runs/" + urllib.parse.quote(run_id, safe=""), token=ADMIN_TOK)
        if code == 200:
            last = rec
            if rec.get("status") != "running":
                return rec
        time.sleep(1.5)
    return last


def log_offset() -> int:
    return LOG_FILE.stat().st_size if LOG_FILE.exists() else 0


def read_log_since(off: int) -> str:
    """runtime 日志自 off 字节起的增量（批次 subprocess 的 pytest 日志，全进程共享单文件）。"""
    if not LOG_FILE.exists():
        return ""
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fp:
            fp.seek(off)
            return fp.read()
    except OSError:
        return ""


def main():
    global ADMIN_TOK
    print(f"目标：{BASE}")
    server, flaky_url = serve_flaky()
    print(f"flaky 站点：{flaky_url}")

    code, _ = req("GET", "/api/health")
    check("健康检查可访问", code == 200, code)
    ADMIN_TOK = login(*ADMIN)

    uniq = uuid.uuid4().hex[:6]
    code, proj = req("POST", "/api/projects", {"name": f"三期冒烟_{uniq}", "kind": "ui"}, ADMIN_TOK)
    check("建 UI 项目 201", code == 201 and proj.get("file", "").endswith(".json"), f"{code} {proj}")
    pfile = proj.get("file")
    if not pfile:
        server.shutdown()
        return 1

    # 三条同分组（chromium/headless=true）用例：两条常规 + 一条 flaky(retry=1)
    cases = [
        {"case_id": f"ok_a_{uniq}", "module": "三期", "name": "常规通过A", "priority": "P1",
         "run": True, "browser": "chromium", "headless": True,
         "steps": [{"action": "goto", "value": FIXTURE_URI},
                   {"action": "expect_visible", "locator": "css=#title"}]},
        {"case_id": f"ok_b_{uniq}", "module": "三期", "name": "常规通过B", "priority": "P1",
         "run": True, "browser": "chromium", "headless": True,
         "steps": [{"action": "goto", "value": FIXTURE_URI},
                   {"action": "expect_visible", "locator": "css=#username"}]},
        {"case_id": f"flaky_{uniq}", "module": "三期", "name": "flaky重试转绿", "priority": "P1",
         "run": True, "browser": "chromium", "headless": True, "retry": 1,
         "steps": [{"action": "goto", "value": flaky_url},
                   {"action": "expect_text", "locator": "css=#msg", "value": "ready"}]},
    ]
    for c in cases:
        code, resp = req("POST", "/api/ui-cases" + qf(pfile), c, ADMIN_TOK)
        check(f"新建用例 {c['case_id']} 201（retry 字段 {'回显' if c.get('retry') else '缺省'} 正确）",
              code == 201 and resp.get("retry", 0) == c.get("retry", 0), f"{code} {resp}")

    if not browser_ready():
        req("DELETE", "/api/projects/" + urllib.parse.quote(proj.get("id", ""), safe=""), token=ADMIN_TOK)
        server.shutdown()
        print(f"\n结果：通过 {_passed} / 失败 {_failed}（执行段跳过）")
        return 1 if _failed else 0

    print("== 批次执行（3 条同分组用例：应只启 1 束浏览器） ==")
    log_off = log_offset()  # 增量窗口起点：只计本批次的池活动
    code, resp = req("POST", "/api/runs", {"file": pfile}, ADMIN_TOK)
    check("发起批次 202", code == 202 and resp.get("run_id"), f"{code} {resp}")
    run_id = resp.get("run_id")
    if not run_id:
        req("DELETE", "/api/projects/" + urllib.parse.quote(proj.get("id", ""), safe=""), token=ADMIN_TOK)
        server.shutdown()
        return 1
    final = wait_run_terminal(run_id)
    check("批次终态通过（含 flaky 重试转绿）", final.get("status") == "passed", f"终态={final.get('status')}")

    code, listing = req("GET", "/api/runs/" + urllib.parse.quote(run_id, safe="") + "/cases", token=ADMIN_TOK)
    items = {c.get("case_id"): c for c in (listing.get("items") or [])}
    flaky_item = items.get(f"flaky_{uniq}") or {}
    check("列表摘要标注「重试 1 次后通过」",
          "重试 1 次后通过" in (flaky_item.get("summary") or ""), flaky_item.get("summary"))
    ok_a = items.get(f"ok_a_{uniq}") or {}
    check("未重试用例摘要不含重试标注", "重试" not in (ok_a.get("summary") or ""), ok_a.get("summary"))

    code, det = req("GET", "/api/runs/" + urllib.parse.quote(run_id, safe="")
                    + "/cases/" + urllib.parse.quote(f"flaky_{uniq}", safe=""), token=ADMIN_TOK)
    ur = (det or {}).get("ui_retry") or {}
    check("详情 ui_retry：attempts=2、final_ok=true、首次失败留痕",
          code == 200 and ur.get("attempts") == 2 and ur.get("final_ok") is True
          and len(ur.get("history") or []) == 1, f"{code} {ur}")
    check("详情步骤轨迹为末次成功轨迹（全部 ok）",
          bool(det.get("ui_steps")) and all(s.get("ok") for s in det["ui_steps"]), det.get("ui_steps"))
    code, det_a = req("GET", "/api/runs/" + urllib.parse.quote(run_id, safe="")
                      + "/cases/" + urllib.parse.quote(f"ok_a_{uniq}", safe=""), token=ADMIN_TOK)
    check("未重试用例详情 ui_retry=None（存量报告形状不变）",
          code == 200 and det_a.get("ui_retry") is None, code)

    print("== 执行日志核验：浏览器启动次数 == 分组数（1），非用例数（3） ==")
    log_text = ""
    for _ in range(10):  # enqueue 落盘可能有延迟，最多等 5s
        log_text = read_log_since(log_off)
        if "批次结束，已回收" in log_text or "新启浏览器束" in log_text:
            break
        time.sleep(0.5)
    launches = len(re.findall(r"新启浏览器束", log_text))
    recycles = len(re.findall(r"批次结束，已回收", log_text))
    check("日志出现浏览器池活动（复用链路已生效）", launches >= 1 and bool(log_text), f"launches={launches}")
    check("启动次数 == 分组数(1) 而非用例数(3)", launches == 1, f"launches={launches}")
    check("批次结束统一回收(1 次)", recycles == 1, f"recycles={recycles}")

    print("== 收尾与零副作用 ==")
    req("DELETE", "/api/runs/" + urllib.parse.quote(run_id, safe=""), token=ADMIN_TOK)
    code, resp = req("DELETE", "/api/projects/" + urllib.parse.quote(proj.get("id", ""), safe=""), token=ADMIN_TOK)
    check("删除临时 UI 项目", code == 200, f"{code} {resp}")
    server.shutdown()

    print(f"\n结果：通过 {_passed} / 失败 {_failed}")
    return 1 if _failed else 0


ADMIN_TOK = None

if __name__ == "__main__":
    sys.exit(main())
