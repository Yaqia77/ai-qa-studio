# -*- coding: utf-8 -*-
"""App（移动端）自动化引擎（当前为脚手架，预留位）。

对齐统一引擎契约（core.engines.base.Engine）：key/label/test_module + matches/validate/execute。
- matches：认领 `.apjson` 载体（与 web 的 `.json`、api 的其余后缀互斥，见 core.engines 注册表）；
- validate：薄封装委托 backend.app.app_case_store.validate_case，与 web 引擎同一口径；
- execute：驱动（Appium 等）尚未接入，返回 AppRunResult(ok=False) 并给出中文指引，
  绝不抛异常，保证收集/执行链路（tests/test_app.py）可跑通且不误判为崩溃。

接入真实驱动时只需替换 AppRunner.run 的实现，其余（Schema/注册/前端）保持不变。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.engines.base import Engine


@dataclass
class AppRunResult:
    case_id: str
    ok: bool = False
    error: Optional[str] = None
    steps: list[dict] = field(default_factory=list)
    duration: float = 0.0
    extracted: dict = field(default_factory=dict)


# 驱动未接入时的统一提示（execute 与 test_app 收集端共用，口径一致）
NOT_IMPLEMENTED = "App 自动化驱动尚未接入（脚手架阶段，用例已收集；接入 Appium 后可执行）"


class AppRunner:
    """App 用例执行器骨架：当前不驱动真机/模拟器，仅归位结果对象。

    与 UiRunner/CaseRunner 同构（run 不抛异常，结果写进 AppRunResult）。真实驱动接入后
    在此实现"连接设备 → 逐步执行 → 变量提取 → 断言"，保持返回契约不变。
    """

    def __init__(self, ctx, base_url: str = "", **deps):
        self.ctx = ctx
        self.base_url = base_url
        self.deps = deps

    def run(self, case: dict) -> AppRunResult:
        start = time.perf_counter()
        result = AppRunResult(case_id=str(case.get("case_id", "")), ok=False, error=NOT_IMPLEMENTED)
        result.duration = time.perf_counter() - start
        return result


class AppEngine(Engine):
    """App 自动化引擎的注册表入口（身份 + 分流 + 统一校验/执行入口）。"""

    key = "app"
    label = "App 自动化"
    test_module = str(Path("tests") / "test_app.py")

    def matches(self, path: Path) -> bool:
        return path.suffix.lower() == ".apjson"

    def validate(self, raw, **deps):
        """校验并归一化一条 App 用例（JSON dict）→ 清洗后 dict；非法抛 AppCaseError。

        懒导入 backend.app.app_case_store（core 不强依赖 backend），与 web 引擎同一口径。
        """
        from backend.app.app_case_store import validate_case  # 单一口径来源
        return validate_case(raw)

    def execute(self, case: dict, ctx, *, base_url: str = "", **deps) -> AppRunResult:
        """执行一条 App 用例：驱动未接入即返回未实现结果（不抛异常，行为可预期）。"""
        return AppRunner(ctx, base_url, **deps).run(case)
