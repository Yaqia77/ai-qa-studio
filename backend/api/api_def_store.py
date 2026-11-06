# -*- coding: utf-8 -*-
"""接口定义库存储：data/api_defs.json（平台「接口定义」页维护，设计文档 2.2）。

定位：接口库是**元数据**，不是执行体 —— 用例始终完整自带 方法/路径/断言，
删掉一条定义不会让任何批次失败。它只承担三件事：
1. 沉淀：把散落在 Excel 里的同名接口收敛成一份权威定义（from_cases 按频次投票）；
2. 建议：新用例选中接口名即可带入推荐断言与请求头，不用猜 `$.code` 该怎么写；
3. 骨架：由定义生成用例行（支持先预览再落 Excel），OpenAPI/Swagger 导入也走这条路。

断言建议的结构校验一律走 excel_loader.check_assert_rules —— 与读用例同一口径，
保证「库里推荐的断言」写进 Excel 后 loader 必然认账。
"""
import json
import os
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

from core import excel_loader

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_FILE = ROOT / "data" / "api_defs.json"
# 可重入锁：create/update/upsert_many 持锁内还要读改写，Lock 不可重入会自锁
_LOCK = threading.RLock()

NAME_MAX = 60
MODULE_MAX = 40
REMARK_MAX = 200
ASSERT_MAX = 10                     # 断言建议条数上限：骨架是起点，不是把断言堆满
AGGREGATE_ASSERT_TOP = 4            # 聚合时最多给出几条建议（取频次最高的）
# 接口名要能安全写进 REST 路径（/api/defs/{name}）：含下列字符时 %2F 会被服务端
# 解码成多一段路径，直接 404 —— 定义建了就改不了也删不掉，故一律拒收
NAME_BAD_CHARS = ("/", "\\", "?", "#")
SOURCE_LABELS = {"manual": "手工录入", "excel": "Excel 聚合", "openapi": "OpenAPI 导入"}


class ApiDefError(ValueError):
    """定义不合法 / 接口名或端点冲突，路由层统一转 400。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_raw() -> list:
    """读全量接口定义（文件不存在返回空列表，故全新仓库开箱即用、无需播种）。"""
    if not DATA_FILE.is_file():
        return []
    try:
        with open(DATA_FILE, encoding="utf-8") as fp:
            raw = json.load(fp)
    except (json.JSONDecodeError, OSError) as e:
        raise ApiDefError(f"接口定义文件解析失败: {e}")
    items = raw.get("apis") if isinstance(raw, dict) else raw
    return items if isinstance(items, list) else []


def _write_raw(items: list) -> None:
    with _LOCK:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"version": 1, "apis": items}, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)


def _norm_path(path) -> str:
    """端点比较用归一化：只去首尾空白与末尾斜杠（路径大小写服务端区分，保留原样）。"""
    return str(path or "").strip().rstrip("/")


def _as_dict(value, label: str):
    if value in (None, {}):
        return None
    if not isinstance(value, dict):
        raise ApiDefError(f"{label} 必须是 JSON 对象 {{...}}")
    return value


def validate(name: str, module: str, method: str, path: str, remark: str = "",
             headers=None, params=None, body=None, extract=None, asserts=None,
             source: str = "manual") -> dict:
    """归一化 + 校验，返回可直接落库的定义（失败抛 ApiDefError，与路由层 400 对应）。"""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ApiDefError("接口名称不能为空")
    if any(ch in clean_name for ch in ("\r", "\n", "\t")):
        raise ApiDefError("接口名称不能包含换行/制表符（用例按名称精确匹配，须保持单行）")
    hit = [ch for ch in NAME_BAD_CHARS if ch in clean_name]
    if hit:
        raise ApiDefError(f"接口名称不能包含 {' '.join(hit)}（接口库按名称直接寻址，"
                          f"请把用例的接口名称列改成不含这些字符的写法）")
    if len(clean_name) > NAME_MAX:
        raise ApiDefError(f"接口名称不能超过 {NAME_MAX} 字，实际 {len(clean_name)}")

    clean_module = str(module or "").strip() or "未分类"
    if len(clean_module) > MODULE_MAX:
        raise ApiDefError(f"模块名不能超过 {MODULE_MAX} 字，实际 {len(clean_module)}")

    clean_method = str(method or "").strip().upper()
    if clean_method not in excel_loader.VALID_METHODS:
        raise ApiDefError(f"请求方式必须是 {'/'.join(sorted(excel_loader.VALID_METHODS))}，实际为 {method!r}")

    clean_path = str(path or "").strip()
    if not clean_path:
        raise ApiDefError("请求路径不能为空")
    if not (clean_path.startswith("/") or clean_path.startswith("http://")
            or clean_path.startswith("https://")):
        raise ApiDefError(f"请求路径必须以 / 或 http(s):// 开头，实际为 {clean_path!r}")
    if any(ch in clean_path for ch in ("\r", "\n", " ")):
        raise ApiDefError("请求路径不能包含空格或换行")

    clean_remark = str(remark or "").strip()
    if len(clean_remark) > REMARK_MAX:
        raise ApiDefError(f"说明不能超过 {REMARK_MAX} 字，实际 {len(clean_remark)}")

    # 请求体只收 JSON 结构：前端只有 JSON 输入框，裸文本存进来既不能当示例也不能选类型
    if body is not None and body not in ({}, [], "") and not isinstance(body, (dict, list)):
        raise ApiDefError(f"请求体示例必须是 JSON 对象或数组，实际为 {type(body).__name__}")

    rules = list(asserts or [])
    if len(rules) > ASSERT_MAX:
        raise ApiDefError(f"断言建议最多 {ASSERT_MAX} 条，实际 {len(rules)}")
    if rules:
        msgs = excel_loader.check_assert_rules(rules)
        if msgs:
            raise ApiDefError(f"断言建议不合法：{msgs[0]}")
        rules = [list(r) for r in rules]     # 元组统一成列表，落 JSON 后不再变形

    return {
        "name": clean_name,
        "module": clean_module,
        "method": clean_method,
        "path": clean_path,
        "remark": clean_remark,
        "headers": _as_dict(headers, "请求头"),
        "params": _as_dict(params, "查询参数"),
        "body": body if isinstance(body, (dict, list)) and body not in ({}, []) else None,
        "extract": _as_dict(extract, "变量提取"),
        "asserts": rules or None,
        "source": source if source in SOURCE_LABELS else "manual",
    }


def list_defs() -> list:
    """按 模块 → 接口名 排序返回全部定义。"""
    return sorted(_read_raw(), key=lambda d: (str(d.get("module") or ""), str(d.get("name") or "")))


def get(name: str) -> dict | None:
    for item in _read_raw():
        if item.get("name") == name:
            return item
    return None


def names() -> list:
    return sorted({str(i.get("name") or "") for i in _read_raw() if i.get("name")})


def modules() -> list:
    return sorted({str(i.get("module") or "未分类") for i in _read_raw()})


def _endpoint_owner(items: list, method: str, path: str, exclude_name: str = "") -> str:
    """找出占用同一 (方法, 路径) 的其他接口名（无则空串）。"""
    key = (str(method or "").upper(), _norm_path(path))
    for i in items:
        if str(i.get("name") or "") == exclude_name:
            continue
        if (str(i.get("method") or "").upper(), _norm_path(i.get("path"))) == key:
            return str(i.get("name") or "")
    return ""


def create(**fields) -> dict:
    """新建定义：接口名唯一 + 端点唯一（同 (方法,路径) 视为同一接口，不允许两个名字）。"""
    source = fields.pop("source", "manual")
    clean = validate(source=source, **fields)
    with _LOCK:
        items = _read_raw()
        if any(str(i.get("name")) == clean["name"] for i in items):
            raise ApiDefError(f"接口已存在: {clean['name']}（请直接编辑，或换一个名称）")
        holder = _endpoint_owner(items, clean["method"], clean["path"])
        if holder:
            raise ApiDefError(f"{clean['method']} {clean['path']} 已登记为接口「{holder}」，"
                              f"请勿用不同名称重复定义同一端点")
        record = {**clean, "updated_at": _now()}
        items.append(record)
        _write_raw(items)
    return record


def update(name: str, patch: dict) -> dict:
    """更新定义。

    文本字段以 None 为「不改」（传空串就是清空）；
    headers/params/body/extract/asserts 以「键在不在 patch 里」判断，
    否则前端无法把一个请求头/断言建议真正清空（置 null 会被当成未改）。
    """
    with _LOCK:
        items = _read_raw()
        target = next((i for i in items if i.get("name") == name), None)
        if target is None:
            raise ApiDefError(f"接口不存在: {name}")
        new_name = str(patch["name"]).strip() if patch.get("name") is not None else name
        merged = {
            "name": new_name,
            "module": patch["module"] if patch.get("module") is not None else target.get("module"),
            "method": patch["method"] if patch.get("method") is not None else target.get("method"),
            "path": patch["path"] if patch.get("path") is not None else target.get("path"),
            "remark": patch["remark"] if patch.get("remark") is not None else target.get("remark"),
            "headers": patch["headers"] if "headers" in patch else target.get("headers"),
            "params": patch["params"] if "params" in patch else target.get("params"),
            "body": patch["body"] if "body" in patch else target.get("body"),
            "extract": patch["extract"] if "extract" in patch else target.get("extract"),
            "asserts": patch["asserts"] if "asserts" in patch else target.get("asserts"),
            "source": str(target.get("source") or "manual"),   # 来源只在创建时确定，之后编辑不篡改血缘
        }
        clean = validate(**merged)
        if new_name != name and any(str(i.get("name")) == new_name for i in items):
            raise ApiDefError(f"目标接口名已存在: {new_name}")
        holder = _endpoint_owner(items, clean["method"], clean["path"], exclude_name=name)
        if holder:
            raise ApiDefError(f"{clean['method']} {clean['path']} 已登记为接口「{holder}」")
        target.clear()
        target.update({**clean, "updated_at": _now()})
        _write_raw(items)
    return target


def delete(name: str) -> None:
    """删除定义（调用方须先确认无用例同名，否则覆盖统计与断言建议就断了源）。"""
    with _LOCK:
        items = _read_raw()
        rest = [i for i in items if i.get("name") != name]
        if len(rest) == len(items):
            raise ApiDefError(f"接口不存在: {name}")
        _write_raw(rest)


def upsert_many(records: list, overwrite: bool) -> dict:
    """批量落库（聚合/导入共用）：一次读一次写，端点冲突只跳过、绝不覆盖别人的定义。

    written_names 回带本次在接口库中有效登记的接口名（新建/覆盖/同名已存在都算，
    被端点冲突拦截的不算）：Agent 文档一键生成据此逐接口生成用例。
    新增键对旧调用方向后兼容（aggregate/import 响应都是 **stats 透传）。
    """
    counts = {"created": 0, "updated": 0, "skipped": 0}
    conflicts: list[str] = []
    written_names: list[str] = []
    with _LOCK:
        items = _read_raw()
        by_name = {str(i.get("name")): i for i in items}
        for rec in records:
            exist = by_name.get(rec["name"])
            if exist is None:
                holder = _endpoint_owner(items, rec["method"], rec["path"])
                if holder:
                    counts["skipped"] += 1
                    conflicts.append(f"{rec['name']}：{rec['method']} {rec['path']} "
                                     f"已登记为「{holder}」，未覆盖")
                    continue
                record = {**rec, "updated_at": _now()}
                items.append(record)
                by_name[record["name"]] = record
                counts["created"] += 1
                written_names.append(str(rec["name"]))
            elif overwrite:
                exist.clear()
                exist.update({**rec, "updated_at": _now()})
                counts["updated"] += 1
                written_names.append(str(rec["name"]))
            else:
                counts["skipped"] += 1
                written_names.append(str(rec["name"]))   # 同名定义已在库中，同样可用于生成用例
        _write_raw(items)
    return {**counts, "conflicts": conflicts, "written_names": written_names}


# ==================== 存量用例 → 接口定义（设计文档 2.2「接口库沉淀」） ====================


def _most_common(values, dumps: bool = False):
    """取出现次数最多的取值（dumps=True 时按 JSON 文本比较，用于 dict/list 这类不可哈希值）。"""
    if not values:
        return None
    if not dumps:
        return Counter(values).most_common(1)[0][0]
    freq = Counter(json.dumps(v, ensure_ascii=False, sort_keys=True) for v in values)
    return json.loads(freq.most_common(1)[0][0])


def _merge_kv(rows: list):
    """键值对列表（dict 数组）→ 每个 key 取出现最多的完整值（与用例请求头建议同一算法）。"""
    key_freq: Counter = Counter()
    kv_freq: Counter = Counter()
    for row in rows:
        for k, v in (row or {}).items():
            key_freq[k] += 1
            kv_freq[(k, str(v))] += 1
    out: dict = {}
    for k, _times in key_freq.most_common():
        best = max(((cnt, vv) for (kk, vv), cnt in kv_freq.items() if kk == k), key=lambda t: t[0])[1]
        out[k] = best
    return out or None


def _top_asserts(cases: list) -> list | None:
    """同名接口的断言按出现频次取前几名 —— 存量用例怎么写，建议就怎么给。

    同一取值路径的 eq 只保留最常见的一个期望值：错误用例的 eq $.code 01000002 与
    正例的 eq $.code 0000 互相矛盾，都放进建议会让生成的骨架自相打架。
    """
    freq: Counter = Counter()
    for c in cases:
        for rule in (c.asserts or []):
            if isinstance(rule, (list, tuple)):
                freq[json.dumps(list(rule), ensure_ascii=False)] += 1
    if not freq:
        return None
    out: list = []
    eq_paths: set = set()
    for text, _cnt in freq.most_common():
        rule = json.loads(text)
        op = str(rule[0]).strip().lower()
        path = str(rule[1]).strip()
        if op == "eq":
            if path in eq_paths:
                continue
            eq_paths.add(path)
        out.append(rule)
        if len(out) >= AGGREGATE_ASSERT_TOP:
            break
    return out or None


def from_cases(cases: list) -> tuple[list, list]:
    """把用例列表聚合成接口定义候选（纯函数：不读盘、不落库，便于校验与冒烟）。

    合并口径：同名接口（api_name）归为一条定义；方法/路径/模块/请求体取众数，
    请求头与变量提取按 key 取各自最常见的值，断言建议取最常见的若干条。
    返回 (定义列表, 跳过说明)：个别接口名不合法（如含 /）只跳过该分组，
    不能让一个脏名称把整批聚合拒掉。
    """
    groups: dict[str, list] = {}
    for c in cases:
        name = str(getattr(c, "api_name", "") or "").strip()
        if name:
            groups.setdefault(name, []).append(c)

    out: list = []
    rejected: list = []
    for name in sorted(groups):
        group = groups[name]
        method = _most_common([str(c.method or "").upper() for c in group])
        path = _most_common([_norm_path(c.url) for c in group]) or str(group[0].url or "").strip()
        try:
            rec = validate(
                name=name,
                module=_most_common([str(c.module or "未分类") for c in group]),
                method=method,
                path=path,
                remark=f"由 {len(group)} 条存量用例聚合",
                headers=_merge_kv([c.headers for c in group]),
                params=_merge_kv([c.params for c in group]),
                body=_most_common([c.body for c in group
                                   if isinstance(c.body, (dict, list)) and c.body not in (None, {}, "", [])],
                                  dumps=True),   # 只收 JSON 结构化请求体：存量里的 raw 文本不当示例
                extract=_merge_kv([c.extract for c in group]),
                asserts=_top_asserts(group),
                source="excel",
            )
        except ApiDefError as e:
            rows = ", ".join(sorted(str(c.case_id) for c in group)[:5])
            rejected.append(f"{name}：{e}（涉及用例 {rows}）")
            continue
        out.append(rec)
    return out, rejected
