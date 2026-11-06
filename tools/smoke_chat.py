# -*- coding: utf-8 -*-
"""对话式全链路在线冒烟：对话(SSE) → 文档登记 → 落库/让号 → 用例管理回读 → 清理。

与 smoke_full.py（平台主链路）互补，这里只验跨服务链路是否真通：

    # 1) 平台开桥接并启动：config.yaml 置 agent.enabled=true
    python -m uvicorn backend.main:app --port 8000
    # 2) Agent 启动（注入平台服务账号密码与网关 Key）
    set QA_AGENT_PLATFORM_PASSWORD=*** && set QA_AGENT_GATEWAY_API_KEY=***
    python main.py                       # qa-agent 仓库，端口 8100
    # 3) 跑本脚本
    set IA_SVC_PASSWORD=***            # agent_svc 密码（tools/init_agent_svc.py 所设）
    python tools/smoke_chat.py

覆盖判定（fake provider 环境，即"无真实模型"下的最大可验面）：
  A. doc 分支在线：OpenAPI 附件透传登记（api_def.write → /api/defs/import/openapi 真通），
     产出草稿；同时锁自动落库红线——降级(fake)产出绝不出现 registered 事件；
  B. 过检产物的落库链路：以 agent_svc 走 preview→commit（meta 审计关联）→ 用例管理回读；
     重复编号再 commit 验证平台让号（renamed 回带，绝不静默覆盖）；
  C. 审计留痕可按 agent_svc + meta 关键字检索；
  D. 零副作用收尾：用例/接口定义/临时项目/会话全部回收。

真实模型在线时（provider=openai_compat），A 段会出现 registered 事件，本脚本的
"registered 恒为 0"断言仅对 fake 环境成立——脚本先探 /health 的 degraded 标记再决定断言。
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("IA_BASE", "http://127.0.0.1:8000").rstrip("/")
AGENT = os.environ.get("IA_AGENT_BASE", "http://127.0.0.1:8100").rstrip("/")
AGENT_KEY = os.environ.get("QA_AGENT_GATEWAY_API_KEY", "smoke-key-2026")
SVC_PASSWORD = os.environ.get("IA_SVC_PASSWORD", "")
ADMIN = ("admin", "admin123")

_passed = _failed = 0
_cleanup = []   # (说明, callable)，main 的 finally 里倒序尽力执行


def check(name, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [OK] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name} {extra}")


def req(method, url, body=None, token=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200]}
    except urllib.error.URLError as e:
        print(f"服务不可达：{url}（{e.reason}）\n请先按脚本头注释启动平台与 Agent", file=sys.stderr)
        sys.exit(2)


def login(username, password):
    code, resp = req("POST", f"{BASE}/api/auth/login", {"username": username, "password": password})
    assert code == 200, f"登录失败 {username}: {code} {resp}"
    return resp["token"]


def stream_chat(body):
    """POST /v1/chat/stream 并逐帧解析 SSE（event:/data: 双行 + 空行分帧）。"""
    r = urllib.request.Request(AGENT + "/v1/chat/stream",
                               data=json.dumps(body).encode("utf-8"), method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("X-Agent-Key", AGENT_KEY)
    events = []
    with urllib.request.urlopen(r) as resp:
        sid = resp.headers.get("X-Session-Id", "")
        etype, datas = "", []
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                datas.append(line[5:].strip())
            elif not line and etype:
                events.append((etype, json.loads("\n".join(datas) or "{}")))
                etype, datas = "", []
    return sid, events


def ev_of(events, t):
    return [p for (et, p) in events if et == t]


def main():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not SVC_PASSWORD:
        print("需设置 IA_SVC_PASSWORD（agent_svc 服务账号密码），否则无法验审计与落库主体", file=sys.stderr)
        return 2
    uniq = uuid.uuid4().hex[:6]
    admin = login(*ADMIN)

    print("== 0. 服务与装配探活 ==")
    code, h = req("GET", AGENT + "/health")
    check("Agent /health 可达且已装配 doc_generate",
          code == 200 and "doc_generate" in (h.get("skills") or []) and "case_generate" in (h.get("skills") or []),
          f"{code} {h.get('skills')}")
    degraded_agent = bool(h.get("degraded"))   # fake=True：红线断言据此分叉
    code, cfg = req("GET", f"{BASE}/api/config", token=admin)
    if code == 200 and not (cfg.get("agent") or {}).get("enabled"):
        print("平台 agent.enabled=false：请先在 config/config.yaml 打开后重启平台", file=sys.stderr)
        return 2

    print("== 1. 临时项目 ==")
    code, proj = req("POST", f"{BASE}/api/projects",
                     {"name": f"冒烟对话_{uniq}", "desc": "smoke_chat"}, admin)
    check("建临时项目", code == 201 and proj.get("file"), f"{code} {proj}")
    pid, pfile = proj["id"], proj["file"]
    base_proj = len(req("GET", f"{BASE}/api/projects", token=admin)[1]) - 1

    print("== 2. 对话 doc 分支（SSE 全事件流） ==")
    iface = f"冒烟对话接口{uniq}"
    spec = {"openapi": "3.0.0", "info": {"title": "smoke", "version": "1"},
            "paths": {f"/demo/ping_{uniq}": {"get": {"operationId": iface, "responses": {"200": {}}}}}}
    sid, events = stream_chat({
        "message": "按附件接口文档一键生成用例", "platform": "interface-auto",
        "project_id": pid, "auto_register": "auto",
        "attachments": [{"name": f"smoke_{uniq}.json", "content": json.dumps(spec, ensure_ascii=False)}]})
    ts = [et for (et, _) in events]
    check("事件流以 done 收尾且回带会话号", ts and ts[-1] == "done", ts[-3:])
    check("第 0 步文档登记成功", any(p.get("seq") == 0 and p.get("ok")
                                    for p in ev_of(events, "step_finish")))
    joined = "".join(p.get("text", "") for p in ev_of(events, "token"))
    check("token 通报登记了 1 个接口", "登记 1 个接口" in joined, joined[:80])
    check("逐接口生成有草稿事件", len(ev_of(events, "case_ready")) >= 1, len(ts))
    check("画布 plan 事件已推送", len(ev_of(events, "plan")) >= 1)
    regs = ev_of(events, "registered")
    if degraded_agent:
        check("红线：降级(fake)产出不出现 registered", not regs, regs)
    else:
        check("真实模型下 auto 应自动落库并回带 renamed 键",
              regs and "renamed" in regs[0], regs)
    check("无错误事件", "error" not in ts, [p for p in ev_of(events, "error")])

    print("== 3. 文档登记的接口定义可在平台回读（api_def.write 真通） ==")
    code, d = req("GET", f"{BASE}/api/defs/" + urllib.parse.quote(iface, safe=""), token=admin)
    check("接口定义已入库且路径同源", code == 200 and d.get("path") == f"/demo/ping_{uniq}",
          f"{code} {d}")
    _cleanup.append(("接口定义", lambda: req("DELETE", f"{BASE}/api/defs/" +
                  urllib.parse.quote(iface, safe=""), token=admin)))

    print("== 4. agent_svc 落库链路：preview→commit（meta 审计）+ 重复编号平台让号 ==")
    svc = login("agent_svc", SVC_PASSWORD)
    draft = {"case_id": f"SM{uniq}-01", "module": "冒烟对话", "api_name": iface,
             "case_desc": "冒烟对话落库", "priority": "P2", "run": "Y",
             "method": "GET", "url": f"/demo/ping_{uniq}",
             "asserts": json.dumps([["eq", "status_code", 200]])}
    code, rep = req("POST", f"{BASE}/api/agent/cases/preview",
                    {"cases": [draft], "project_id": pid}, svc)
    check("agent_svc preview 过检", code == 200 and rep.get("passed") == 1, f"{code} {rep}")
    meta = {"origin": "chat", "trace_id": f"smoke-{uniq}"}
    code, c1 = req("POST", f"{BASE}/api/agent/cases/commit",
                   {"cases": [draft], "project_id": pid, "meta": meta}, svc)
    check("commit 落库 1 条且无让号", code == 200 and c1.get("committed") == 1
          and c1.get("renamed") == [], f"{code} {c1}")
    check("commit 回显 meta（审计关联）", c1.get("meta") == meta, c1.get("meta"))
    code, c2 = req("POST", f"{BASE}/api/agent/cases/commit",
                   {"cases": [draft], "project_id": pid, "meta": meta}, svc)
    check("重复编号自动让号 01→02（不静默覆盖）",
          code == 200 and c2.get("committed") == 1
          and c2.get("renamed") == [{"from": f"SM{uniq}-01", "to": f"SM{uniq}-02"}], f"{code} {c2}")

    print("== 5. 用例管理回读 + 审计留痕 ==")
    code, lst = req("GET", f"{BASE}/api/cases?" +
                    urllib.parse.urlencode({"file": pfile, "keyword": f"SM{uniq}"}), token=admin)
    check("用例管理可回读 2 条（原号+让号）", code == 200 and lst.get("total") == 2, f"{code} {lst}")
    code, audit = req("GET", f"{BASE}/api/audit/logs?" +
                      urllib.parse.urlencode({"username": "agent_svc", "keyword": uniq}), token=admin)
    logs = audit.get("items") or audit.get("logs") or []   # 路由包壳键是 items
    check("审计可检 agent_svc 的 chat 落库记录（meta 进体）",
          code == 200 and any("chat" in json.dumps(r, ensure_ascii=False) for r in logs),
          f"{code} n={len(logs)}")

    print("== 6. 收尾：会话/用例/定义/项目全部回收 ==")
    req("DELETE", f"{AGENT}/v1/chat/sessions/" + urllib.parse.quote(sid or "x", safe=""),
        headers={"X-Agent-Key": AGENT_KEY})
    for cid in (f"SM{uniq}-01", f"SM{uniq}-02"):
        req("DELETE", f"{BASE}/api/cases/" + urllib.parse.quote(cid, safe="") + "?" +
            urllib.parse.urlencode({"file": pfile}), token=admin)
    for label, fn in reversed(_cleanup):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"  [WARN] 清理 {label} 失败：{e}")
    code, resp = req("DELETE", f"{BASE}/api/projects/" + urllib.parse.quote(pid, safe=""), token=admin)
    check("删除临时项目", code == 200, f"{code} {resp}")
    after = len(req("GET", f"{BASE}/api/projects", token=admin)[1])
    check("项目数回到基线（零副作用）", after == base_proj, f"{base_proj} -> {after}")
    code, d = req("GET", f"{BASE}/api/defs/" + urllib.parse.quote(iface, safe=""), token=admin)
    check("冒烟接口定义已回收", code == 404, code)

    print(f"\n结果：通过 {_passed} / 失败 {_failed}")
    return 1 if _failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for label, fn in reversed(_cleanup):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
