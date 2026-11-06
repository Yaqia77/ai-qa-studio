# -*- coding: utf-8 -*-
"""引擎注册表：三合一统一架构的分流入口。

用法：`engine_for_file(path)` 返回负责该用例文件的引擎（按后缀判定，与 main.py 一致），
`get_engine(key)` 按标识取引擎。新增引擎（如 app）只需实现 Engine 子类并在此登记。
"""
from __future__ import annotations

from pathlib import Path

from .api.engine import ApiEngine
from .app.engine import AppEngine
from .base import Engine
from .web.engine import WebEngine

__all__ = ["Engine", "ApiEngine", "WebEngine", "AppEngine", "REGISTRY", "get_engine", "engine_for_file"]

REGISTRY: tuple[Engine, ...] = (ApiEngine(), WebEngine(), AppEngine())
_BY_KEY: dict[str, Engine] = {e.key: e for e in REGISTRY}


def get_engine(key: str) -> Engine:
    """按引擎标识取引擎；未注册抛 KeyError（配置错误应显式暴露，而非静默回落）。"""
    return _BY_KEY[key]


def engine_for_file(path: str | Path) -> Engine | None:
    """返回第一个认领该用例文件的引擎，无认领返回 None。"""
    p = Path(path)
    for engine in REGISTRY:
        if engine.matches(p):
            return engine
    return None
