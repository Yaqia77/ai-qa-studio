# -*- coding: utf-8 -*-
"""公共脚本（前后置处理器）冒烟：定义校验 + 引擎真实执行 + Excel 无损兼容 + 引用保护。

用法：先启动后端（它同时是被测服务，脚本步骤会真的打它的 HTTP 接口）
    .venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
    .venv\Scripts\python.exe tools\smoke_scripts.py
    set IA_BASE=http://127.0.0.1:8899 && .venv\Scripts\python.exe tools\smoke_scripts.py

零污染约定：Excel 写回全部在 data/~$smoke_scripts_cases.xlsx 副本上进行（~$ 前缀既被 loader
目录模式排除，也被 .gitignore 排除），真实用例文件与真实脚本都不动；冒烟脚本用完即删。
"""
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import excel_loader  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

BASE = os.environ.get("IA_BASE", "http://127.0.0.1:8000").rstrip("/")
PASS = {"admin": os.environ.get("IA_ADMIN_PASS", "admin123"),
        "qa": os.environ.get("IA_QA_PASS", "qa123"),
        "viewer": os.environ.get("IA_VIEWER_PASS", "viewer123")}
TMP_REL = "data/~$smoke_scripts_cases.xlsx"          # 传给接口 ?file= 的相对路径
TMP_CASES = ROOT / TMP_REL

# 三条冒烟脚本：正常链（四步动作全覆盖）、后置清理、必然失败（反向用例）
S_OK = "冒烟健康链"
S_CLEAN = "冒烟清理打点"
S_BAD = "冒烟必失败"
HEALTH = "/api/health"

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
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200]}
    except urllib.error.URLError as e:
        print(f"后端不可达：{BASE}（{e.reason}）", file=sys.stderr)
        sys.exit(2)


def login(role):
    code, resp = req("POST", "/api/auth/login", {"username": role, "password": PASS[role]})
    assert code == 200, f"登录失败 {role}: {code} {resp}"
    return resp["token"]


def q(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def header_row(path: Path) -> list[str]:
    """读「用例」表表头（openpyxl 的工作簿不是上下文管理器，用完必须关掉）。"""
    wb = load_workbook(path)
    try:
        return [str(c.value).strip() for c in wb["用例"][1] if c.value is not None]
    finally:
        wb.close()


def main():
    # loguru 的逐请求日志会淹没冒烟结论，默认静音（IA_SCRIPT_DEBUG=1 可恢复）
    from loguru import logger
    if os.environ.get("IA_SCRIPT_DEBUG") != "1":
        logger.remove()

    from backend.api import excel_writer
    from core import scripts as core_scripts
    from core.context import Context
    from core.http_client import HttpExecutor
    from core.models import TestCase
    from core.engines.api.engine import CaseRunner

    print(f"目标：{BASE}")
    admin_token, qa_token, vt = login("admin"), login("qa"), login("viewer")

    # ==================== 1. 脚本库接口（CRUD + 元数据） ====================
    print("== 1. 脚本库接口（CRUD + 步骤编辑器元数据） ==")
    for name in (S_OK, S_CLEAN, S_BAD):
        req("DELETE", "/api/scripts/" + q(name), token=admin_token)   # 预清理，保证可重复执行

    steps_ok = [
        {"action": "request", "desc": "探活并提取变量", "method": "GET", "url": HEALTH,
         "extract": {"health_status": "$.status", "env_name": "$.default_env"},
         "assert": [["eq", "status_code", 200], ["eq", "$.status", "ok"]]},
        {"action": "set_var", "vars": {"auth_token": "SECRET-VALUE-123", "run_tag": "smoke-pass"}},
        {"action": "assert", "assert": [["not_null", "var.health_status"],
                                         ["eq", "var.run_tag", "smoke-pass"]]},
        {"action": "sleep", "seconds": 0.1},
    ]
    code, meta = req("GET", "/api/scripts/meta", token=admin_token)
    check("步骤编辑器元数据可读", code == 200 and meta.get("actions"), f"{code} {meta}")
    check("元数据动作清单与执行口径同源",
          [a["action"] for a in meta.get("actions", [])] == list(core_scripts.ACTIONS),
          [a["action"] for a in meta.get("actions", [])])
    check("元数据带出各动作可用字段（前端表单据此渲染）",
          set(meta["actions"][0]["fields"]) == core_scripts.ACTION_FIELDS["request"], meta["actions"][0])
    check("元数据带出步骤上限/等待上限/禁用字符",
          meta.get("step_limit") == core_scripts.STEP_LIMIT
          and meta.get("sleep_limit") == core_scripts.SLEEP_LIMIT
          and "," in (meta.get("name_forbidden") or ""), meta.get("name_forbidden"))
    check("取值路径候选含变量池 var.",
          any(str(p["path"]).startswith("var") for p in meta.get("path_options", [])), meta.get("path_options"))

    code, resp = req("POST", "/api/scripts", {"name": S_OK, "remark": "冒烟用：探活+取变量+断言",
                                              "steps": steps_ok}, admin_token)
    check("新建脚本成功（request/set_var/assert/sleep 四步链）",
          code == 201 and resp.get("name") == S_OK, f"{code} {str(resp)[:200]}")
    code, resp = req("POST", "/api/scripts",
                     {"name": S_CLEAN, "steps": [{"action": "set_var", "vars": {"cleanup_done": "yes"}}]},
                     admin_token)
    check("新建后置清理脚本成功", code == 201, f"{code} {str(resp)[:160]}")
    code, resp = req("POST", "/api/scripts",
                     {"name": S_BAD, "steps": [{"action": "assert",
                                                "assert": [["eq", "$.status", "definitely-not"]]}]},
                     admin_token)
    check("新建必失败脚本（供反向用例）", code == 201, f"{code} {str(resp)[:160]}")

    code, items = req("GET", "/api/scripts", token=admin_token)
    names = {i["name"] for i in items} if code == 200 else set()
    check("脚本列表含三条冒烟脚本", {S_OK, S_CLEAN, S_BAD} <= names, names)
    check("列表附「引用它的用例编号」字段", bool(items) and all("used_by" in i for i in items), items[:1])

    # ==================== 2. 定义校验（与执行期同一口径） ====================
    print("== 2. 定义校验：非法脚本存不进、改不了 ==")
    bad_defs = [
        ({"steps": [{"action": "python", "code": "import os"}]}, "任意代码执行动作"),
        ({"steps": [{"action": "request", "method": "GET", "url": "/x", "script": "evil"}]}, "拼错的未知字段"),
        ({"steps": [{"action": "sleep", "seconds": 99999}]}, "sleep 超上限"),
        ({"steps": [{"action": "assert", "assert": [["between", "$.a", 1]]}]}, "断言操作符非法"),
        ({"steps": [{"action": "assert", "assert": [["eq", "", 1]]}]}, "断言取值路径为空"),
        ({"steps": [{"action": "set_var", "vars": {}}]}, "set_var 空对象"),
        ({"steps": []}, "空步骤链"),
        ({"name": "冒烟,非法", "steps": [{"action": "sleep", "seconds": 1}]}, "脚本名含逗号"),
        ({"name": "冒烟 非法", "steps": [{"action": "sleep", "seconds": 1}]}, "脚本名含空格"),
        ({"name": "冒烟/非法", "steps": [{"action": "sleep", "seconds": 1}]}, "脚本名含斜杠"),
        ({"name": "", "steps": [{"action": "sleep", "seconds": 1}]}, "脚本名为空"),
        ({"steps": [{"action": "sleep", "seconds": 1}] * 21}, "步骤数超上限"),
    ]
    for body, label in bad_defs:
        payload = {"name": "冒烟非法", **body}
        code, resp = req("POST", "/api/scripts", payload, admin_token)
        check(f"{label} 被拒 400", code == 400, f"{code} {str(resp)[:120]}")
    code, items = req("GET", "/api/scripts", token=admin_token)
    check("非法定义全部未落库", code == 200 and "冒烟非法" not in {i["name"] for i in items},
          {i["name"] for i in items} if code == 200 else code)

    code, resp = req("PUT", "/api/scripts/" + q(S_OK),
                     {"steps": [{"action": "request", "method": "TELEPORT", "url": "/x"}]}, admin_token)
    check("更新时非法请求方式被拒 400", code == 400, f"{code} {str(resp)[:120]}")
    code, resp = req("GET", "/api/scripts/" + q(S_OK), token=admin_token)
    check("非法更新未污染原定义（步骤仍为 4）",
          code == 200 and len(resp.get("steps", [])) == 4, f"{code} {resp.get('steps')}")

    # ==================== 3. 引擎真实执行（真的发 HTTP、真的取值断言） ====================
    print("== 3. 脚本引擎真实执行（对本地后端打真实请求） ==")
    executor = HttpExecutor(base_url=BASE)
    ctx = Context()
    trace: list = []
    core_scripts.run_scripts([S_OK], ctx, executor, "前置", trace)
    check("四步链全部执行通过", len(trace) == 4 and all(t["ok"] for t in trace), trace)
    check("request 步骤真实取回响应并提取变量", ctx.get("health_status") == "ok", ctx.get("health_status"))
    check("extract 写入的变量可被后续 var. 断言使用", ctx.get("run_tag") == "smoke-pass", ctx.get("run_tag"))
    detail_all = json.dumps(trace, ensure_ascii=False)
    check("敏感变量值绝不落轨迹（token 类命中掩码）", "SECRET-VALUE-123" not in detail_all, detail_all[:200])
    check("非敏感变量值正常可见", "smoke-pass" in detail_all, detail_all[:200])
    check("sleep 步骤有可读摘要", any("等待" in t["detail"] for t in trace), [t["detail"] for t in trace])
    check("request 步骤摘要含方法与实际 URL",
          trace[0]["detail"].startswith("GET ") and HEALTH in trace[0]["detail"], trace[0]["detail"])

    def expect_error(names, phase):
        try:
            core_scripts.run_scripts(names, Context(), HttpExecutor(base_url=BASE), phase, [])
            return None
        except core_scripts.ScriptError as e:
            return str(e)

    msg = expect_error([S_BAD], "前置")
    check("断言不通过时抛 ScriptError 并定位到第几步",
          bool(msg) and "第1步" in msg and S_BAD in msg, msg)
    msg = expect_error(["冒烟根本不存在的脚本"], "前置")
    check("引用不存在的脚本直接报错（执行期最后一道保护）",
          bool(msg) and "不存在" in msg, msg)

    def make_case(**kw) -> TestCase:
        base = dict(row=999, source="smoke.xlsx", case_id="SMOKE_SCRIPT_1", module="冒烟",
                    api_name="健康检查", case_desc="脚本引擎冒烟", priority="P2", run=True,
                    method="GET", url=HEALTH, asserts=[["eq", "status_code", 200]])
        base.update(kw)
        return TestCase(**base)

    def run_case(**kw):
        return CaseRunner(HttpExecutor(base_url=BASE), Context()).run(make_case(**kw))

    res = run_case(pre_scripts=[S_OK], post_scripts=[S_CLEAN])
    check("用例引用前置+后置脚本整体通过", res.ok, res.error)
    check("轨迹按「前置→后置」顺序累积",
          [t["phase"] for t in res.script_trace] == ["前置"] * 4 + ["后置"],
          [t["phase"] for t in res.script_trace])
    bad_pre = run_case(pre_scripts=[S_BAD])
    check("前置脚本失败即中断：主请求未发出、错误归因清晰",
          (not bad_pre.ok) and bad_pre.request_snapshot == {}
          and bad_pre.error.startswith("公共脚本执行失败"),
          (bad_pre.ok, bad_pre.request_snapshot, bad_pre.error[:120]))
    post_after_fail = run_case(asserts=[["eq", "status_code", 599]], post_scripts=[S_CLEAN])
    check("主流程断言失败时后置脚本仍然执行（清理造数）",
          (not post_after_fail.ok)
          and any(t["phase"] == "后置" and t["ok"] for t in post_after_fail.script_trace),
          post_after_fail.script_trace)
    post_fail = run_case(post_scripts=[S_BAD])
    check("后置脚本失败判用例失败并提示数据可能未清理",
          (not post_fail.ok) and "数据可能未清理" in post_fail.error, post_fail.error[:160])

    # ==================== 4. Excel 无损兼容（全部在副本上验证） ====================
    print("== 4. Excel「前置/后置脚本」列：向后兼容与无损写回 ==")
    real = ROOT / "data" / "api_cases.xlsx"
    shutil.copyfile(real, TMP_CASES)
    headers_before = header_row(TMP_CASES)
    check("存量用例文件原本没有脚本列（兼容性前提）",
          "前置脚本" not in headers_before and "后置脚本" not in headers_before, headers_before[-3:])
    cases_before = excel_loader.load(TMP_CASES).cases
    first, second = cases_before[0], cases_before[1]
    check("老文件缺列照常加载，脚本引用回落空列表",
          first.pre_scripts == [] and first.post_scripts == [], (first.pre_scripts, first.post_scripts))

    excel_writer.apply_update(TMP_CASES, first.case_id, first.row,
                              {"pre_scripts": [S_OK], "post_scripts": [S_CLEAN]})
    headers_after = header_row(TMP_CASES)
    check("写回时自动补齐「前置脚本/后置脚本」表头（无需手工重建文件）",
          "前置脚本" in headers_after and "后置脚本" in headers_after, headers_after[-3:])
    back = next(c for c in excel_loader.load(TMP_CASES).cases if c.case_id == first.case_id)
    check("脚本引用无损往返（写入 == 读回）",
          back.pre_scripts == [S_OK] and back.post_scripts == [S_CLEAN],
          (back.pre_scripts, back.post_scripts))
    check("补列未波及其他列",
          back.url == first.url and back.asserts == first.asserts and back.priority == first.priority,
          (back.url, first.url))
    excel_writer.apply_update(TMP_CASES, second.case_id, second.row, {"pre_scripts": [S_OK, S_CLEAN]})
    back2 = next(c for c in excel_loader.load(TMP_CASES).cases if c.case_id == second.case_id)
    check("多个引用以逗号存放并能正确拆回", back2.pre_scripts == [S_OK, S_CLEAN], back2.pre_scripts)
    excel_writer.apply_update(TMP_CASES, back2.case_id, back2.row, {"pre_scripts": []})
    back3 = next(c for c in excel_loader.load(TMP_CASES).cases if c.case_id == second.case_id)
    check("清空引用写回空单元格", back3.pre_scripts == [], back3.pre_scripts)

    try:
        excel_writer.apply_update(TMP_CASES, first.case_id, first.row, {"pre_scripts": ["含,逗号的名字"]})
        write_err = None
    except excel_writer.CaseWriteError as e:
        write_err = str(e)
    check("脚本名含分隔符时拒绝写回（不静默拆成两条）",
          bool(write_err) and "分隔符" in write_err, write_err)

    # ==================== 5. 引用保护与权限（接口侧，仍指向副本） ====================
    print("== 5. 引用保护与权限 ==")
    enc = "?file=" + urllib.parse.quote(TMP_REL)
    code, resp = req("PUT", "/api/cases/" + q(first.case_id) + enc, {"pre_scripts": [S_OK]}, admin_token)
    check("用例保存脚本引用（写回 Excel）", code == 200 and resp.get("pre_scripts") == [S_OK],
          f"{code} {str(resp)[:200]}")
    code, resp = req("PUT", "/api/cases/" + q(first.case_id) + enc,
                     {"pre_scripts": ["冒烟不存在的脚本名"]}, admin_token)
    check("用例引用未定义的脚本时保存被拒 400", code == 400 and "不存在" in str(resp), f"{code} {str(resp)[:160]}")
    code, resp = req("PUT", "/api/cases/" + q(first.case_id) + enc, {"priority": "P1"}, vt)
    check("viewer 改同一用例仍被后端 403（引用不改变安全边界）", code == 403, f"{code} {resp}")

    code, resp = req("DELETE", "/api/scripts/" + q(S_OK) + enc, token=qa_token)
    check("删除被引用的脚本 → 409 引用保护", code == 409 and first.case_id in str(resp),
          f"{code} {str(resp)[:200]}")
    code, resp = req("PUT", "/api/scripts/" + q(S_OK) + enc, {"name": "冒烟改名"}, token=qa_token)
    check("被引用的脚本改名同样被拦下 409", code == 409, f"{code} {str(resp)[:160]}")
    code, items = req("GET", "/api/scripts" + enc, token=qa_token)
    users = next((i.get("used_by") for i in items if i["name"] == S_OK), None) if code == 200 else None
    check("列表可查到具体是哪条用例在引用", users == [first.case_id], users)

    for method, path, body, label in [
        ("POST", "/api/scripts", {"name": "冒烟越权", "steps": [{"action": "sleep", "seconds": 1}]}, "新建脚本"),
        ("PUT", "/api/scripts/" + q(S_OK), {"remark": "x"}, "改脚本"),
        ("DELETE", "/api/scripts/" + q(S_OK), None, "删脚本"),
    ]:
        code, resp = req(method, path, body, vt)
        check(f"viewer {label} 被后端 403 拦截", code == 403, f"{code} {str(resp)[:120]}")

    code, resp = req("PUT", "/api/scripts/" + q(S_CLEAN) + enc,
                     {"remark": "冒烟用：后置清理打点（无人引用）"}, qa_token)
    check("无人引用的脚本可正常编辑", code == 200 and resp.get("remark"), f"{code} {str(resp)[:160]}")
    code, resp = req("POST", "/api/scripts", {"name": S_OK, "steps": steps_ok}, admin_token)
    check("重名脚本被拒 400", code == 400, f"{code} {str(resp)[:120]}")

    # ==================== 收尾：清空副本里的脚本引用，再删冒烟脚本与副本 ====================
    # 引用未清空时删除会被引用保护拦下（409），这正是上面那条断言的反面，必须按顺序复原
    excel_writer.apply_update(TMP_CASES, first.case_id, first.row,
                              {"pre_scripts": [], "post_scripts": []})
    cleared = next(c for c in excel_loader.load(TMP_CASES).cases if c.case_id == first.case_id)
    check("多字段一次写回可同时清空前置与后置引用",
          cleared.pre_scripts == [] and cleared.post_scripts == [],
          (cleared.pre_scripts, cleared.post_scripts))
    for name in (S_OK, S_CLEAN, S_BAD):
        req("DELETE", "/api/scripts/" + q(name) + enc, token=admin_token)
    TMP_CASES.unlink(missing_ok=True)
    code, left = req("GET", "/api/scripts", token=admin_token)
    check("冒烟脚本已回收（脚本库无残留）",
          code == 200 and not [i for i in left if str(i.get("name", "")).startswith("冒烟")],
          [i.get("name") for i in left] if code == 200 else left)
    check("临时用例副本已删除", not TMP_CASES.exists())
    check("真实用例文件未被冒烟波及（仍不含脚本列，引用全为空）",
          all(c.pre_scripts == [] and c.post_scripts == [] for c in excel_loader.load(real).cases))

    print(f"\n结果：通过 {_passed} / 失败 {_failed}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
