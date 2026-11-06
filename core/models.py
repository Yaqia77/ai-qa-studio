"""数据模型定义。"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TestCase:
    """一条接口用例（Excel「用例」表中的一行）。"""

    row: int                     # Excel 行号，用于报错定位
    source: str                  # 来源 Excel 文件名
    case_id: str
    module: str
    api_name: str
    case_desc: str
    priority: str                # P0~P5
    run: bool                    # 是否执行
    method: str
    url: str
    headers: dict | None = None
    params: dict | None = None
    body: Any = None             # dict / list / str / None
    extract: dict | None = None  # {"变量名": "$.json.path"}
    asserts: list | None = None  # [["eq", "$.code", 0], ...]
    timeout: float | None = None
    pre_scripts: list = field(default_factory=list)   # 前置公共脚本名（Excel 逗号分隔）
    post_scripts: list = field(default_factory=list)  # 后置公共脚本名


@dataclass
class AssertionResult:
    """单条断言的执行结果。"""

    op: str
    path: str
    expected: Any
    actual: Any
    passed: bool
    message: str = ""


@dataclass
class CaseResult:
    """单条用例的执行结果。"""

    case: TestCase
    ok: bool
    duration: float = 0.0
    request_snapshot: dict = field(default_factory=dict)
    response_snapshot: dict = field(default_factory=dict)
    assertions: list = field(default_factory=list)
    script_trace: list = field(default_factory=list)  # 公共脚本执行轨迹（前置/后置逐步结果）
    error: str = ""
