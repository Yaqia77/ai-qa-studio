/** Axios 封装 + 接口函数（开发期经 Vite 代理 /api → FastAPI:8000）。 */
import axios from "axios";
import { ElMessage } from "element-plus";
import type { CaseItem, CasePage, CaseMeta, UiCaseItem, UiCasePage, UiCaseMeta, UiSession, UiSessionStatus, UiSessionCollectResult, UiSessionMeta, AppCaseItem, AppCasePage, AppCaseMeta, CaseHistoryResp, EnvsResp, EnvsUpdate, RunCasesResp, RunCaseDetail, RunItem, SecretsResp, PlanItem, PlanCreateReq, PlanUpdateReq, WebhookItem, WebhookCreateReq, WebhookUpdateReq, ScheduleItem, ScheduleCreateReq, ScheduleUpdateReq, LoginResp, UserInfo, UserCreateReq, UserUpdateReq, AuditItem, AuditLogsResp, PermMatrixResp, PermSaveResp, ScriptItem, ScriptCreateReq, ScriptUpdateReq, ScriptMeta, ApiDefItem, ApiDefCreateReq, ApiDefUpdateReq, ApiDefMeta, AggregateResult, OpenApiImportResult, SkeletonReq, SkeletonResult, ImportPreviewResp, ImportCaseDraft, Project, UiImportPreviewResp, GenerationReq, GenerationOut, AgentPreviewResp, AgentCommitResp, AgentCasesBody, ChatSessionInfo, ChatSessionDetail } from "../types";
import { clearToken, getToken } from "../utils/session";
import { friendlyAgentError, isUserReadable } from "../utils/errors";

const http = axios.create({ baseURL: "/api", timeout: 30000 });

// 请求拦截：统一注入登录态
http.interceptors.request.use((cfg) => {
  const token = getToken();
  if (token) cfg.headers.Authorization = `Bearer ${token}`;
  return cfg;
});

http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    // 401：登录态失效，清 token 后硬跳登录页（避开在登录页本身死循环）
    if (error.response?.status === 401) {
      clearToken();
      if (window.location.pathname !== "/login") {
        ElMessage.warning("登录已过期，请重新登录");
        window.location.assign("/login");
      } else {
        // 登录页自身的 401（用户名或密码错误）：拦截器原先静默放行，页面上毫无提示，
        // 这里补展示后端 detail，登录页再叠加卡内红色告警，双重可见
        const d = error.response?.data?.detail;
        if (d) ElMessage.error(typeof d === "string" ? d : JSON.stringify(d));
      }
      return Promise.reject(error);
    }
    // 错误归一：后端 detail 面向用户时直接展示；技术性文案（含异常原文/英文报错）翻译成可读提示
    const detail = error.response?.data?.detail ?? error.message;
    const raw = typeof detail === "string" ? detail : JSON.stringify(detail);
    ElMessage.error(isUserReadable(raw) ? raw : friendlyAgentError("internal_error", raw).msg);
    return Promise.reject(error);
  }
);

/**
 * qa-agent 专用通道：/agent 代理 → 独立 Agent 服务（8100）。
 * 与平台 http 实例分开：不注入平台 token（Agent 认 X-Agent-Key），
 * 生成链路含检索+模型调用耗时高，超时放宽到 2 分钟；错误文案按 Agent 语义归一。
 */
const agentHttp = axios.create({ baseURL: "/agent", timeout: 120000 });

agentHttp.interceptors.request.use((cfg) => {
  // 网关 Key 由部署方通过环境变量注入 Agent 侧；本地 require_key=false 时留空即可
  const key = localStorage.getItem("agent_key");
  if (key) cfg.headers["X-Agent-Key"] = key;
  return cfg;
});

agentHttp.interceptors.response.use(
  (resp) => resp,
  (error) => {
    // 后台状态探测（silent）失败不弹错：页面已有「在线/不可达」指示，掉线时逐条刷屏只会扰民
    if (error.config?.silent) return Promise.reject(error);
    const status: number = error.response?.status || 0;
    let msg: string;
    if (!error.response) {
      msg = "智能助手服务暂时不可达，请稍后重试或联系管理员";
    } else if (status === 401) {
      msg = "智能助手鉴权失败，请联系管理员检查接入配置";
    } else {
      // detail 面向用户才直接展示；技术性文案（异常原文/端口/内部服务名）一律翻译成可读提示，
      // 5xx 归入服务器内部错误；5xx 且无统一错误体（如开发代理层）几乎总是 Agent 掉了，提示不可达
      const d = typeof error.response.data?.detail === "string" ? error.response.data.detail : "";
      if (!d && status >= 500) msg = "智能助手服务暂时不可达，请稍后重试或联系管理员";
      else msg = isUserReadable(d) ? d : friendlyAgentError(status >= 500 ? "internal_error" : undefined, d).msg;
    }
    ElMessage.error(msg);
    return Promise.reject(error);
  }
);

export const api = {
  /* ---------- 登录与用户 ---------- */
  login: (body: { username: string; password: string }) =>
    http.post<LoginResp>("/auth/login", body).then((r) => r.data),

  logout: () => http.post<{ ok: boolean }>("/auth/logout").then((r) => r.data),

  getMe: () => http.get<UserInfo>("/auth/me").then((r) => r.data),

  listUsers: () => http.get<UserInfo[]>("/auth/users").then((r) => r.data),

  createUser: (body: UserCreateReq) =>
    http.post<UserInfo>("/auth/users", body).then((r) => r.data),

  updateUser: (username: string, body: UserUpdateReq) =>
    http.put<UserInfo>(`/auth/users/${encodeURIComponent(username)}`, body).then((r) => r.data),

  deleteUser: (username: string) =>
    http.delete<{ ok: boolean }>(`/auth/users/${encodeURIComponent(username)}`).then((r) => r.data),

  /* ---------- 角色权限矩阵（8.2，仅 admin） ---------- */

  /** 权限码目录 + 当前分配 + 默认基线 + 护栏规则 */
  getPermissions: () => http.get<PermMatrixResp>("/auth/permissions").then((r) => r.data),

  /** 整体提交矩阵；后端校验不过（造码/缺码/破坏护栏）一律 400 并回具体原因 */
  updatePermissions: (matrix: Record<string, string[]>) =>
    http.put<PermSaveResp>("/auth/permissions", { matrix }).then((r) => r.data),

  resetPermissions: () => http.post<PermSaveResp>("/auth/permissions/reset").then((r) => r.data),

  /* ---------- 操作审计（admin/tl） ---------- */
  listAuditLogs: (params: {
    limit?: number;
    username?: string;
    keyword?: string;
    days?: number;
    only_failed?: boolean;
  }) => http.get<AuditLogsResp>("/audit/logs", { params }).then((r) => r.data),

  auditOperators: () => http.get<{ operators: string[] }>("/audit/operators").then((r) => r.data),

  /** 清理审计日志：keep_days>0 仅保留最近 N 天，0 表示清空（仅 admin） */
  purgeAudit: (keepDays: number) =>
    http
      .delete<{ keep_days: number; removed: number }>(`/audit/logs?keep_days=${keepDays}`)
      .then((r) => r.data),

  /**
   * CSV 导出直链：浏览器新标签下载无法带 Authorization 头，
   * 故 token 走 query（后端 extract_token 已支持 ?token= 兜底）。
   */
  auditExportUrl: (params: {
    username?: string;
    keyword?: string;
    days?: number;
    only_failed?: boolean;
  }) => {
    const q = new URLSearchParams();
    if (params.username) q.set("username", params.username);
    if (params.keyword) q.set("keyword", params.keyword);
    if (params.days) q.set("days", String(params.days));
    if (params.only_failed) q.set("only_failed", "true");
    const token = getToken();
    if (token) q.set("token", token);
    return `/api/audit/export?${q.toString()}`;
  },

  listCases: (params: {
    module?: string;
    priority?: string;
    keyword?: string;
    run?: boolean;
    file?: string;
  }) => http.get<CasePage>("/cases", { params }).then((r) => r.data),

  /* ---------- 用例项目（项目 = 一个 Excel 用例文件）---------- */
  listProjects: () => http.get<Project[]>("/projects").then((r) => r.data),

  createProject: (body: { name: string; desc?: string; kind?: "api" | "ui" | "app" }) =>
    http.post<Project>("/projects", body).then((r) => r.data),

  updateProject: (id: string, body: { name?: string; desc?: string }) =>
    http.put<Project>(`/projects/${encodeURIComponent(id)}`, body).then((r) => r.data),

  deleteProject: (id: string) =>
    http.delete<{ deleted: string; removed_file: string | null }>(
      `/projects/${encodeURIComponent(id)}`
    ).then((r) => r.data),

  listEnvs: () => http.get<EnvsResp>("/envs").then((r) => r.data),

  /** 保存环境配置（写回 config.yaml，下一次执行/拉取即生效） */
  updateEnvs: (body: EnvsUpdate) =>
    http.put<EnvsResp>("/envs", body).then((r) => r.data),

  /** 更新用例（只传改动字段；后端写回 Excel，校验不外溢） */
  updateCase: (caseId: string, changes: Record<string, unknown>, file?: string) =>
    http
      .put<CaseItem>(`/cases/${encodeURIComponent(caseId)}`, changes, { params: { file } })
      .then((r) => r.data),

  /** 新增用例（整行追加写回 Excel 末行，编号唯一） */
  createCase: (data: Record<string, unknown>, file?: string) =>
    http.post<CaseItem>("/cases", data, { params: { file } }).then((r) => r.data),

  /** 删除用例（Excel 整行删除） */
  deleteCase: (caseId: string, file?: string) =>
    http
      .delete<{ deleted: string; file: string }>(
        `/cases/${encodeURIComponent(caseId)}`, { params: { file } }
      )
      .then((r) => r.data),

  /** 迁移用例到另一个项目：源 file 走查询参数，to_file/new_module 走请求体 */
  moveCase: (caseId: string, body: { to_file: string; new_module?: string }, file?: string) =>
    http
      .post<{ moved: string; from_file: string; to_file: string; case: CaseItem | null }>(
        `/cases/${encodeURIComponent(caseId)}/move`, body, { params: { file } }
      )
      .then((r) => r.data),

  /** 编辑用元数据：优先级/模块/请求头/断言操作符/变量候选 */
  getCaseMeta: (file?: string) =>
    http.get<CaseMeta>("/cases/meta", { params: { file } }).then((r) => r.data),

  /* ---------- UI 用例（项目 kind=ui，载体 JSON，走 /api/ui-cases） ---------- */
  listUiCases: (params: {
    file: string;
    module?: string;
    priority?: string;
    keyword?: string;
    run?: boolean;
  }) => http.get<UiCasePage>("/ui-cases", { params }).then((r) => r.data),

  getUiCase: (caseId: string, file: string) =>
    http.get<UiCaseItem>(`/ui-cases/${encodeURIComponent(caseId)}`, { params: { file } }).then((r) => r.data),

  getUiCaseMeta: (file: string) =>
    http.get<UiCaseMeta>("/ui-cases/meta", { params: { file } }).then((r) => r.data),

  /** UI 变量候选（供步骤取值插入 chip） */
  getUiCaseVariables: (file: string) =>
    http.get<{ excel_variables: string[]; secret_variables: string[] }>(
      "/ui-cases/variables", { params: { file } }).then((r) => r.data),

  /** UI 用例批量导入模板（JSON blob，浏览器侧下载）；fmt=excel 下载 .xlsx 一步一行模板 */
  downloadUiCaseTemplate: (fmt: "json" | "excel" = "json") =>
    http.get<Blob>("/ui-cases/template", { params: { format: fmt }, responseType: "blob" }).then((r) => r.data as Blob),

  /** 导出项目 UI 用例（blob 下载）：excel=一步一行可回导，json=与导入同构无损；支持当前筛选条件 */
  exportUiCases: (file: string, fmt: "excel" | "json" = "excel", filters?: {
    module?: string; priority?: string; keyword?: string; run?: boolean;
  }) => http.get<Blob>("/ui-cases/export",
    { params: { file, format: fmt, ...filters }, responseType: "blob" }).then((r) => r.data as Blob),

  /** 上传 UI 步骤 Excel 解析预览（不落盘）：返回逐条 case/marks/importable */
  previewUiExcel: (file: File, project: string) => {
    const fd = new FormData();
    fd.append("file", file);
    return http
      .post<UiImportPreviewResp>("/ui-cases/preview", fd, { params: { project } })
      .then((r) => r.data);
  },

  /** 批量导入 UI 用例：逐条校验，编号重复跳过，非法逐条报因 */
  importUiCases: (cases: Record<string, unknown>[], file: string) =>
    http.post<{ imported: number; imported_ids: string[]; skipped: string[]; errors: string[] }>(
      "/ui-cases/import", { cases }, { params: { file } }).then((r) => r.data),

  createUiCase: (data: Record<string, unknown>, file: string) =>
    http.post<UiCaseItem>("/ui-cases", data, { params: { file } }).then((r) => r.data),

  updateUiCase: (caseId: string, changes: Record<string, unknown>, file: string) =>
    http.put<UiCaseItem>(`/ui-cases/${encodeURIComponent(caseId)}`, changes, { params: { file } }).then((r) => r.data),

  deleteUiCase: (caseId: string, file: string) =>
    http.delete<{ deleted: string; file: string }>(
      `/ui-cases/${encodeURIComponent(caseId)}`, { params: { file } }
    ).then((r) => r.data),

  /** UI 用例迁移到另一个 UI 项目 */
  moveUiCase: (caseId: string, body: { to_file: string; new_module?: string }, file: string) =>
    http.post<{ moved: string; from_file: string; to_file: string; case: UiCaseItem }>(
      `/ui-cases/${encodeURIComponent(caseId)}/move`, body, { params: { file } }
    ).then((r) => r.data),

  /* ---------- UI 登录态会话（storage_state 复用，全局注册表，走 /api/ui-sessions） ---------- */
  listUiSessions: () => http.get<UiSession[]>("/ui-sessions").then((r) => r.data),

  getUiSession: (id: string) =>
    http.get<UiSession>(`/ui-sessions/${encodeURIComponent(id)}`).then((r) => r.data),

  getUiSessionStatus: (id: string) =>
    http.get<UiSessionStatus>(`/ui-sessions/${encodeURIComponent(id)}/status`).then((r) => r.data),

  getUiSessionMeta: () =>
    http.get<UiSessionMeta>("/ui-sessions/meta").then((r) => r.data),

  createUiSession: (body: Record<string, unknown>) =>
    http.post<UiSession>("/ui-sessions", body).then((r) => r.data),

  updateUiSession: (id: string, changes: Record<string, unknown>) =>
    http.put<UiSession>(`/ui-sessions/${encodeURIComponent(id)}`, changes).then((r) => r.data),

  deleteUiSession: (id: string) =>
    http.delete<{ deleted: string; entity_removed: string | null }>(
      `/ui-sessions/${encodeURIComponent(id)}`
    ).then((r) => r.data),

  /** 立即采集/刷新登录态（同步跑采集用例导出 storage_state，需浏览器内核就绪） */
  collectUiSession: (id: string) =>
    http.post<UiSessionCollectResult>(`/ui-sessions/${encodeURIComponent(id)}/collect`).then((r) => r.data),

  /* ---------- App 用例（项目 kind=app，载体 .apjson，走 /api/app-cases；预留位） ---------- */
  listAppCases: (params: {
    file: string;
    module?: string;
    priority?: string;
    keyword?: string;
    run?: boolean;
  }) => http.get<AppCasePage>("/app-cases", { params }).then((r) => r.data),

  getAppCase: (caseId: string, file: string) =>
    http.get<AppCaseItem>(`/app-cases/${encodeURIComponent(caseId)}`, { params: { file } }).then((r) => r.data),

  getAppCaseMeta: (file: string) =>
    http.get<AppCaseMeta>("/app-cases/meta", { params: { file } }).then((r) => r.data),

  /** App 变量候选（供步骤取值插入 chip） */
  getAppCaseVariables: (file: string) =>
    http.get<{ excel_variables: string[]; secret_variables: string[] }>(
      "/app-cases/variables", { params: { file } }).then((r) => r.data),

  createAppCase: (data: Record<string, unknown>, file: string) =>
    http.post<AppCaseItem>("/app-cases", data, { params: { file } }).then((r) => r.data),

  updateAppCase: (caseId: string, changes: Record<string, unknown>, file: string) =>
    http.put<AppCaseItem>(`/app-cases/${encodeURIComponent(caseId)}`, changes, { params: { file } }).then((r) => r.data),

  deleteAppCase: (caseId: string, file: string) =>
    http.delete<{ deleted: string; file: string }>(
      `/app-cases/${encodeURIComponent(caseId)}`, { params: { file } }
    ).then((r) => r.data),

  /** App 用例迁移到另一个 App 项目 */
  moveAppCase: (caseId: string, body: { to_file: string; new_module?: string }, file: string) =>
    http.post<{ moved: string; from_file: string; to_file: string; case: AppCaseItem }>(
      `/app-cases/${encodeURIComponent(caseId)}/move`, body, { params: { file } }
    ).then((r) => r.data),

  listRuns: (limit = 20) =>
    http.get<RunItem[]>("/runs", { params: { limit } }).then((r) => r.data),

  createRun: (body: {
    env?: string;
    module?: string;
    priority?: string;
    keyword?: string;
    cases?: string[];
    vars?: Record<string, unknown>;
    file?: string;
    workers?: number;
  }) => http.post<RunItem>("/runs", body).then((r) => r.data),

  getRun: (runId: string) =>
    http.get<RunItem>(`/runs/${encodeURIComponent(runId)}`).then((r) => r.data),

  /** 取消运行中批次（后端 taskkill 进程树；已结束批次幂等返回当前状态） */
  cancelRun: (runId: string) =>
    http
      .post<{ run_id: string; status: string; cancelling: boolean }>(
        `/runs/${encodeURIComponent(runId)}/cancel`
      )
      .then((r) => r.data),

  /** 删除批次报告（含报告目录；需 report:delete 权限） */
  deleteRun: (runId: string) =>
    http
      .delete<{ run_id: string; deleted_report_dir: string | null }>(
        `/runs/${encodeURIComponent(runId)}`
      )
      .then((r) => r.data),

  /** 清理旧报告：保留最近 keep 个批次，其余删除 */
  cleanupRuns: (keep = 20) =>
    http
      .post<{ keep: number; scanned: number; deleted: number; deleted_ids: string[] }>(
        `/runs/cleanup?keep=${keep}`
      )
      .then((r) => r.data),

  getRunLog: (runId: string) =>
    http
      .get<{ run_id: string; status: string; lines: number; log: string }>(
        `/runs/${encodeURIComponent(runId)}/log`
      )
      .then((r) => r.data),

  /** 平台原生明细：批次用例结果摘要（已掩码） */
  getRunCases: (runId: string) =>
    http
      .get<RunCasesResp>(`/runs/${encodeURIComponent(runId)}/cases`)
      .then((r) => r.data),

  /** 单用例请求/响应快照（已掩码+截断） */
  getRunCaseDetail: (runId: string, caseId: string) =>
    http
      .get<RunCaseDetail>(
        `/runs/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}`
      )
      .then((r) => r.data),

  /** 单用例跨批次执行历史（新→旧） */
  getCaseHistory: (caseId: string, limit = 30) =>
    http
      .get<CaseHistoryResp>(`/runs/history/${encodeURIComponent(caseId)}`, { params: { limit } })
      .then((r) => r.data),

  /** 单文件 Allure 中文报告（新标签打开；后端首次访问会现场生成） */
  reportUrl: (runId: string) =>
    `/api/runs/${encodeURIComponent(runId)}/report`,

  /** UI 步骤截图取图直链：<img> 无法带 Authorization 头，token 走 ?token= 兜底 */
  reportAttachmentUrl: (runId: string, filename: string) => {
    const token = getToken();
    const q = token ? `?token=${encodeURIComponent(token)}` : "";
    return `/api/runs/${encodeURIComponent(runId)}/attachment/${encodeURIComponent(filename)}${q}`;
  },

  /** 失败 trace zip 下载直链（同截图取图口径，token 走 ?token= 兜底；下本地后 npx playwright show-trace 回放） */
  reportTraceUrl: (runId: string, filename: string) => {
    const token = getToken();
    const q = token ? `?token=${encodeURIComponent(token)}` : "";
    return `/api/runs/${encodeURIComponent(runId)}/trace/${encodeURIComponent(filename)}${q}`;
  },

  /* ---------- 凭证管理（值全程掩码，不回显明文） ---------- */
  listSecrets: () => http.get<SecretsResp>("/secrets").then((r) => r.data),

  createSecret: (body: { name: string; value: string; remark?: string }) =>
    http.post<SecretsResp>("/secrets", body).then((r) => r.data),

  updateSecret: (name: string, body: { value?: string; remark?: string }) =>
    http
      .put<SecretsResp>(`/secrets/${encodeURIComponent(name)}`, body)
      .then((r) => r.data),

  deleteSecret: (name: string) =>
    http
      .delete<SecretsResp>(`/secrets/${encodeURIComponent(name)}`)
      .then((r) => r.data),

  /* ---------- 测试场景 ---------- */
  listPlans: () => http.get<PlanItem[]>("/plans").then((r) => r.data),

  createPlan: (body: PlanCreateReq) =>
    http.post<PlanItem>("/plans", body).then((r) => r.data),

  getPlan: (id: string) =>
    http.get<PlanItem>(`/plans/${encodeURIComponent(id)}`).then((r) => r.data),

  updatePlan: (id: string, body: PlanUpdateReq) =>
    http.put<PlanItem>(`/plans/${encodeURIComponent(id)}`, body).then((r) => r.data),

  deletePlan: (id: string) =>
    http.delete<{ ok: boolean }>(`/plans/${encodeURIComponent(id)}`).then((r) => r.data),

  /** 按场景发起执行，返回新批次 RunItem */
  runPlan: (id: string) =>
    http.post<RunItem>(`/plans/${encodeURIComponent(id)}/run`).then((r) => r.data),

  /* ---------- 用例导入导出（target 为当前项目文件，走 file 查询参数） ---------- */
  /** 导出筛选结果为 xlsx 下载 */
  exportCases: (params: { module?: string; priority?: string; keyword?: string; run?: boolean; file?: string }) =>
    http.get("/cases/export", { params, responseType: "blob" }).then((r) => r.data as Blob),

  /** 导入 xlsx 文件追加用例到目标项目文件 */
  importCases: (upload: File, targetFile?: string) => {
    const fd = new FormData();
    fd.append("uploaded_file", upload);
    return http
      .post<{ imported: number; skipped: number; errors: string[] }>("/cases/import", fd,
        { params: { file: targetFile }, headers: { "Content-Type": "multipart/form-data" }, timeout: 60000 })
      .then((r) => r.data);
  },

  /** 下载空白用例模板（含下拉校验与填写说明） */
  downloadTemplate: () =>
    http.get("/cases/template", { responseType: "blob" }).then((r) => r.data as Blob),

  /** 智能导入预览：上传任意表头 xlsx，返回转换后的用例草稿与标注（不落盘） */
  smartImportPreview: (upload: File, targetFile?: string) => {
    const fd = new FormData();
    fd.append("uploaded_file", upload);
    return http
      .post<ImportPreviewResp>("/cases/import/preview", fd,
        { params: { file: targetFile }, headers: { "Content-Type": "multipart/form-data" }, timeout: 60000 })
      .then((r) => r.data);
  },

  /** 智能导入落盘：把预览确认后的用例批量写入目标项目文件 */
  smartImportConfirm: (cases: ImportCaseDraft[], targetFile?: string) =>
    http
      .post<{ imported: number; skipped: number; errors: string[] }>("/cases/import/confirm", { cases },
        { params: { file: targetFile }, timeout: 60000 })
      .then((r) => r.data),

  /* ---------- 公共脚本（设计文档 2.4：前后置声明式步骤链） ---------- */
  listScripts: () => http.get<ScriptItem[]>("/scripts").then((r) => r.data),

  /** 步骤编辑器元数据：动作白名单与字段上限（与后端校验同源） */
  getScriptMeta: () => http.get<ScriptMeta>("/scripts/meta").then((r) => r.data),

  getScript: (name: string) =>
    http.get<ScriptItem>(`/scripts/${encodeURIComponent(name)}`).then((r) => r.data),

  createScript: (body: ScriptCreateReq) =>
    http.post<ScriptItem>("/scripts", body).then((r) => r.data),

  updateScript: (name: string, body: ScriptUpdateReq) =>
    http.put<ScriptItem>(`/scripts/${encodeURIComponent(name)}`, body).then((r) => r.data),

  deleteScript: (name: string) =>
    http.delete<{ deleted: string }>(`/scripts/${encodeURIComponent(name)}`).then((r) => r.data),

  /* ---------- 接口定义库（设计文档 2.2：接口库沉淀 + OpenAPI 导入 + 生成用例骨架） ---------- */
  listApiDefs: () => http.get<ApiDefItem[]>("/defs").then((r) => r.data),

  /** 接口编辑元数据（方法/断言操作符/长度上限/导入限额） */
  getApiDefMeta: () => http.get<ApiDefMeta>("/defs/meta").then((r) => r.data),

  getApiDef: (name: string) =>
    http.get<ApiDefItem>(`/defs/${encodeURIComponent(name)}`).then((r) => r.data),

  createApiDef: (body: ApiDefCreateReq) =>
    http.post<ApiDefItem>("/defs", body).then((r) => r.data),

  /** 更新定义（只传改动字段；被同名用例引用时不可改名，返 409） */
  updateApiDef: (name: string, body: ApiDefUpdateReq) =>
    http.put<ApiDefItem>(`/defs/${encodeURIComponent(name)}`, body).then((r) => r.data),

  deleteApiDef: (name: string) =>
    http.delete<{ deleted: string }>(`/defs/${encodeURIComponent(name)}`).then((r) => r.data),

  /** 把存量 Excel 用例收敛成接口库（同名合并，方法/路径/断言按频次投票） */
  aggregateApiDefs: (overwrite = false) =>
    http.post<AggregateResult>("/defs/aggregate", null, { params: { overwrite } }).then((r) => r.data),

  /** 导入 OpenAPI/Swagger 文档原文（JSON 或 YAML，大文档放宽超时） */
  importOpenApi: (text: string, overwrite = false) =>
    http.post<OpenApiImportResult>("/defs/import/openapi", { text, overwrite },
      { timeout: 120000 }).then((r) => r.data),

  /** 由接口定义生成用例骨架（dry_run=true 只预览，=false 才追写 Excel） */
  genSkeleton: (name: string, body: SkeletonReq) =>
    http.post<SkeletonResult>(`/defs/${encodeURIComponent(name)}/skeleton`, body).then((r) => r.data),

  /* ---------- Webhook 通知管理 ---------- */
  listWebhooks: () => http.get<WebhookItem[]>("/webhooks").then((r) => r.data),

  createWebhook: (body: WebhookCreateReq) =>
    http.post<WebhookItem>("/webhooks", body).then((r) => r.data),

  updateWebhook: (id: string, body: WebhookUpdateReq) =>
    http.put<WebhookItem>(`/webhooks/${encodeURIComponent(id)}`, body).then((r) => r.data),

  deleteWebhook: (id: string) =>
    http.delete<{ ok: boolean }>(`/webhooks/${encodeURIComponent(id)}`).then((r) => r.data),

  testWebhook: (id: string) =>
    http.post<{ ok: boolean; status_code: number; response: string }>(
      `/webhooks/${encodeURIComponent(id)}/test`).then((r) => r.data),

  /* ---------- 定时任务 ---------- */
  listSchedules: () => http.get<ScheduleItem[]>("/schedules").then((r) => r.data),

  createSchedule: (body: ScheduleCreateReq) =>
    http.post<ScheduleItem>("/schedules", body).then((r) => r.data),

  updateSchedule: (id: string, body: ScheduleUpdateReq) =>
    http.put<ScheduleItem>(`/schedules/${encodeURIComponent(id)}`, body).then((r) => r.data),

  deleteSchedule: (id: string) =>
    http.delete<{ ok: boolean }>(`/schedules/${encodeURIComponent(id)}`).then((r) => r.data),

  runSchedule: (id: string) =>
    http.post<RunItem>(`/schedules/${encodeURIComponent(id)}/run`).then((r) => r.data),

  previewCron: (cron: string, n = 5) =>
    http.get<{ cron: string; next_runs: string[] }>("/schedules/preview/next-runs", {
      params: { cron, n },
    }).then((r) => r.data),

  /* ---------- 用例依赖 ---------- */
  getAllDeps: () => http.get<Record<string, string[]>>("/dependencies").then((r) => r.data),

  getDeps: (caseId: string) =>
    http.get<{ case_id: string; dependencies: string[] }>(
      `/dependencies/${encodeURIComponent(caseId)}`).then((r) => r.data),

  setDeps: (caseId: string, deps: string[]) =>
    http.put<{ case_id: string; dependencies: string[] }>(
      `/dependencies/${encodeURIComponent(caseId)}`, { dependencies: deps }).then((r) => r.data),

  /* ---------- AI 助手（qa-agent 直连）----------
   * 生成直连 qa-agent（/agent 代理 → 8100，独立服务无平台登录态）；
   * 预览/落库走平台 /api/agent/*（权威校验闸门 + case:write 权限），两条链路职责分离。
   */
  /* ---------- 后台探测与列表：失败静默（状态指示/空列表兜底），避免服务掉线时刷屏报错 ---------- */
  agentHealth: () =>
    agentHttp.get<{ skills: string[]; provider?: string; degraded?: boolean;
                    web_search_enabled?: boolean;
                    models?: string[]; default_model?: string }>("/health", { silent: true }).then((r) => r.data),

  agentGenerate: (body: GenerationReq) =>
    agentHttp.post<GenerationOut>("/v1/generations", body).then((r) => r.data),

  /** 平台权威校验预览（不落盘）：逐条 passed/exists/problems */
  agentPreviewCases: (body: AgentCasesBody) =>
    http.post<AgentPreviewResp>("/agent/cases/preview", body).then((r) => r.data),

  /** 确认落库：整批先验后写，任一非法即全拒（后端 400 带 rejected 明细） */
  agentCommitCases: (body: AgentCasesBody) =>
    http.post<AgentCommitResp>("/agent/cases/commit", body).then((r) => r.data),

  /* ---------- 对话式 AI 生成：会话历史在 Agent 侧，流式走 utils/sse 不走 axios ---------- */
  agentChatSessions: () =>
    agentHttp.get<{ items: ChatSessionInfo[] }>("/v1/chat/sessions", { silent: true }).then((r) => r.data.items),

  agentChatSession: (sid: string) =>
    agentHttp.get<ChatSessionDetail>(`/v1/chat/sessions/${encodeURIComponent(sid)}`).then((r) => r.data),

  agentChatDelete: (sid: string) =>
    agentHttp.delete<{ id: string }>(`/v1/chat/sessions/${encodeURIComponent(sid)}`, { silent: true }).then((r) => r.data),
};
