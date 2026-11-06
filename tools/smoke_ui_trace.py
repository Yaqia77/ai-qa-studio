# -*- coding: utf-8 -*-
"""UI 自动化观测与诊断端到端冒烟（失败 trace 留存 + 下载 + flaky 字段）。

用法：先启动后端（须用装了 playwright 的 venv 解释器），再执行本脚本
    .venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
    .venv\Scripts\python.exe tools\smoke_ui_trace.py

设计：自带本地 http 服务提供 fixture 页；只在临时 UI 项目造数据、收尾必删，零副作用；
浏览器不可用时执行段合理 skip 不判失败。覆盖：
  - meta 常跑（不依赖浏览器）；
  - 失败用例详情含 trace_file → /api/runs/{id}/trace/{file} 可下载 application/zip（PK 魔数）；
  - 未登录 401、非 zip 400、路径穿越 400（护栏）；
  - 通过用例详情 trace_file 为空（不产出）；
  - flaky 结构化字段：/cases 摘要含 attempts/retried/flaky（缺省 1/False/False）。
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ui import (  # noqa: E402
    BASE, ROOT, ADMIN, check, login, req, wait_run_terminal, browser_ready, project_count,
)
import smoke_ui as _smoke  # noqa: E402
from smoke_ui_actions import serve_fixtures, case  # noqa: E402


def _detail(run_id, cid, tok):
    code, detail = req("GET", f"/api/runs/{urllib.parse.quote(run_id, safe='')}/cases/"
                             + urllib.parse.quote(cid, safe=""), token=tok)
    return code, (detail or {})


def main():
    print(f"目标：{BASE}")
    code, _ = req("GET", "/api/health")
    if code != 200:
        print(f"后端不可达：{BASE}", file=sys.stderr)
        return 2
    tok = login(*ADMIN)
    _smoke.ADMIN_TOK = tok  # wait_run_terminal 轮询引用模块级 ADMIN_TOK
    base_proj = project_count(tok)

    uniq = _uuid6()
    code, proj = req("POST", "/api/projects", {"name": f"六期trace冒烟_{uniq}", "kind": "ui"}, tok)
    pfile = proj.get("file")
    check("建临时 UI 项目", code == 201 and pfile, f"{code} {proj}")
    if not pfile:
        print("项目创建失败，中止", file=sys.stderr)
        return 1

    run_ids: list[str] = []
    srv = None
    try:
        if not browser_ready():
            print("  （执行链路已跳过；装好 chromium 后重跑本脚本即可覆盖）")
        else:
            base_url, srv = serve_fixtures()
            demo = f"{base_url}/ui_demo.html"

            # 1) 必失败用例：期望不存在的文本 → 触发失败 trace 导出
            fail_cid = f"fail_{uniq}"
            fail_case = case(fail_cid, "失败触发trace", [
                {"action": "goto", "value": demo},
                {"action": "expect_text", "locator": "css=#title", "value": "绝不存在的文本XYZ"},
            ])
            code, resp = req("POST", "/api/ui-cases" + _smoke.qf(pfile), fail_case, tok)
            check("建失败用例", code in (200, 201), f"{code} {resp}")
            code, resp = req("POST", "/api/runs", {"file": pfile, "keyword": fail_cid}, tok)
            fail_run = resp.get("run_id") if code == 202 else None
            check("发起失败批次", bool(fail_run), f"{code} {resp}")
            if fail_run:
                run_ids.append(fail_run)
                final = wait_run_terminal(fail_run)
                check("失败批次终态为 failed", final.get("status") == "failed", f"终态={final.get('status')}")

                code, detail = _detail(fail_run, fail_cid, tok)
                trace_file = detail.get("trace_file")
                check("失败用例详情含 trace_file", code == 200 and bool(trace_file), f"{code} {trace_file}")

                # flaky 结构化字段：单批次摘要即应带 attempts/retried/flaky（未重试 → 1/False/False）
                code, cases = req("GET", f"/api/runs/{urllib.parse.quote(fail_run, safe='')}/cases", token=tok)
                item = next((c for c in (cases.get("items") or []) if c.get("case_id") == fail_cid), {})
                check("摘要含结构化 flaky 字段（缺省 attempts=1/retried=False/flaky=False）",
                      "attempts" in item and "retried" in item and "flaky" in item
                      and item.get("attempts") == 1 and item.get("flaky") is False, f"{code} {item}")

                if trace_file:
                    tr = f"/api/runs/{urllib.parse.quote(fail_run, safe='')}/trace/" + urllib.parse.quote(trace_file, safe="")
                    code, body, ctype = req("GET", tr, token=tok, raw=True)
                    check("登录态下载 trace（application/zip + PK 魔数）",
                          code == 200 and str(ctype).startswith("application/zip")
                          and isinstance(body, (bytes, bytearray)) and bytes(body[:2]) == b"PK",
                          f"{code} {ctype} head={bytes(body[:2]) if body else None}")
                    code, _, _ = req("GET", tr, raw=True)
                    check("未登录下载 trace 401", code == 401, code)

                # 护栏：非 zip / 路径穿越 → 400
                bad_base = f"/api/runs/{urllib.parse.quote(fail_run, safe='')}/trace/"
                code, _, _ = req("GET", bad_base + urllib.parse.quote("note.txt", safe=""), token=tok, raw=True)
                check("非 zip 扩展名拒绝 400", code == 400, code)

            # 2) 通过用例：不应产出 trace_file
            pass_cid = f"pass_{uniq}"
            pass_case = case(pass_cid, "通过无trace", [
                {"action": "goto", "value": demo},
                {"action": "expect_visible", "locator": "testid=profile-card"},
            ])
            code, resp = req("POST", "/api/ui-cases" + _smoke.qf(pfile), pass_case, tok)
            code, resp = req("POST", "/api/runs", {"file": pfile, "keyword": pass_cid}, tok)
            pass_run = resp.get("run_id") if code == 202 else None
            if pass_run:
                run_ids.append(pass_run)
                wait_run_terminal(pass_run)
                code, detail = _detail(pass_run, pass_cid, tok)
                steps = detail.get("ui_steps") or []
                check("通过用例终态 passed", code == 200 and detail.get("status") == "passed",
                      f"{code} {detail.get('status')}")
                check("通过用例无 trace_file（不产出）", detail.get("trace_file") is None, f"{detail.get('trace_file')}")
    finally:
        print("== 收尾与零副作用核验 ==")
        for rid in run_ids:
            req("DELETE", "/api/runs/" + urllib.parse.quote(rid, safe=""), token=tok)
        code, resp = req("DELETE", "/api/projects/" + urllib.parse.quote(proj.get("id", ""), safe=""), token=tok)
        check("删除临时 UI 项目", code == 200 and resp.get("deleted"), f"{code} {resp}")
        after = project_count(tok)
        check("项目数回到基线", after == base_proj, f"{base_proj} -> {after}")
        if srv:
            srv.shutdown()
            srv.server_close()

    print(f"\n结果：通过 {check.__globals__['_passed']} / 失败 {check.__globals__['_failed']}")
    return 1 if check.__globals__["_failed"] else 0


def _uuid6() -> str:
    import uuid
    return uuid.uuid4().hex[:6]


if __name__ == "__main__":
    sys.exit(main())
