/** 与后端 OpenAPI 契约对齐的类型（backend/schemas.py）。 */

export interface CaseItem {
  case_id: string;
  module: string;
  api_name: string;
  case_desc: string;
  priority: string;
  run: boolean;
  method: string;
  url: string;
  headers: Record<string, unknown> | null;
  params: Record<string, unknown> | null;
  body: unknown;
  extract: Record<string, string> | null;
  asserts: string[][] | null;
  timeout: number | null;
  pre_scripts: string[];
  post_scripts: string[];
  source: string;
  row: number;
}

export interface CasePage {
  total: number;
  items: CaseItem[];
}

/** UI 用例单步（编辑态，与后端 UiStep 对齐） */
export interface UiStep {
  action: string;
  locator?: string | null;
  value?: unknown;
  desc?: string;
}

/** UI 变量提取（source: text|<locator> / input_value|<locator> / cookie:<name> / url） */
export interface UiExtract {
  var: string;
  source: string;
}

/** 一条 UI 用例（/api/ui-cases，与后端 UiCaseOut 对齐） */
export interface UiCaseItem {
  case_id: string;
  module: string;
  name: string;
  desc: string;
  priority: string;
  run: boolean;
  browser: string;
  headless: boolean;
  steps: UiStep[];
  extract: UiExtract[];
  pre_scripts: string[];
  post_scripts: string[];
  /** 引用的登录态会话 id（开机加载其 storage_state）；null/缺省=不引用 */
  session?: string | null;
  /** 失败重试次数 0–3（三期-B；任一次通过即判通过，缺省=0 不重试） */
  retry?: number;
}

export interface UiCasePage {
  total: number;
  items: UiCaseItem[];
}

/** UI 步骤编辑器元数据（/api/ui-cases/meta）：动作字典驱动下拉 */
export interface UiActionMeta {
  action: string;
  desc: string;
  need_locator: boolean;
  need_value: boolean;
  /** 定位器/取值填写说明（编辑器对应输入框 placeholder），可缺省 */
  hint?: string;
  /** 下拉分组：常用（新手优先）/ 高级；缺省按高级归类 */
  group?: string;
}

export interface UiCaseMeta {
  priorities: string[];
  browsers: string[];
  modules: string[];
  actions: UiActionMeta[];
}

/** UI Excel 一步一行上传解析预览（/api/ui-cases/preview，不落盘） */
export interface UiImportPreviewItem {
  row: number;
  case_id: string;
  name: string;
  steps_count: number;
  case: Record<string, unknown>;
  marks: ImportMark[];
  importable: boolean;
  exists: boolean;
}

export interface UiImportPreviewResp {
  items: UiImportPreviewItem[];
  unmapped_headers: string[];
  total: number;
  error?: string;
}

/** UI 登录态会话派生状态（二期 storage_state 复用） */
export type UiSessionStatusKind = "not_collected" | "valid" | "expired" | "failed";

/** 一条登录态会话（/api/ui-sessions，注册表元数据 + 派生状态；不含 storage_state 实体） */
export interface UiSession {
  id: string;
  name: string;
  desc: string;
  env: string;
  browser: string;
  headless: boolean;
  file: string;
  capture_case: string;
  ttl_seconds: number;
  created_at?: string | null;
  captured_at?: string | null;
  last_error?: string | null;
  /** 列表视图附带（GET /api/ui-sessions）；详情/状态端点各自返回 */
  status?: UiSessionStatusKind;
  remaining_seconds?: number | null;
}

/** 单查状态（/api/ui-sessions/{id}/status） */
export interface UiSessionStatus {
  id: string;
  status: UiSessionStatusKind;
  captured_at?: string | null;
  ttl_seconds: number;
  remaining_seconds?: number | null;
  last_error?: string | null;
}

/** 采集返回（POST /api/ui-sessions/{id}/collect） */
export interface UiSessionCollectResult {
  ok: boolean;
  error: string | null;
  id: string;
  status: UiSessionStatusKind;
  captured_at?: string | null;
  remaining_seconds?: number | null;
}

/** 新建/编辑会话下拉候选（/api/ui-sessions/meta） */
export interface UiSessionMeta {
  envs: string[];
  browsers: string[];
  ui_projects: { file: string; name: string; cases: { case_id: string; name: string }[] }[];
}

/** App 变量提取（与 UiExtract 同构，source 面向移动端） */
export interface AppExtract {
  var: string;
  source: string;
}

/** 一条 App 用例（/api/app-cases，与后端 AppCaseOut 对齐；browser→platform，加 app 包名） */
export interface AppCaseItem {
  case_id: string;
  module: string;
  name: string;
  desc: string;
  priority: string;
  run: boolean;
  platform: string;
  app: string;
  steps: UiStep[];
  extract: AppExtract[];
  pre_scripts: string[];
  post_scripts: string[];
}

export interface AppCasePage {
  total: number;
  items: AppCaseItem[];
}

/** App 步骤编辑器元数据（/api/app-cases/meta）：动作字典与 UI 同构 */
export interface AppCaseMeta {
  priorities: string[];
  platforms: string[];
  modules: string[];
  actions: UiActionMeta[];
}

/** 用例项目（一个业务系统 = 一个 Excel 用例文件）：/api/projects */
export interface Project {
  id: string;
  name: string;
  desc: string;
  /** 相对项目根的用例文件路径（接口 xlsx / UI json）；前端据此把 file 贯穿所有用例接口 */
  file: string;
  /** 项目类型：api=接口用例(Excel) | ui=UI用例(JSON) | app=App用例(.apjson)，创建后不可改 */
  kind: "api" | "ui" | "app";
  builtin: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  case_count: number;
}

/** 智能导入：转换后的用例草稿（与 CaseCreate 请求体同构，字段可能缺省走默认值） */
export interface ImportCaseDraft {
  case_id: string;
  module: string;
  api_name: string;
  case_desc: string;
  priority: string;
  run: boolean;
  method: string;
  url: string;
  headers?: Record<string, unknown> | null;
  params?: Record<string, unknown> | null;
  body?: unknown;
  extract?: Record<string, string> | null;
  asserts?: unknown[][] | null;
  timeout?: number | null;
  pre_scripts?: string[];
  post_scripts?: string[];
}

/** 智能导入：单行转换标注（warn=需确认，error=不可导入） */
export interface ImportMark {
  level: "info" | "warn" | "error";
  field: string;
  msg: string;
}

export interface ImportPreviewItem {
  row: number;
  case: ImportCaseDraft;
  marks: ImportMark[];
  importable: boolean;
  exists: boolean;
}

export interface ImportPreviewResp {
  sheet: string;
  format: "standard" | "mapped";
  column_map: { header: string; field: string }[];
  unmapped_headers: string[];
  items: ImportPreviewItem[];
}

export interface RunItem {
  run_id: string;
  status: "pending" | "running" | "passed" | "failed" | "error" | "unknown";
  source: "api" | "cli";
  args: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  report_dir: string | null;
  log_tail: string | null;
}

export interface EnvInfo {
  name: string;
  base_url: string | null;
  timeout?: number | null;
  verify_ssl?: boolean;
  default_headers?: Record<string, string>;
}

export interface EnvsResp {
  default_env: string | null;
  envs: EnvInfo[];
  secret_vars_file?: string | null;
  report?: Record<string, boolean>;
  log?: Record<string, string>;
}

/** PUT /api/envs 请求体：整体覆盖式保存环境段 */
export interface EnvsUpdate {
  default_env: string;
  envs: EnvInfo[];
}

/** 编辑用元数据（/api/cases/meta）：下拉候选统一来源 */
export interface AssertOpInfo {
  op: string;
  desc: string;
  need_expected: boolean;
}

export interface HeaderSuggest {
  key: string;
  value: string;
  used: number;
}

export interface CaseMeta {
  priorities: string[];
  methods: string[];
  assert_ops: AssertOpInfo[];
  modules: string[];
  header_suggest: HeaderSuggest[];
  /** 存量用例出现过的接口名（接口定义库另由 /api/defs 提供带断言建议的那一份） */
  api_names?: string[];
  variables: string[];
}

/** 批次用例结果摘要（/api/runs/{id}/cases，后端已掩码） */
export interface RunCaseSummary {
  case_id: string;
  name: string;
  priority: string;
  status: "passed" | "failed" | "broken" | "skipped" | string;
  duration_ms: number;
  error: string;
  summary: string;
  assert_total: number | null;
  assert_failed: number | null;
  /** 六期 flaky 观测：实际尝试次数（缺省 1=未重试）；retried=发生过重试；flaky=重试后通过（不稳定） */
  attempts?: number;
  retried?: boolean;
  flaky?: boolean;
}

/** 单条断言执行结果（报告详情“断言结果”Tab） */
export interface AssertResult {
  op: string;
  path: string;
  expected: unknown;
  actual: unknown;
  passed: boolean;
  message: string;
}

export interface RunCasesResp {
  run_id: string;
  total: number;
  stats: Record<string, number>;
  items: RunCaseSummary[];
}

export interface RunCaseDetail {
  case_id: string;
  name: string;
  status: string;
  description: string;
  error: string;
  trace: string;
  request: Record<string, unknown> | null;
  response: Record<string, unknown> | null;
  assertions: AssertResult[] | null;
  /** 前后置公共脚本执行轨迹（无脚本时为空数组） */
  scripts: ScriptTraceItem[];
  /** UI 用例步骤轨迹（接口用例为 null）；截图经 /api/runs/{id}/attachment/{screenshot_source} 取图 */
  ui_steps: UiStepTrace[] | null;
  /** UI 重试轨迹（三期-B）；未发生重试/非 UI 用例为 null */
  ui_retry?: UiRetryTrace | null;
  /** 六期失败 trace zip 在批次 allure-results 中的源文件名（报告页下载 show-trace 用）；非 UI/无 trace 为 null */
  trace_file?: string | null;
}

/** UI 重试单次失败摘要（后端已掩码） */
export interface UiRetryAttemptInfo {
  attempt: number;
  ok: boolean;
  error: string | null;
  duration_ms: number;
  steps_failed: number;
}

/** UI 重试轨迹汇总（「UI重试轨迹」附件解析结果） */
export interface UiRetryTrace {
  attempts: number;
  final_ok: boolean;
  history: UiRetryAttemptInfo[];
}

/** UI 单步执行轨迹（core.ui_runner 落「UI步骤」附件，后端已掩码） */
export interface UiStepTrace {
  index: number;
  action: string;
  action_desc: string;
  locator: string | null;
  value: string;
  desc: string;
  ok: boolean;
  duration_ms: number;
  error: string | null;
  screenshot: string | null;
  screenshot_source?: string | null;
}

/** 脚本单步执行轨迹（敏感变量值由后端掩码后才落报告） */
export interface ScriptTraceItem {
  script: string;
  phase: string;      // 前置 | 后置
  index: number;
  action: string;
  desc: string;
  ok: boolean;
  detail: string;
}

/** 凭证变量（/api/secrets）：后端仅回掩码值，绝不透传明文 */
export interface SecretVar {
  name: string;
  masked_value: string;
  length: number;
  empty: boolean;
  kind: string;      // 登录态/凭证 | 密文 | 固定值
  remark: string;
}

export interface SecretsResp {
  exists: boolean;
  items: SecretVar[];
}

/** 单用例跨批次执行历史（/api/runs/history/{case_id}） */
export interface CaseHistoryEntry {
  run_id: string;
  source: "api" | "cli" | string;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  duration_ms: number;
  summary: string;
  error: string;
}
export interface CaseHistoryResp {
  case_id: string;
  scanned_batches: number;
  found: number;
  passed: number;
  failed: number;
  consec_fail: number;
  history: CaseHistoryEntry[];
}

/** 测试场景（/api/plans） */
export interface PlanItem {
  id: string;
  name: string;
  desc: string;
  case_ids: string[];
  env: string;
  created_at: string;
  updated_at: string;
  last_run_id: string | null;
  last_run_at: string | null;
}

export interface PlanCreateReq {
  name: string;
  desc?: string;
  case_ids: string[];
  env?: string;
}

export interface PlanUpdateReq {
  name?: string;
  desc?: string;
  case_ids?: string[];
  env?: string;
}

/** Webhook 通知配置（/api/webhooks） */
export interface WebhookItem {
  id: string;
  name: string;
  url: string;
  type: "wechat_work" | "feishu" | "custom";
  events: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface WebhookCreateReq {
  name: string;
  url: string;
  type?: string;
  events?: string[];
  enabled?: boolean;
}

export interface WebhookUpdateReq {
  name?: string;
  url?: string;
  type?: string;
  events?: string[];
  enabled?: boolean;
}

/** 定时任务（/api/schedules） */
export interface ScheduleItem {
  id: string;
  name: string;
  cron: string;
  plan_id: string;
  run_config: Record<string, unknown>;
  enabled: boolean;
  last_run_id: string | null;
  last_run_at: string | null;
  next_run_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ScheduleCreateReq {
  name: string;
  cron: string;
  plan_id?: string;
  run_config?: Record<string, unknown>;
  enabled?: boolean;
}

export interface ScheduleUpdateReq {
  name?: string;
  cron?: string;
  plan_id?: string;
  run_config?: Record<string, unknown>;
  enabled?: boolean;
}

/** 用户与权限（/api/auth）：后端绝不下发密码字段 */
export interface UserInfo {
  username: string;
  display_name: string;
  role: "admin" | "tl" | "qa" | "dev" | "viewer" | string;
  role_name: string;
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
  permissions: string[];
}

export interface LoginResp {
  token: string;
  user: UserInfo;
}

export interface UserCreateReq {
  username: string;
  password: string;
  display_name?: string;
  role: string;
}

export interface UserUpdateReq {
  display_name?: string;
  role?: string;
  enabled?: boolean;
  password?: string;
}

/** 操作审计留痕（/api/audit）：谁在什么时候改了什么 · 结果如何 */
export interface AuditItem {
  id: string;
  ts: string;
  username: string;
  display_name: string;
  role: string;
  method: string;
  path: string;
  action: string;
  target: string;
  detail: string;
  query: string;
  status: number;
  ok: boolean;
  ip: string;
}

export interface AuditLogsResp {
  total: number;
  returned: number;
  items: AuditItem[];
}

/** 角色权限矩阵（/api/auth/permissions，设计文档 8.2） */
export interface PermRoleOption {
  value: string;
  label: string;
}

export interface PermCatalogItem {
  code: string;
  name: string;
  desc: string;
  roles: string[];
}

export interface PermMatrixResp {
  roles: PermRoleOption[];
  catalog: PermCatalogItem[];
  matrix: Record<string, string[]>;
  defaults: Record<string, string[]>;
  rules: { locked_role: Record<string, string>; must_nonempty: string[] };
}

export interface PermSaveResp {
  matrix: Record<string, string[]>;
  changed: string[];
  actor_permissions: string[];
}

/** 公共脚本（设计文档 2.4）：声明式步骤链，动作白名单由 /api/scripts/meta 下发 */
export interface ScriptStep {
  action: string;
  desc?: string;
  /* request */
  method?: string;
  url?: string;
  headers?: Record<string, unknown>;
  params?: Record<string, unknown>;
  body?: unknown;
  extract?: Record<string, string>;
  assert?: unknown[][];
  timeout?: number;
  /* sleep */
  seconds?: number;
  /* set_var */
  vars?: Record<string, unknown>;
}

export interface ScriptItem {
  name: string;
  remark: string;
  steps: ScriptStep[];
  used_by: string[];
  updated_at: string;
}

export interface ScriptCreateReq {
  name: string;
  remark?: string;
  steps: ScriptStep[];
}

export interface ScriptUpdateReq {
  name?: string;
  remark?: string;
  steps?: ScriptStep[];
}

export interface ScriptActionMeta {
  action: string;
  desc: string;
  fields: string[];
}

/** 步骤编辑器元数据：表单字段与后端校验同一份来源，杜绝口径漂移 */
export interface ScriptMeta {
  actions: ScriptActionMeta[];
  step_limit: number;
  sleep_limit: number;
  name_max: number;
  name_forbidden: string;
  methods: string[];
  assert_ops: string[];
  ops_need_expected: string[];
  path_options: { path: string; desc: string }[];
  variables: string[];
}

/**
 * 接口定义（设计文档 2.2）：接口库元数据。
 * 用例仍自带完整请求要素，删定义不影响执行；used_by 为接口名称同名的用例编号。
 */
export interface ApiDefItem {
  name: string;
  module: string;
  method: string;
  path: string;
  remark: string;
  headers: Record<string, unknown> | null;
  params: Record<string, unknown> | null;
  body: unknown;
  extract: Record<string, string> | null;
  asserts: unknown[][] | null;
  source: string;      // manual | excel | openapi
  used_by: string[];
  updated_at: string;
}

export interface ApiDefCreateReq {
  name: string;
  method: string;
  path: string;
  module?: string;
  remark?: string;
  headers?: Record<string, unknown> | null;
  params?: Record<string, unknown> | null;
  body?: unknown;
  extract?: Record<string, string> | null;
  asserts?: unknown[][] | null;
}

/** 更新：只传改动字段；headers/params/body/extract/asserts 传空对象/空数组即清空 */
export type ApiDefUpdateReq = Partial<ApiDefCreateReq>;

/** 接口编辑元数据（/api/defs/meta），常量与后端同源 */
export interface ApiDefMeta {
  methods: string[];
  assert_ops: AssertOpInfo[];
  ops_need_expected: string[];
  path_options: { path: string; desc: string }[];
  default_asserts: unknown[][];
  modules: string[];
  name_max: number;
  module_max: number;
  remark_max: number;
  assert_max: number;
  sources: { value: string; label: string }[];
  spec_limit_mb: number;
  variables: string[];
  def_count: number;
}

/** 从存量用例聚合的结果 */
export interface AggregateResult {
  file: string;
  scanned_cases: number;
  groups: number;
  created: number;
  updated: number;
  skipped: number;
  conflicts: string[];
  rejected: string[];   // 接口名不合规（如含 /）被跳过的分组
}

/** OpenAPI/Swagger 导入结果 */
export interface OpenApiImportResult {
  parsed: number;
  created: number;
  updated: number;
  skipped: number;
  conflicts: string[];
  skipped_notes: string[];
  note_count: number;
}

/** 由接口定义生成用例骨架（默认 dry_run 只预览） */
export interface SkeletonReq {
  case_ids: string[];
  case_desc?: string;
  priority?: string;
  run?: boolean;
  dry_run?: boolean;
}

export interface SkeletonCase {
  case_id: string;
  api_name: string;
  module: string;
  case_desc: string;
  priority: string;
  run: boolean;
  method: string;
  url: string;
  asserts: unknown[][];
  headers?: Record<string, unknown>;
  params?: Record<string, unknown>;
  extract?: Record<string, string>;
  body?: unknown;
}

export interface SkeletonResult {
  dry_run: boolean;
  file: string;
  cases: SkeletonCase[];
  written: string[];
  errors: string[];
}

/* ================= AI 生成（qa-agent 直连 + 平台落库闸门，Agent 接入一期 M4） ================= */

/** POST /v1/generations 请求（对齐 qa-agent GenerationIn；dry_run 缺省 true 恒不落库） */
export interface GenerationReq {
  target: string;
  mode?: "generate" | "complete" | "expand";
  platform?: string;
  project_id?: string;
  intent?: string;
  count?: number;
  dry_run?: boolean;
}

/** 闸门逐条结果（Agent 预检） */
export interface GenerationValidationItem {
  case_id: string;
  passed: boolean;
  problems: string[];
}

/** 校验闸门报告；authoritative=false 表示仅结构预检（平台离线，产出仅供建议） */
export interface GenerationValidation {
  checked: number;
  passed: number;
  failed: number;
  items: GenerationValidationItem[];
  authoritative: boolean;
  degraded?: boolean;
  note?: string;
  platform?: string;
}

/** 知识引用（每条生成用例都要能点开看依据） */
export interface GenerationCitation {
  kb_id: string;
  source_uri: string;
  domain: string;
  title?: string;
  score?: number;
}

/** POST /v1/generations 响应（qa-agent GenerationOut）；cases 列名对齐 Excel 用例表 */
export interface GenerationOut {
  ok: boolean;
  mode: string;
  cases: Record<string, unknown>[];
  validation: GenerationValidation;
  errors: string[];
  citations: GenerationCitation[];
  degraded: boolean;
  trace_id: string;
}

/** 平台权威校验预览 POST /api/agent/cases/preview（不落盘） */
export interface AgentPreviewItem {
  case_id: string;
  passed: boolean;
  exists: boolean;
  problems: string[];
}

export interface AgentPreviewResp {
  platform: string;
  file: string;
  checked: number;
  passed: number;
  failed: number;
  items: AgentPreviewItem[];
  summary: string;
}

/** 确认落库 POST /api/agent/cases/commit（整批先验后写，任一非法即全拒） */
export interface AgentCommitResp {
  platform: string;
  file: string;
  committed: number;
  skipped: number;
  errors: string[];
  summary: string;
}

/** preview/commit 共用的请求体 */
export interface AgentCasesBody {
  cases: Record<string, unknown>[];
  project_id?: string;
  file?: string;
}
