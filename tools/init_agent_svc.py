# -*- coding: utf-8 -*-
"""初始化 Agent 服务账号 agent_svc（设计文档 5.3：只读 + case:write）。

为什么单独一个脚本而不是播种：播种账号密码写在源码里（admin123 等）是 MVP 妥协，
服务账号绝不能再走这条路——密码只能随机生成或操作者显式传入，只在生成那一刻打印。

做两件事（均幂等，可重复执行）：
1. 权限矩阵：确保 case:write 授予 agent_svc（落 data/permissions.json）；
2. 账号：确保 users.json 存在 agent_svc（角色 agent_svc，只读靠登录态）。

密码填写位置（脚本跑完会再提示一遍）：
- qa-agent 侧：环境变量 QA_AGENT_PLATFORM_PASSWORD（config.py 密钥唯一入口，不写 yaml）；
- interface-auto 侧：config.yaml agent.account.username 已写死 agent_svc，平台不需要
  持有该密码（平台是被登录方，验证哈希即可）。

用法：
    .venv\\Scripts\\python.exe tools\\init_agent_svc.py                 # 随机密码并打印一次
    .venv\\Scripts\\python.exe tools\\init_agent_svc.py --password xxx  # 指定密码
    .venv\\Scripts\\python.exe tools\\init_agent_svc.py --reset-password --password xxx  # 改密
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))   # 支持 python tools\init_agent_svc.py 直接执行

from backend.shared import perm_store, user_store  # noqa: E402

USERNAME = "agent_svc"


def ensure_perm_matrix() -> bool:
    """case:write 未含 agent_svc 时补上并落盘；返回是否发生修改。"""
    matrix = perm_store.matrix()
    if USERNAME in matrix.get("case:write", []):
        return False
    matrix["case:write"] = list(matrix.get("case:write", [])) + [USERNAME]
    perm_store.save(matrix)   # 走完整校验与护栏，不合规宁可拒绝落盘
    return True


def ensure_user(password: str | None, reset: bool) -> tuple[str, str]:
    """返回 (动作, 密码)。动作 created/existing/reset；created/reset 时密码为新值。"""
    user = user_store.get_user(USERNAME)
    if user is None:
        pwd = password or secrets.token_urlsafe(15)
        user_store.create_user(USERNAME, pwd, "Agent 服务账号", USERNAME)
        return "created", pwd
    if not reset:
        return "existing", ""
    if not password:
        password = secrets.token_urlsafe(15)
    user_store.update_user(USERNAME, password=password)
    return "reset", password


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="初始化/校验 Agent 服务账号 agent_svc")
    ap.add_argument("--password", default="", help="指定密码（缺省随机生成，仅打印一次）")
    ap.add_argument("--reset-password", action="store_true", help="已存在时强制改密")
    args = ap.parse_args(argv)

    changed = ensure_perm_matrix()
    print(f"权限矩阵 case:write: {'已加入 agent_svc' if changed else '已包含 agent_svc（跳过）'}")

    action, pwd = ensure_user(args.password or None, args.reset_password)
    if action in ("created", "reset"):
        print(f"账号 {USERNAME}: {'已创建' if action == 'created' else '已重置密码'}，密码（只显示这一次）: {pwd}")
    else:
        print(f"账号 {USERNAME}: 已存在，密码未改动")

    print("\n凭证填写位置：")
    print("  1) qa-agent 启动前设置环境变量:  $env:QA_AGENT_PLATFORM_PASSWORD = \"<上面的密码>\"")
    print("     （qa-agent config.yaml 的 platforms.interface-auto.username 已是 agent_svc，密码不落任何文件）")
    print("  2) interface-auto config.yaml:  agent.enabled 置 true 后重启后端，桥接端点即生效")
    print("  3) 校验:  cd qa-agent && python tools\\smoke_agent.py（或平台『角色权限』页查看分配）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
