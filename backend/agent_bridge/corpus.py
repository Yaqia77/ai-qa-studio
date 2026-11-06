# -*- coding: utf-8 -*-
"""知识库语料导出：把接口定义 / 存量用例转成统一记录形状（设计文档 4.2 / 4.3）。

当前导出三域：
- **D1 契约域**：来自 `data/api_defs.json`（`api_def_store.list_defs`），一个接口一块；
- **D2 用例域**：来自各接口项目的 Excel（`core.excel_loader`），一条用例一块；
- **D2' UI 用例域**：来自各 UI 项目的 JSON（`backend.ui.ui_case_store`），一条用例一块，
  供 Agent 生成 UI 用例时做风格样板与变量池（步骤序列渲染成中文动作句，检索召回友好）。

为什么在这里拼自然语言正文，而不是直接把结构化 JSON 丢给 Agent：
知识库是被检索、被塞进 prompt 的，`{"op":"eq","path":"status_code"}` 这种写法
命中不了"状态码""断言"这类中文检索词，也教不会模型该怎么写。这里按平台既有的
「断言中文语义化」约定把它渲染成人/模型都读得懂的句子，检索召回和 few-shot 质量同时受益。
真正的切块与向量化在 Agent 侧做，这里只保证「一条记录 = 一个完整语义单元 + 可回跳来源」。

增量口径：每条记录带 `updated_at`，Agent 传 `since`（上次水位）时只返回更新的记录。
- D1 用定义自身的 `updated_at`；
- D2 的 Excel 没有行级时间戳，用文件 mtime —— 文件没动过就整批不返回，天然省算力。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from backend.api import api_def_store
from core import excel_loader

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = "interface-auto"

# 平台能导出的域；其余域（执行/业务/规范）结构预留，导出为空但明确标注 unsupported
EXPORTABLE_DOMAINS = ("contract", "case", "ui_case")
ALL_DOMAINS = ("contract", "case", "ui_case", "execution", "business", "convention")

# 断言操作符中文语义（与用例编辑页口径一致，供语料正文复用）
_ASSERT_DESC = {
    "eq": "等于", "neq": "不等于", "contains": "包含", "gt": "大于", "ge": "大于等于",
    "lt": "小于", "le": "小于等于", "regex": "正则匹配", "not_null": "不为空", "len_eq": "长度等于",
}

# 正文里请求体/参数的最大字符数：过大既撑爆块又淹没检索，截断并标注
_MAX_JSON_CHARS = 1200


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _dumps(value) -> str:
    """把 dict/list 压成单行紧凑 JSON；裸字符串原样返回；空值返回空串。"""
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > _MAX_JSON_CHARS:
        text = text[:_MAX_JSON_CHARS] + "…(已截断)"
    return text


def _render_headers(kv) -> str:
    if not isinstance(kv, dict) or not kv:
        return ""
    # 值本就是 {{var}} 引用或静态头，无明文凭证；只保留 key=值，压缩成一行
    return "; ".join(f"{k}={v}" for k, v in kv.items())


def _render_asserts(rules) -> list[str]:
    out: list[str] = []
    for rule in (rules or []):
        if not isinstance(rule, (list, tuple)) or len(rule) < 2:
            continue   # 至少要有 op + path，单元素/空规则是脏数据，跳过不渲染成残句
        op = str(rule[0]).strip().lower()
        path = str(rule[1]).strip()
        desc = _ASSERT_DESC.get(op, op)
        if op == "not_null":
            out.append(f"断言 {path} 不为空")
        elif len(rule) > 2:
            expected = rule[2]
            exp_text = expected if isinstance(expected, str) else _dumps(expected)
            out.append(f"断言 {path} {desc} {exp_text}")
        else:
            out.append(f"断言 {path} {desc}")
    return out


def _kb_id(*parts: str) -> str:
    """稳定主键：同一来源两次导出得到同一个 kb_id，Agent 侧才能按 hash 做增量、按 id 去重。"""
    joined = "|".join(str(p).strip() for p in parts if str(p).strip())
    if not joined:
        return ""
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]
    return f"kb_{digest}"


def contract_records(base_url: str, since: str = "") -> list[dict]:
    """D1 契约域：一个接口定义一块。"""
    records: list[dict] = []
    for d in api_def_store.list_defs():
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        updated = str(d.get("updated_at") or "")
        if since and updated <= since:
            continue
        body = _dumps(d.get("body"))
        params = _dumps(d.get("params"))
        headers = _render_headers(d.get("headers"))
        extract = _dumps(d.get("extract"))
        lines = [
            f"模块：{d.get('module') or '未分类'}",
            f"请求：{d.get('method') or ''} {d.get('path') or ''}",
        ]
        remark = str(d.get("remark") or "").strip()
        if remark:
            lines.append(f"说明：{remark}")
        if headers:
            lines.append(f"请求头：{headers}")
        if params:
            lines.append(f"查询参数：{params}")
        if body:
            lines.append(f"请求体示例：{body}")
        if extract:
            lines.append(f"变量提取：{extract}")
        asserts = _render_asserts(d.get("asserts"))
        if asserts:
            lines.append("推荐断言：\n  - " + "\n  - ".join(asserts))
        records.append({
            "kb_id": _kb_id("contract", name),
            "domain": "contract",
            "platform": PLATFORM,
            "project_id": "",
            "source_type": "api_def",
            "source_uri": f"{base_url}/api/defs/{quote(name, safe='')}",
            "title": name,
            "text": "\n".join(lines),
            "tags": [t for t in [str(d.get("module") or ""), str(d.get("method") or "")] if t],
            "version": 1,
            "updated_at": updated,
        })
    return records


def _interface_projects(project_id: str = "") -> list[dict]:
    """挑出接口类项目（Excel 载体）；UI/App（.json/.apjson）不属于 D2 用例域。"""
    from backend.shared import project_store  # 延迟导入避开注册期循环

    projects = project_store.list_projects()
    if project_id:
        projects = [p for p in projects if str(p.get("id")) == project_id]
    out = []
    for p in projects:
        rel = str(p.get("file") or "")
        if not rel or rel.lower().endswith((".json", ".apjson")):
            continue
        out.append(p)
    return out


def case_records(base_url: str, since: str = "", *, project_id: str = "",
                 file: str = "") -> list[dict]:
    """D2 用例域：一条存量用例一块，正文带请求要点与中文语义化断言。"""
    if file:
        candidates = [{"id": "", "name": "", "file": file}]
    else:
        candidates = _interface_projects(project_id)

    records: list[dict] = []
    for p in candidates:
        rel = str(p.get("file") or "")
        pid = str(p.get("id") or "")
        path = Path(rel)
        if not path.is_absolute():
            path = ROOT / rel
        if not path.is_file():
            continue
        updated = _iso(path.stat().st_mtime)
        if since and updated <= since:
            continue
        try:
            result = excel_loader.load(path, allow_empty=True)
        except Exception:  # noqa: BLE001 —— 单个坏文件不拖垮整域导出（记 0 条，不抛）
            continue
        for c in result.cases:
            case_id = str(c.case_id or "").strip()
            if not case_id:
                continue
            lines = [
                f"模块：{c.module or '未分类'}    优先级：{c.priority or ''}",
                f"接口：{c.api_name or ''}",
                f"描述：{c.case_desc or ''}",
                f"请求：{c.method or ''} {c.url or ''}",
            ]
            headers = _render_headers(c.headers)
            if headers:
                lines.append(f"请求头：{headers}")
            params = _dumps(c.params)
            if params:
                lines.append(f"查询参数：{params}")
            body = _dumps(c.body)
            if body:
                lines.append(f"请求体：{body}")
            extract = _dumps(c.extract)
            if extract:
                lines.append(f"变量提取：{extract}")
            asserts = _render_asserts(c.asserts)
            if asserts:
                lines.append("断言：\n  - " + "\n  - ".join(asserts))
            scripts = []
            if c.pre_scripts:
                scripts.append("前置脚本=" + ",".join(str(s) for s in c.pre_scripts))
            if c.post_scripts:
                scripts.append("后置脚本=" + ",".join(str(s) for s in c.post_scripts))
            if scripts:
                lines.append("脚本：" + "; ".join(scripts))
            records.append({
                "kb_id": _kb_id("case", pid or rel, case_id),
                "domain": "case",
                "platform": PLATFORM,
                "project_id": pid,
                "source_type": "excel_case",
                "source_uri": f"{base_url}/api/cases/{quote(case_id, safe='')}"
                              f"?file={quote(rel, safe='')}",
                "title": f"{case_id} {c.case_desc or c.api_name or ''}".strip(),
                "text": "\n".join(lines),
                "tags": [t for t in [str(c.module or ""), str(c.priority or "")] if t],
                "version": 1,
                "updated_at": updated,
            })
    return records


def _ui_projects(project_id: str = "") -> list[dict]:
    """挑出 UI 类项目（.json 载体）；接口/App 项目不属 UI 用例域。"""
    from backend.shared import project_store  # 延迟导入避开注册期循环

    projects = project_store.list_projects()
    if project_id:
        projects = [p for p in projects if str(p.get("id")) == project_id]
    out = []
    for p in projects:
        rel = str(p.get("file") or "")
        if str(p.get("kind") or "") != "ui" or not rel.lower().endswith(".json"):
            continue
        out.append(p)
    return out


def _render_ui_step(step: dict) -> str:
    """单步 → 中文动作句：动作中文名来自 ui_case_store.ACTIONS（单一口径）。"""
    from backend.ui import ui_case_store  # 延迟导入：corpus 模块不在启动路径上拉 Playwright 依赖链

    action = str(step.get("action") or "").strip()
    meta = ui_case_store.ACTIONS.get(action) or {}
    desc = str(meta.get("desc") or action)
    bits = [desc]
    locator = str(step.get("locator") or "").strip()
    if locator:
        bits.append(f"定位器 {locator}")
    value = step.get("value")
    if value not in (None, "", [], {}):
        bits.append(f"取值 {value}")
    step_desc = str(step.get("desc") or "").strip()
    if step_desc:
        bits.append(step_desc)
    return "：".join(bits[:1] + ["，".join(bits[1:])]) if len(bits) > 1 else bits[0]


def ui_case_records(base_url: str, since: str = "", *, project_id: str = "",
                    file: str = "") -> list[dict]:
    """UI 用例域：一条存量 UI 用例一块，正文带中文步骤序列与提取变量。"""
    from backend.ui import ui_case_store

    if file:
        candidates = [{"id": "", "name": "", "file": file}]
    else:
        candidates = _ui_projects(project_id)

    records: list[dict] = []
    for p in candidates:
        rel = str(p.get("file") or "")
        pid = str(p.get("id") or "")
        path = Path(rel)
        if not path.is_absolute():
            path = ROOT / rel
        if not path.is_file():
            continue
        updated = _iso(path.stat().st_mtime)
        if since and updated <= since:
            continue
        try:
            cases = ui_case_store.load_cases(path)
        except Exception:  # noqa: BLE001 —— 单个坏文件不拖垮整域导出
            continue
        for c in cases:
            case_id = str(c.get("case_id") or "").strip()
            if not case_id:
                continue
            lines = [
                f"模块：{c.get('module') or '未分类'}    优先级：{c.get('priority') or ''}",
                f"用例名：{c.get('name') or ''}",
                f"描述：{c.get('desc') or ''}",
                f"浏览器：{c.get('browser') or 'chromium'}（无头={c.get('headless', True)}）"
                + (f"，登录态会话：{c['session']}" if c.get("session") else ""),
            ]
            steps = [_render_ui_step(s) for s in (c.get("steps") or []) if isinstance(s, dict)]
            if steps:
                lines.append("步骤序列：\n  " + "\n  ".join(
                    f"{i}. {t}" for i, t in enumerate(steps, 1)))
            extract = c.get("extract") or []
            if extract:
                lines.append("变量提取：" + "; ".join(
                    f"{e.get('var')}={e.get('source')}" for e in extract if isinstance(e, dict)))
            records.append({
                "kb_id": _kb_id("ui_case", pid or rel, case_id),
                "domain": "ui_case",
                "platform": PLATFORM,
                "project_id": pid,
                "source_type": "ui_json_case",
                "source_uri": f"{base_url}/api/ui-cases/{quote(case_id, safe='')}"
                              f"?file={quote(rel, safe='')}",
                "title": f"{case_id} {c.get('name') or c.get('desc') or ''}".strip(),
                "text": "\n".join(lines),
                "tags": [t for t in [str(c.get("module") or ""), str(c.get("priority") or "")] if t],
                "version": 1,
                "updated_at": updated,
            })
    return records


def export_domain(domain: str, base_url: str, *, since: str = "",
                  project_id: str = "", file: str = "") -> list[dict]:
    """按域导出记录。domain 合法但暂未实现 → 返回空（不误报错误，也不假装有权重数据）。"""
    if domain == "contract":
        return contract_records(base_url, since=since)
    if domain == "case":
        return case_records(base_url, since=since, project_id=project_id, file=file)
    if domain == "ui_case":
        return ui_case_records(base_url, since=since, project_id=project_id, file=file)
    if domain in ALL_DOMAINS:
        return []  # execution/business/convention：结构预留，导出为空
    raise ValueError(f"未知语料域 {domain!r}（可用: {list(ALL_DOMAINS)}）")
