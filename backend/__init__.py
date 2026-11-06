"""FastAPI 后端包。

平台化封装现有 ai-qa-studio 多端测试框架：
- 查询类接口直接复用 core/（excel_loader / context）
- 执行类接口以子进程调用 main.py（零改写复用 pytest 编排，进程隔离）
"""
