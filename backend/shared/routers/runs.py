"""执行批次接口：以子进程调用 main.py（零改写复用 pytest+报告编排），后台线程收集日志。

批次记录落盘 reports/runs_history.json：服务重启/热重载不再丢失平台发起的批次历史；
日志仅保留尾部 200 行，重启后仍在报告页可查。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from backend.shared import allure_store, authz
from backend.shared.schemas import RunCreate, RunOut

ROOT = Path(__file__).resolve().parents[3]  # 项目根（backend/shared/routers/ 上溯三级）

router = APIRouter(prefix="/api/runs", tags=["执行中心"])

# 内存态登记表：run_id -> 记录。持久化兜底见 _persist / _load_history。
_runs: dict[str, dict] = {}
_LOCK = threading.Lock()

_HISTORY = ROOT / "reports" / "runs_history.json"
_LOG_KEEP = 200           # 落盘仅留日志尾部，排障够用且文件可控
_PERSIST_KEYS = ("run_id", "status", "args", "started_at", "finished_at", "exit_code", "report_dir")


def _persist(rec: dict) -> None:
    """单批次记录落盘（状态变更时调用；失败只记日志不影响主流程）。"""
    try:
        _HISTORY.parent.mkdir(parents=True, exist_ok=True)
        history: dict = {}
        if _HISTORY.exists():
            try:
                with open(_HISTORY, encoding="utf-8") as fp:
                    history = json.load(fp)
            except (json.JSONDecodeError, OSError):
                history = {}
        entry = {k: rec[k] for k in _PERSIST_KEYS}
        entry["log_tail"] = rec["log"][-_LOG_KEEP:]
        history[rec["run_id"]] = entry
        # 只留最近 200 条，防无限增长
        if len(history) > 200:
            for old in sorted(history)[: len(history) - 200]:
                history.pop(old)
        tmp = _HISTORY.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(history, fp, ensure_ascii=False)
        os.replace(tmp, _HISTORY)
    except OSError:
        pass


_loaded = False


def _load_history() -> None:
    """懒加载：首次访问接口时恢复历史批次；running 残档说明进程已死，标 error。"""
    global _loaded
    _loaded = True
    if not _HISTORY.exists():
        return
    try:
        with open(_HISTORY, encoding="utf-8") as fp:
            history = json.load(fp)
    except (json.JSONDecodeError, OSError):
        return
    for rid, entry in history.items():
        if rid in _runs:
            continue
        log = entry.pop("log_tail", []) or []
        rec = {"run_id": rid, "log": log, "proc": None, **{k: entry.get(k) for k in _PERSIST_KEYS}}
        if rec["status"] == "running":
            rec["status"] = "error"
            log.append("（服务重启，批次执行线程已中断，状态不可知）")
        _runs[rid] = rec

_REPORT_DIR_RE = re.compile(r"\[interface-auto\] 报告目录:\s*(.+)$")


def _fire_notification(rec: dict) -> None:
    """批次完成后发送 Webhook 通知（失败不影响主流程）。"""
    try:
        from backend.shared.notifier import send_run_summary
        status = _status_from(rec)
        # 尝试从 allure-results 获取用例统计
        total = passed = failed = 0
        report_dir = rec.get("report_dir")
        if report_dir:
            results = ROOT / report_dir / "allure-results"
            if results.is_dir():
                cases_info = allure_store.list_cases(results)
                total = cases_info.get("total", 0)
                stats = cases_info.get("stats", {})
                passed = stats.get("passed", 0)
                failed = stats.get("failed", 0) + stats.get("broken", 0)
        # 计算耗时
        duration = ""
        if rec.get("started_at") and rec.get("finished_at"):
            from datetime import datetime as _dt
            t0 = _dt.fromisoformat(rec["started_at"])
            t1 = _dt.fromisoformat(rec["finished_at"])
            duration = f"{int((t1 - t0).total_seconds())}s"
        payload = {
            "run_id": rec["run_id"],
            "status": status,
            "total": total,
            "passed": passed,
            "failed": failed,
            "duration": duration,
            "env": (rec.get("args") or {}).get("env", "-"),
        }
        send_run_summary(payload)
    except Exception:
        pass  # 通知失败不影响主流程


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rel(path: str) -> str:
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return path


def _status_from(rec: dict) -> str:
    if rec["status"] != "running" or rec["exit_code"] is None:
        return rec["status"]
    return "passed" if rec["exit_code"] == 0 else "failed"


def _to_out(rec: dict, log_tail: int = 0) -> RunOut:
    tail = None
    if log_tail and rec.get("log"):
        tail = "\n".join(rec["log"][-log_tail:])
    return RunOut(
        run_id=rec["run_id"], status=_status_from(rec), source="api",
        args=rec["args"], started_at=rec["started_at"], finished_at=rec["finished_at"],
        exit_code=rec["exit_code"], report_dir=rec["report_dir"], log_tail=tail,
    )


def _worker(run_id: str, cmd: list[str]):
    """子进程执行 + 日志逐行收集（供前端轮询/后续升级 WebSocket）。"""
    rec = _runs[run_id]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
        )
        rec["proc"] = proc
        for line in proc.stdout:                      # 阻塞逐行读，进程结束自然退出
            line = line.rstrip()
            rec["log"].append(line)
            m = _REPORT_DIR_RE.search(line)
            if m:
                rec["report_dir"] = _rel(m.group(1).strip())
        rec["exit_code"] = proc.wait()
    except Exception as e:
        rec["log"].append(f"后端执行异常: {e!r}")
        rec["status"] = "error"
    finally:
        rec["finished_at"] = _now()
        if rec["status"] == "running":
            rec["status"] = _status_from(rec)
        _persist(rec)
        # 批次完成后触发 Webhook 通知
        _fire_notification(rec)


@router.post("", response_model=RunOut, summary="发起执行批次", status_code=202)
def create_run(body: RunCreate, _: dict = Depends(authz.require_perm("run:create"))):
    # 用例文件存在性前置校验（格式错误留给执行时报错，与 CLI 行为一致）
    f = Path(body.file)
    if not f.is_absolute():
        f = ROOT / f
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"用例文件不存在: {body.file}")

    cmd = [sys.executable, str(ROOT / "main.py"), "--file", body.file]
    if body.env:
        cmd.append(f"--env={body.env}")
    if body.module:
        cmd.append(f"--module={body.module}")
    if body.priority:
        cmd.append(f"--priority={body.priority}")
    if body.keyword:
        cmd.append(f"--keyword={body.keyword}")
    if body.cases:
        # 去重去空，保持前端传入顺序（但实际执行序以 Excel 行序为准，见 conftest）
        ids = list(dict.fromkeys(c for c in body.cases if c))
        if ids:
            cmd.append("--cases=" + ",".join(ids))
    if body.vars:
        cmd.append("--vars=" + json.dumps(body.vars, ensure_ascii=False))
    # 并行：workers 仅对 UI 项目（.json 用例）透传；接口/App 批次忽略（参数留在 args 供审计）。
    # 并行度折算与依赖降级护栏在下游：main.py 折算上限，conftest 收集期决定分片/软串行。
    if body.workers and f.suffix.lower() == ".json":
        cmd.append(f"--workers={body.workers}")

    run_id = f"api_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    rec = {
        "run_id": run_id, "status": "running", "proc": None, "log": [],
        "args": body.model_dump(exclude_none=True), "started_at": _now(),
        "finished_at": None, "exit_code": None, "report_dir": None,
    }
    with _LOCK:
        if not _loaded:
            _load_history()
        _runs[run_id] = rec
        _persist(rec)
    threading.Thread(target=_worker, args=(run_id, cmd), daemon=True).start()
    return _to_out(rec)


@router.get("", response_model=list[RunOut], summary="批次列表（API 发起 + reports/ 历史）")
def list_runs(limit: int = 20):
    with _LOCK:
        if not _loaded:
            _load_history()
    items: list[RunOut] = [_to_out(r) for r in _runs.values()]

    # 扫描本地 CLI 历史运行目录，补全（source=cli；状态从 allure-results 推断）
    reports = ROOT / "reports"
    known = {r["report_dir"] for r in _runs.values() if r["report_dir"]}
    if reports.is_dir():
        for d in sorted(reports.glob("run_*"), reverse=True):
            rel = _rel(str(d))
            if rel in known:
                continue
            ts = _dir_ts(d.name)
            results = d / "allure-results"
            status = allure_store.infer_status(results) if results.is_dir() else "unknown"
            items.append(RunOut(run_id=rel, status=status, source="cli",
                                started_at=ts, report_dir=rel))
            if len(items) >= limit:
                break

    items.sort(key=lambda x: (x.started_at or ""), reverse=True)
    return items[:limit]


@router.get("/history/{case_id}", summary="单用例跨批次执行历史（新→旧）")
def case_history(case_id: str, limit: int = 30):
    """扫描近期批次的 allure-results，抽取该用例逐次结果，供用例历史时间线。

    只返回含该用例的批次；cli 历史目录会现场推断（扫描量受 limit 约束）。
    """
    limit = max(1, min(limit, 100))
    runs = list_runs(limit=500)
    history: list[dict] = []
    for r in runs:
        if len(history) >= limit:
            break
        rel = r.report_dir or _resolve_report_dir(r.run_id)
        if not rel:
            continue
        results = (ROOT / rel) / "allure-results"
        if not results.is_dir():
            continue
        rec = allure_store.case_run_summary(results, case_id)
        if rec is None:
            continue
        history.append({
            "run_id": r.run_id, "source": r.source, "started_at": r.started_at,
            "finished_at": r.finished_at, **rec,
        })
    # 连续失败（从最新往回数，非失败即断链）
    FAILED = {"failed", "broken"}
    consec = 0
    for h in history:
        if h["status"] in FAILED:
            consec += 1
        else:
            break
    return {
        "case_id": case_id,
        "scanned_batches": min(len(runs), 500),
        "found": len(history),
        "passed": sum(1 for h in history if h["status"] == "passed"),
        "failed": sum(1 for h in history if h["status"] in FAILED),
        "consec_fail": consec,
        "history": history,
    }


@router.get("/{run_id}", response_model=RunOut, summary="批次详情（含日志尾部）")
def get_run(run_id: str):
    with _LOCK:
        if not _loaded:
            _load_history()
        rec = _runs.get(run_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"批次不存在: {run_id}")
    return _to_out(rec, log_tail=100)


@router.post("/{run_id}/cancel", summary="取消运行中的批次（终止子进程树）")
def cancel_run(run_id: str, _: dict = Depends(authz.require_perm("run:create"))):
    with _LOCK:
        if not _loaded:
            _load_history()
        rec = _runs.get(run_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"批次不存在: {run_id}")
        if rec["status"] != "running":
            return {"run_id": run_id, "status": rec["status"], "cancelling": False}
        proc = rec.get("proc")
        rec["status"] = "cancelled"
        rec["finished_at"] = _now()
        rec["log"].append("[runner] 已请求取消（终止进程树）")
        _persist(rec)
    if proc is not None and proc.poll() is None:
        # Windows：terminate 只杀 main.py 自身，pytest 孙进程需 /T 连树杀掉
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, text=True)
    return {"run_id": run_id, "status": "cancelled", "cancelling": True}


@router.get("/{run_id}/log", summary="批次完整日志（纯文本，供实时轮询）")
def get_log(run_id: str):
    with _LOCK:
        if not _loaded:
            _load_history()
        rec = _runs.get(run_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"批次不存在: {run_id}")
    return {"run_id": run_id, "status": _status_from(rec),
            "lines": len(rec["log"]), "log": "\n".join(rec["log"])}


# 历史批次 id 形式：run_YYYYMMDD_HHMMSS 或 reports/run_YYYYMMDD_HHMMSS（列表接口返回的 cli 条目）
_CLI_RUN_RE = re.compile(r"^(?:reports[/\\])?run_\d{8}_\d{6}$")


def _dir_ts(name: str) -> str:
    """run_YYYYMMDD_HHMMSS 目录名 → 可读时间；手工改名/临时目录返回空而不拖垮整个列表。"""
    try:
        return datetime.strptime(name[4:], "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


def _resolve_report_dir(run_id: str) -> str | None:
    """run_id -> 报告目录（相对项目根）。内存优先，其次认领 reports/ 历史目录。"""
    with _LOCK:
        if not _loaded:
            _load_history()
        rec = _runs.get(run_id)
    if rec:
        return rec["report_dir"]
    if _CLI_RUN_RE.match(run_id):
        return run_id if run_id.startswith("reports") else f"reports/{run_id}"
    return None


@router.get("/{run_id}/report", summary="批次报告（allure=中文单文件 / html=pytest-html）")
def get_report(run_id: str, view: str = "allure"):
    """直接返回 HTML，前端可 iframe 内嵌或 window.open。

    allure 首次访问时现场生成（allure generate + allurerc.mjs 中文单文件），之后走已生成缓存。
    """
    rel = _resolve_report_dir(run_id)
    if rel is None:
        raise HTTPException(status_code=404, detail=f"批次不存在或报告目录未生成: {run_id}")
    d = (ROOT / rel).resolve()
    if d != ROOT and ROOT not in d.parents:
        raise HTTPException(status_code=400, detail="非法报告路径")
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"报告目录不存在: {rel}")

    if view == "html":
        p = d / "report.html"
        if not p.exists():
            raise HTTPException(status_code=404, detail="pytest-html 报告不存在（运行时可能用了 --no-html）")
        return FileResponse(p, media_type="text/html")

    results, out = d / "allure-results", d / "allure-report"
    index = out / "index.html"
    if not index.exists():
        if not results.is_dir():
            fallback = d / "report.html"
            if fallback.exists():  # 无 allure 结果时退回 pytest-html，避免前端白屏
                return FileResponse(fallback, media_type="text/html")
            raise HTTPException(status_code=404, detail="allure-results 不存在，无法生成报告")
        allure_cmd = shutil.which("allure")
        if not allure_cmd:
            raise HTTPException(status_code=501, detail="未找到 allure CLI（npm install -g allure）")
        cfg = ROOT / "allurerc.mjs"
        cmd = [allure_cmd, "generate", str(results), "--output", str(out)]
        if cfg.exists():
            cmd += ["--config", str(cfg)]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180, cwd=str(ROOT))
        if proc.returncode != 0 or not index.exists():
            raise HTTPException(status_code=500, detail=f"allure generate 失败: {(proc.stderr or proc.stdout)[-400:]}")
    return FileResponse(index, media_type="text/html")


def _results_dir_or_404(run_id: str) -> Path:
    """平台明细接口共用：定位批次的 allure-results 目录。"""
    rel = _resolve_report_dir(run_id)
    if rel is None:
        raise HTTPException(status_code=404, detail=f"批次不存在或报告目录未生成: {run_id}")
    d = (ROOT / rel).resolve()
    if d != ROOT and ROOT not in d.parents:
        raise HTTPException(status_code=400, detail="非法报告路径")
    results = d / "allure-results"
    if not results.is_dir():
        raise HTTPException(status_code=404, detail="allure-results 不存在，无法提供明细")
    return results


@router.get("/{run_id}/cases", summary="批次用例结果摘要（平台原生报告页，已掩码）")
def get_run_cases(run_id: str):
    return {"run_id": run_id, **allure_store.list_cases(_results_dir_or_404(run_id))}


@router.get("/{run_id}/cases/{case_id}", summary="单用例请求/响应明细（已掩码+截断）")
def get_run_case(run_id: str, case_id: str):
    detail = allure_store.case_detail(_results_dir_or_404(run_id), case_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"批次 {run_id} 中不存在用例 {case_id}")
    return detail


_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


@router.get("/{run_id}/attachment/{filename}", summary="批次图片附件（UI 步骤截图，供报告页取图）")
def get_attachment(run_id: str, filename: str):
    """从批次 allure-results 目录取图片附件（按文件名）。

    只认纯图片文件名 + 限定目录，防路径穿越；与报告 HTML 一致走登录态（前端 <img> 携 ?token=）。
    """
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="非法附件名")
    media = _IMAGE_TYPES.get(Path(filename).suffix.lower())
    if media is None:
        raise HTTPException(status_code=400, detail="只允许图片附件")
    results = _results_dir_or_404(run_id)
    p = (results / filename).resolve()
    if p.parent != results.resolve() or not p.is_file():
        raise HTTPException(status_code=404, detail="附件不存在")
    return FileResponse(p, media_type=media)


@router.get("/{run_id}/trace/{filename}", summary="失败 trace 附件下载（zip，供本地 npx playwright show-trace 回放）")
def get_trace(run_id: str, filename: str):
    """从批次 allure-results 取 trace zip（按文件名）。

    仅失败 UI 用例产出。限定 .zip + 纯文件名 + 限定目录，防路径穿越；与截图/报告一致走登录态。
    """
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="非法 trace 文件名")
    if Path(filename).suffix.lower() != ".zip":
        raise HTTPException(status_code=400, detail="只允许 zip trace 文件")
    results = _results_dir_or_404(run_id)
    p = (results / filename).resolve()
    if p.parent != results.resolve() or not p.is_file():
        raise HTTPException(status_code=404, detail="trace 文件不存在")
    return FileResponse(p, media_type="application/zip", filename=filename)


# ==================== 报告删除 / 保留策略（需 report:delete 权限） ====================


def _remove_history(run_id: str) -> None:
    """从 runs_history.json 拽掉指定批次记录。"""
    try:
        if not _HISTORY.exists():
            return
        with open(_HISTORY, encoding="utf-8") as fp:
            history = json.load(fp)
        if run_id in history:
            history.pop(run_id)
            tmp = _HISTORY.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(history, fp, ensure_ascii=False)
            os.replace(tmp, _HISTORY)
    except (json.JSONDecodeError, OSError):
        pass


def _clear_plan_refs(run_id: str) -> None:
    """清理场景卡片对已删批次的引用，避免「查看上次报告」指向 404。"""
    try:
        from backend.shared import plan_store
        for p in plan_store.list_all():
            if p.get("last_run_id") == run_id:
                plan_store.update(p["id"], last_run_id=None, last_run_at=None)
    except Exception:
        pass  # 引用清理失败不影响删除主流程


def _delete_run_internal(run_id: str) -> dict:
    """删除单个批次：内存记录 + 历史条目 + 报告目录。抛 HTTPException 由调用方兜权限。"""
    with _LOCK:
        if not _loaded:
            _load_history()
        rec = _runs.get(run_id)
        if rec is not None and rec["status"] == "running":
            raise HTTPException(status_code=400, detail=f"批次运行中，不可删除: {run_id}")

    rel = _resolve_report_dir(run_id)
    deleted_dir = None
    if rel:
        d = (ROOT / rel).resolve()
        reports_root = (ROOT / "reports").resolve()
        # 安全护栏：只允许删 reports/ 下的批次目录，防路径穿越
        if reports_root not in d.parents or d == reports_root:
            raise HTTPException(status_code=400, detail=f"非法报告目录，拒绝删除: {rel}")
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            deleted_dir = rel

    # 既无内存记录也无可删目录 → 批次不存在（避免前端误报“删除成功”）
    if rec is None and deleted_dir is None:
        raise HTTPException(status_code=404, detail=f"批次不存在或报告目录已缺失: {run_id}")

    with _LOCK:
        _runs.pop(run_id, None)
    _remove_history(run_id)
    _clear_plan_refs(run_id)
    return {"run_id": run_id, "deleted_report_dir": deleted_dir}


@router.delete("/{run_id}", summary="删除批次报告（含目录清理）")
def delete_run(run_id: str, _: dict = Depends(authz.require_perm("report:delete"))):
    return _delete_run_internal(run_id)


@router.post("/cleanup", summary="清理旧报告：按时间保留最近 keep 个批次，其余删除")
def cleanup_runs(keep: int = 20, _: dict = Depends(authz.require_perm("report:delete"))):
    keep = max(keep, 0)
    runs = list_runs(limit=1000)
    # 只删已结束且能定位到报告目录的批次，按开始时间倒序保留前 keep 个
    finished = [r for r in runs if r.status not in ("running", "pending") and r.report_dir]
    doomed = finished[keep:]
    deleted: list[str] = []
    for r in doomed:
        try:
            _delete_run_internal(r.run_id)
            deleted.append(r.run_id)
        except HTTPException:
            continue  # 单个失败不阻断批量清理
    return {"keep": keep, "scanned": len(runs), "deleted": len(deleted), "deleted_ids": deleted}
