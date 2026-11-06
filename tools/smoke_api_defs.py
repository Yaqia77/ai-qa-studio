# -*- coding: utf-8 -*-
"""接口定义管理冒烟：校验同源 + Excel 聚合 + OpenAPI 导入 + 引用保护 + 用例骨架。

用法：先启动后端
    .venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
    .venv\Scripts\python.exe tools\smoke_api_defs.py
    set IA_BASE=http://127.0.0.1:8899 && .venv\Scripts\python.exe tools\smoke_api_defs.py

零污染约定：Excel 写入全部在 data/~$smoke_defs_cases.xlsx 副本上进行（~$ 前缀既被 loader
目录模式排除，也被 .gitignore 排除）；接口库只回收本次新建的定义，开始前已存在的一律不动。
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

BASE = os.environ.get("IA_BASE", "http://127.0.0.1:8000").rstrip("/")
PASS = {"admin": os.environ.get("IA_ADMIN_PASS", "admin123"),
        "qa": os.environ.get("IA_QA_PASS", "qa123"),
        "viewer": os.environ.get("IA_VIEWER_PASS", "viewer123")}
TMP_REL = "data/~$smoke_defs_cases.xlsx"        # 传给接口 ?file= 的相对路径
TMP_CASES = ROOT / TMP_REL
REAL_CASES = ROOT / "data" / "api_cases.xlsx"

# 引用保护用聚合产物验证：真实用例里存在「接口名称」同名的 16 条用例
REF_NAME = "统一相册列表(分页)"
A_NAME = "冒烟接口A"
B_NAME = "冒烟接口B"

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


# ==================== OpenAPI / Swagger 样例（覆盖两种版本、$ref、脏值省略、multipart） ====================

SPEC3 = {
    "openapi": "3.0.1",
    "info": {"title": "冒烟相册服务", "version": "v2"},
    "paths": {
        "/album/{id}": {
            "get": {
                "tags": ["相册管理"],
                "summary": "冒烟获取相册详情",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}},
                    {"name": "pageNum", "in": "query", "schema": {"type": "integer", "default": 1}},
                    # 文档没给样例的参数：宁可缺一行，也不往骨架里塞空值
                    {"name": "keyword", "in": "query", "schema": {"type": "string"}},
                    {"name": "x-tenant", "in": "header", "schema": {"type": "string", "example": "t-1"}},
                ],
                "responses": {"200": {"description": "ok", "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/AlbumResp"}}}}},
            },
            # 与 GET 同名的 summary：应被唯一化为「…(2)」；给空 responses 验证状态码回落
            "patch": {"tags": ["相册管理"], "summary": "冒烟获取相册详情", "responses": {}},
            # 既无 summary 也无 operationId：兜底命名「方法 路径」
            "delete": {"responses": {"204": {"description": "no content"}}},
        },
        "/album/upload": {
            "post": {
                "summary": "冒烟上传相册图",
                "requestBody": {"content": {"multipart/form-data": {
                    "schema": {"type": "object", "properties": {"file": {"type": "string", "format": "binary"}}}}}},
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {
                        "code": {"type": "string"},
                        "data": {"type": "object", "properties": {"token": {"type": "string"}}}}}}}}},
            }
        },
    },
    "components": {"schemas": {"AlbumResp": {
        "type": "object", "properties": {
            "code": {"type": "string", "example": "0000"},
            "success": {"type": "boolean"},
            "data": {"type": "object", "properties": {
                "token": {"type": "string", "example": "TK-1"}, "total": {"type": "integer"}}}}}}},
}

SPEC2_YAML = """
swagger: "2.0"
info: {title: 冒烟订单服务, version: v1}
basePath: /api/v1
paths:
  /orders/{orderId}:
    put:
      operationId: 冒烟更新订单
      parameters:
        - {name: orderId, in: path, type: string}
        - {name: body, in: body, schema: {type: object, properties: {status: {type: string, enum: [PAID, DONE]}}}}
      responses:
        default:
          description: 兜底响应
          schema: {type: object, properties: {code: {type: string}}}
"""


def find_def(items, name):
    return next((i for i in items if i.get("name") == name), None)


def main():
    # loguru 的逐请求日志会淹没冒烟结论，默认静音（IA_DEF_DEBUG=1 可恢复）
    from loguru import logger
    if os.environ.get("IA_DEF_DEBUG") != "1":
        logger.remove()

    from backend.api import api_def_store, excel_writer, openapi_import

    print(f"目标：{BASE}")
    admin_token, qa_token, vt = login("admin"), login("qa"), login("viewer")

    code, before = req("GET", "/api/defs", token=admin_token)
    check("接口库初始可读（新仓库无文件也返回空列表）", code == 200 and isinstance(before, list),
          f"{code} {str(before)[:160]}")
    # 历次跑崩在这里时留下的残留先回收（名字含“冒烟”或来自冒烟 spec），保证可重复执行
    stale = {i["name"] for i in before if "冒烟" in i["name"] or "-album-" in i["name"]}
    for name in sorted(stale):
        req("DELETE", "/api/defs/" + q(name), token=admin_token)
    # 开始前就存在且不属于残留的定义 = 用户数据，收尾一律不动
    saved_names = {i["name"] for i in before} - stale
    saved_remark = {i["name"]: i.get("remark") for i in before if i["name"] in saved_names}

    # ==================== 1. 元数据与校验口径同源 ====================
    print("== 1. /api/defs/meta：前端表单常量与后端校验同一处定义 ==")
    code, meta = req("GET", "/api/defs/meta", token=admin_token)
    check("元数据可读", code == 200 and meta.get("methods"), f"{code} {str(meta)[:200]}")
    check("方法候选与 loader 一致", sorted(meta["methods"]) == sorted(excel_loader.VALID_METHODS),
          meta.get("methods"))
    check("断言操作符候选与 loader 一致",
          sorted(o["op"] for o in meta["assert_ops"]) == sorted(excel_loader.ASSERT_OPS),
          [o["op"] for o in meta.get("assert_ops", [])])
    check("需要期望值的操作符与 loader 一致",
          sorted(meta["ops_need_expected"]) == sorted(excel_loader.OPS_NEED_EXPECTED),
          meta.get("ops_need_expected"))
    check("长度上限来自存储层常量",
          meta["name_max"] == api_def_store.NAME_MAX and meta["assert_max"] == api_def_store.ASSERT_MAX,
          (meta.get("name_max"), meta.get("assert_max")))
    check("来源枚举含三种（手工/聚合/导入）",
          {s["value"] for s in meta["sources"]} == set(api_def_store.SOURCE_LABELS), meta.get("sources"))
    check("导入大小限制与解析层常量一致",
          meta["spec_limit_mb"] * 1024 * 1024 == openapi_import.SPEC_MAX_BYTES, meta.get("spec_limit_mb"))
    check("取值路径候选含 HTTP 码与变量池",
          {p["path"] for p in meta["path_options"]} >= {"status_code", "var."}, meta.get("path_options"))

    # ==================== 2. CRUD + 定义校验 ====================
    print("== 2. 单条定义 CRUD 与合法性校验 ==")
    for nm in (A_NAME, B_NAME):
        req("DELETE", "/api/defs/" + q(nm), token=admin_token)      # 预清理，保证可重复执行

    payload_a = {
        "name": A_NAME, "module": "冒烟模块", "method": "post", "path": "/smoke/album/a",
        "remark": "冒烟用：校验请求头/断言建议/变量提取都能原样保存",
        "headers": {"Content-Type": "application/json", "Authorization": "Basic {{auth_token}}"},
        "params": {"pageNum": "1"},
        "body": {"albumId": "{{album_id}}", "pageSize": 20},
        "extract": {"album_id": "$.data.id"},
        "asserts": [["eq", "status_code", 200], ["not_null", "$.data"], ["eq", "$.code", "0000"]],
    }
    code, resp = req("POST", "/api/defs", payload_a, qa_token)
    check("qa（有 api:manage）新建定义成功且小写方法被归一化",
          code == 201 and resp.get("method") == "POST" and resp.get("source") == "manual",
          f"{code} {str(resp)[:200]}")
    check("新建即带上引用字段（当前无用例引用）", resp.get("used_by") == [], resp.get("used_by"))
    code, got = req("GET", "/api/defs/" + q(A_NAME), token=vt)
    check("viewer 可读详情（读接口只需登录，用例页要靠它给建议）", code == 200, f"{code} {str(got)[:120]}")
    check("请求头/参数/请求体/提取/断言五段全存回",
          got.get("headers") == payload_a["headers"] and got.get("params") == payload_a["params"]
          and got.get("body") == payload_a["body"] and got.get("extract") == payload_a["extract"]
          and got.get("asserts") == payload_a["asserts"],
          {k: got.get(k) for k in ("headers", "params", "body", "extract", "asserts")})

    bad_defs = [
        ({"name": ""}, "名称为空"),
        ({"name": "x" * 61}, "名称超上限"),
        ({"name": "冒烟\n接口"}, "名称含换行（用例按名精确匹配，须单行）"),
        ({"name": "冒烟/斜杠名"}, "名称含斜杠（接口库按名称寻址，含 / 的定义无法回访）"),
        ({"method": "TELEPORT"}, "请求方式非法"),
        ({"path": "smoke/album/a"}, "路径不以 / 或 http(s):// 开头"),
        ({"path": "/smoke/a b"}, "路径含空格"),
        ({"path": ""}, "路径为空"),
        ({"asserts": [["between", "$.a", 1]]}, "断言操作符非法"),
        ({"asserts": [["eq", ""]]}, "断言取值路径为空"),
        ({"asserts": [["eq", "status_code", 200]] * 11}, "断言建议超上限"),
        ({"remark": "说" * 201}, "说明超上限"),
        ({"module": "块" * 41}, "模块名超上限"),
        ({"headers": ["不是对象"]}, "请求头不是 JSON 对象"),
        ({"body": "非 JSON 文本"}, "请求体是裸字符串"),
    ]
    for patch, label in bad_defs:
        body = {**payload_a, "name": "冒烟非法定义", **patch}
        code, resp = req("POST", "/api/defs", body, admin_token)
        # 结构类型错在 pydantic 层就被拦下（422），业务合法性在存储层（400），两者都是拒绝
        check(f"{label} 被拒 4xx", code in (400, 422), f"{code} {str(resp)[:140]}")
    code, items = req("GET", "/api/defs", token=admin_token)
    check("非法定义全部未落库",
          code == 200 and not [i for i in items if str(i["name"]).startswith("冒烟非法")],
          [i["name"] for i in items] if code == 200 else code)

    code, resp = req("PUT", "/api/defs/" + q(A_NAME),
                    {"asserts": [["eq", "status_code", 200], ["nope", "$.x"]]}, admin_token)
    check("更新时非法断言被拒 400", code == 400, f"{code} {str(resp)[:140]}")
    code, resp = req("PUT", "/api/defs/" + q("冒烟根本不存在的接口"), {"remark": "x"}, admin_token)
    check("更新不存在的定义 404", code == 404, f"{code} {str(resp)[:120]}")

    # 清空语义：headers/params/body/extract/asserts 传 null 必须真清掉（否则页面无法删干净）
    code, resp = req("PUT", "/api/defs/" + q(A_NAME),
                     {"headers": None, "params": None, "body": None,
                      "extract": None, "asserts": None, "remark": ""}, admin_token)
    check("可把请求头/参数/请求体/提取/断言一次清空",
          code == 200 and all(resp.get(k) in (None, [], {}) for k in
                              ("headers", "params", "body", "extract", "asserts"))
          and resp.get("remark") == "", f"{code} {str(resp)[:200]}")
    check("清空后来源血缘不变（编辑不篡改 excel/openapi 标记）", resp.get("source") == "manual",
          resp.get("source"))

    # ==================== 3. 唯一性：接口名 + 端点 ====================
    print("== 3. 名称唯一 + 同 (方法,路径) 视为同一端点 ==")
    code, resp = req("POST", "/api/defs", {**payload_a, "path": "/smoke/album/z"}, admin_token)
    check("重名接口被拒 400", code == 400 and "已存在" in str(resp), f"{code} {str(resp)[:160]}")
    code, resp = req("POST", "/api/defs",
                     {**payload_a, "name": B_NAME, "method": "POST", "path": "/smoke/album/a"}, admin_token)
    check("不同名称重复登记同一端点被拒 400",
          code == 400 and "已登记为接口" in str(resp), f"{code} {str(resp)[:200]}")
    code, resp = req("POST", "/api/defs",
                     {**payload_a, "name": B_NAME, "method": "POST", "path": "/smoke/album/a/"}, admin_token)
    check("末尾斜杠差异也算同一端点（归一化后判重）",
          code == 400 and "已登记为接口" in str(resp), f"{code} {str(resp)[:200]}")
    code, resp = req("POST", "/api/defs",
                     {**payload_a, "name": B_NAME, "method": "GET", "path": "/smoke/album/a"}, admin_token)
    check("换个方法即不同端点，允许重复登记",
          code == 201 and resp.get("name") == B_NAME, f"{code} {str(resp)[:160]}")

    # ==================== 4. 从存量用例聚合（接口库沉淀） ====================
    print("== 4. Excel 聚合：同名接口收敛成一份定义 ==")
    shutil.copyfile(REAL_CASES, TMP_CASES)
    enc = "?file=" + urllib.parse.quote(TMP_REL)
    real_result = excel_loader.load(REAL_CASES)
    api_names = {str(c.api_name) for c in real_result.cases if c.api_name}
    code, resp = req("POST", f"/api/defs/aggregate{enc}&overwrite=true", token=admin_token)
    check("聚合成功且分组数 == 存量接口名数",
          code == 200 and resp.get("groups") == len(api_names), f"{code} {str(resp)[:220]}")
    check("聚合产物全部落库（新建 + 覆写已有 == 分组数）",
          code == 200 and resp.get("created") + resp.get("updated") == len(api_names)
          and resp.get("skipped") == 0, f"{code} {str(resp)[:220]}")
    check("扫描条数与 loader 一致", resp.get("scanned_cases") == len(real_result.cases),
          resp.get("scanned_cases"))
    check("无端点冲突", resp.get("conflicts") == [], resp.get("conflicts"))
    check("存量接口名全部合规，无被拒分组", resp.get("rejected") == [], resp.get("rejected"))
    code, ref = req("GET", "/api/defs/" + q(REF_NAME) + enc, token=admin_token)
    check("聚合出的定义标注来源为 Excel 聚合", code == 200 and ref.get("source") == "excel",
          f"{code} {str(ref)[:160]}")
    expect_users = [c.case_id for c in real_result.cases if str(c.api_name) == REF_NAME]
    check("聚合定义立即拥有同名用例的覆盖清单", ref.get("used_by") == expect_users,
          (len(ref.get("used_by", [])), len(expect_users)))
    check("聚合的说明写明来源条数", f"由 {len(expect_users)} 条存量用例聚合" == ref.get("remark"),
          ref.get("remark"))
    grp = [c for c in real_result.cases if str(c.api_name) == REF_NAME]
    check("聚合的路径/方法取存量用例的写法",
          api_def_store._norm_path(ref["path"]) == api_def_store._norm_path(grp[0].url)
          and ref["method"] == str(grp[0].method).upper(), (ref.get("path"), grp[0].url))
    check("聚合的请求头取该 key 最常见的完整值",
          all(str(k) in (ref.get("headers") or {}) for k in (grp[0].headers or {})),
          ref.get("headers"))
    check("断言建议落库前已过 loader 校验（推荐即可用）",
          excel_loader.check_assert_rules(ref.get("asserts") or []) == [],
          ref.get("asserts"))
    eq_pairs = [(str(r[1]), json.dumps(r[2], ensure_ascii=False))
                for r in (ref.get("asserts") or []) if str(r[0]).lower() == "eq"]
    check("同一取值路径的 eq 只保留一个期望值（正例与错误码不打架）",
          len({p for p, _ in eq_pairs}) == len(eq_pairs), ref.get("asserts"))
    check("断言建议条数不超聚合上限",
          len(ref.get("asserts") or []) <= api_def_store.AGGREGATE_ASSERT_TOP, ref.get("asserts"))

    code, second = req("POST", "/api/defs/aggregate" + enc, token=admin_token)
    check("重复聚合幂等（默认不覆盖：全部跳过）",
          code == 200 and second.get("created") == 0 and second.get("updated") == 0
          and second.get("skipped") == len(api_names), f"{str(second)[:200]}")
    code, third = req("POST", f"/api/defs/aggregate{enc}&overwrite=true", token=admin_token)
    check("overwrite=true 时用最新用例刷新同名定义",
          code == 200 and third.get("updated") == len(api_names) and third.get("created") == 0,
          f"{str(third)[:200]}")

    # ==================== 5. 引用保护（与脚本库同一口径） ====================
    print("== 5. 引用保护：被同名用例引用的定义不可改名/删除 ==")
    code, resp = req("DELETE", "/api/defs/" + q(REF_NAME) + enc, token=admin_token)
    check("删除被引用的定义 → 409 并点名用例",
          code == 409 and expect_users[0] in str(resp), f"{code} {str(resp)[:220]}")
    code, resp = req("PUT", "/api/defs/" + q(REF_NAME) + enc, {"name": "冒烟改名"}, admin_token)
    check("被引用的定义改名 → 409（改名等于掐断覆盖统计）", code == 409, f"{code} {str(resp)[:200]}")
    code, resp = req("PUT", "/api/defs/" + q(REF_NAME) + enc,
                     {"remark": "冒烟改说明：仅改元数据不动用例"}, admin_token)
    check("不改名的元数据编辑照常放行", code == 200 and resp.get("remark"), f"{code} {str(resp)[:160]}")
    code, resp = req("DELETE", "/api/defs/" + q(A_NAME), token=admin_token)
    check("无人引用的定义可正常删除", code == 200 and resp.get("deleted") == A_NAME, f"{code} {resp}")

    # ==================== 6. OpenAPI / Swagger 导入 ====================
    print("== 6. OpenAPI 3 / Swagger 2 导入 ==")
    code, resp = req("POST", "/api/defs/import/openapi", {"text": json.dumps(SPEC3)}, qa_token)
    check("OpenAPI3 JSON 导入 4 个接口", code == 200 and resp.get("parsed") == 4
          and resp.get("created") == 4, f"{code} {str(resp)[:220]}")
    code, items = req("GET", "/api/defs", token=admin_token)
    det = find_def(items, "冒烟获取相册详情")
    dup = find_def(items, "冒烟获取相册详情(2)")
    upl = find_def(items, "冒烟上传相册图")
    dele = find_def(items, "DELETE -album-{{id}}")
    check("同文档内 summary 重名自动唯一化 (2)",
          bool(det) and bool(dup) and dup["method"] == "PATCH" and det["method"] == "GET",
          [i["name"] for i in items if str(i["name"]).startswith("冒烟获取")])
    check("兜底名里的斜杠被清洗成连字符（名字必须能进 REST 路径）",
          bool(dele) and dele["name"] == "DELETE -album-{{id}}" and dele["path"] == "/album/{{id}}",
          dele and (dele["name"], dele["path"]))
    check("路径参数 {id} 转框架占位符 {{id}}",
          det and det["path"] == "/album/{{id}}", det and det["path"])
    check("说明里列出需赋值的变量（导入后一眼看出还缺什么）",
          det and "需赋值变量: id" in det["remark"], det and det["remark"])
    check("有 default/example 的参数才进骨架",
          det and det.get("params") == {"pageNum": 1}, det and det.get("params"))
    check("header 参数进请求头建议",
          det and det.get("headers") == {"x-tenant": "t-1"}, det and det.get("headers"))
    check("$ref 响应被展开成断言建议",
          det and det.get("asserts") == [["eq", "status_code", 200], ["not_null", "$.code"],
                                         ["not_null", "$.data"], ["not_null", "$.success"]],
          det and det.get("asserts"))
    check("token 类字段给出变量提取建议（递归进 data 层）",
          det and det.get("extract") == {"token": "$.data.token"}, det and det.get("extract"))
    check("无 requestBody 时不造请求体", det and det.get("body") is None, det and det.get("body"))
    check("tags 首项作为模块", det and det.get("module") == "相册管理", det and det.get("module"))
    check("无 summary/operationId 时用「方法 路径」兜底",
          dele and dele["method"] == "DELETE" and dele["path"] == "/album/{{id}}",
          dele and (dele["method"], dele["path"]))
    check("空 responses 的定义导入不报错（状态码回落 200）",
          dup and dup.get("asserts") == [["eq", "status_code", 200]], dup and dup.get("asserts"))
    check("非 JSON content-type 不猜 Content-Type（multipart 交给真实客户端）",
          upl and "Content-Type" not in (upl.get("headers") or {}) and not upl.get("body"),
          upl and (upl.get("headers"), upl.get("body")))
    check("204 无响应体的接口只给状态码断言",
          dele and dele.get("asserts") == [["eq", "status_code", 204]] and not dele.get("extract"),
          dele and (dele.get("asserts"), dele.get("extract")))
    check("导入接口标来源 openapi",
          {i["source"] for i in (det, dup, upl, dele)} == {"openapi"}, "openapi" if det else None)

    code, resp = req("POST", "/api/defs/import/openapi", {"text": SPEC2_YAML}, admin_token)
    check("Swagger2 YAML 导入成功", code == 200 and resp.get("created") == 1, f"{code} {str(resp)[:200]}")
    code, items = req("GET", "/api/defs", token=admin_token)
    o2 = find_def(items, "冒烟更新订单")
    check("basePath 前缀补全 + 路径参数占位符",
          o2 and o2["path"] == "/api/v1/orders/{{orderId}}", o2 and o2["path"])
    check("Swagger2 的 in=body schema 提炼成请求体示例（enum 取首值）",
          o2 and o2.get("body") == {"status": "PAID"}, o2 and o2.get("body"))
    check("JSON body 才配 Content-Type",
          o2 and (o2.get("headers") or {}).get("Content-Type") == "application/json",
          o2 and o2.get("headers"))
    check("只有 default 响应时状态码回落 200",
          o2 and o2.get("asserts") == [["eq", "status_code", 200], ["not_null", "$.code"]],
          o2 and o2.get("asserts"))

    code, resp = req("POST", "/api/defs/import/openapi",
                     {"text": json.dumps(SPEC3), "overwrite": False}, admin_token)
    check("重复导入同名接口默认跳过不覆盖", code == 200 and resp.get("created") == 0
          and resp.get("skipped") == 4, f"{code} {str(resp)[:200]}")

    bad_specs = [
        ("随便一句话", "既非 JSON 也非合法 spec 结构"),
        ('{"openapi": "3.0.1"}', "缺少 paths"),
        ('{"swagger": "2.0", "paths": {}}', "paths 为空"),
        ('{"paths": {"/a": {"get": {}}}}', "缺少版本字段"),
        ('{"openapi": "3.0", "paths": {"/a": {"options": {"summary": "不支持的方法"}}}}', "方法全不支持"),
        ("", "空内容"),
    ]
    for text, label in bad_specs:
        code, resp = req("POST", "/api/defs/import/openapi", {"text": text}, admin_token)
        check(f"{label} 导入被拒 400", code == 400, f"{code} {str(resp)[:160]}")

    # ==================== 7. 由定义生成用例骨架（写 Excel，权限跟着副作用走） ====================
    print("== 7. 用例骨架：先预览再落盘 ==")
    sk_ids = ["smoke_skeleton_001", "smoke_skeleton_002"]
    code, resp = req("POST", f"/api/defs/{q(REF_NAME)}/skeleton{enc}",
                     {"case_ids": sk_ids, "dry_run": True}, qa_token)
    check("dry_run 默认只回预览，不碰 Excel",
          code == 200 and resp.get("dry_run") is True and resp.get("written") == []
          and len(resp.get("cases", [])) == 2, f"{code} {str(resp)[:220]}")
    check("副本文件未因预览而增长",
          len(excel_loader.load(TMP_CASES).cases) == len(real_result.cases),
          len(excel_loader.load(TMP_CASES).cases))
    prev = resp["cases"][0]
    check("骨架带的是定义里的 方法/路径/断言/请求头",
          prev["method"] == ref["method"] and prev["url"] == ref["path"]
          and prev["asserts"] == ref["asserts"] and prev.get("headers") == ref.get("headers"),
          {k: prev.get(k) for k in ("method", "url", "asserts", "headers")})
    check("骨架的接口名称就是定义名（落地即计入覆盖）",
          prev["api_name"] == REF_NAME and prev["module"] == ref["module"],
          (prev.get("api_name"), prev.get("module")))
    check("默认描述指向该接口", "正常流程" in prev["case_desc"], prev.get("case_desc"))

    code, resp = req("POST", f"/api/defs/{q(REF_NAME)}/skeleton{enc}",
                     {"case_ids": sk_ids, "priority": "P1", "run": False, "dry_run": False}, qa_token)
    check("确认后写入 Excel 两条骨架", code == 200 and resp.get("written") == sk_ids
          and resp.get("errors") == [], f"{code} {str(resp)[:220]}")
    after = excel_loader.load(TMP_CASES)
    new_cases = [c for c in after.cases if c.case_id in sk_ids]
    check("写入的用例 loader 能原样读回（断言/请求头/URL 无损）",
          len(new_cases) == 2 and all(c.asserts == ref["asserts"] and c.api_name == REF_NAME
                                      for c in new_cases),
          [(c.case_id, c.asserts) for c in new_cases])
    check("优先级与启用标记按请求落盘",
          all(c.priority == "P1" and c.run is False for c in new_cases),
          [(c.priority, c.run) for c in new_cases])
    check("新用例已计入该接口的覆盖清单",
          REF_NAME in {str(c.api_name) for c in after.cases}, after.cases[:1])
    code, cov = req("GET", "/api/defs/" + q(REF_NAME) + enc, token=admin_token)
    check("覆盖清单实时多出刚生成的两条（接口库与 Excel 不漂移）",
          code == 200 and set(sk_ids) <= set(cov.get("used_by", [])),
          f"{code} {cov.get('used_by') if code == 200 else str(cov)[:160]}")
    code, resp = req("POST", f"/api/defs/{q(REF_NAME)}/skeleton{enc}",
                     {"case_ids": ["smoke_skeleton_001"], "dry_run": False}, qa_token)
    check("编号撞车时整批拒绝 409 且不覆盖已有用例",
          code == 409 and "已存在" in str(resp), f"{code} {str(resp)[:180]}")
    check("被拒的骨架写入未污染文件（条数仍为上一次）",
          len(excel_loader.load(TMP_CASES).cases) == len(after.cases),
          len(excel_loader.load(TMP_CASES).cases))
    code, resp = req("POST", f"/api/defs/{q(REF_NAME)}/skeleton{enc}", {"case_ids": []}, qa_token)
    check("空编号列表 400", code == 400, f"{code} {str(resp)[:140]}")
    code, resp = req("POST", f"/api/defs/{q(REF_NAME)}/skeleton{enc}",
                     {"case_ids": ["x_1", "x_1"]}, qa_token)
    check("编号自带重复 400", code == 400 and "重复" in str(resp), f"{code} {str(resp)[:140]}")
    code, resp = req("POST", "/api/defs/" + q("冒烟不存在的接口") + "/skeleton",
                     {"case_ids": ["x_2"]}, qa_token)
    check("给不存在的定义生成骨架 404", code == 404, f"{code} {str(resp)[:140]}")

    # 脏接口名只跳过该分组，不拖垮整批（往副本追加一条接口名含斜杠的用例）
    excel_writer.append_case(TMP_CASES, {
        "case_id": "smoke_badname_001", "api_name": "冒烟/斜杠接口名", "module": "冒烟",
        "case_desc": "验证聚合对脏接口名的容错", "priority": "P2", "run": False,
        "method": "GET", "url": "/smoke/badname", "asserts": [["eq", "status_code", 200]]})
    code, resp = req("POST", f"/api/defs/aggregate{enc}&overwrite=true", token=admin_token)
    check("脏接口名只跳过该分组，其余分组照常聚合",
          code == 200 and len(resp.get("rejected") or []) == 1
          and resp.get("updated") == len(api_names) and resp.get("created") == 0,
          f"{code} {str(resp)[:240]}")
    check("被拒分组点名到具体用例，便于回 Excel 修名",
          "smoke_badname_001" in str(resp.get("rejected")), resp.get("rejected"))
    check("脏定义未落库",
          not [i for i in req("GET", "/api/defs", token=admin_token)[1]
               if "斜杠接口名" in str(i["name"])])

    # 坏用例文件：聚合/骨架要 strict 拒绝，展示侧降级可用
    broken = ROOT / "data" / "~$smoke_defs_broken.xlsx"
    broken.write_bytes(b"not an xlsx at all")
    bench = "?file=" + urllib.parse.quote("data/~$smoke_defs_broken.xlsx")
    code, resp = req("POST", "/api/defs/aggregate" + bench, token=admin_token)
    check("用例文件不可读时聚合被拒 400（不基于坏数据造定义）", code == 400, f"{code} {str(resp)[:160]}")
    code, resp = req("GET", "/api/defs" + bench, token=admin_token)
    check("同样坏的文件，列表展示降级为无引用而不是 500",
          code == 200 and all(i.get("used_by") == [] for i in resp), f"{code} {str(resp)[:160]}")
    broken.unlink(missing_ok=True)

    # ==================== 8. 权限边界 ====================
    print("== 8. 权限：写走 api:manage，生成骨架走 case:write ==")
    code, perms = req("GET", "/api/auth/permissions", token=admin_token)
    check("api:manage 已在权限矩阵中并授予 admin/tl/qa",
          code == 200 and {"admin", "tl", "qa"} <= set(perms["matrix"].get("api:manage", [])),
          perms.get("matrix", {}).get("api:manage") if code == 200 else code)
    forbidden = [
        ("POST", "/api/defs", payload_a, "新建接口定义"),
        ("PUT", "/api/defs/" + q(B_NAME), {"remark": "越权改"}, "改接口定义"),
        ("DELETE", "/api/defs/" + q(B_NAME), None, "删接口定义"),
        ("POST", "/api/defs/aggregate", None, "聚合"),
        ("POST", "/api/defs/import/openapi", {"text": json.dumps(SPEC3)}, "OpenAPI 导入"),
        ("POST", f"/api/defs/{q(REF_NAME)}/skeleton", {"case_ids": ["x_3"]}, "生成用例骨架"),
    ]
    for method, path, body, label in forbidden:
        code, resp = req(method, path, body, vt)
        check(f"viewer {label} 被后端 403 拦截", code == 403, f"{code} {str(resp)[:140]}")
    code, resp = req("GET", "/api/defs", token=vt)
    check("viewer 仍可浏览接口库（只读不给写权限）", code == 200, f"{code} {str(resp)[:140]}")
    code, resp = req("POST", "/api/defs", payload_a, qa_token)
    check("qa 有 api:manage，接口库写操作放行", code == 201, f"{code} {str(resp)[:140]}")
    code, resp = req("PUT", "/api/defs/" + q(B_NAME), {"module": "冒烟模块"}, admin_token)
    check("未被引用的定义可自由换模块", code == 200 and resp.get("module") == "冒烟模块",
          f"{code} {str(resp)[:160]}")

    # ==================== 收尾：回收库内新定义 + 删副本，验证零污染 ====================
    # §5 为了验证“不动用例也能改元数据”改过引用接口的说明，开始前已存在的一律恢复原话
    if REF_NAME in saved_names:
        req("PUT", "/api/defs/" + q(REF_NAME), {"remark": saved_remark.get(REF_NAME, "")}, admin_token)
    code, items = req("GET", "/api/defs", token=admin_token)
    # 被真实用例同名的聚合定义会被引用保护拦下（§5 已验证该行为），清理时指向一个不存在的
    # 用例文件：按既定语义「读不到 = 无引用」，只回收定义本身，不动任何用例行
    free = "?file=" + urllib.parse.quote("data/~$smoke_defs_none.xlsx")
    for item in items if code == 200 else []:
        if item["name"] in saved_names:
            continue
        req("DELETE", "/api/defs/" + q(item["name"]) + free, token=admin_token)
    TMP_CASES.unlink(missing_ok=True)
    code, left = req("GET", "/api/defs", token=admin_token)
    check("冒烟期间新建的定义已全部回收（接口库回到开始前的状态）",
          code == 200 and {i["name"] for i in left} == saved_names,
          sorted({i["name"] for i in left} - saved_names) if code == 200 else left)
    check("临时用例副本已删除", not TMP_CASES.exists())
    check("真实用例文件未被冒烟波及（条数与内容原样）",
          excel_loader.load(REAL_CASES).cases == real_result.cases,
          len(excel_loader.load(REAL_CASES).cases))

    print(f"\n结果：通过 {_passed} / 失败 {_failed}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
