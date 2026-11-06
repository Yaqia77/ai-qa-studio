# -*- coding: utf-8 -*-
"""UI 自动化端到端冒烟：用例并行执行（xdist loadgroup 分组×分片 + 依赖护栏软降级）。

用法（须用装了 playwright 的 venv 解释器启动后端，再执行本脚本）：
    .venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
    .venv\Scripts\python.exe tools\smoke_ui_parallel.py

覆盖并行执行的三项新证据（既有冒烟只守串行存量，不含这三条）：
1. 并行生效：单组 6 条用例、workers=2 → 恰 2 个分片；执行日志「新启浏览器束」== 分片数(2)，
   统一回收 2 束（每 worker 一束）；同一分片的用例钉在同一 worker（loadgroup 组原子性）。
2. 重试正交：并行批次里的 flaky 用例（retry=1）仍转绿且报告标注「重试 1 次后通过」。
3. 依赖护栏：批次含 dependencies.json 依赖时软降级串行——整批钉同一 worker，依赖先行。

浏览器未就绪时执行段整体跳过（与本仓其它冒烟同一口径）。脚本只在临时 UI 项目造数据，
依赖登记与批次收尾全部删除，不影响其它项目。
"""
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
# 执行日志共享文件（与 core/log_setup 同源）：按发起前偏移量读增量窗口，跨批次不串计。
LOG_FILE = ROOT / "logs" / "interface-auto_runtime.log"
FIXTURE_URI = (ROOT / "tests" / "fixtures" / "ui_demo.html").as_uri()
ADMIN = ("admin", "admin123")

# 控制器进度行：形如 "[gw0] [ 16%] PASSED tests/test_ui.py::test_ui[par_0]@chromium|True#0"
_GW_LINE = re.compile(r"\[gw(\d+)\].*(PASSED|FAILED|ERROR).*test_ui\[([^\]]+)\]@(\S+)")

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


def wait_run_terminal(run_id, timeout=240):
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


def get_full_log(run_id) -> str:
    code, resp = req("GET", "/api/runs/" + urllib.parse.quote(run_id, safe="") + "/log", token=ADMIN_TOK)
    return (resp or {}).get("log", "") if code == 200 else ""


def log_offset() -> int:
    return LOG_FILE.stat().st_size if LOG_FILE.exists() else 0


def read_log_since(off: int, contains: str = "", tries: int = 10) -> str:
    """读增量日志；enqueue 落盘有延迟，直到含关键串或重试耗尽。"""
    text = ""
    for _ in range(tries):
        if LOG_FILE.exists():
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fp:
                fp.seek(off)
                text = fp.read()
        if not contains or contains in text:
            return text
        time.sleep(0.5)
    return text


def parse_gw_lines(log_text: str) -> dict:
    """{case_id: (gw, shard)}——只取结果行，调度先后不影响组原子性断言。"""
    out = {}
    for m in _GW_LINE.finditer(log_text):
        out[m.group(3)] = (m.group(1), m.group(4))
    return out


def main():
    global ADMIN_TOK
    print(f"目标：{BASE}")
    server, flaky_url = serve_flaky()

    code, _ = req("GET", "/api/health")
    check("健康检查可访问", code == 200, code)
    ADMIN_TOK = login(*ADMIN)

    uniq = uuid.uuid4().hex[:6]
    code, proj = req("POST", "/api/projects", {"name": f"四期冒烟_{uniq}", "kind": "ui"}, ADMIN_TOK)
    check("建 UI 项目 201", code == 201 and proj.get("file", "").endswith(".json"), f"{code} {proj}")
    pfile = proj.get("file")
    pid = proj.get("id", "")
    if not pfile:
        server.shutdown()
        return 1

    def cleanup():
        try:
            req("DELETE", "/api/projects/" + urllib.parse.quote(pid, safe=""), token=ADMIN_TOK)
        except Exception:  # noqa: BLE001
            pass
        server.shutdown()

    # 单组 6 条（同 chromium/headless）：轮转分片后 shard0=第0,2,4条、shard1=第1,3,5条；最后一条 flaky
    ids = [f"par_{i}_{uniq}" for i in range(6)]
    cases = []
    for i, cid in enumerate(ids):
        c = {"case_id": cid, "module": "四期", "name": f"并行用例{i}", "priority": "P1",
             "run": True, "browser": "chromium", "headless": True,
             "steps": [{"action": "goto", "value": FIXTURE_URI},
                       {"action": "expect_visible", "locator": "css=#title"}]}
        if i == 5:
            c["retry"] = 1
            c["steps"] = [{"action": "goto", "value": flaky_url},
                          {"action": "expect_text", "locator": "css=#msg", "value": "ready"}]
        cases.append(c)
    for c in cases:
        code, _ = req("POST", "/api/ui-cases" + qf(pfile), c, ADMIN_TOK)
        check(f"新建用例 {c['case_id']} 201", code == 201, code)

    if not browser_ready():
        cleanup()
        print(f"\n结果：通过 {_passed} / 失败 {_failed}（执行段跳过）")
        return 1 if _failed else 0

    # ==================== 批次一：workers=2 并行 + flaky 重试正交 ====================
    print("== 批次一：单组 6 条 × workers=2（期望 2 分片、2 束浏览器） ==")
    off1 = log_offset()
    code, resp = req("POST", "/api/runs", {"file": pfile, "workers": 2}, ADMIN_TOK)
    check("发起并行批次 202（workers=2 受理）", code == 202 and resp.get("run_id"), f"{code} {resp}")
    run_id = resp.get("run_id")
    if not run_id:
        cleanup()
        return 1
    check("workers 记录进批次 args（审计可见）", (resp.get("args") or {}).get("workers") == 2,
          resp.get("args"))
    final = wait_run_terminal(run_id)
    check("并行批次终态通过（含 flaky 重试转绿）", final.get("status") == "passed", f"终态={final.get('status')}")

    full_log = get_full_log(run_id)
    check("执行输出打印并行 worker 数（xdist 已启用）", "并行 worker 数: 2" in full_log)
    gw = parse_gw_lines(full_log)
    check("6 条用例结果行齐全", len(gw) == 6, sorted(gw))
    shard_of: dict = {}
    for cid, (g, shard) in gw.items():
        shard_of.setdefault(shard, set()).add((g, cid))
    check("恰为 2 个分片（单组×workers=2）", len(shard_of) == 2, list(shard_of))
    same_group_one_worker = all(len({g for g, _ in v}) == 1 for v in shard_of.values())
    check("同一分片钉在同一 worker（loadgroup 组原子性）", same_group_one_worker,
          {k: sorted(v) for k, v in shard_of.items()})
    sizes = sorted(len(v) for v in shard_of.values())
    check("轮转分片均衡（3+3）", sizes == [3, 3], sizes)

    code, listing = req("GET", "/api/runs/" + urllib.parse.quote(run_id, safe="") + "/cases", token=ADMIN_TOK)
    flaky_item = {c.get("case_id"): c for c in (listing.get("items") or [])}.get(ids[5], {})
    check("并行下重试仍正交：flaky 摘要「重试 1 次后通过」",
          "重试 1 次后通过" in (flaky_item.get("summary") or ""), flaky_item.get("summary"))

    rt = read_log_since(off1, contains="批次结束，已回收")
    launches = len(re.findall(r"新启浏览器束", rt))
    recycles = len(re.findall(r"批次结束，已回收", rt))
    check("浏览器束启动次数 == 分片数(2)，非用例数(6)", launches == 2, f"launches={launches}")
    check("每 worker 收尾各回收 1 束（合计 2）", recycles == 2, f"recycles={recycles}")

    # ==================== 批次二：依赖护栏软降级串行 ====================
    print("== 批次二：含依赖用例 × workers=2（期望软降级：整批同 worker、依赖先行） ==")
    code, _ = req("PUT", "/api/dependencies/" + urllib.parse.quote(ids[1], safe=""),
                  {"dependencies": [ids[0]]}, ADMIN_TOK)
    check("登记依赖 par_1←par_0 2xx", 200 <= code < 300, code)
    off2 = log_offset()
    code, resp = req("POST", "/api/runs",
                     {"file": pfile, "workers": 2, "cases": [ids[0], ids[1]]}, ADMIN_TOK)
    check("发起含依赖批次 202", code == 202 and resp.get("run_id"), f"{code} {resp}")
    run_id2 = resp.get("run_id")
    if run_id2:
        final2 = wait_run_terminal(run_id2)
        check("含依赖批次终态通过", final2.get("status") == "passed", f"终态={final2.get('status')}")
        log2 = get_full_log(run_id2)
        gw2 = parse_gw_lines(log2)
        check("护栏生效：整批钉同一 worker（软降级串行）",
              len(gw2) == 2 and len({g for g, _ in gw2.values()}) == 1, gw2)
        order = [m.group(3) for m in _GW_LINE.finditer(log2)]
        check("依赖先行（par_0 结果行先于 par_1）",
              order.index(ids[0]) < order.index(ids[1]) if len(order) == 2 else False, order)
        rt2 = read_log_since(off2, contains="软降级为串行", tries=6)
        check("收集期日志留「软降级为串行」护栏说明", "软降级为串行" in rt2)
        req("DELETE", "/api/runs/" + urllib.parse.quote(run_id2, safe=""), token=ADMIN_TOK)

    # ==================== 收尾与零副作用 ====================
    print("== 收尾与零副作用 ==")
    req("PUT", "/api/dependencies/" + urllib.parse.quote(ids[1], safe=""), {"dependencies": []}, ADMIN_TOK)
    code, deps = req("GET", "/api/dependencies", token=ADMIN_TOK)
    check("依赖登记已清空", code == 200 and not (deps.get(ids[1]) if isinstance(deps, dict) else None), deps)
    req("DELETE", "/api/runs/" + urllib.parse.quote(run_id, safe=""), token=ADMIN_TOK)
    code, resp = req("DELETE", "/api/projects/" + urllib.parse.quote(pid, safe=""), token=ADMIN_TOK)
    check("删除临时 UI 项目", code == 200, f"{code} {resp}")
    server.shutdown()

    print(f"\n结果：通过 {_passed} / 失败 {_failed}")
    return 1 if _failed else 0


ADMIN_TOK = None

if __name__ == "__main__":
    sys.exit(main())
