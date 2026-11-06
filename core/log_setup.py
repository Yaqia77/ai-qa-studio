# -*- coding: utf-8 -*-
"""运行时日志配置（共享）：控制台 + logs/ 固定文件。

原属 core.runner，因 api/web 两条链路都要用（tests/conftest 的 app_config fixture 统一初始化），
故提到 core 顶层共享位置，避免 conftest 反向依赖某个具体引擎包。

并行化改造：xdist controller 与各 worker 是独立进程、并发写日志。旧实现
「{time} 时间戳文件名 + retention=10」在两类场景致命：
- 每个进程 add sink 时 loguru 会按同 pattern glob+stat+删除旧文件：多进程并发下是
  TOCTOU 竞态，且可能删掉**其他进程正打开的文件**（Windows 表现为
  FileNotFoundError 直接把 worker 打成 ERROR）；
- 「每进程一个新时间戳文件」把上述竞态窗口成倍放大。
改为「全进程共享一个固定文件」：enqueue=True（loguru 官方多进程安全姿势，队列线程落盘），
不设 rotation/retention（任何清理触发都是跨进程不安全的）。文件追加增长、logs/ 属可再生内容。
"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent  # 项目根（core/ 上一级）

_RUNTIME_FILE = "interface-auto_runtime.log"
_INITIALIZED = False


def runtime_log_path() -> Path:
    """执行日志共享文件路径（单点事实来源：冒烟按此路径读增量窗口）。"""
    return ROOT / "logs" / _RUNTIME_FILE


def setup_logging(level: str = "INFO") -> None:
    """配置 loguru：控制台 + logs/ 固定文件（全进程共享）。同进程幂等，重复调用不重建 sink。"""
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True
    logger.remove()
    logger.add(sys.stderr, level=level)
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    logger.add(
        str(log_dir / _RUNTIME_FILE),
        level=level,
        encoding="utf-8",
        enqueue=True,  # 队列落盘：多线程/多进程并发安全，进程崩溃收尾也不丢已发日志
    )
