# -*- coding: utf-8 -*-
"""平台认证与权限冒烟测试（RBAC 全链路回归）。

用法：先启动后端，再执行本脚本
    python -m uvicorn backend.main:app --port 8000
    python tools/smoke_authz.py                  # 默认打 127.0.0.1:8000
    set IA_BASE=http://127.0.0.1:8899 && python tools/smoke_authz.py

覆盖：登录/登出、未登录 401、越权 403、报告删除与保留策略清理、路径穿越护栏、用户 CRUD、操作审计留痕、
角色权限矩阵可视化编辑（8.2）、全部写接口的后端权限拦截（前端隐藏 ≠ 后端放行）。
脚本只在 reports/ 下造一个 1970 年的假批次并删除它，不触碰任何真实报告，可重复执行；
第 7 节的越权探测全部用「必填字段留空 / 资源不存在 / 自依赖」构造，探测前后会比对各类数据条数，确保零副作用。
"""
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("IA_BASE", "http://127.0.0.1:8000").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PASS = {"admin": "admin123", "tl": "tl123", "qa": "qa123",
                "dev": "dev123", "viewer": "viewer123"}

# 用 cli 目录形式造一个干净的假批次（不污染 runs_history，只被 list_runs 的目录认领分支扫到）
FAKE_ID = "run_19700101_000000"
FAKE_DIR = ROOT / "reports" / FAKE_ID
FAKE_REL = f"reports\\{FAKE_ID}"      # list_runs 在 Windows 下返回反斜杠相对路径
TRAVERSAL = "reports" + chr(92) + ".." + chr(92) + "config"   # 路径穿越样本

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
    """极简 HTTP 客户端（只用标准库，避免给后端测试拉额外依赖）。"""
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


def login(username, password):
    code, resp = req("POST", "/api/auth/login", {"username": username, "password": password})
    assert code == 200, f"登录失败 {username}: {code} {resp}"
    return resp["token"], resp["user"]


def req_text(method, path, token=None):
    """取文本响应（CSV 导出类接口）。"""
    r = urllib.request.Request(BASE + path, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, resp.read().decode("utf-8-sig", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    if not FAKE_DIR.parent.is_dir():
        print("reports/ 不存在，后端可能未初始化", file=sys.stderr)
        return 2
    shutil.rmtree(FAKE_DIR, ignore_errors=True)
    (FAKE_DIR / "allure-results").mkdir(parents=True)
    (FAKE_DIR / "report.html").write_text("fake", encoding="utf-8")

    print(f"目标：{BASE}")
    print("== 1. 认证边界 ==")
    code, _ = req("GET", "/api/health")
    check("免登录白名单 /api/health 可访问", code == 200, code)
    code, resp = req("GET", "/api/runs")
    check("未登录访问业务接口 401", code == 401, f"{code} {resp}")
    code, resp = req("GET", "/api/runs", token="bogus-token")
    check("非法 token 401", code == 401, f"{code} {resp}")
    code, resp = req("POST", "/api/auth/login", {"username": "admin", "password": "wrong-pass"})
    check("错误密码 401", code == 401, f"{code} {resp}")

    print("== 2. 登录与权限矩阵 ==")
    _, viewer_user = login("viewer", DEFAULT_PASS["viewer"])
    check("viewer 权限集为空", viewer_user["permissions"] == [], viewer_user["permissions"])
    check("角色名中文回显", viewer_user["role_name"] == "只读访客", viewer_user["role_name"])
    admin_token, admin_user = login("admin", DEFAULT_PASS["admin"])
    check("admin 权限全量（含用户/审计管理）",
          {"user:manage", "report:delete", "audit:view", "audit:manage"} <= set(admin_user["permissions"]),
          admin_user["permissions"])
    # 预清理：上一轮冒烟可能残留测试账号，保证脚本可重复执行
    req("DELETE", "/api/auth/users/smoke_qa", token=admin_token)
    tl_token, tl_user = login("tl", DEFAULT_PASS["tl"])
    check("tl 可删报告", "report:delete" in tl_user["permissions"], tl_user["permissions"])
    check("tl 不可管用户", "user:manage" not in tl_user["permissions"], tl_user["permissions"])
    qa_token, qa_user = login("qa", DEFAULT_PASS["qa"])
    check("qa 不可删报告", "report:delete" not in qa_user["permissions"], qa_user["permissions"])
    vt = login("viewer", DEFAULT_PASS["viewer"])[0]
    code, resp = req("GET", "/api/runs", token=vt)
    check("viewer 可只读批次列表", code == 200 and isinstance(resp, list), f"{code} {resp}")
    code, resp = req("DELETE", "/api/runs/" + urllib.parse.quote(FAKE_REL, safe=""), token=vt)
    check("viewer 删报告被拒 403", code == 403, f"{code} {resp}")
    code, resp = req("POST", "/api/runs/cleanup?keep=9999", token=vt)
    check("viewer 清理旧报告被拒 403", code == 403, f"{code} {resp}")
    code, resp = req("GET", "/api/auth/users", token=qa_token)
    check("qa 查用户列表被拒 403", code == 403, f"{code} {resp}")

    print("== 3. 报告删除与保留策略 ==")
    code, resp = req("GET", "/api/runs?limit=1000", token=tl_token)
    check("tl 列表能看到播种的假批次", code == 200 and any(FAKE_ID in r["run_id"] for r in resp), code)
    code, resp = req("POST", "/api/runs/cleanup?keep=9999", token=tl_token)
    check("清理 keep 过大时不删任何批次", code == 200 and resp["deleted"] == 0, f"{code} {resp}")
    enc = urllib.parse.quote(FAKE_REL, safe="")
    code, resp = req("DELETE", f"/api/runs/{enc}", token=admin_token)
    check("admin 删 cli 批次成功", code == 200 and resp["deleted_report_dir"] == FAKE_REL, f"{code} {resp}")
    check("报告目录已物理删除", not FAKE_DIR.exists())
    code, resp = req("DELETE", f"/api/runs/{enc}", token=admin_token)
    check("重复删除返回 404", code == 404, f"{code} {resp}")
    code, resp = req("DELETE", "/api/runs/" + urllib.parse.quote(TRAVERSAL, safe=""), token=admin_token)
    check("路径穿越被拦截（非 200）", code != 200, f"{code} {resp}")
    check("config 目录未被误删", (ROOT / "config" / "config.yaml").exists())
    check("真实报告目录未被冒烟波及", len(list((ROOT / "reports").glob("run_20*"))) > 0)

    print("== 4. 用户 CRUD（仅 admin） ==")
    new_user = {"username": "smoke_qa", "password": "smoke123", "role": "qa"}
    code, resp = req("POST", "/api/auth/users", new_user, admin_token)
    check("admin 建用户", code == 201 and resp["role"] == "qa", f"{code} {resp}")
    code, resp = req("POST", "/api/auth/users", new_user, tl_token)
    check("tl 建用户被拒 403", code == 403, f"{code} {resp}")
    nu = login("smoke_qa", "smoke123")[1]
    check("新用户可登录", nu["username"] == "smoke_qa", nu)
    code, resp = req("PUT", "/api/auth/users/smoke_qa", {"enabled": False}, admin_token)
    check("admin 停用用户", code == 200 and resp["enabled"] is False, f"{code} {resp}")
    disabled_rejected = False
    try:
        login("smoke_qa", "smoke123")
    except AssertionError:
        disabled_rejected = True
    check("停用后禁止登录", disabled_rejected)
    code, resp = req("DELETE", "/api/auth/users/admin", token=admin_token)
    check("内置 admin 不可删", code == 400, f"{code} {resp}")
    code, resp = req("DELETE", "/api/auth/users/smoke_qa", token=admin_token)
    check("admin 删用户", code == 200, f"{code} {resp}")
    code, resp = req("POST", "/api/auth/logout", token=admin_token)
    check("登出接口可用", code == 200, f"{code} {resp}")
    code, resp = req("GET", "/api/auth/me", token=admin_token)
    check("登出后 token 失效 401", code == 401, f"{code} {resp}")

    # 后续审计用例需重新取一个有效 admin token
    admin_token, _ = login("admin", DEFAULT_PASS["admin"])

    print("== 5. 操作审计留痕 ==")
    code, resp = req("GET", "/api/audit/logs")
    check("未登录查审计 401", code == 401, f"{code} {resp}")
    code, resp = req("GET", "/api/audit/logs", token=vt)
    check("viewer 查审计被拒 403", code == 403, f"{code} {resp}")
    code, resp = req("GET", "/api/audit/logs", token=tl_token)
    check("tl 可查审计（系统管理只读粒度）", code == 200 and "items" in resp, f"{code} {resp}")

    # 造一条含敏感字段的写操作，验证留痕 + 脱敏
    code, resp = req("POST", "/api/auth/users",
                     {"username": "smoke_aud", "password": "TopSecret123", "role": "qa"}, admin_token)
    check("审计样本写操作成功", code == 201, f"{code} {resp}")
    code, resp = req("GET", "/api/audit/logs?keyword=smoke_aud&limit=50", token=admin_token)
    hits = resp.get("items", []) if code == 200 else []
    check("写操作已落审计", code == 200 and len(hits) >= 1, f"{code} {resp}")
    if hits:
        top = hits[0]
        check("审计动作名为可读中文", bool(top.get("action")) and top["action"] != "POST", top.get("action"))
        check("审计记录操作人与角色", top.get("username") == "admin" and top.get("role") == "admin", top)
        check("请求体已脱敏（明文密码不落盘）", "TopSecret123" not in json.dumps(top, ensure_ascii=False), top)
        check("脱敏后仍保留字段轮廓", "***" in (top.get("detail") or ""), top.get("detail"))
        print(f"       动作示例：{top.get('action')} | 对象：{top.get('target')} | 摘要：{top.get('detail')}")

    # 失败登录与越权尝试也必须留痕
    req("POST", "/api/auth/login", {"username": "admin", "password": "nope-not-right"})
    code, resp = req("GET", "/api/audit/logs?keyword=%E7%99%BB%E5%BD%95%E5%B0%9D%E8%AF%95&limit=20", token=admin_token)
    log_hits = resp.get("items", []) if code == 200 else []
    check("登录尝试已留痕", code == 200 and len(log_hits) >= 1, f"{code} {resp}")
    check("登录留痕不含明文密码",
          all("nope-not-right" not in json.dumps(h, ensure_ascii=False) for h in log_hits), log_hits[:1])

    code, resp = req("GET", "/api/audit/logs?only_failed=true&limit=50", token=admin_token)
    fails = resp.get("items", []) if code == 200 else []
    check("越权/未授权尝试作为失败留痕可筛", code == 200 and any(h.get("status") in (401, 403) for h in fails),
          f"{code} {[h.get('status') for h in fails][:8]}")

    code, resp = req("GET", "/api/audit/logs?username=admin&days=1&limit=20", token=admin_token)
    scoped = resp.get("items", []) if code == 200 else []
    check("按操作人 + 天数筛选生效",
          code == 200 and scoped and all(h["username"] == "admin" for h in scoped), f"{code} {len(scoped)}")

    code, body = req_text("GET", "/api/audit/export?token=" + admin_token)
    check("审计 CSV 可导出（?token= 鉴权）", code == 200 and body.startswith("时间"), f"{code} {body[:40]}")

    code, resp = req("DELETE", "/api/audit/logs?keep_days=0", token=tl_token)
    check("tl 清理审计被拒 403", code == 403, f"{code} {resp}")

    # 回收本轮自建账号：smoke_qa 已在第 4 节删除，此处只回收 smoke_aud
    code, resp = req("DELETE", "/api/auth/users/smoke_aud", token=admin_token)
    check("冒烟测试账号已回收", code == 200, f"{code} {resp}")

    print("== 6. 角色权限矩阵可视化编辑（8.2） ==")
    code, resp = req("GET", "/api/auth/permissions")
    check("未登录取权限矩阵 401", code == 401, f"{code} {resp}")
    code, resp = req("GET", "/api/auth/permissions", token=qa_token)
    check("qa 取权限矩阵被拒 403", code == 403, f"{code} {resp}")
    code, resp = req("GET", "/api/auth/permissions", token=admin_token)
    check("admin 可取权限矩阵", code == 200 and len(resp.get("catalog", [])) >= 12 and resp.get("roles"),
          f"{code} catalog={len(resp.get('catalog', []))}")
    check("权限码附中文名与说明",
          all(c.get("name") and c.get("desc") for c in resp.get("catalog", [])))
    check("防锁死护栏规则随矩阵下发", bool(resp.get("rules", {}).get("locked_role")), resp.get("rules"))
    base = dict(resp["matrix"])
    check("基线：tl 持有报告删除", "tl" in base.get("report:delete", []), base.get("report:delete"))
    check("基线：tl 不能管用户", "tl" not in base.get("user:manage", []), base.get("user:manage"))

    # 热生效实证：tl 的 token 早已签发，改矩阵后不重登就必须立刻失去 cleanup 能力
    code, resp = req("POST", "/api/runs/cleanup?keep=9999", token=tl_token)
    check("改动前 tl 可清理旧报告", code == 200, f"{code} {resp}")
    revoked = {k: list(v) for k, v in base.items()}
    revoked["report:delete"] = [r for r in revoked["report:delete"] if r != "tl"]
    code, resp = req("PUT", "/api/auth/permissions", {"matrix": revoked}, admin_token)
    check("保存矩阵：收回 tl 的报告删除",
          code == 200 and "报告删除" in "".join(resp.get("changed", [])),
          f"{code} {resp.get('changed')}")
    # 操作人（admin）自身权限未被变更，回带的权限集应与 /auth/me 完全一致，前端直接套用即可
    code, me_now = req("GET", "/api/auth/me", token=admin_token)
    check("保存接口回带的权限集与 /auth/me 一致（免重登对齐）",
          code == 200 and me_now.get("permissions") == resp.get("actor_permissions"),
          resp.get("actor_permissions"))
    code, resp = req("POST", "/api/runs/cleanup?keep=9999", token=tl_token)
    check("收回后 tl 未重新登录即 403（热生效）", code == 403, f"{code} {resp}")

    # 四类拒绝：凭空造码 / 破坏 admin 锁死 / 关键码全员为空 / 提交不完整
    forged = {k: list(v) for k, v in base.items()}
    forged["delete:everything"] = ["qa"]
    code, resp = req("PUT", "/api/auth/permissions", {"matrix": forged}, admin_token)
    check("凭空造未知权限码被拒 400", code == 400, f"{code} {resp}")
    locked_break = {k: list(v) for k, v in base.items()}
    locked_break["user:manage"] = ["tl"]
    code, resp = req("PUT", "/api/auth/permissions", {"matrix": locked_break}, admin_token)
    check("收回 admin 的用户管理被拒（防锁死）", code == 400, f"{code} {resp}")
    empty_run = {k: list(v) for k, v in base.items()}
    empty_run["run:create"] = []
    code, resp = req("PUT", "/api/auth/permissions", {"matrix": empty_run}, admin_token)
    check("触发执行权限全员清空被拒 400", code == 400, f"{code} {resp}")
    partial = {k: list(v) for k, v in base.items() if k != "case:write"}
    code, resp = req("PUT", "/api/auth/permissions", {"matrix": partial}, admin_token)
    check("提交缺码矩阵被拒 400", code == 400, f"{code} {resp}")
    bad_role = {k: list(v) for k, v in base.items()}
    bad_role["case:write"] = ["superman"]
    code, resp = req("PUT", "/api/auth/permissions", {"matrix": bad_role}, admin_token)
    check("非法角色名被拒 400", code == 400, f"{code} {resp}")

    code, resp = req("PUT", "/api/auth/permissions", {"matrix": base}, admin_token)
    check("在页面上改回原值可保存", code == 200, f"{code} {resp}")
    code, resp = req("POST", "/api/auth/permissions/reset", token=admin_token)
    check("恢复默认矩阵成功", code == 200 and "tl" in resp["matrix"].get("report:delete", []), f"{code} {resp}")
    code, resp = req("POST", "/api/runs/cleanup?keep=9999", token=tl_token)
    check("恢复后 tl 清理旧报告能力回归", code == 200, f"{code} {resp}")

    code, resp = req("GET", "/api/audit/logs?keyword=%E8%A7%92%E8%89%B2%E6%9D%83%E9%99%90&limit=50", token=admin_token)
    perm_logs = resp.get("items", []) if code == 200 else []
    check("权限变更已落审计留痕", code == 200 and len(perm_logs) >= 2, f"{code} {len(perm_logs)}")
    if perm_logs:
        check("审计可回溯到具体权限动作", any("权限" in (h.get("action") or "") for h in perm_logs),
              perm_logs[0].get("action"))

    print("== 7. 写接口后端权限拦截（前端隐藏 ≠ 后端放行） ==")
    # 探测体全部构造为「必定失败」：必填字段留空 → 422，资源不存在 → 404，自依赖 → 400。
    # FastAPI 先跑依赖再校 body，故守卫缺失时返回的是业务错误码而非 403，能直接坐实缺口；
    # 同时即使某个接口忘了加守卫，这些请求也不会真的写坏数据。
    probes = [
        ("POST", "/api/cases", {"api_name": None}, "新增用例", "case:write"),
        ("POST", "/api/cases/import", None, "导入用例", "case:write"),
        ("PUT", "/api/cases/IA_SMOKE_NO_SUCH", {"priority": "P0"}, "更新用例", "case:write"),
        ("DELETE", "/api/cases/IA_SMOKE_NO_SUCH", None, "删除用例", "case:delete"),
        ("PUT", "/api/dependencies/IA_SMOKE_SELF", {"dependencies": ["IA_SMOKE_SELF"]},
         "设置用例依赖", "case:write"),
        ("POST", "/api/plans", {"name": None}, "新建场景", "plan:manage"),
        ("PUT", "/api/plans/ia_smoke_no_such", {"name": "x"}, "更新场景", "plan:manage"),
        ("DELETE", "/api/plans/ia_smoke_no_such", None, "删除场景", "plan:manage"),
        ("POST", "/api/secrets", {"name": None}, "新建凭证", "secret:manage"),
        ("PUT", "/api/secrets/ia_smoke_no_such", {"value": "x"}, "更新凭证", "secret:manage"),
        ("DELETE", "/api/secrets/ia_smoke_no_such", None, "删除凭证", "secret:manage"),
        ("PUT", "/api/envs", {"default_env": None}, "保存环境配置", "env:manage"),
        ("POST", "/api/webhooks", {"name": None}, "新建 Webhook", "webhook:manage"),
        ("PUT", "/api/webhooks/ia_smoke_no_such", {"name": "x"}, "更新 Webhook", "webhook:manage"),
        ("DELETE", "/api/webhooks/ia_smoke_no_such", None, "删除 Webhook", "webhook:manage"),
        ("POST", "/api/webhooks/ia_smoke_no_such/test", None, "测试 Webhook", "webhook:manage"),
        ("POST", "/api/schedules", {"name": None}, "新建定时任务", "schedule:manage"),
        ("PUT", "/api/schedules/ia_smoke_no_such", {"name": "x"}, "更新定时任务", "schedule:manage"),
        ("DELETE", "/api/schedules/ia_smoke_no_such", None, "删除定时任务", "schedule:manage"),
        ("POST", "/api/scripts", {"name": None}, "新建公共脚本", "script:manage"),
        ("PUT", "/api/scripts/ia_smoke_no_such", {"remark": "x"}, "更新公共脚本", "script:manage"),
        ("DELETE", "/api/scripts/ia_smoke_no_such", None, "删除公共脚本", "script:manage"),
        ("POST", "/api/defs", {"name": None}, "新建接口定义", "api:manage"),
        ("PUT", "/api/defs/ia_smoke_no_such", {"remark": "x"}, "更新接口定义", "api:manage"),
        ("DELETE", "/api/defs/ia_smoke_no_such", None, "删除接口定义", "api:manage"),
        # 聚合必须指向读不到的用例文件：404 落在 upsert 之前，才会一次写入都没有
        ("POST", "/api/defs/aggregate?file=data/~$no_such.xlsx", None, "聚合接口定义", "api:manage"),
        ("POST", "/api/defs/import/openapi", {"text": ""}, "导入 OpenAPI 文档", "api:manage"),
        ("POST", "/api/defs/ia_smoke_no_such/skeleton", {"case_ids": []}, "生成用例骨架", "case:write"),
        ("POST", "/api/runs/ia_smoke_no_such/cancel", None, "取消批次", "run:create"),
    ]

    def snapshot() -> dict:
        """各类业务数据条数快照，用于证明一轮探测零副作用。"""
        snap: dict = {}
        code, page = req("GET", "/api/cases", token=admin_token)
        snap["用例"] = page.get("total") if code == 200 else None
        for key, path in (("场景", "/api/plans"), ("凭证", "/api/secrets"),
                          ("Webhook", "/api/webhooks"), ("定时任务", "/api/schedules"),
                          ("公共脚本", "/api/scripts"), ("接口定义", "/api/defs")):
            code, resp = req("GET", path, token=admin_token)
            if code != 200:
                snap[key] = None
            elif isinstance(resp, list):
                snap[key] = len(resp)
            else:  # 凭证接口是 {"exists": .., "items": [..]} 包装
                snap[key] = len(resp.get("items") or [])
        code, resp = req("GET", "/api/envs", token=admin_token)
        snap["环境"] = (resp.get("default_env"), len(resp.get("envs") or [])) if code == 200 else None
        return snap

    before = snapshot()
    check("探测前数据快照可读", all(v is not None for v in before.values()), before)
    matrix_now = req("GET", "/api/auth/permissions", token=admin_token)[1]["matrix"]
    dev_token, dev_user = login("dev", DEFAULT_PASS["dev"])
    check("dev 仅持有触发执行权限", dev_user["permissions"] == ["run:create"], dev_user["permissions"])
    tokens = {"admin": admin_token, "tl": tl_token, "qa": qa_token, "dev": dev_token, "viewer": vt}

    miss = []
    for method, path, body, label, _perm in probes:
        code, resp = req(method, path, body, vt)
        if code != 403:
            miss.append(f"{label}→{code}")
    check(f"viewer 的 {len(probes)} 个写探测全部被后端 403 拦截", not miss, miss)

    # 持有对应权限码的角色（取矩阵里首个非 admin，更贴近真实使用路径）必须能越过权限层，
    # 但因探测体故意非法，不能拿到 2xx（既证明不是「一律放行」，也证明没有写成功）
    leak = []
    for method, path, body, label, perm in probes:
        holder = next((r for r in matrix_now.get(perm, []) if r in tokens and r != "admin"), "admin")
        code, resp = req(method, path, body, tokens[holder])
        if code == 403 or 200 <= code < 300:
            leak.append(f"{label}({holder})→{code}")
    check("持权角色均越过权限层且未写成功（按权限码分治而非一刀切）", not leak, leak)

    code, resp = req("PUT", "/api/cases/IA_SMOKE_NO_SUCH", {"priority": "P0"}, qa_token)
    check("qa 改用例已越过权限层", code != 403, f"{code} {resp}")
    code, resp = req("DELETE", "/api/cases/IA_SMOKE_NO_SUCH", token=qa_token)
    check("qa 删用例仍被拦 403（case:delete 需 TL+）", code == 403, f"{code} {resp}")
    code, resp = req("PUT", "/api/cases/IA_SMOKE_NO_SUCH", {"priority": "P0"}, dev_token)
    check("dev 改用例被拦 403（低权限角色不误放行）", code == 403, f"{code} {resp}")
    code, resp = req("POST", "/api/runs/ia_smoke_no_such/cancel", token=dev_token)
    check("dev 取消批次落到业务 404（已穿过权限层）", code == 404, f"{code} {resp}")

    after = snapshot()
    check("一轮探测零副作用（用例/场景/凭证/Webhook/定时/脚本/接口/环境均未变）",
          before == after, {k: (before[k], after[k]) for k in before if before[k] != after[k]})

    code, resp = req("GET",
                     "/api/audit/logs?only_failed=true&username=viewer&limit=500", token=admin_token)
    hits = resp.get("items", []) if code == 200 else []
    check("viewer 的越权写尝试整体落审计（失败记录 ≥ 探测次数）",
          code == 200 and len(hits) >= len(probes), f"{len(hits)} 条 / 探测 {len(probes)} 次")
    check("审计动作名可直接读（取到路由中文 summary）",
          all(h.get("action") and h["action"] != h["method"] for h in hits),
          [h.get("action") for h in hits[:3]])

    # ---- 收尾：删除假批次目录（不碰任何真实报告） ----
    shutil.rmtree(FAKE_DIR, ignore_errors=True)
    print(f"\n结果：通过 {_passed} / 失败 {_failed}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
