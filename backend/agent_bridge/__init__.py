"""Agent 桥接层：只读语料导出 + 生成用例落库闸门。

平台侧为独立 Agent 服务（qa-agent）暴露的最小接口：
- 把「接口定义 + 存量用例」组织成知识库语料（D1 契约域 / D2 用例域）供 Agent 增量拉取；
- `case_gate` 提供 preview（权威校验不落盘）+ commit（人确认后原子写 Excel）。

边界纪律（设计文档 3.2 / 4.5 / 五章）：
- 读走只读语料，写必须过权威校验闸门：`case.preview`/`case.commit` 都以 `core.excel_loader`
  为唯一校验口径，且需 `case:write`；Agent 产出不自动落库，人确认后才写；
- 不导出密钥：语料里只有 `{{var}}` 变量引用，`secret_vars.xlsx` 的真实值永不进语料；
- 默认关闭：`config.yaml` 的 `agent.enabled` 缺省 false，端点不注册，存量行为零回归。
"""
