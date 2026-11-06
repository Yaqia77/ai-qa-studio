# -*- coding: utf-8 -*-
"""OpenAPI / Swagger 文档 → 接口定义库（设计文档 2.2 的「导入」入口）。

支持 OpenAPI 3.x（JSON / YAML）与 Swagger 2.0，只提取框架真正用得上的部分：
方法、路径、请求头、查询参数、请求体示例、响应结构推导出的断言建议与 token 提取建议。

三个刻意的取舍：
- **不生成具体期望值**：文档里没写 example/default/enum 的参数一律不填，
  避免造出 `{"page": ""}` 这类看着完整、实则会把脏值发给真实接口的骨架；
- **路径参数转框架占位符**：`/orders/{id}` → `/orders/{{id}}`，与 core/variable.py
  的 `{{变量}}` 语法对齐，执行时若变量未定义会直接报「变量 {id} 未定义」，指向明确；
- **断言建议只给稳的**：状态码用 eq，结构字段（code/data/success）用 not_null，
  不猜业务错误码该等于什么 —— 猜错的断言比没有断言更害人。
"""
import json
import re

import yaml

from backend.api.api_def_store import ApiDefError, _norm_path, validate

SPEC_MAX_BYTES = 4 << 20               # 4MB：整份微服务聚合文档也够用，再大就该先裁剪
NAME_MAX_LEN = 60
SUPPORT_METHODS = ("get", "post", "put", "delete", "patch")   # 与 excel_loader.VALID_METHODS 对应
_JSON_CT = "application/json"
_STRUCT_FIELDS = ("code", "data", "success")                  # 响应里值得 not_null 的结构字段
_TOKEN_HINTS = ("token", "access_token", "auth")              # 变量提取建议的命中词
_SCHEMA_MAX_DEPTH = 6                   # 示例值递归深度（防自引用 schema 无限展开）
_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")   # 与 core/variable.py 同一占位符语法


def _load_spec(text: str) -> dict:
    """先按 JSON 读，失败再按 YAML 读（Swagger 文档两种书写都有）。"""
    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        try:
            spec = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ApiDefError(f"既不是合法 JSON 也不是合法 YAML，无法按 OpenAPI/Swagger 解析: {e}")
    if not isinstance(spec, dict):
        raise ApiDefError("Spec 根节点必须是对象（{...}）")
    if not (spec.get("openapi") or spec.get("swagger")):
        raise ApiDefError("不是 OpenAPI/Swagger 文档：缺少顶层 openapi / swagger 版本字段")
    return spec


def _resolve(node, spec, seen: set):
    """展开 $ref（只在同一份文档内查表，不支持外部 $ref 文件）。"""
    cur = node
    depth = 0
    while isinstance(cur, dict) and "$ref" in cur and depth < _SCHEMA_MAX_DEPTH:
        ref = str(cur["$ref"])
        if not ref.startswith("#/"):
            return {}
        target = spec
        for seg in ref[2:].split("/"):
            if not isinstance(target, dict):
                return {}
            target = target.get(seg.replace("~1", "/").replace("~0", "~"))
        if target is None or id(target) in seen:
            return {}
        seen.add(id(target))
        cur = target
        depth += 1
    return cur if isinstance(cur, dict) else {}


def _dig_schema(node: dict, spec: dict):
    """兼容 OpenAPI3（schema）与 Swagger2（直接就是 schema）两种写法。"""
    node = _resolve(node, spec, set())
    if not isinstance(node, dict):
        return None
    if "schema" in node:
        return _resolve(node["schema"], spec, set())
    if node.get("type") or node.get("properties") or node.get("$ref"):
        return node
    return None


def _example(node, spec, depth: int = 0, seen: set | None = None):
    """由 schema 推导示例值，返回 (值, 是否来自文档明确声明)。

    real=False 表示这个值只是类型空壳（空串/0），调用方应据此省略，不写进骨架。
    """
    seen = seen if seen is not None else set()
    if depth > _SCHEMA_MAX_DEPTH or not isinstance(node, dict):
        return None, False
    if id(node) in seen:
        return None, False
    seen = seen | {id(node)}

    node = _resolve(node, spec, seen)
    if "example" in node:
        return node["example"], True
    if "default" in node:
        return node["default"], True
    for key in ("oneOf", "anyOf", "allOf"):
        if isinstance(node.get(key), list) and node[key]:
            return _example(node[key][0], spec, depth + 1, seen)

    t = node.get("type")
    props = node.get("properties")
    if isinstance(props, dict) and props or t == "object":
        out: dict = {}
        any_real = False
        for pname, pschema in (props or {}).items():
            val, real = _example(pschema, spec, depth + 1, seen)
            if real:
                out[str(pname)] = val
                any_real = True
        return (out if any_real else None), any_real
    if t == "array" or "items" in node:
        val, real = _example(node.get("items") or {}, spec, depth + 1, seen)
        return ([val] if real else None), real
    if isinstance(node.get("enum"), list) and node["enum"]:
        return node["enum"][0], True
    return None, False


def _request_of(op: dict, spec: dict):
    """取请求内容：OpenAPI3 的 requestBody / Swagger2 的 in=body 参数。

    返回 (内容类型或 None, 示例体或 None)；只有文档真给了 example/default/enum 才返非空。
    """
    body = op.get("requestBody")
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, dict) and content:
            key = _JSON_CT if _JSON_CT in content else str(next(iter(content)))
            node = content.get(key)
            if isinstance(node, dict):
                example, real = _example(node.get("schema"), spec)
                if not (real and key == _JSON_CT):
                    return None, None      # 非 JSON 内容类型不猜 Content-Type，交给环境默认头
                return _JSON_CT, example
    for p in (op.get("parameters") or []):
        p = _resolve(p, spec, set())
        if str(p.get("in") or "") == "body":
            example, real = _example(p.get("schema"), spec)
            return (_JSON_CT if real else None), (example if real else None)
    return None, None


def _parameters_of(op: dict, spec: dict, shared: list):
    """合并路径级与操作级参数（同名以操作级为准），只保留文档给了实例的 query/header 参数。"""
    merged: dict[str, dict] = {}
    for item in list(shared or []) + list(op.get("parameters") or []):
        p = _resolve(item, spec, set())
        pname = str(p.get("name") or "").strip()
        if pname:
            merged[pname] = p
    query: dict = {}
    headers: dict = {}
    for pname, p in merged.items():
        where = str(p.get("in") or "")
        if where not in ("query", "header"):
            continue          # path 参数已转成 {{占位符}}，body/formData 由请求体承载
        val, real = _example(p.get("schema") or p, spec)
        if not real:
            continue          # 文档没给样例就不填：骨架宁可缺一行，也不发脏值
        (headers if where == "header" else query)[pname] = val
    return (query or None), (headers or None)


def _ok_response(op: dict, spec: dict):
    """取 2xx 响应（没有则 default），返回 (状态码, schema)。"""
    resp = op.get("responses") or {}
    if not isinstance(resp, dict):
        return None, None
    code = None
    node = None
    for key in sorted(resp, key=lambda s: str(s)):
        text = str(key)
        if text.startswith("2"):
            code, node = text, resp[key]
            break
    if node is None:
        code, node = "default", (resp.get("default") or {})
    schema = None
    if isinstance(node, dict):
        content = node.get("content")
        if isinstance(content, dict) and content:
            item = content.get(_JSON_CT) or next(iter(content.values()), {})
            schema = _dig_schema(item if isinstance(item, dict) else {}, spec)
        elif isinstance(node.get("schema"), dict):      # Swagger 2.0
            schema = _resolve(node["schema"], spec, set())
    try:
        status = int(code)
    except (TypeError, ValueError):
        status = 200
    return status, schema


def _suggest_asserts(status: int, schema: dict | None) -> list:
    """断言建议：状态码必给，结构字段只给 not_null（不断言业务码的具体值）。"""
    out: list = [["eq", "status_code", status if status else 200]]
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict):
        for field in _STRUCT_FIELDS:
            if field in props:
                out.append(["not_null", f"$.{field}"])
    return out[:4]


def _suggest_extract(schema: dict | None, spec: dict) -> dict | None:
    """响应里出现 token 类字段时给出变量提取建议（对应框架 extract → {{token}} 链路）。"""
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return None

    def walk(node: dict, prefix: str, depth: int):
        if depth > 3:
            return None
        for pname, pschema in (node.get("properties") or {}).items():
            path = f"{prefix}.{pname}"
            low = str(pname).lower()
            child = _dig_schema(pschema if isinstance(pschema, dict) else {}, spec) or {}
            if any(h in low for h in _TOKEN_HINTS):
                return {str(pname): path}
            if isinstance(child.get("properties"), dict):
                hit = walk(child, path, depth + 1)
                if hit:
                    return hit
        return None

    return walk({"properties": props}, "$", 1)


def _join_path(base: str, raw: str) -> str:
    """Swagger 2.0 的 basePath 前缀补全（OpenAPI3 的 servers 含主机名，交给 base_url 管）。"""
    base = _norm_path(str(base or ""))
    path = raw if raw.startswith("/") else "/" + raw
    return (base + path) if base and base != "/" and not path.startswith(base) else path


def _to_placeholder(path: str) -> str:
    """`/orders/{id}` → `/orders/{{id}}`：路径参数换成框架变量语法。"""
    out = str(path or "")
    for seg in [s for s in out.split("/") if s.startswith("{") and s.endswith("}")]:
        var = seg[1:-1].strip()
        if var:
            out = out.replace(seg, "{{" + var + "}}")
    return out


def parse_spec(text: str) -> tuple[list, list]:
    """解析文档文本 → (接口定义列表, 跳过说明列表)。失败抛 ApiDefError。"""
    raw = str(text or "")
    if not raw.strip():
        raise ApiDefError("Spec 内容为空")
    if len(raw.encode("utf-8", "replace")) > SPEC_MAX_BYTES:
        raise ApiDefError(f"Spec 超过 {SPEC_MAX_BYTES >> 20}MB 上限，请先只保留需要的 paths 再导入")
    spec = _load_spec(raw)
    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise ApiDefError("paths 段为空或不是对象，没有可导入的接口")

    defs: list = []
    skipped: list = []
    used: set = set()
    for raw_path in sorted(paths):
        item = paths[raw_path]
        if not isinstance(item, dict):
            skipped.append(f"{raw_path}：路径项不是对象，已跳过")
            continue
        shared = item.get("parameters") or []
        path = _to_placeholder(_join_path(spec.get("basePath"), str(raw_path)))
        if not path.startswith(("/", "http://", "https://")):
            skipped.append(f"{raw_path}：路径不合法，已跳过")
            continue
        for method in SUPPORT_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue          # 没定义该动作（或只是 options/head 等框架不支持的方式）
            name = _op_name(op, method, path, used)
            used.add(name)
            ct, body = _request_of(op, spec)
            query, extra_headers = _parameters_of(op, spec, shared)
            status, schema = _ok_response(op, spec)
            headers = dict(extra_headers or {})
            if ct and "Content-Type" not in headers:
                headers["Content-Type"] = _JSON_CT
            tags = op.get("tags")
            module = str(tags[0]).strip()[:40] if isinstance(tags, list) and tags else "未分类"
            defs.append(validate(
                name=name,
                module=module,
                method=method.upper(),
                path=path,
                remark=_remark(op, path),
                headers=headers or None,
                params=query,
                body=body,
                extract=_suggest_extract(schema, spec),
                asserts=_suggest_asserts(status, schema),
                source="openapi",
            ))
    if not defs:
        raise ApiDefError("未解析到可导入的接口（请求方式仅支持 "
                          + "/".join(m.upper() for m in SUPPORT_METHODS) + "）")
    return defs, skipped


def _unique(base: str, used: set) -> str:
    """同一文档内接口名必须互不重复（重复 summary 很常见），后缀递增且保证不超长度上限。"""
    stem = base[:NAME_MAX_LEN]
    if stem not in used:
        return stem
    stem = base[:NAME_MAX_LEN - 6] or "接口"
    for n in range(2, 1000):
        cand = f"{stem}({n})"
        if cand not in used:
            return cand
    return f"{stem}({len(used)})"[:NAME_MAX_LEN]


def _safe_name(text: str) -> str:
    """接口名要能安全写进 REST 路径（/api/defs/{name}）：/ \\ ? # 换成连字符。

    文档里“查询/列表”这类带斜杠的 summary 很常见，硬拒绝会让整批导入失败，
    所以导入侧自动清洗；手工录入仍由 api_def_store.validate 拒收（口径一致、提示更直接）。
    """
    out = re.sub(r"[\r\n\t]+", " ", str(text or "")).strip()
    return re.sub(r"[/\\?#]", "-", out)


def _op_name(op: dict, method: str, path: str, used: set) -> str:
    """接口名优先取中文 summary，其次 operationId，最后兜底「方法 路径」。"""
    for key in ("summary", "operationId"):
        cand = _safe_name(op.get(key))
        if cand:
            return _unique(cand, used)
    return _unique(_safe_name(f"{method.upper()} {path}"), used)


def _remark(op: dict, path: str) -> str:
    """说明：文档描述 + 需要赋值的占位符变量清单（导入后一眼看出还缺什么）。"""
    desc = str(op.get("description") or op.get("summary") or "").strip()
    vars_needed = _VAR_RE.findall(path)
    parts = []
    if desc and desc != path:
        parts.append(desc[:120])
    parts.append("来源 OpenAPI")
    if vars_needed:
        parts.append("需赋值变量: " + ", ".join(vars_needed))
    return "；".join(parts)[:200]
