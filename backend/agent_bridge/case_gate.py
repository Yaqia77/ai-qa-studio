# -*- coding: utf-8 -*-
"""M3 生成用例落库闸门：preview（权威校验，不落盘） + commit（先校验后原子写）。

为什么要单独一层，而不是让 Agent 直接拼 Excel：
- **校验口径单一真源在平台**（设计文档 5.3）。Agent 侧闸门只做与执行引擎无关的确定性预检
  （结构/引用/编号/变量），method/断言算子/JSON 列的合法性一律以 `core.excel_loader` 为准 ——
  与执行时读文件走的是同一套 `_parse_case`，杜绝"库里存得下、执行读不了";
- **产出必先预览、人确认后才落库**（设计文档第五节硬约束）。commit 复用 `excel_writer`
  的"写临时文件→全量校验→原子替换"，任一字段非法即整批拒绝，绝不部分污染用例文件；
- **默认关闭 + 双重鉴权**：路由仅在 `agent.enabled=true` 时注册，端点内再校验一次；
  写权限 `case:write` 由后端 `authz` 兜底，Agent 服务账号只给只读 + case:write。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from backend.api import excel_writer
from backend.shared import project_store
from core import excel_loader
from core.excel_loader import ExcelFormatError

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FILE = "data/api_cases.xlsx"

# 与 backend/api/routers/cases.py 同源的落库错误映射（保持全平台一致的 HTTP 语义）
_ERROR_STATUS = {"invalid": 400, "not_found": 404, "conflict": 409, "locked": 409}


class CaseGenerateBody(BaseModel):
    """preview/commit 共用请求体：cases 为 Agent 产出的草稿（列名对齐 Excel「用例」表）。"""

    cases: list[dict] = Field(default_factory=list, description="待校验/落库的用例草稿")
    project_id: str = Field(default="", description="目标项目 id（据此解析用例文件）")
    file: str = Field(default="", description="显式目标文件（相对项目根，优先于 project_id）")


def _prepare(case: dict) -> dict:
    """补齐 append_case 同款默认值：模块/描述/优先级/是否执行。

    Agent schema 里这几项可选（补全/扩展场景常不带），落库口径须与平台"新增用例"一致，
    否则一条只是没写模块的草稿会被 Excel 硬约束误判为非法。
    """
    row = dict(case)
    row.setdefault("module", "未分类")
    row.setdefault("case_desc", "新建用例")
    row.setdefault("priority", "P2")
    row.setdefault("run", True)
    return row


def resolve_target(body: CaseGenerateBody) -> Path:
    """把 file / project_id 解析成目标用例文件绝对路径。缺省回落默认文件。"""
    rel = (body.file or "").strip()
    if not rel and (body.project_id or "").strip():
        entry = project_store.get(body.project_id.strip())
        if entry is None:
            raise HTTPException(status_code=404, detail=f"项目不存在: {body.project_id}")
        rel = str(entry.get("file") or "")
    rel = rel or DEFAULT_FILE
    path = Path(rel)
    if not path.is_absolute():
        path = ROOT / rel
    return path


def _require_file(path: Path) -> None:
    if path.is_dir():
        raise HTTPException(status_code=400, detail=f"目标是用例目录，请指定具体 xlsx: {path.name}")
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"用例文件不存在: {path.name}")


def _existing_ids(path: Path) -> set[str]:
    """读目标文件已有编号做判重；文件读不到就返回空集（不阻断字段校验）。"""
    try:
        return {str(c.case_id).strip().lower() for c in excel_loader.load(path, allow_empty=True).cases}
    except Exception:  # noqa: BLE001 —— 判重尽力而为，权威落库校验在 excel_writer 兜底
        return set()


def _validate(row: dict) -> tuple[Any, list[str]]:
    """跑平台权威校验：合法返回 (TestCase, [])，否则 (None, problems)。"""
    try:
        return excel_loader.validate_case(_prepare(row)), []
    except ExcelFormatError as exc:
        lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
        problems = [ln for ln in lines[1:]] or lines or ["用例校验未通过"]
        return None, problems


def preview(cases: list[dict], *, project_id: str = "", file: str = "") -> dict:
    """逐条权威校验（不落盘），返回逐条报告 + 编号已存在提示。"""
    if not cases:
        raise HTTPException(status_code=400, detail="没有待校验的用例")
    path = resolve_target(CaseGenerateBody(cases=cases, project_id=project_id, file=file))
    _require_file(path)
    existing = _existing_ids(path)

    items: list[dict] = []
    passed = 0
    for raw in cases:
        _, problems = _validate(raw)
        cid = str((raw or {}).get("case_id") or "").strip()
        exists = bool(cid) and cid.lower() in existing
        ok = not problems
        passed += 1 if ok else 0
        item_problems = list(problems)
        if exists:
            item_problems.append(f"用例编号已存在: {cid}（落库时将被跳过）")
        items.append({"case_id": cid, "passed": ok, "exists": exists, "problems": item_problems})

    rel = str(path.relative_to(ROOT)).replace("\\", "/") if path.is_relative_to(ROOT) else path.name
    return {
        "platform": "interface-auto",
        "file": rel,
        "checked": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "items": items,
        "summary": f"平台权威校验 {len(cases)} 条，通过 {passed} 条"
                   f"（口径同执行读取 excel_loader，落库前请人工确认）",
    }


def commit(cases: list[dict], *, project_id: str = "", file: str = "") -> dict:
    """整批先权威校验，全合法才原子落库；任一非法即拒绝（不落盘），编号已存在则跳过。"""
    if not cases:
        raise HTTPException(status_code=400, detail="没有待落库的用例")
    body = CaseGenerateBody(cases=cases, project_id=project_id, file=file)
    path = resolve_target(body)
    _require_file(path)

    # 权威闸门：字段非法 → 整批拒绝，绝不带着坏数据去写文件
    tcs, rejected = [], []
    for raw in cases:
        tc, problems = _validate(raw)
        if problems:
            rejected.append({"case_id": str((raw or {}).get("case_id") or ""), "problems": problems})
        else:
            tcs.append(tc)
    if rejected:
        raise HTTPException(status_code=400,
                            detail={"message": "存在校验不通过的用例，已拒绝整批落库", "rejected": rejected})

    try:
        summary = excel_writer.bulk_append(path, tcs)
    except excel_writer.CaseWriteError as exc:
        raise HTTPException(status_code=_ERROR_STATUS.get(exc.kind, 400), detail=str(exc))

    rel = str(path.relative_to(ROOT)).replace("\\", "/") if path.is_relative_to(ROOT) else path.name
    return {
        "platform": "interface-auto",
        "file": rel,
        "committed": summary.get("imported", 0),
        "skipped": summary.get("skipped", 0),
        "errors": summary.get("errors", []),
        "summary": f"落库 {summary.get('imported', 0)} 条，跳过（编号已存在）{summary.get('skipped', 0)} 条",
    }
