# -*- coding: utf-8 -*-
"""UI 登录态（storage_state 复用）端到端冒烟：采集 → 复用 → 引用保护 → 未采集指引 → 清理。

用法（须用装了 playwright 的 venv 解释器启动后端，再执行本脚本）：
    .venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
    .venv\Scripts\python.exe tools\smoke_ui_session.py

链路（离线、零外部站点）：本地起 http 服务提供 tests/fixtures 演示页，登录会写 cookie + localStorage，
storage_state 往返依赖 http origin（file:// 不可靠）。浏览器未就绪时，采集/复用执行段跳过，
但「未采集引用即失败并给中文指引」这条不依赖浏览器（会话解析在启动浏览器之前中断），照常校验。
脚本只在临时 UI 项目与临时会话上造数据，收尾必删，接口/UI 存量零污染。
"""
import functools
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = os.environ.get("IA_BASE", "http://127.0.0.1:8000").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
ENTITY_DIR = ROOT / "data" / "ui_sessions"
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


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102
        pass


def serve_fixtures():
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES.resolve()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}/ui_demo.html"


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
        print(f"  [SKIP] 浏览器不可用，采集/复用执行段跳过：{str(e).splitlines()[0]}")
        return False


def wait_run_terminal(run_id, timeout=150):
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


def run_one(file, keyword):
    """对单条用例（keyword 精确命中其唯一编号）发起批次，返回聚合终态。"""
    code, resp = req("POST", "/api/runs", {"file": file, "keyword": keyword}, ADMIN_TOK)
    rid = resp.get("run_id") if code == 202 else None
    if not rid:
        return {"status": "error", "detail": resp}
    return wait_run_terminal(rid)


def main():
    global ADMIN_TOK
    print(f"目标：{BASE}")
    server, demo_url = serve_fixtures()
    print(f"fixture：{demo_url}")

    print("== 1. 登录 + 选环境 ==")
    code, _ = req("GET", "/api/health")
    check("健康检查可访问", code == 200, code)
    ADMIN_TOK = login(*ADMIN)
    code, envs = req("GET", "/api/envs", token=ADMIN_TOK)
    env_name = envs.get("default_env") or (envs.get("envs") or [{}])[0].get("name")
    check("拿到合法环境名（会话 env 必须是 config.yaml 里的）", code == 200 and bool(env_name), f"{code} {env_name}")
    if not env_name:
        print("  无可用环境，中止", file=sys.stderr)
        server.shutdown()
        return 1

    print("== 2. 建 UI 项目 + 采集用例（登录流程） ==")
    uniq = uuid.uuid4().hex[:6]
    code, proj = req("POST", "/api/projects", {"name": f"登录态冒烟_{uniq}", "kind": "ui"}, ADMIN_TOK)
    check("建 UI 项目 201（kind=ui, .json）", code == 201 and proj.get("file", "").endswith(".json"), f"{code} {proj}")
    pfile = proj.get("file")
    if not pfile:
        server.shutdown()
        return 1

    cap_id = f"cap_login_{uniq}"
    capture_case = {
        "case_id": cap_id, "module": "登录采集", "name": "采集登录态", "priority": "P0", "run": True,
        "browser": "chromium", "headless": True,
        "steps": [
            {"action": "goto", "value": demo_url},
            {"action": "fill", "locator": "css=#username", "value": f"qa_{uniq}"},
            {"action": "click", "locator": "css=#submit"},
            {"action": "expect_visible", "locator": "css=.welcome"},
        ],
    }
    code, _ = req("POST", "/api/ui-cases" + qf(pfile), capture_case, ADMIN_TOK)
    check("新建采集用例 201", code == 201, code)

    print("== 3. 会话 meta + 新建登录态（未采集态） ==")
    code, smeta = req("GET", "/api/ui-sessions/meta", token=ADMIN_TOK)
    check("meta 含环境/浏览器/UI 项目候选",
          code == 200 and env_name in (smeta.get("envs") or []) and
          any(p.get("file") == pfile for p in (smeta.get("ui_projects") or [])), f"{code} {smeta}")

    sid = f"sess_{uniq}"
    body = {"id": sid, "name": f"冒烟登录态_{uniq}", "env": env_name, "browser": "chromium",
            "headless": True, "file": pfile, "capture_case": cap_id, "ttl_seconds": 3600}
    code, sess = req("POST", "/api/ui-sessions", body, ADMIN_TOK)
    check("新建会话 201 且状态 not_collected", code == 201 and sess.get("status") == "not_collected", f"{code} {sess}")

    code, lst = req("GET", "/api/ui-sessions", token=ADMIN_TOK)
    check("会话列表含新会话（不含实体内容）",
          code == 200 and any(s.get("id") == sid and "captured_at" in s for s in lst) and
          not any("cookies" in json.dumps(s) for s in lst), f"{code}")

    print("== 4. 采集登录态（需浏览器） ==")
    collected = False
    if browser_ready():
        code, res = req("POST", f"/api/ui-sessions/{sid}/collect", None, ADMIN_TOK)
        collected = code == 200 and res.get("ok") is True
        check("立即采集成功 → 状态 valid", collected and res.get("status") == "valid", f"{code} {res}")
        ep = ENTITY_DIR / f"{sid}__{env_name}.json"
        check("storage_state 实体文件已落盘", ep.exists(), ep)
    else:
        print("  （浏览器未就绪，采集段跳过）")

    print("== 5. 复用：无登录步骤的用例引用会话 → 仅业务步骤即已登录 ==")
    reuse_id = f"reuse_{uniq}"
    reuse_case = {
        "case_id": reuse_id, "module": "业务复用", "name": "复用登录态看欢迎", "priority": "P0", "run": True,
        "browser": "chromium", "headless": True, "session": sid,
        "steps": [
            {"action": "goto", "value": demo_url},          # 开机带 cookie → 页面 onload 直接渲染已登录
            {"action": "expect_visible", "locator": "css=.welcome"},  # 无填账号/点登录，仍能见到登录后才有的元素
        ],
    }
    code, _ = req("POST", "/api/ui-cases" + qf(pfile), reuse_case, ADMIN_TOK)
    check("新建复用用例 201（引用会话，无登录步骤）", code == 201, code)

    if collected:
        final = run_one(pfile, reuse_id)
        check("复用批次通过（证明登录态生效，无需重复登录）", final.get("status") == "passed",
              f"终态={final.get('status')}")

    print("== 6. 引用保护：被引用的会话禁止删除/改环境 ==")
    code, resp = req("DELETE", f"/api/ui-sessions/{sid}", None, ADMIN_TOK)
    check("删除被引用会话被拒 409", code == 409, f"{code} {resp}")
    code, resp = req("PUT", f"/api/ui-sessions/{sid}", {"env": (envs.get("envs") or [{}])[1].get("name") or env_name}, ADMIN_TOK)
    check("被引用会话改环境被拒 409（若仅一个环境则同环境不作废放行）", code in (409, 200), f"{code} {resp}")

    print("== 7. 未采集引用 → 失败并给中文指引（不依赖浏览器） ==")
    sid2 = f"sess_nc_{uniq}"
    code, _ = req("POST", "/api/ui-sessions",
                  {"id": sid2, "env": env_name, "file": pfile, "capture_case": cap_id, "ttl_seconds": 3600}, ADMIN_TOK)
    check("新建第二个会话（不采集）", code == 201, code)
    nc_id = f"nc_{uniq}"
    code, _ = req("POST", "/api/ui-cases" + qf(pfile),
                  {"case_id": nc_id, "name": "引用未采集会话", "priority": "P1", "run": True,
                   "session": sid2, "browser": "chromium", "headless": True,
                   "steps": [{"action": "goto", "value": demo_url}]}, ADMIN_TOK)
    check("新建引用未采集会话的用例 201", code == 201, code)
    final = run_one(pfile, nc_id)
    rid = final.get("run_id")
    guidance_ok = final.get("status") == "failed"
    # 尽量取明细里的错误文本佐证“不误判为通过 + 指引采集”
    if rid:
        code, det = req("GET", f"/api/runs/{urllib.parse.quote(rid, safe='')}/cases/{urllib.parse.quote(nc_id, safe='')}", token=ADMIN_TOK)
        blob = json.dumps(det, ensure_ascii=False)
        guidance_ok = guidance_ok and ("尚未采集" in blob or "立即采集" in blob)
    check("引用未采集会话的用例执行失败且带采集指引", guidance_ok, f"终态={final.get('status')}")

    print("== 8. 收尾与零副作用 ==")
    # 先删项目（连带解除所有引用），再删会话——否则被引用会话的 DELETE 会被 409 拒绝而残留
    code, resp = req("DELETE", "/api/projects/" + urllib.parse.quote(proj.get("id", ""), safe=""), token=ADMIN_TOK)
    check("删除临时 UI 项目", code == 200, f"{code} {resp}")
    req("DELETE", f"/api/ui-sessions/{sid2}", None, ADMIN_TOK)
    code, resp = req("DELETE", f"/api/ui-sessions/{sid}", None, ADMIN_TOK)
    check("解除引用后会话可删除", code == 200, f"{code} {resp}")
    check("会话已清理（列表不含冒烟会话）",
          all(s.get("id") not in (sid, sid2) for s in (req("GET", "/api/ui-sessions", token=ADMIN_TOK)[1] or [])))
    check("已采集会话的实体文件随删除清理",
          not (ENTITY_DIR / f"{sid}__{env_name}.json").exists(), ENTITY_DIR / f"{sid}__{env_name}.json")
    server.shutdown()

    print(f"\n结果：通过 {_passed} / 失败 {_failed}")
    return 1 if _failed else 0


ADMIN_TOK = None

if __name__ == "__main__":
    sys.exit(main())
