# -*- coding: utf-8 -*-
"""agent_launcher：后端自动带起 qa-agent 的行为契约。

锁三件事：开关组合决定是否尝试、端口已被占用绝不双起、拉起失败绝不影响后端启动。
真子进程一律 mock，不在 CI/单测里起任何服务。
"""
from __future__ import annotations

import socket

import pytest

from backend.shared import agent_launcher


@pytest.fixture(autouse=True)
def _reset_child():
    agent_launcher._child = None
    yield
    agent_launcher._child = None


class FakePopen:
    pid = 4321

    def __init__(self, rc=None):
        self._rc = rc
        self.terminated = False

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_开关关闭_不尝试启动(monkeypatch):
    monkeypatch.setattr(agent_launcher, "_agent_cfg", lambda: {"enabled": False, "auto_start": True})
    spawned = []
    monkeypatch.setattr(agent_launcher, "_spawn", lambda *a: spawned.append(a) or FakePopen())
    agent_launcher.start()
    assert spawned == []


def test_auto_start关闭_不尝试启动(monkeypatch):
    monkeypatch.setattr(agent_launcher, "_agent_cfg", lambda: {"enabled": True, "auto_start": False})
    spawned = []
    monkeypatch.setattr(agent_launcher, "_spawn", lambda *a: spawned.append(a) or FakePopen())
    agent_launcher.start()
    assert spawned == []


def test_端口已被占用_跳过双起(monkeypatch):
    monkeypatch.setattr(agent_launcher, "_agent_cfg", lambda: {"enabled": True, "auto_start": True, "port": 8100})
    monkeypatch.setattr(agent_launcher, "_port_listening", lambda port, host="127.0.0.1": True)
    spawned = []
    monkeypatch.setattr(agent_launcher, "_spawn", lambda *a: spawned.append(a) or FakePopen())
    agent_launcher.start()
    assert spawned == []
    assert agent_launcher._child is None


def test_正常路径_组装命令并记录子进程(monkeypatch):
    monkeypatch.setattr(agent_launcher, "_agent_cfg", lambda: {"enabled": True, "auto_start": True})
    monkeypatch.setattr(agent_launcher, "_port_listening", lambda port, host="127.0.0.1": False)
    fake = FakePopen()
    monkeypatch.setattr(agent_launcher, "_spawn", lambda cmd, cwd, env: fake)
    monkeypatch.setattr(agent_launcher, "_hook_sigterm", lambda: None)
    monkeypatch.setattr(agent_launcher, "_spawn_cmd",
                        lambda cfg: (["py", "-m", "uvicorn", "agent.main:app", "--host", "127.0.0.1", "--port", "8100"],
                                     agent_launcher.ROOT, {}))
    agent_launcher.start()
    assert agent_launcher._child is fake


def test_拉起失败_只记日志不抛出(monkeypatch):
    monkeypatch.setattr(agent_launcher, "_agent_cfg", lambda: {"enabled": True, "auto_start": True})
    monkeypatch.setattr(agent_launcher, "_port_listening", lambda port, host="127.0.0.1": False)
    monkeypatch.setattr(agent_launcher, "_spawn_cmd", lambda cfg: (["x"], agent_launcher.ROOT, {}))

    def _boom(*a):
        raise OSError("spawn failed")

    monkeypatch.setattr(agent_launcher, "_spawn", _boom)
    agent_launcher.start()          # 不抛异常即通过
    assert agent_launcher._child is None


def test_stop_只收自己拉起的孩子(monkeypatch):
    fake = FakePopen()
    agent_launcher._child = fake
    agent_launcher.stop()
    assert fake.terminated and agent_launcher._child is None
    agent_launcher.stop()           # 二次调用幂等，不炸


def test_port_listening_对真实监听套接字返回真():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        assert agent_launcher._port_listening(srv.getsockname()[1]) is True
    finally:
        srv.close()
    assert agent_launcher._port_listening(1) is False


def test_spawn_cmd_缺仓库或venv_返回None():
    cfg = {"dir": "不存在的目录"}
    assert agent_launcher._spawn_cmd(cfg) is None
