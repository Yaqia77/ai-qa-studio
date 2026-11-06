# -*- coding: utf-8 -*-
"""UI 用例落库闸门：与 case_gate 同构，权威校验口径换成 `ui_case_store.validate_case`。

为什么单开一个文件而不是给 case_gate 加 kind 分支：
- 接口用例的载体是 Excel（excel_loader/excel_writer 一条链），UI 用例是**一项目一 JSON**
  （ui_case_store 的 _LOCK + 原子替换），两者的"读已有编号 / 追加落盘"路径完全不同，
  混在一个函数里只会长出满屏 `if kind ==`；
- 校验口径单一真源的约束不变：这里只做"目标必须是 kind=ui 项目 + .json 文件"的
  路径裁决，字段合法性一律以 `ui_case_store.validate_case` 为准 —— 与页面「新增 UI 用例」
  和执行器读用例走的是同一套解析，杜绝"库里存得下、执行读不了"；
- 语义与接口用例 commit 对齐：**任一非法整批拒绝**（区别于 /api/ui-cases/import 的
  逐条跳过），编号冲突自动递增改名并回带映射，AI 产出绝不静默覆盖存量。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from backend.agent_bridge.case_gate import CaseGenerateBody, _bump_id
from backend.shared import project_store
from backend.ui import ui_case_store
from backend.ui.ui_case_store import UiCaseError

ROOT = Path(__file__).resolve().parents[2]


def resolve_ui_target(body: CaseGenerateBody) -> Path:
    """把 project_id / file 解析成 UI 用例 JSON 绝对路径。

    与 case_gate.resolve_target 的差异：默认文件不存在（新 UI 项目可能还没用例，
    文件由 ui_case_store 首次写入时创建），但**项目必须存在且 kind=ui**，
    显式 file 也必须是 .json —— 防把 UI 草稿灌进接口用例 Excel。
    """
    rel = (body.file or "").strip()
    if not rel and (body.project_id or "").strip():
        entry = project_store.get(body.project_id.strip())
        if entry is None:
            raise HTTPException(status_code=404, detail=f"项目不存在: {body.project_id}")
        if str(entry.get("kind") or "") != "ui":
            raise HTTPException(status_code=400,
                                detail=f"项目 {body.project_id} 不是 UI 类型（kind={entry.get('kind')}），"
                                       f"UI 用例请指定 kind=ui 项目")
        rel = str(entry.get("file") or "")
    if not rel:
        raise HTTPException(status_code=400, detail="UI 用例落库必须指定 project_id 或 file")
    path = Path(rel)
    if not path.is_absolute():
        path = ROOT / rel
    if path.suffix.lower() != ".json":
        raise HTTPException(status_code=400, detail=f"UI 用例目标必须是 .json 文件: {rel}")
    if path.is_dir():
        raise HTTPException(status_code=400, detail=f"目标是用例目录，请指定具体 json: {path.name}")
    return path


def _validate(row: dict) -> tuple[Any, list[str]]:
    """跑平台权威校验：合法返回 (清洗后 dict, [])，否则 (None, problems)。"""
    try:
        return ui_case_store.validate_case(row), []
    except UiCaseError as exc:
        return None, [str(exc)]


def _existing_ids(path: Path) -> set[str]:
    """读目标文件已有编号做判重；文件不存在（新项目）返回空集。"""
    try:
        return {str(c.get("case_id") or "").strip().lower()
                for c in ui_case_store.load_cases(path) if c.get("case_id")}
    except UiCaseError:
        return set()


def preview(cases: list[dict], *, project_id: str = "", file: str = "") -> dict:
    """逐条权威校验（不落盘），返回逐条报告 + 编号已存在提示（口径同接口用例 preview）。"""
    if not cases:
        raise HTTPException(status_code=400, detail="没有待校验的 UI 用例")
    path = resolve_ui_target(CaseGenerateBody(cases=cases, project_id=project_id, file=file))
    existing = _existing_ids(path)

    items: list[dict] = []
    passed = 0
    for raw in cases:
        _, problems = _validate(raw or {})
        cid = str((raw or {}).get("case_id") or "").strip()
        exists = bool(cid) and cid.lower() in existing
        ok = not problems
        passed += 1 if ok else 0
        item_problems = list(problems)
        if exists:
            item_problems.append(f"用例编号已存在: {cid}（落库时将自动递增改名）")
        items.append({"case_id": cid, "passed": ok, "exists": exists, "problems": item_problems})

    rel = str(path.relative_to(ROOT)).replace("\\", "/") if path.is_relative_to(ROOT) else path.name
    return {
        "platform": "interface-auto",
        "file": rel,
        "checked": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "items": items,
        "summary": f"平台权威校验 {len(cases)} 条 UI 用例，通过 {passed} 条"
                   f"（口径同 ui_case_store.validate_case，落库前请人工确认）",
    }


def commit(cases: list[dict], *, project_id: str = "", file: str = "",
           meta: dict | None = None) -> dict:
    """整批先权威校验，全合法才一次原子写；任一非法即拒绝（不落盘）。

    编号冲突自动递增改名（同 case_gate 语义），映射随 `renamed` 回带。
    写入复用 ui_case_store 的 _LOCK + _read/_write：整批追加后单次落盘，
    不会写出"半批"状态的 JSON 文件。
    """
    if not cases:
        raise HTTPException(status_code=400, detail="没有待落库的 UI 用例")
    path = resolve_ui_target(CaseGenerateBody(cases=cases, project_id=project_id, file=file))

    existing = _existing_ids(path)
    cleaned, rejected, renamed = [], [], []
    for raw in cases:
        row = dict(raw or {})
        cid = str(row.get("case_id") or "").strip()
        if cid and cid.lower() in existing:
            new_id = _bump_id(cid, existing)
            if new_id is None:
                raise HTTPException(status_code=409,
                                    detail=f"编号 {cid} 连续递增 50 次仍冲突，请人工处理后再落库")
            row["case_id"] = new_id
            renamed.append({"from": cid, "to": new_id})
            cid = new_id
        if cid:
            existing.add(cid.lower())        # 同批内部互斥：前一条占了新号，后一条接着让
        tc, problems = _validate(row)
        if problems:
            rejected.append({"case_id": str((raw or {}).get("case_id") or ""), "problems": problems})
        else:
            cleaned.append(tc)
    if rejected:
        raise HTTPException(status_code=400,
                            detail={"message": "存在校验不通过的 UI 用例，已拒绝整批落库",
                                    "rejected": rejected})

    try:
        _append_all(path, cleaned)
    except UiCaseError as exc:
        status = {"invalid": 400, "not_found": 404, "conflict": 409, "locked": 409}.get(exc.kind, 400)
        raise HTTPException(status_code=status, detail=str(exc))

    rel = str(path.relative_to(ROOT)).replace("\\", "/") if path.is_relative_to(ROOT) else path.name
    note = f"，自动改名 {len(renamed)} 条" if renamed else ""
    return {
        "platform": "interface-auto",
        "file": rel,
        "committed": len(cleaned),
        "skipped": 0,
        "errors": [],
        "renamed": renamed,
        "meta": dict(meta or {}),
        "summary": f"落库 {len(cleaned)} 条 UI 用例{note}",
    }


def _append_all(path: Path, cases: list[dict]) -> None:
    """在锁内整批追加：读全量 → append → 一次原子写（任何一条撞号都整体失败）。"""
    with ui_case_store._LOCK:
        data = ui_case_store._read(path) if path.exists() else {"version": 1, "cases": []}
        have = {str(c.get("case_id") or "").strip().lower() for c in data["cases"]}
        for c in cases:
            if str(c.get("case_id") or "").strip().lower() in have:
                raise UiCaseError("conflict", f"用例编号已存在: {c.get('case_id')}")
            data["cases"].append(c)
        ui_case_store._write(path, data)
