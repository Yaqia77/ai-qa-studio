# -*- coding: utf-8 -*-
"""UI 自动化端到端冒烟（项目分型 api/ui 的 UI 链路回归）。

用法：先启动后端（须用装了 playwright 的 venv 解释器），再执行本脚本
    .venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
    .venv\Scripts\python.exe tools\smoke_ui.py                 # 默认打 127.0.0.1:8000
    set IA_BASE=http://127.0.0.1:8899 && .venv\Scripts\python.exe tools\smoke_ui.py

覆盖：
  1. 健康与登录基线
  2. 建 UI 项目（kind=ui）→ 脚手架 .json 空表 → 列表 total=0 → meta 动作字典（31 个）
  3. UI 用例 CRUD：新建/列表/详情/更新/非法(无步骤 400)/查重(409)/viewer 越权(403)/模块候选
  4. 跨 UI 项目迁移（含同项目 400）；迁移后源/目标条数正确
  5. 执行链路（需 chromium 就绪）：发起批次→轮询终态→详情含 ui_steps 步骤轨迹→
     失败步骤截图可经 attachment 取图（登录态 200 / 未登录 401）→删除批次
  6. 收尾：临时 UI 项目/批次全回收，项目数回到基线，接口链路默认项目零污染

脚本只在临时 UI 项目文件（data/cases/proj_*.json）与内存批次造数据，收尾必删；
执行用例打本地 fixture 页（file:// 绝对地址），零外部依赖。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = os.environ.get("IA_BASE", "http://127.0.0.1:8000").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
FIXTURE_URI = (ROOT / "tests" / "fixtures" / "ui_demo.html").as_uri()
ADMIN = ("admin", "admin123")
VIEWER = ("viewer", "viewer123")

_passed = _failed = 0


def check(name, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [OK] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name} {extra}")


def req(method, path, body=None, token=None, raw=False):
    """极简 JSON HTTP 客户端（仅标准库）；raw=True 时不解码 JSON，返回 (status, bytes/None, headers)。"""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r) as resp:
            if raw:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "")
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw_bytes = e.read()
        if raw:
            return e.code, raw_bytes, e.headers.get("Content-Type", "")
        try:
            return e.code, json.loads(raw_bytes.decode() or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw_bytes.decode()[:200]}
    except urllib.error.URLError as e:
        print(f"后端不可达：{BASE}（{e.reason}）", file=sys.stderr)
        sys.exit(2)


def login(username, password):
    code, resp = req("POST", "/api/auth/login", {"username": username, "password": password})
    assert code == 200, f"登录失败 {username}: {code} {resp}"
    return resp["token"]


def qf(file, **extra):
    """把 file（+可选参数）拼成查询串。"""
    q = {"file": file}
    q.update({k: v for k, v in extra.items() if v is not None})
    return "?" + urllib.parse.urlencode(q)


def wait_run_terminal(run_id, timeout=120):
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


def browser_ready():
    """探测浏览器能否真正启动（与 ui_runner 同口径：先试自带 chromium，再回退本机 Chrome）。

    都不可用则返回 False（执行段跳过而非判失败），不阻塞 CRUD 回归。
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            try:
                b = p.chromium.launch(headless=True)
            except Exception:  # noqa: BLE001 自带内核缺失→回退本机 Google Chrome
                b = p.chromium.launch(headless=True, channel="chrome")
            b.close()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] chromium 不可用（且本机 Chrome 不可启动），跳过执行链路：{str(e).splitlines()[0]}")
        return False


def project_count(token):
    code, resp = req("GET", "/api/projects", token=token)
    return len(resp) if code == 200 else -1


def passing_case(cid):
    return {
        "case_id": cid, "module": "冒烟登录", "name": "演示页登录成功",
        "desc": "填用户名点登录并校验欢迎语与地址/cookie", "priority": "P0", "run": True,
        "browser": "chromium", "headless": True,
        "steps": [
            {"action": "goto", "value": FIXTURE_URI, "desc": "打开本地演示页"},
            {"action": "fill", "locator": "css=#username", "value": "qa冒烟"},
            {"action": "click", "locator": "css=#submit"},
            {"action": "expect_visible", "locator": "css=.welcome"},
            {"action": "expect_text", "locator": "css=.welcome", "value": "欢迎"},
            {"action": "expect_url", "value": "logged_in=1"},
        ],
        "extract": [{"var": "smoke_session", "source": "cookie:session_token"}],
    }


def failing_case(cid):
    return {
        "case_id": cid, "module": "冒烟登录", "name": "标题断言失败（造截图）",
        "desc": "期望文本不存在，用于验证失败即中断并截全屏", "priority": "P1", "run": True,
        "browser": "chromium", "headless": True,
        "steps": [
            {"action": "goto", "value": FIXTURE_URI},
            {"action": "expect_text", "locator": "css=#title", "value": "绝不存在的文本XYZ"},
        ],
    }


def main():
    global ADMIN_TOK
    print(f"目标：{BASE}")
    print(f"fixture：{FIXTURE_URI}")

    print("== 1. 健康与登录基线 ==")
    code, _ = req("GET", "/api/health")
    check("健康检查可访问", code == 200, code)
    ADMIN_TOK = login(*ADMIN)
    vt = login(*VIEWER)
    base_proj = project_count(ADMIN_TOK)
    check("项目列表基线可读", base_proj >= 1, base_proj)

    print("== 2. 建 UI 项目 + 脚手架 + meta ==")
    uniq = uuid.uuid4().hex[:6]
    code, proj = req("POST", "/api/projects", {"name": f"冒烟UI_{uniq}", "desc": "自动创建", "kind": "ui"}, ADMIN_TOK)
    check("建 UI 项目 201，file 为 .json 且 kind=ui、case_count=0",
          code == 201 and proj.get("file", "").endswith(".json") and
          proj.get("kind") == "ui" and proj.get("case_count") == 0, f"{code} {proj}")
    pid, pfile = proj.get("id"), proj.get("file")
    if not pid:
        print("  建 UI 项目失败，中止", file=sys.stderr)
        return 1

    code, resp = req("GET", "/api/ui-cases" + qf(pfile), token=ADMIN_TOK)
    check("空 UI 项目列表 total=0", code == 200 and resp.get("total") == 0, f"{code} {resp}")
    code, resp = req("GET", "/api/ui-cases/meta" + qf(pfile), token=ADMIN_TOK)
    check("UI meta 动作字典下发（31 个）+ 优先级 + chromium",
          code == 200 and len(resp.get("actions", [])) == 31 and resp.get("priorities") and
          "chromium" in (resp.get("browsers") or []), f"{code} {resp}")
    # UI 接口拒收 Excel 项目文件（防误写）
    code, resp = req("GET", "/api/ui-cases" + qf("data/api_cases.xlsx"), token=ADMIN_TOK)
    check("UI 用例接口拒绝非 .json 文件 400", code == 400, f"{code} {resp}")

    print("== 3. UI 用例 CRUD + 校验 + 查重 + 越权 ==")
    pass_id = f"ui_pass_{uniq}"
    fail_id = f"ui_fail_{uniq}"
    code, resp = req("POST", "/api/ui-cases" + qf(pfile), passing_case(pass_id), ADMIN_TOK)
    check("新增通过用例 201 并回读 steps/extract",
          code == 201 and resp.get("case_id") == pass_id and len(resp.get("steps", [])) == 6, f"{code} {resp}")
    code, resp = req("POST", "/api/ui-cases" + qf(pfile), failing_case(fail_id), ADMIN_TOK)
    check("新增失败用例 201", code == 201 and resp.get("case_id") == fail_id, f"{code} {resp}")

    bad = dict(passing_case(f"ui_bad_{uniq}")); bad["steps"] = []
    code, resp = req("POST", "/api/ui-cases" + qf(pfile), bad, ADMIN_TOK)
    check("无步骤用例被拒 400（不落盘）", code == 400, f"{code} {resp}")
    code, resp = req("POST", "/api/ui-cases" + qf(pfile), passing_case(pass_id), ADMIN_TOK)
    check("重复编号 409", code == 409, f"{code} {resp}")
    code, resp = req("POST", "/api/ui-cases" + qf(pfile), passing_case(f"ui_v_{uniq}"), vt)
    check("viewer 建 UI 用例被拒 403（复用 case:write）", code == 403, f"{code} {resp}")

    code, resp = req("GET", "/api/ui-cases" + qf(pfile), token=ADMIN_TOK)
    check("列表 total=2", code == 200 and resp.get("total") == 2, f"{code} {resp}")
    code, resp = req("GET", "/api/ui-cases/" + urllib.parse.quote(pass_id, safe="") + qf(pfile), token=ADMIN_TOK)
    check("详情读回通过用例一致", code == 200 and resp.get("case_id") == pass_id, f"{code} {resp}")
    code, resp = req("PUT", "/api/ui-cases/" + urllib.parse.quote(pass_id, safe="") + qf(pfile),
                     {"priority": "P1", "desc": "改优先级"}, ADMIN_TOK)
    check("更新用例（改优先级）", code == 200 and resp.get("priority") == "P1", f"{code} {resp}")
    code, resp = req("GET", "/api/ui-cases/meta" + qf(pfile), token=ADMIN_TOK)
    check("meta 模块候选含新建模块", code == 200 and "冒烟登录" in (resp.get("modules") or []), f"{code} {resp}")
    code, resp = req("GET", "/api/ui-cases/variables" + qf(pfile), token=ADMIN_TOK)
    check("变量候选含 extract 出的 smoke_session",
          code == 200 and "smoke_session" in (resp.get("excel_variables") or []), f"{code} {resp}")

    print("== 4. 跨 UI 项目迁移 ==")
    code, proj2 = req("POST", "/api/projects", {"name": f"冒烟UI目标_{uniq}", "kind": "ui"}, ADMIN_TOK)
    pid2, pfile2 = proj2.get("id"), proj2.get("file")
    check("建第二个 UI 项目（迁移目标）", code == 201 and bool(pfile2), f"{code} {proj2}")
    code, resp = req("POST", "/api/ui-cases/" + urllib.parse.quote(fail_id, safe="") + "/move" + qf(pfile),
                     {"to_file": pfile2}, ADMIN_TOK)
    check("迁移失败用例到目标 UI 项目", code == 200 and resp.get("to_file", "").endswith(".json"), f"{code} {resp}")
    code, resp = req("GET", "/api/ui-cases" + qf(pfile), token=ADMIN_TOK)
    check("源项目剩 1 条", code == 200 and resp.get("total") == 1, f"{code} {resp}")
    code, resp = req("GET", "/api/ui-cases" + qf(pfile2), token=ADMIN_TOK)
    check("目标项目有 1 条", code == 200 and resp.get("total") == 1, f"{code} {resp}")
    code, resp = req("POST", "/api/ui-cases/" + urllib.parse.quote(pass_id, safe="") + "/move" + qf(pfile),
                     {"to_file": pfile}, ADMIN_TOK)
    check("迁移到同一项目被拒 400", code == 400, f"{code} {resp}")

    print("== 5. 执行链路（需 chromium） ==")
    run_id = None
    if browser_ready():
        code, resp = req("POST", "/api/runs", {"file": pfile}, ADMIN_TOK)
        check("发起 UI 批次 202 且拿到 run_id", code == 202 and resp.get("run_id"), f"{code} {resp}")
        run_id = resp.get("run_id")
        if run_id:
            final = wait_run_terminal(run_id)
            check("批次到达终态", final.get("status") in ("passed", "failed", "error", "broken", "cancelled"),
                  f"终态={final.get('status')}")
            code, cases = req("GET", "/api/runs/" + urllib.parse.quote(run_id, safe="") + "/cases", token=ADMIN_TOK)
            check("批次用例明细可读且含通过用例",
                  code == 200 and any(c.get("case_id") == pass_id for c in (cases.get("items") or [])),
                  f"{code} {cases}")
            code, detail = req("GET",
                               "/api/runs/" + urllib.parse.quote(run_id, safe="") + "/cases/" + urllib.parse.quote(pass_id, safe=""),
                               token=ADMIN_TOK)
            steps = (detail or {}).get("ui_steps") or []
            check("通过用例详情含 ui_steps 步骤轨迹", code == 200 and len(steps) == 7 and
                  all(s.get("ok") for s in steps) and steps[-1]["action"] == "final_shot", f"{code} {detail}")
            # 截图：把失败用例迁回源，单独跑一批拿失败截图
            code, resp = req("POST", "/api/ui-cases/" + urllib.parse.quote(fail_id, safe="") + "/move" + qf(pfile2),
                             {"to_file": pfile}, ADMIN_TOK)
            check("迁回失败用例以便截图", code == 200, f"{code} {resp}")
            code, resp = req("POST", "/api/runs", {"file": pfile, "keyword": fail_id}, ADMIN_TOK)
            run2 = resp.get("run_id") if code == 202 else None
            check("为失败用例单独发起批次", bool(run2), f"{code} {resp}")
            if run2:
                wait_run_terminal(run2)
                code, detail = req("GET",
                                   "/api/runs/" + urllib.parse.quote(run2, safe="") + "/cases/" + urllib.parse.quote(fail_id, safe=""),
                                   token=ADMIN_TOK)
                steps = (detail or {}).get("ui_steps") or []
                shot = next((s.get("screenshot_source") for s in steps if s.get("screenshot_source")), None)
                check("失败用例详情含失败步骤截图", code == 200 and any(not s.get("ok") for s in steps) and bool(shot),
                      f"{code} {steps}")
                if shot:
                    att = "/api/runs/" + urllib.parse.quote(run2, safe="") + "/attachment/" + urllib.parse.quote(shot, safe="")
                    code, body, ctype = req("GET", att, token=ADMIN_TOK, raw=True)
                    check("登录态可取截图（image/*）", code == 200 and str(ctype).startswith("image/"), f"{code} {ctype}")
                    code, _, _ = req("GET", att, raw=True)
                    check("未登录取截图 401", code == 401, code)
                    # viewer 有 report:view（登录态）即可取图
                    code, _, ctype2 = req("GET", att, token=vt, raw=True)
                    check("viewer 登录态可取截图", code == 200 and str(ctype2).startswith("image/"), f"{code} {ctype2}")
                req("DELETE", "/api/runs/" + urllib.parse.quote(run2, safe=""), token=ADMIN_TOK)
    else:
        print("  （执行链路已跳过；装好 chromium 后重跑本脚本即可覆盖）")

    print("== 6. 收尾与零副作用核验 ==")
    if run_id:
        req("DELETE", "/api/runs/" + urllib.parse.quote(run_id, safe=""), token=ADMIN_TOK)
    req("DELETE", "/api/projects/" + urllib.parse.quote(pid2, safe=""), token=ADMIN_TOK)
    code, resp = req("DELETE", "/api/projects/" + urllib.parse.quote(pid, safe=""), token=ADMIN_TOK)
    check("删除临时 UI 项目成功", code == 200 and resp.get("deleted") == pid, f"{code} {resp}")
    code, resp = req("GET", "/api/ui-cases" + qf(pfile), token=ADMIN_TOK)
    check("项目文件随删除不可达（404）", code == 404, f"{code} {resp}")
    after_proj = project_count(ADMIN_TOK)
    check("项目数回到基线（临时项已回收）", after_proj == base_proj, f"{base_proj} -> {after_proj}")
    code, resp = req("GET", "/api/cases" + qf("data/api_cases.xlsx", keyword=uniq), token=ADMIN_TOK)
    check("默认接口项目未被 UI 冒烟污染（关键字 0 命中）", code == 200 and resp.get("total") == 0, f"{code} {resp}")

    print(f"\n结果：通过 {_passed} / 失败 {_failed}")
    return 1 if _failed else 0


ADMIN_TOK = None

if __name__ == "__main__":
    sys.exit(main())
