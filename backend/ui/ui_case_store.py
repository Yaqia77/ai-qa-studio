"""UI 用例库：一个项目一个 JSON 文件（data/cases/<id>.json）。

设计：UI 用例是多步骤流程，Excel 单元格难以表达，故与接口用例分轨——接口仍走
Excel，UI 走结构化 JSON。本模块负责 load/validate/增删改查/查重/跨项目迁移，
校验不通过绝不落盘（临时文件 + os.replace 原子替换），错误口径与 excel_writer
对齐（UiCaseError.kind ∈ invalid/not_found/conflict/locked），便于路由统一映射 HTTP 状态。

单条用例结构见 validate_case；步骤动作集与定位器前缀在 ACTIONS / _LOCATOR_PREFIXES 定义，
core.ui_runner 执行器按同一口径解释，二者是唯二消费方。
"""
import json
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

VALID_PRIORITIES = {"P0", "P1", "P2", "P3", "P4", "P5"}
VALID_BROWSERS = {"chromium", "firefox", "webkit"}

# 失败重试上限：最多 3 次额外尝试；默认 0=不重试，保证存量行为不变。
MAX_RETRY = 3

# 步骤动作元数据：desc 中文名；locator/value 标记该动作是否需要定位器/取值；
# hint（可选）为该动作定位器/取值的填写说明，随 meta() 下发供步骤编辑器做输入占位提示
# （单一口径：新增动作只在此定义，校验/执行/前端下拉三处自动跟随）。
ACTIONS: dict[str, dict] = {
    "goto":           {"desc": "打开地址", "locator": False, "value": True, "hint": "相对路径（自动拼环境 base_url）或完整 URL"},
    "click":          {"desc": "点击",     "locator": True,  "value": False},
    "fill":           {"desc": "输入文本", "locator": True,  "value": True},
    "press":          {"desc": "按键",     "locator": True,  "value": True, "hint": "按键名，如 Enter / Tab / Control+a"},
    "select":         {"desc": "下拉选择", "locator": True,  "value": True, "hint": "按选项可见文本（label）选择"},
    "check":          {"desc": "勾选",     "locator": True,  "value": False},
    "uncheck":        {"desc": "取消勾选", "locator": True,  "value": False},
    "dblclick":       {"desc": "双击",     "locator": True,  "value": False},
    "upload":         {"desc": "上传文件", "locator": True,  "value": True, "hint": "本机文件路径（相对项目根或绝对路径），定位器指向 input[type=file]"},
    "hover":          {"desc": "悬停",     "locator": True,  "value": False},
    "wait_for":       {"desc": "等待出现", "locator": True,  "value": False},
    "screenshot":     {"desc": "截图",     "locator": False, "value": False},  # 可选定位器：无则整页
    # ---- 交互与等待补齐 ----
    "right_click":    {"desc": "右键点击", "locator": True,  "value": False},
    "scroll_to":      {"desc": "滚动到元素", "locator": True, "value": False},
    "drag_to":        {"desc": "拖拽到目标", "locator": True,  "value": True, "hint": "定位器=源元素，取值=目标元素定位器（如 #drop-zone）"},
    "navigate":       {"desc": "前进/后退/刷新", "locator": False, "value": True, "hint": "back / forward / reload"},
    "wait_load":      {"desc": "等待加载状态", "locator": False, "value": True, "hint": "load / domcontentloaded / networkidle"},
    "wait_state":     {"desc": "等待元素状态", "locator": True,  "value": True, "hint": "visible / hidden / attached / detached"},
    "sleep":          {"desc": "固定等待(毫秒)", "locator": False, "value": True, "hint": "毫秒数，如 1000（兜底硬等待，优先用显式等待）"},
    # ---- 断言补齐 ----
    "expect_visible": {"desc": "断言可见", "locator": True,  "value": False},
    "expect_hidden":  {"desc": "断言不可见", "locator": True,  "value": False, "hint": "元素不可见或不存在均判通过"},
    "expect_text":    {"desc": "断言文本", "locator": True,  "value": True},
    "expect_count":   {"desc": "断言元素数量", "locator": True,  "value": True, "hint": "期望数量（整数）"},
    "expect_value":   {"desc": "断言输入值", "locator": True,  "value": True, "hint": "输入框 value 期望等于"},
    "expect_attr":    {"desc": "断言属性", "locator": True,  "value": True, "hint": "格式 属性名=期望值，如 href=/login"},
    "expect_enabled": {"desc": "断言可交互", "locator": True,  "value": False},
    "expect_url":     {"desc": "断言地址", "locator": False, "value": True},
    "expect_title":   {"desc": "断言标题", "locator": False, "value": True, "hint": "页面 title 需包含的文本"},
    # ---- iframe / 多标签窗口 ----
    "switch_frame":   {"desc": "切入 iframe", "locator": True,  "value": False, "hint": "iframe 元素定位器（如 css=#app-frame）"},
    "switch_main":    {"desc": "回到主文档", "locator": False, "value": False},
    "switch_window":  {"desc": "切换标签页", "locator": False, "value": True, "hint": "页签序号（0 起）或 last（最新打开的窗口）"},
}
VALID_ACTIONS = set(ACTIONS)

# 中文/常见别名 → 规范动作键：供 Excel 一步一行解析与 JSON 导入共用，
# 让非技术 QA 只填「打开/点击/输入」即可，无需记 goto/click/fill。
# 规范英文键本身也在内（幂等）。
ACTION_ALIASES: dict[str, str] = {
    "goto": "goto", "打开": "goto", "打开地址": "goto", "访问": "goto", "访问页面": "goto",
    "click": "click", "点击": "click", "单击": "click",
    "fill": "fill", "输入": "fill", "填写": "fill", "输入文本": "fill", "填表": "fill",
    "press": "press", "按键": "press", "回车": "press", "按下": "press",
    "select": "select", "选择": "select", "下拉": "select", "下拉选择": "select",
    "check": "check", "勾选": "check", "选中": "check",
    "uncheck": "uncheck", "取消勾选": "uncheck", "取消选中": "uncheck",
    "dblclick": "dblclick", "双击": "dblclick",
    "right_click": "right_click", "右键": "right_click", "右键点击": "right_click",
    "upload": "upload", "上传": "upload", "上传文件": "upload", "选择文件": "upload",
    "scroll_to": "scroll_to", "滚动": "scroll_to", "滚动到": "scroll_to", "滚动到元素": "scroll_to",
    "drag_to": "drag_to", "拖拽": "drag_to", "拖动": "drag_to", "拖到": "drag_to",
    "navigate": "navigate", "前进": "navigate", "后退": "navigate", "刷新": "navigate", "导航": "navigate",
    "wait_load": "wait_load", "等待加载": "wait_load", "页面加载": "wait_load",
    "wait_state": "wait_state", "等待状态": "wait_state", "等待消失": "wait_state",
    "sleep": "sleep", "硬等待": "sleep", "固定等待": "sleep", "停顿": "sleep",
    "expect_hidden": "expect_hidden", "断言不可见": "expect_hidden", "不可见": "expect_hidden",
    "expect_count": "expect_count", "断言数量": "expect_count", "数量断言": "expect_count",
    "expect_value": "expect_value", "断言输入值": "expect_value", "值断言": "expect_value",
    "expect_attr": "expect_attr", "断言属性": "expect_attr", "属性断言": "expect_attr",
    "expect_enabled": "expect_enabled", "断言可交互": "expect_enabled", "可用": "expect_enabled",
    "expect_title": "expect_title", "断言标题": "expect_title", "标题断言": "expect_title",
    "switch_frame": "switch_frame", "切帧": "switch_frame", "切入iframe": "switch_frame", "切换iframe": "switch_frame",
    "switch_main": "switch_main", "回主文档": "switch_main", "切回主文档": "switch_main",
    "switch_window": "switch_window", "切换窗口": "switch_window", "切换标签": "switch_window", "切标签页": "switch_window",
    "hover": "hover", "悬停": "hover", "移动": "hover",
    "wait_for": "wait_for", "等待": "wait_for", "等待出现": "wait_for",
    "screenshot": "screenshot", "截图": "screenshot",
    "expect_visible": "expect_visible", "断言可见": "expect_visible", "可见": "expect_visible",
    "expect_text": "expect_text", "断言文本": "expect_text", "包含文本": "expect_text", "断言包含": "expect_text",
    "expect_url": "expect_url", "断言地址": "expect_url", "断言url": "expect_url", "url断言": "expect_url",
}

# 布尔别名（无头/是否执行列）：中文是/否、Y/N、1/0 一律接受
BOOL_TRUE = {"true", "1", "y", "yes", "是", "启用", "执行", "t"}
BOOL_FALSE = {"false", "0", "n", "no", "否", "停用", "不执行", "跳过", "f"}


def normalize_action(raw: object) -> str:
    """把录入的动作文本（中文别名/英文键/大小写）归一为规范动作键；无法识别时原样返回（去空白小写后）。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    return ACTION_ALIASES.get(text) or ACTION_ALIASES.get(text.lower()) or text.lower()


def normalize_bool(raw: object, default: bool = True) -> bool:
    """把无头/是否执行的文本归一为布尔；无法识别返回 default。"""
    if isinstance(raw, bool):
        return raw
    text = str(raw if raw is not None else "").strip().lower()
    if text in BOOL_TRUE:
        return True
    if text in BOOL_FALSE:
        return False
    return default

# 定位器前缀（与 core.engines.web.engine._resolve_locator 解析口径一致）；裸选择器默认按 css 处理。
# testid=/placeholder=/alt= 稳定标识方言（对应 get_by_test_id/placeholder/alt_text）。
_LOCATOR_PREFIXES = ("css=", "xpath=", "text=", "role=", "testid=", "placeholder=", "alt=")

# 高频动作（新手优先展示）：其余归入「高级」；供 meta() 给步骤编辑器分组下拉。
COMMON_ACTIONS = {"goto", "click", "fill", "check", "uncheck", "dblclick", "upload",
                  "expect_text", "expect_visible", "expect_hidden", "wait_for"}

_CASE_ID_RE = re.compile(r"[\r\n\t]")


class UiCaseError(Exception):
    """kind: invalid（字段非法）/ not_found（文件或用例不存在）/ conflict（编号重复）/ locked（文件被占用）。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


_LOCK = threading.Lock()


# ==================== 读写底座 ====================

def scaffold(path: Path) -> None:
    """新建 UI 项目时脚手架一张空用例表：{"version":1,"cases":[]}。"""
    _write(path, {"version": 1, "cases": []})


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
    except OSError as e:
        _safe_remove(tmp)
        raise UiCaseError("invalid", f"写入临时文件失败: {e!r}")
    try:
        os.replace(tmp, path)
    except PermissionError:
        _safe_remove(tmp)
        raise UiCaseError("locked", f"文件 {path.name} 正被其他程序占用，请关闭后重试")


def _safe_remove(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def _read(path: Path) -> dict:
    if not path.exists():
        raise UiCaseError("not_found", f"UI 用例文件不存在: {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise UiCaseError("invalid", f"UI 用例文件不是合法 JSON: {path.name}（{e}）")
    except OSError as e:
        raise UiCaseError("invalid", f"读取 UI 用例文件失败: {e!r}")
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise UiCaseError("invalid", f"UI 用例文件结构应为 {{'cases': [...]}}: {path.name}")
    return data


def load_cases(path: Path) -> list[dict]:
    """容错读取用例列表：文件缺失返回空（供项目计数/列表在 allow_empty 语义下复用）。"""
    if not path.exists():
        return []
    return _read(path)["cases"]


# ==================== 校验 ====================

def _validate_locator(loc: str, idx: int) -> str:
    loc = (loc or "").strip()
    if not loc:
        raise UiCaseError("invalid", f"第 {idx} 步缺少定位器")
    if not loc.startswith(_LOCATOR_PREFIXES) and ("=" not in loc.split("]", 1)[0]):
        # 裸选择器（如 #id、.class、button）按 css 处理，允许；仅拒绝明显畸形（空/纯空白已在上方拦截）
        pass
    return loc


def _validate_step(step: object, idx: int) -> dict:
    if not isinstance(step, dict):
        raise UiCaseError("invalid", f"第 {idx} 步必须是对象")
    action = str(step.get("action") or "").strip()
    action = normalize_action(action)  # 中文别名/英文键统一归一，再校验合法性
    if action not in VALID_ACTIONS:
        raise UiCaseError("invalid", f"第 {idx} 步动作非法: {action!r}（可选: {'/'.join(sorted(VALID_ACTIONS))}）")
    meta = ACTIONS[action]
    out: dict = {"action": action, "desc": str(step.get("desc") or "").strip()}
    locator = str(step.get("locator") or "").strip()
    value = step.get("value")
    if meta["locator"]:
        out["locator"] = _validate_locator(locator, idx)
    elif locator:  # screenshot 等可选定位器
        out["locator"] = locator
    if meta["value"]:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise UiCaseError("invalid", f"第 {idx} 步（{ACTIONS[action]['desc']}）缺少取值")
        out["value"] = value if not isinstance(value, str) else value
    elif value is not None and value != "":
        out["value"] = value
    return out


def validate_case(raw: dict) -> dict:
    """整条用例规范化 + 校验；返回清洗后的 dict（不落盘）。任一字段非法即抛 UiCaseError。"""
    if not isinstance(raw, dict):
        raise UiCaseError("invalid", "用例必须是对象")
    case_id = str(raw.get("case_id") or "").strip()
    if not case_id:
        raise UiCaseError("invalid", "用例编号不能为空")
    if _CASE_ID_RE.search(case_id):
        raise UiCaseError("invalid", "用例编号不能包含换行/制表符")

    priority = str(raw.get("priority") or "P2").strip().upper()
    if priority not in VALID_PRIORITIES:
        raise UiCaseError("invalid", f"优先级必须是 {'/'.join(sorted(VALID_PRIORITIES))}，实际为 {priority!r}")

    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise UiCaseError("invalid", "UI 用例至少需要一个步骤")
    clean_steps = [_validate_step(s, i + 1) for i, s in enumerate(steps)]

    extract = raw.get("extract") or []
    if not isinstance(extract, list):
        raise UiCaseError("invalid", "变量提取必须是数组")
    clean_extract = []
    for i, e in enumerate(extract, 1):
        if not isinstance(e, dict) or not str(e.get("var") or "").strip() or not str(e.get("source") or "").strip():
            raise UiCaseError("invalid", f"第 {i} 条变量提取需含 var 与 source")
        clean_extract.append({"var": str(e["var"]).strip(), "source": str(e["source"]).strip()})

    browser = str(raw.get("browser") or "chromium").strip().lower()
    if browser not in VALID_BROWSERS:
        raise UiCaseError("invalid", f"浏览器只能是 {'/'.join(sorted(VALID_BROWSERS))}，实际为 {browser!r}")

    def _str_list(key: str) -> list[str]:
        v = raw.get(key) or []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise UiCaseError("invalid", f"{key} 必须是字符串数组")
        return [x.strip() for x in v if x.strip()]

    # 引用登录态会话：仅规范化（strip，空串→None），不做存在性硬校验——
    # 会话库读不到时用例仍应能被列出/保存（元数据非执行体），执行期由 session_collect 解析。
    session = str(raw.get("session") or "").strip() or None

    # 失败重试次数：宽归一化而非拒绝——非法/负数一律收敛到 0（不重试），
    # 上限截断到 MAX_RETRY；默认 0 保证存量用例逐条行为不变。
    try:
        retry = max(0, min(int(raw.get("retry") or 0), MAX_RETRY))
    except (TypeError, ValueError):
        retry = 0

    return {
        "case_id": case_id,
        "module": str(raw.get("module") or "未分类").strip() or "未分类",
        "name": str(raw.get("name") or "新建UI用例").strip() or "新建UI用例",
        "desc": str(raw.get("desc") or "").strip(),
        "priority": priority,
        "run": bool(raw.get("run", True)),
        "browser": browser,
        "headless": bool(raw.get("headless", True)),
        "steps": clean_steps,
        "extract": clean_extract,
        "pre_scripts": _str_list("pre_scripts"),
        "post_scripts": _str_list("post_scripts"),
        "session": session,
        "retry": retry,
    }


# ==================== 查询 ====================

def _match(case: dict, module: str | None, priority: str | None,
           keyword: str | None, run: bool | None) -> bool:
    if run is not None and bool(case.get("run", True)) is not run:
        return False
    if module and module not in str(case.get("module", "")):
        return False
    if priority:
        wanted = {p.strip().upper() for p in priority.split(",") if p.strip()}
        if str(case.get("priority", "")).upper() not in wanted:
            return False
    if keyword:
        kw = keyword.strip().lower()
        hay = " ".join([str(case.get("case_id", "")), str(case.get("name", "")),
                        str(case.get("desc", ""))]).lower()
        if kw not in hay:
            return False
    return True


def list_cases(path: Path, module: str | None = None, priority: str | None = None,
               keyword: str | None = None, run: bool | None = None) -> list[dict]:
    cases = load_cases(path)
    return [c for c in cases if _match(c, module, priority, keyword, run)]


def get_case(path: Path, case_id: str) -> dict | None:
    return next((c for c in load_cases(path) if c.get("case_id") == case_id), None)


def meta(path: Path) -> dict:
    """编辑用元数据：模块/优先级候选 + 动作字典（前端步骤编辑器据此渲染下拉）。"""
    cases = load_cases(path)
    return {
        "priorities": sorted(VALID_PRIORITIES),
        "browsers": sorted(VALID_BROWSERS),
        "modules": sorted({str(c.get("module", "")) for c in cases if c.get("module")}),
        "actions": [{"action": a, "desc": m["desc"],
                     "need_locator": m["locator"], "need_value": m["value"],
                     "hint": m.get("hint", ""),
                     "group": "常用" if a in COMMON_ACTIONS else "高级"}
                    for a, m in sorted(ACTIONS.items())],
    }


# ==================== 写操作 ====================

def create_case(path: Path, raw: dict) -> dict:
    case = validate_case(raw)
    with _LOCK:
        data = _read(path)
        if any(c.get("case_id") == case["case_id"] for c in data["cases"]):
            raise UiCaseError("conflict", f"用例编号已存在: {case['case_id']}")
        data["cases"].append(case)
        _write(path, data)
    return case


def update_case(path: Path, case_id: str, changes: dict) -> dict:
    """按编号定位整条替换可改字段（case_id 不可改）；合并后重走全量校验才落盘。"""
    changes = {k: v for k, v in changes.items() if k != "case_id"}
    if not changes:
        raise UiCaseError("invalid", "没有需要更新的字段")
    with _LOCK:
        data = _read(path)
        idx = next((i for i, c in enumerate(data["cases"]) if c.get("case_id") == case_id), None)
        if idx is None:
            raise UiCaseError("not_found", f"用例不存在: {case_id}")
        merged = {**data["cases"][idx], **changes}
        merged["case_id"] = case_id
        cleaned = validate_case(merged)
        data["cases"][idx] = cleaned
        _write(path, data)
    return cleaned


def delete_case(path: Path, case_id: str) -> None:
    with _LOCK:
        data = _read(path)
        before = len(data["cases"])
        data["cases"] = [c for c in data["cases"] if c.get("case_id") != case_id]
        if len(data["cases"]) == before:
            raise UiCaseError("not_found", f"用例不存在: {case_id}")
        _write(path, data)


def move_case(src_path: Path, dst_path: Path, case_id: str, new_module: str | None = None) -> dict:
    """跨 UI 项目迁移：先写目标（查重）→ 再从源删；源删失败回滚目标，保证不出现双份。"""
    if src_path == dst_path:
        raise UiCaseError("invalid", "源项目与目标项目相同，无需迁移")
    with _LOCK:
        src = _read(src_path)
        case = next((c for c in src["cases"] if c.get("case_id") == case_id), None)
        if case is None:
            raise UiCaseError("not_found", f"源项目中不存在用例: {case_id}")
        moved = dict(case)
        if (new_module or "").strip():
            moved["module"] = new_module.strip()
        moved = validate_case(moved)

        dst = _read(dst_path)
        if any(c.get("case_id") == case_id for c in dst["cases"]):
            raise UiCaseError("conflict", f"目标项目已存在同编号用例: {case_id}")
        dst["cases"].append(moved)
        _write(dst_path, dst)

        # 相位二：从源删除；失败回滚目标
        src["cases"] = [c for c in src["cases"] if c.get("case_id") != case_id]
        try:
            _write(src_path, src)
        except UiCaseError:
            rollback = _read(dst_path)
            rollback["cases"] = [c for c in rollback["cases"] if c.get("case_id") != case_id]
            _write(dst_path, rollback)
            raise
    return moved
