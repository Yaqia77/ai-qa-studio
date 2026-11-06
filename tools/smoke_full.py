# -*- coding: utf-8 -*-
"""平台全功能端到端冒烟测试（项目维度重构后的主链路回归）。

用法：先启动后端，再执行本脚本
    python -m uvicorn backend.main:app --port 8000
    python tools/smoke_full.py                 # 默认打 127.0.0.1:8000
    set IA_BASE=http://127.0.0.1:8899 && python tools/smoke_full.py

覆盖（与 smoke_authz 的权限专项互补，这里聚焦「功能主链路跑通」）：
  1. 健康与登录基线、只读统计接口
  2. 用例项目 CRUD：建项目→脚手架空文件→空项目可读（allow_empty）→改名→重名/非法名/内置项保护
  3. 用例 CRUD：file 参数贯穿新建/列表/元数据/更新/删除（模块按项目隔离）
  4. 智能导入：预览（任意表头→标准用例）→ 确认落盘（写入临时项目文件）
  5. 用例依赖：设置/读取/覆盖
  6. Cron 预览 + 定时任务 CRUD（run_config 指向临时项目文件）
  7. 执行链路：发起批次→轮询终态→报告可取→删除批次（含目录清理）
  8. 零副作用收尾：临时项目/场景/批次全部回收，项目数回到基线

脚本只在临时项目文件（data/cases/proj_*.xlsx）与内存批次里造数据，收尾必删；
执行批次用「关键字不命中任何用例」构造，零外部 API 依赖、不触碰真实用例与报告。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from io import BytesIO

BASE = os.environ.get("IA_BASE", "http://127.0.0.1:8000").rstrip("/")
ADMIN = ("admin", "admin123")
VIEWER = ("viewer", "viewer123")
QA = ("qa", "qa123")

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
    """极简 JSON HTTP 客户端（仅标准库，避免给后端测试拉额外依赖）。"""
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


def upload(method, path, filename, file_bytes, token=None):
    """multipart/form-data 上传（智能导入/文件导入用），仅标准库手工拼边界。"""
    boundary = "----smokefull" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="uploaded_file"; filename="{filename}"\r\n'
        f"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
    ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    r = urllib.request.Request(BASE + path, data=body, method=method)
    r.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
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


def login(username, password):
    code, resp = req("POST", "/api/auth/login", {"username": username, "password": password})
    assert code == 200, f"登录失败 {username}: {code} {resp}"
    return resp["token"]


def build_cases_xlsx(rows):
    """用 openpyxl 造一张标准表头的用例 xlsx（智能导入/文件导入的上传样本）。

    rows: list[dict]，键用平台字段名（case_id/module/api_name/...）。
    """
    from openpyxl import Workbook
    from core.excel_loader import CASE_COLUMNS

    wb = Workbook()
    ws = wb.active
    ws.title = "用例"
    ws.append(list(CASE_COLUMNS.keys()))          # 中文表头，顺序与 loader 一致
    for r in rows:
        line = []
        for field in CASE_COLUMNS.values():
            v = r.get(field)
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            line.append(v)
        ws.append(line)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def wait_run_terminal(run_id, timeout=90):
    """轮询批次直到非 running，返回最终 RunOut；超时返回最后一次读到的记录。"""
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


def project_count(token):
    code, resp = req("GET", "/api/projects", token=token)
    return len(resp) if code == 200 else -1


def main():
    global ADMIN_TOK
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if not BASE.startswith("http"):
        print("IA_BASE 需为 http(s):// 开头", file=sys.stderr)
        return 2

    print(f"目标：{BASE}")

    print("== 1. 健康与登录基线 ==")
    code, _ = req("GET", "/api/health")
    check("健康检查可访问", code == 200, code)
    ADMIN_TOK = login(*ADMIN)
    qa_tok = login(*QA)
    vt = login(*VIEWER)
    code, resp = req("GET", "/api/projects")
    check("未登录访问项目列表 401", code == 401, f"{code} {resp}")
    base_proj = project_count(ADMIN_TOK)
    check("项目列表基线可读且含默认项目", base_proj >= 1 and
          any(p["id"] == "default" and p["builtin"] for p in req("GET", "/api/projects", token=ADMIN_TOK)[1]),
          base_proj)

    print("== 2. 用例项目 CRUD + 空项目可读 ==")
    uniq = uuid.uuid4().hex[:6]
    pname = f"冒烟临时项目_{uniq}"
    code, proj = req("POST", "/api/projects", {"name": pname, "desc": "冒烟自动创建"}, ADMIN_TOK)
    check("建项目成功 201，返回 file 与 case_count=0",
          code == 201 and proj.get("file", "").startswith("data/cases/") and proj.get("case_count") == 0,
          f"{code} {proj}")
    pid, pfile = proj.get("id"), proj.get("file")
    if not pid:
        print("  建项目失败，后续用例/导入/依赖无法进行，中止", file=sys.stderr)
        return 1

    code, resp = req("POST", "/api/projects", {"name": pname}, ADMIN_TOK)
    check("同名项目冲突 409", code == 409, f"{code} {resp}")
    code, resp = req("POST", "/api/projects", {"name": "非法/含斜杠"}, ADMIN_TOK)
    check("名称含 / 被拒 400", code == 400, f"{code} {resp}")
    code, resp = req("POST", "/api/projects", {"name": pname}, vt)
    check("viewer 建项目被拒 403（复用 case:write）", code == 403, f"{code} {resp}")

    # 空项目文件应能被列表接口读到空结果（allow_empty），而非 400
    code, resp = req("GET", "/api/cases?" + urllib.parse.urlencode({"file": pfile}), token=ADMIN_TOK)
    check("刚建项目零用例可读为 total=0（allow_empty 生效）",
          code == 200 and resp.get("total") == 0, f"{code} {resp}")
    code, resp = req("GET", "/api/cases/meta?" + urllib.parse.urlencode({"file": pfile}), token=ADMIN_TOK)
    check("空项目元数据可读（有优先级/方法候选）",
          code == 200 and resp.get("priorities") and resp.get("methods"), f"{code} {resp}")

    code, resp = req("PUT", "/api/projects/" + urllib.parse.quote(pid, safe=""),
                     {"name": pname + "_改名"}, ADMIN_TOK)
    check("项目改名成功且 file 不变",
          code == 200 and resp.get("file") == pfile and resp.get("name") == pname + "_改名", f"{code} {resp}")
    code, resp = req("DELETE", "/api/projects/default", token=ADMIN_TOK)
    check("默认项目不可删除 400", code == 400, f"{code} {resp}")

    print("== 3. 用例 CRUD（file 参数贯穿，模块按项目隔离） ==")
    cid = f"smoke_{uniq}_c1"
    case_body = {
        "case_id": cid, "module": f"冒烟模块_{uniq}", "api_name": "冒烟接口",
        "url": "/demo/ping", "method": "GET", "priority": "P1",
        "asserts": [["eq", "status_code", 200]],
    }
    code, resp = req("POST", "/api/cases?" + urllib.parse.urlencode({"file": pfile}), case_body, ADMIN_TOK)
    check("在项目文件新增用例 201", code == 201 and resp.get("case_id") == cid, f"{code} {resp}")
    code, resp = req("GET", "/api/cases?" + urllib.parse.urlencode({"file": pfile, "keyword": cid}), token=ADMIN_TOK)
    check("按 file 列表能查到该用例", code == 200 and resp.get("total") == 1, f"{code} {resp}")
    code, resp = req("GET", "/api/cases?" + urllib.parse.urlencode({"file": "data/api_cases.xlsx", "keyword": cid}), token=ADMIN_TOK)
    check("默认项目查不到该用例（项目隔离）", code == 200 and resp.get("total") == 0, f"{code} {resp}")
    code, resp = req("PUT", "/api/cases/" + urllib.parse.quote(cid, safe="") + "?" + urllib.parse.urlencode({"file": pfile}),
                     {"priority": "P3"}, ADMIN_TOK)
    check("按 file 更新用例优先级", code == 200 and resp.get("priority") == "P3", f"{code} {resp}")
    code, resp = req("GET", "/api/projects/" + urllib.parse.quote(pid, safe=""), token=ADMIN_TOK)
    check("项目用例数随新增变为 1", code == 200 and resp.get("case_count") == 1, f"{code} {resp}")

    print("== 3b. 用例迁移（跨项目移动 + 目标查重保护） ==")
    code, proj2 = req("POST", "/api/projects", {"name": f"冒烟迁移目标_{uniq}", "desc": "迁移目标"}, ADMIN_TOK)
    pid2, pfile2 = proj2.get("id"), proj2.get("file")
    check("建迁移目标项目", code == 201 and bool(pid2), f"{code} {proj2}")
    mv_cid = f"smoke_{uniq}_mv"
    mv_qs = urllib.parse.urlencode({"file": pfile})
    mv_body = {"case_id": mv_cid, "module": f"待迁移_{uniq}", "api_name": "迁移接口",
               "url": "/demo/mv", "method": "GET", "priority": "P2",
               "asserts": [["eq", "status_code", 200]]}
    code, resp = req("POST", "/api/cases?" + mv_qs, mv_body, ADMIN_TOK)
    check("在源项目建待迁移用例", code == 201, f"{code} {resp}")
    mv_path = "/api/cases/" + urllib.parse.quote(mv_cid, safe="") + "/move?" + mv_qs
    code, resp = req("POST", mv_path, {"to_file": pfile2, "new_module": f"已迁入_{uniq}"}, ADMIN_TOK)
    check("迁移 200：写入目标且改模块",
          code == 200 and resp.get("to_file") == pfile2 and
          (resp.get("case") or {}).get("module") == f"已迁入_{uniq}", f"{code} {resp}")
    code, resp = req("GET", "/api/cases?" + urllib.parse.urlencode({"file": pfile, "keyword": mv_cid}), token=ADMIN_TOK)
    check("源项目已无该用例", code == 200 and resp.get("total") == 0, f"{code} {resp}")
    code, resp = req("GET", "/api/cases?" + urllib.parse.urlencode({"file": pfile2, "keyword": mv_cid}), token=ADMIN_TOK)
    check("目标项目已有该用例", code == 200 and resp.get("total") == 1, f"{code} {resp}")
    # 目标查重保护：源再建同编号用例，再迁往已含同编号的目标 → 409，且源不被删
    req("POST", "/api/cases?" + mv_qs, mv_body, ADMIN_TOK)
    code, resp = req("POST", mv_path, {"to_file": pfile2}, ADMIN_TOK)
    check("目标已存同编号被拒 409", code == 409, f"{code} {resp}")
    code, resp = req("POST", mv_path, {"to_file": pfile2}, vt)
    check("viewer 迁移被拒 403（复用 case:write）", code == 403, f"{code} {resp}")
    # 清理：删源里重建的那条 + 删整个目标项目（连带其内迁入的用例）
    req("DELETE", "/api/cases/" + urllib.parse.quote(mv_cid, safe="") + "?" + mv_qs, token=ADMIN_TOK)
    req("DELETE", "/api/projects/" + urllib.parse.quote(pid2, safe=""), token=ADMIN_TOK)
    check("迁移临时项已回收（项目数回到基线）", project_count(ADMIN_TOK) == base_proj + 1,
          project_count(ADMIN_TOK))

    print("== 4. 智能导入（预览→确认，写入临时项目文件） ==")
    imp_rows = [
        {"case_id": f"smoke_{uniq}_imp1", "module": "导入模块", "api_name": "导入接口A",
         "case_desc": "导入描述A", "priority": "P2", "run": "Y", "method": "POST",
         "url": "/demo/a", "asserts": [["eq", "status_code", 200]]},
        {"case_id": f"smoke_{uniq}_imp2", "module": "导入模块", "api_name": "导入接口B",
         "case_desc": "导入描述B", "priority": "P0", "run": "Y", "method": "GET",
         "url": "/demo/b", "asserts": [["not_null", "$.data"]]},
    ]
    xlsx = build_cases_xlsx(imp_rows)
    pv_url = "/api/cases/import/preview?" + urllib.parse.urlencode({"file": pfile})
    code, resp = upload("POST", pv_url, "smoke_imp.xlsx", xlsx, ADMIN_TOK)
    items = resp.get("items", []) if code == 200 else []
    check("智能导入预览返回可导入项", code == 200 and len(items) == 2 and
          all(i.get("importable") for i in items), f"{code} {resp}")
    drafts = [i["case"] for i in items if i.get("importable")]
    cf_url = "/api/cases/import/confirm?" + urllib.parse.urlencode({"file": pfile})
    code, resp = req("POST", cf_url, {"cases": drafts}, ADMIN_TOK)
    check("智能导入确认落盘 imported=2", code == 200 and resp.get("imported") == 2, f"{code} {resp}")
    code, resp = req("GET", "/api/cases?" + urllib.parse.urlencode({"file": pfile}), token=ADMIN_TOK)
    check("导入后项目共 3 条用例", code == 200 and resp.get("total") == 3, f"{code} {resp}")

    print("== 5. 用例依赖 CRUD ==")
    dep_url = "/api/dependencies/" + urllib.parse.quote(cid, safe="")
    dep_val = f"smoke_{uniq}_imp1"
    code, resp = req("PUT", dep_url, {"dependencies": [dep_val]}, ADMIN_TOK)
    check("设置用例依赖", code == 200 and resp.get("dependencies") == [dep_val], f"{code} {resp}")
    code, resp = req("GET", dep_url, token=ADMIN_TOK)
    check("读取用例依赖一致", code == 200 and resp.get("dependencies") == [dep_val], f"{code} {resp}")
    code, resp = req("PUT", dep_url, {"dependencies": []}, ADMIN_TOK)
    check("清空依赖（覆盖式）", code == 200 and resp.get("dependencies") == [], f"{code} {resp}")

    print("== 6. Cron 预览 + 定时任务 CRUD ==")
    code, resp = req("GET", "/api/schedules/preview/next-runs?" + urllib.parse.urlencode({"cron": "0 9 * * 1-5", "n": 3}), token=ADMIN_TOK)
    check("Cron 预览返回 3 个未来时间", code == 200 and len(resp.get("next_runs", [])) == 3, f"{code} {resp}")
    code, resp = req("GET", "/api/schedules/preview/next-runs?" + urllib.parse.urlencode({"cron": "not a cron"}), token=ADMIN_TOK)
    check("非法 Cron 被拒 400", code == 400, f"{code} {resp}")
    sch_body = {"name": f"冒烟定时_{uniq}", "cron": "30 8 * * *",
                "run_config": {"file": pfile, "keyword": "zzz_never_match"}}
    code, resp = req("POST", "/api/schedules", sch_body, ADMIN_TOK)
    check("建定时任务 201（run_config 指向项目文件）", code == 201 and resp.get("id"), f"{code} {resp}")
    sch_id = resp.get("id")
    if sch_id:
        code, resp = req("PUT", "/api/schedules/" + urllib.parse.quote(sch_id, safe=""), {"enabled": False}, ADMIN_TOK)
        check("更新定时任务停用", code == 200 and resp.get("enabled") is False, f"{code} {resp}")

    print("== 7. 执行链路（发起→终态→报告→删除，零外部依赖） ==")
    code, resp = req("POST", "/api/runs", {"file": pfile, "keyword": "zzz_never_match_smoke"}, ADMIN_TOK)
    check("发起批次 202 且拿到 run_id", code == 202 and resp.get("run_id"), f"{code} {resp}")
    run_id = resp.get("run_id")
    if run_id:
        final = wait_run_terminal(run_id)
        check("批次到达终态（非 running）", final.get("status") in
              ("passed", "failed", "error", "cancelled", "broken"), f"终态={final.get('status')}")
        code, resp = req("GET", "/api/runs/" + urllib.parse.quote(run_id, safe="") + "/log", token=ADMIN_TOK)
        check("批次日志可读", code == 200 and "log" in resp, f"{code} {resp}")
        code, rec = req("GET", "/api/runs/" + urllib.parse.quote(run_id, safe=""), token=ADMIN_TOK)
        rd = (rec or {}).get("report_dir")
        check("批次已生成报告目录", bool(rd), f"report_dir={rd}")
        # 删除接口按 run_id 寻址（内存记录 + 历史条目 + 报告目录一并清理）
        code, resp = req("DELETE", "/api/runs/" + urllib.parse.quote(run_id, safe=""), token=ADMIN_TOK)
        check("删除批次报告（含目录）", code == 200 and resp.get("deleted_report_dir") == rd, f"{code} {resp}")
        code, resp = req("GET", "/api/runs/" + urllib.parse.quote(run_id, safe=""), token=ADMIN_TOK)
        check("批次记录已不存在 404", code == 404, f"{code} {resp}")

    print("== 8. 收尾与零副作用核验 ==")
    if sch_id:
        req("DELETE", "/api/schedules/" + urllib.parse.quote(sch_id, safe=""), token=ADMIN_TOK)
    code, resp = req("DELETE", "/api/projects/" + urllib.parse.quote(pid, safe=""), token=ADMIN_TOK)
    check("删除临时项目成功", code == 200 and resp.get("deleted") == pid, f"{code} {resp}")
    code, resp = req("GET", "/api/cases?" + urllib.parse.urlencode({"file": pfile}), token=ADMIN_TOK)
    check("项目文件随删除不可达（404）", code == 404, f"{code} {resp}")
    after_proj = project_count(ADMIN_TOK)
    check("项目数回到基线（临时项已回收）", after_proj == base_proj, f"{base_proj} -> {after_proj}")
    code, resp = req("GET", "/api/cases?" + urllib.parse.urlencode({"file": "data/api_cases.xlsx", "keyword": uniq}), token=ADMIN_TOK)
    check("默认项目未被冒烟污染（关键字 0 命中）", code == 200 and resp.get("total") == 0, f"{code} {resp}")

    print(f"\n结果：通过 {_passed} / 失败 {_failed}")
    return 1 if _failed else 0


ADMIN_TOK = None

if __name__ == "__main__":
    sys.exit(main())
