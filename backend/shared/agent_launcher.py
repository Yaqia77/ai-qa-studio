"""随平台后端拉起独立 Agent 服务（qa-agent）：开发机一键启动的最后一块拼图。

行为约定（config.yaml `agent` 段）：
- `enabled=true` 且 `auto_start=true` 才生效；8100 已有监听（手动起过 / start_all 起过）
  则跳过，绝不双实例抢端口；
- Agent 是附属服务：拉起失败只记日志，绝不影响平台后端自身启动；
- 子进程随平台后端退出（atexit terminate），不留孤儿占端口——
  此前"改了配置没生效"的元凶之一就是孤儿旧进程持着 8000/8100 的 socket。

路径与凭据全部取自本机开发环境惯例（主仓根 ./qa-agent + 其独立 venv；
agent_svc 密码存 logs/_svc_pw.txt，经环境变量注入，不落 Agent 配置文件）。
"""
from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import threading
from pathlib import Path

from loguru import logger

from core.context import load_config

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AGENT_DIR = ROOT / "qa-agent"
DEFAULT_PORT = 8100
_PW_FILE = ROOT / "logs" / "_svc_pw.txt"

_child: subprocess.Popen | None = None
_signal_hooked = False


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _agent_cfg() -> dict:
    return (load_config() or {}).get("agent") or {}


def _spawn_cmd(cfg: dict) -> tuple[list[str], Path, dict] | None:
    """组装子进程命令行 / 工作目录 / 环境变量；关键路径缺失返回 None 并说明原因。"""
    agent_dir = Path(cfg.get("dir") or DEFAULT_AGENT_DIR)
    if not agent_dir.is_absolute():
        agent_dir = ROOT / agent_dir   # 配置里的相对路径一律相对主仓根
    is_win = os.name == "nt"
    py = agent_dir / ".venv" / ("Scripts" if is_win else "bin") / ("python.exe" if is_win else "python")
    if not py.is_file() or not (agent_dir / "agent" / "main.py").is_file():
        logger.info(f"Agent 自动启动跳过：未找到 qa-agent 仓库或其独立 venv（{agent_dir}）")
        return None
    env = os.environ.copy()
    if _PW_FILE.is_file():
        env["QA_AGENT_PLATFORM_PASSWORD"] = _PW_FILE.read_text(encoding="utf-8").strip()
    port = int(cfg.get("port") or DEFAULT_PORT)
    cmd = [str(py), "-m", "uvicorn", "agent.main:app", "--host", "127.0.0.1", "--port", str(port)]
    return cmd, agent_dir, env


def _spawn(cmd: list[str], cwd: Path, env: dict):
    """真起子进程；Windows 下不弹控制台窗口，日志进文件句柄避免句柄泄漏。"""
    port = int((cmd[-1]))
    log_path = ROOT / "logs" / f"qa-agent_autostart_{port}.log"
    logf = open(log_path, "a", encoding="utf-8")  # noqa: SIM115（进程生命周期内持续写日志）
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=logf, stderr=logf,
                            creationflags=flags)


def start() -> None:
    """按配置尝试拉起 Agent；幂等，可被重复调用。"""
    global _child
    cfg = _agent_cfg()
    if not cfg.get("enabled") or not cfg.get("auto_start", True):
        return
    port = int(cfg.get("port") or DEFAULT_PORT)
    if _child is not None and _child.poll() is None:
        return
    if _port_listening(port):
        logger.info(f"Agent 服务已在 :{port} 监听，跳过自动启动")
        return
    spec = _spawn_cmd(cfg)
    if spec is None:
        return
    cmd, cwd, env = spec
    try:
        _child = _spawn(cmd, cwd, env)
    except OSError as e:
        logger.warning(f"Agent 服务自动启动失败（不影响平台功能）：{e!r}")
        return
    atexit.register(stop)
    _hook_sigterm()
    logger.info(f"Agent 服务已随后端自动启动（pid={_child.pid}，:{port}，日志 logs/qa-agent_autostart_{port}.log）")


def _hook_sigterm() -> None:
    """SIGTERM 也收子进程：atexit 只覆盖正常退出，--reload 重载/服务终止走信号，
    不挂这个钩子就会留下孤儿 Agent 占着端口（历史事故根源之一）。"""
    global _signal_hooked
    if _signal_hooked or threading.current_thread() is not threading.main_thread():
        return   # signal 只能主线程注册
    prev = signal.getsignal(signal.SIGTERM)
    if prev not in (signal.SIG_DFL, None):
        return   # 已有自定义处理器（如测试框架）：不抢钩子，交给持有者
    try:
        signal.signal(signal.SIGTERM, lambda *_: (_stop_child(), _reraise_default()))
    except (ValueError, OSError):
        return   # 该平台/运行环境不允许接管 SIGTERM（如 Windows 部分运行时）：仅靠 atexit
    _signal_hooked = True


def _reraise_default() -> None:
    """清理完恢复默认处置并重新发信号，不改变宿主进程的终止语义。"""
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)


def stop() -> None:
    """平台后端退出时收掉自己拉起的 Agent 子进程（外部起的实例不动）。"""
    global _child
    if _child is None:
        return
    child, _child = _child, None
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()


_stop_child = stop   # 信号处理器引用别名，避免钩子闭包里的 global 声明歧义


def child_pid() -> int | None:
    return _child.pid if _child is not None else None
