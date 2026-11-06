# -*- coding: utf-8 -*-
"""引擎基类：三合一统一自动化架构的底座。

底座只做两件事，且**零行为变更**：
1. 把接口(Web 之外的 api)与 UI(web)两个引擎代码块分别收进 core/engines/{api,web}/；
2. 用一个注册表取代 main.py 里硬编码的"按文件后缀分流"，分流映射结果与原来完全一致。

引擎在此承担"身份 + 分流 + 统一入口"职责：matches/test_module 负责分流，
validate/execute 把接口(api)与 UI(web)各自的校验、执行入口对齐到同一签名，供上层
（main.py 按 key 分发、用例执行链）以多态方式统一调用。

统一入口仅“就位委派封装”，仍**零行为变更**：execute 薄封装委托既有 CaseRunner/UiRunner，
validate 薄封装委托既有单用例校验（api=excel_loader 行解析、web=ui_case_store.validate_case），
落库/报告/依赖编排等结果与重构前完全一致。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class Engine(ABC):
    """一个自动化引擎的元数据与分流判据。子类只需声明 key/label/test_module 并实现 matches。"""

    key: str = ""                 # 引擎标识：api / web / app
    label: str = ""               # 中文展示名
    test_module: str = ""         # 相对项目根的 pytest 收集入口，如 tests/test_api.py

    @abstractmethod
    def matches(self, path: Path) -> bool:
        """该引擎是否负责这个用例文件（按文件后缀判定，与 main.py 分流口径一致）。"""
        raise NotImplementedError

    @abstractmethod
    def validate(self, raw, **deps):
        """校验并归一化一条本引擎原生用例，非法即抛异常，返回可执行形态。

        原生入参按引擎而异：api=Excel「用例」表一行的 {字段: 原值} dict，web=UI 用例 JSON dict。
        统一的是"给定单条原生用例 → 归一化/校验"这一入口语义与调用形状。
        """
        raise NotImplementedError

    @abstractmethod
    def execute(self, case, ctx, **deps):
        """执行一条已就绪用例，返回引擎结果对象（api=CaseResult / web=UiRunResult）。

        统一位置参数 (case, ctx)；引擎特有依赖（api 需 executor，web 需 base_url 及 allure
        回调/会话 provider 等）经 **deps 关键字透传，保持签名一致。委托各自 runner，行为不变。
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Engine {self.key} -> {self.test_module}>"
