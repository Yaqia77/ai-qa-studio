<script setup lang="ts">
/** 用例管理：项目切换 + 模块导航 + 筛选 + 编辑/新增抽屉（候选来自 /api/cases/meta?file=，改动直写回该项目 Excel）。 */
import { computed, nextTick, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import { hasPerm } from "../stores/auth";
import type { ApiDefItem, CaseHistoryResp, CaseItem, CaseMeta, ImportPreviewItem, ImportPreviewResp, Project, ScriptItem } from "../types";
import JsonViewer from "../components/JsonViewer.vue";
import BatchIoDialog from "../components/BatchIoDialog.vue";
import UiCasesView from "./UiCasesView.vue";
import AppCasesView from "./AppCasesView.vue";

const router = useRouter();
const route = useRoute();
const carried = ref("");   // 从工作台等页面携带过来的筛选条件描述（非空则顶部提示）

const loading = ref(false);
const cases = ref<CaseItem[]>([]);
const total = ref(0);
const meta = ref<CaseMeta | null>(null);
const filters = reactive({
  priority: [] as string[],
  keyword: "",
  onlyEnabled: true,
});

/* ================= 用例项目（项目 = 一个 Excel 文件，模块/用例按项目天然隔离） ================= */
const PROJ_KEY = "ia_project";   // 会话级记住当前项目（与登录态 ia_token 同一 localStorage 惯例）
const projects = ref<Project[]>([]);
const activeProjectId = ref(localStorage.getItem(PROJ_KEY) || "default");

/** 当前项目实体（列表尚未加载到时兜底为默认项目） */
const currentProject = computed(() =>
  projects.value.find((p) => p.id === activeProjectId.value) ??
  projects.value.find((p) => p.id === "default") ?? null
);

/** 所有用例接口都要透传的 file 参数；兜底默认项目的存量文件路径 */
const currentFile = computed(() => currentProject.value?.file ?? "data/api_cases.xlsx");

/** 项目形态：ui=可视化步骤用例（交 UiCasesView），app=App 用例（交 AppCasesView），api=接口 Excel 用例（本页原有形态） */
const isUi = computed(() => currentProject.value?.kind === "ui");
const isApp = computed(() => currentProject.value?.kind === "app");
/** 非接口形态（JSON/.apjson 载体）：Excel 筛选面板与本页原生拉取均需让位给子视图 */
const isNonApi = computed(() => isUi.value || isApp.value);

const kindLabel = (k: string) => (k === "ui" ? "UI 用例" : k === "app" ? "App 用例" : "接口用例");
/** 顶部三段切换：反映当前项目类型，点击切到该类型的第一个项目（无则提示新建） */
const currentKind = computed(() => currentProject.value?.kind ?? "api");
const kindCounts = computed(() => {
  const c: Record<string, number> = { api: 0, ui: 0, app: 0 };
  for (const p of projects.value) c[p.kind] = (c[p.kind] ?? 0) + 1;
  return c;
});
function switchKind(k: "api" | "ui" | "app") {
  if (k === currentKind.value) return;
  const p = projects.value.find((x) => x.kind === k);
  if (p) activeProjectId.value = p.id;
  else ElMessage.info(`暂无「${kindLabel(k)}」项目，可点上方“＋ 新建”选择该类型`);
}

async function loadProjects() {
  try { projects.value = await api.listProjects(); } catch { /* 拦截器已提示 */ }
  // 记住的项目可能已被删除/无权访问，兜底回默认项目，避免切换器空选
  if (projects.value.length && !projects.value.some((p) => p.id === activeProjectId.value)) {
    activeProjectId.value = "default";
  }
}

function projectLabel(p: Project) {
  return `${p.name}（${p.case_count} 条）`;
}

/* ---------- 项目管理：新建 / 重命名·说明 / 删除（builtin 默认项目禁删） ---------- */
const projDlgVisible = ref(false);
const projSaving = ref(false);
const projForm = reactive({ name: "", desc: "", kind: "api" as "api" | "ui" | "app" });
const projEditingId = ref("");   // "" = 新建；否则为待更新项目 id

function openProjCreate() {
  projEditingId.value = "";
  projForm.name = "";
  projForm.desc = "";
  projForm.kind = "api";
  projDlgVisible.value = true;
}

function openProjEdit() {
  const p = currentProject.value;
  if (!p) return;
  projEditingId.value = p.id;
  projForm.name = p.name;
  projForm.desc = p.desc ?? "";
  projDlgVisible.value = true;
}

async function saveProj() {
  const name = projForm.name.trim();
  if (!name) { ElMessage.error("项目名称不能为空"); return; }
  projSaving.value = true;
  try {
    if (projEditingId.value) {
      await api.updateProject(projEditingId.value, { name, desc: projForm.desc.trim() });
      ElMessage.success("项目已更新");
    } else {
      const item = await api.createProject({ name, desc: projForm.desc.trim(), kind: projForm.kind });
      ElMessage.success(`项目「${item.name}」已创建，可切换过去新建用例`);
    }
    projDlgVisible.value = false;
    await loadProjects();
  } catch { /* 拦截器已提示（重名 409 / 名称非法 400） */ } finally {
    projSaving.value = false;
  }
}

async function removeProject() {
  const p = currentProject.value;
  if (!p || p.builtin) return;
  try {
    await ElMessageBox.confirm(
      `将删除项目「${p.name}」及其用例文件 ${p.file}（共 ${p.case_count} 条用例，不可恢复）。请先在执行中心确认无定时任务引用该文件。`,
      "删除项目",
      { type: "warning", confirmButtonText: "确认删除", cancelButtonText: "取消" },
    );
  } catch { return; }
  try {
    await api.deleteProject(p.id);
    ElMessage.success(`项目「${p.name}」已删除`);
    activeProjectId.value = "default";
    await loadProjects();
  } catch { /* 拦截器已提示 */ }
}

/* ---------- 用例迁移（批量）：勾选用例 → 点顶部"迁移" → 选目标项目 → 串行逐条迁移 ---------- */
const moveVisible = ref(false);
const moveSaving = ref(false);
const moveForm = reactive({ toProjectId: "", new_module: "" });
const moveTargetModules = ref<string[]>([]);   // 目标项目的模块候选
const moveProjects = computed(() =>
  projects.value.filter((p) => p.id !== activeProjectId.value && p.kind === (currentProject.value?.kind ?? "api")));

function openMove() {
  if (!selected.value.length) { ElMessage.warning("请先勾选要迁移的用例"); return; }
  moveForm.toProjectId = "";
  moveForm.new_module = "";
  moveTargetModules.value = [];
  moveVisible.value = true;
}

/** 选定目标项目后拉其模块作为候选（迁移时可顺手归入目标项目的某个模块） */
async function onMoveTargetChange() {
  moveForm.new_module = "";
  moveTargetModules.value = [];
  const p = projects.value.find((x) => x.id === moveForm.toProjectId);
  if (!p) return;
  try {
    moveTargetModules.value = (await api.getCaseMeta(p.file)).modules;
  } catch { /* 拿不到候选不阻断，仍可自由输入模块名 */ }
}

async function doMove() {
  const targets = selected.value;
  if (!targets.length) return;
  const target = projects.value.find((p) => p.id === moveForm.toProjectId);
  if (!target) { ElMessage.warning("请选择目标项目"); return; }
  moveSaving.value = true;
  let ok = 0;
  const failed: string[] = [];
  for (const c of targets) {
    try {
      await api.moveCase(
        c.case_id,
        { to_file: target.file, new_module: moveForm.new_module.trim() || undefined },
        currentFile.value,
      );
      ok++;
    } catch {
      failed.push(c.case_id);  // 拦截器已逐条提示
    }
  }
  moveSaving.value = false;
  if (failed.length) {
    ElMessage.warning(`已迁移 ${ok} 条到「${target.name}」，失败 ${failed.length} 条（${failed.join(", ")}）`);
  } else {
    ElMessage.success(`已迁移 ${ok} 条用例到「${target.name}」`);
  }
  moveVisible.value = false;
  clearSelection();
  await loadProjects();
  await search();
  try { meta.value = await api.getCaseMeta(currentFile.value); } catch { /* 拦截器已提示 */ }
}

// 切换项目：持久化选择、重置模块导航并重新拉取该项目的用例与元数据
watch(activeProjectId, (v) => {
  localStorage.setItem(PROJ_KEY, v);
  activeModule.value = "";
  currentPage.value = 1;
  search();
  if (!isNonApi.value) api.getCaseMeta(currentFile.value).then((m) => (meta.value = m)).catch(() => { /* 拦截器已提示 */ });
});

/* ================= 模块导航 + 整体分页（左侧选模块，右侧分页表格） ================= */
const activeModule = ref("","");      // "" = 全部用例
const pageSize = ref(10);
const currentPage = ref(1);

/** 各模块用例数（基于当前筛选结果，按 zh 排序） */
const modules = computed(() => {
  const c = new Map<string, number>();
  for (const it of cases.value) c.set(it.module, (c.get(it.module) ?? 0) + 1);
  return [...c.entries()]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => a.name.localeCompare(b.name, "zh"));
});

/** 当前模块（或全部）的用例：按模块 + 编号排序 */
const visibleCases = computed(() => {
  const list = activeModule.value ? cases.value.filter((c) => c.module === activeModule.value) : cases.value;
  return [...list].sort(
    (a, b) => a.module.localeCompare(b.module, "zh") || a.case_id.localeCompare(b.case_id)
  );
});
const pagedCases = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value;
  return visibleCases.value.slice(start, start + pageSize.value);
});

function selectModule(name: string) {
  activeModule.value = name;
  currentPage.value = 1;
  clearSelection();
}

async function search() {
  if (isNonApi.value) { cases.value = []; total.value = 0; return; }  // UI/App 项目由子视图自行拉取
  loading.value = true;
  try {
    const resp = await api.listCases({
      priority: filters.priority.length ? filters.priority.join(",") : undefined,
      keyword: filters.keyword || undefined,
      run: filters.onlyEnabled ? true : undefined,
      file: currentFile.value,
    });
    cases.value = resp.items;
    total.value = resp.items.length;
    // 当前选中模块若已不在结果中（被筛掉/改名）则回退到全部
    if (activeModule.value && !resp.items.some((c) => c.module === activeModule.value)) activeModule.value = "";
    currentPage.value = 1;
    clearSelection();
  } finally {
    loading.value = false;
  }
}

function reset() {
  filters.priority = [];
  filters.keyword = "";
  filters.onlyEnabled = true;
  activeModule.value = "";
  search();
}

/** 清除从工作台携带过来的筛选条件，回到默认视图 */
function clearCarried() {
  filters.priority = [];
  filters.keyword = "";
  activeModule.value = "";
  carried.value = "";
  router.replace({ path: "/cases" });
  search();
}

/** 携带当前筛选条件（模块/优先级/关键字）跳到执行中心预填，形成“看用例→直接跑”闭环 */
function goExecute() {
  router.push({
    path: "/execution",
    query: {
      module: activeModule.value || undefined,
      priority: filters.priority.length ? filters.priority.join(",") : undefined,
      keyword: filters.keyword || undefined,
      file: currentFile.value,
    },
  });
}

const prioType = (p: string) =>
  ({ P0: "danger", P1: "warning", P2: "info", P3: "info", P4: "info", P5: "info" }[p] ?? "info");

/** 接口定义来源说明（与 /api/defs 后端 SOURCE_LABELS 同口径） */
const SOURCE_LABEL: Record<string, string> = { manual: "手工录入", excel: "Excel 聚合", openapi: "OpenAPI 导入" };

/* ================= 编辑抽屉 ================= */

interface KvRow { key: string; value: string }
interface AssertRow { op: string; path: string; expected: string }

const editVisible = ref(false);
const saving = ref(false);
const editing = ref<CaseItem | null>(null);   // 打开时的原始行（差分基准；新增/复制模式为 null）
const mode = ref<"edit" | "create">("edit");
const copyFrom = ref("");                      // 复制来源编号（仅提示用）

const form = reactive({
  case_id: "",
  module: "",
  api_name: "",
  case_desc: "",
  priority: "P0",
  run: true,
  method: "POST",
  url: "",
  timeout: "",
  body: "",
  pre_scripts: [] as string[],
  post_scripts: [] as string[],
});

/* 公共脚本候选（设计文档 2.4）：后端保存用例会校验脚本存在性，故只允许从库里选 */
const scriptOptions = ref<ScriptItem[]>([]);
async function loadScripts() {
  try {
    scriptOptions.value = await api.listScripts();
  } catch { /* 拿不到候选不阻断用例编辑 */ }
}

/* 接口定义候选（设计文档 2.2）：接口库是元数据，用它给「接口名称」提供建议并一键带入配置；
   拿不到定义库时接口名称仍可自由输入，用例保存逻辑完全不依赖它。 */
const apiDefs = ref<ApiDefItem[]>([]);
async function loadApiDefs() {
  try {
    apiDefs.value = await api.listApiDefs();
  } catch { /* 无 api:manage 权限或后端未落库时静默降级为自由文本 */ }
}

/** 名称 → 定义，同名只有一条定义（后端按名称唯一） */
const defByName = computed(() => new Map(apiDefs.value.map((d) => [d.name, d])));

/** 候选 = 已有用例的接口名 ∪ 接口库定义名：前者保证不改名时也能选回原值 */
const apiNameOptions = computed(() => {
  const set = new Set<string>(meta.value?.api_names ?? []);
  apiDefs.value.forEach((d) => set.add(d.name));
  if (form.api_name.trim()) set.add(form.api_name.trim());
  return [...set].sort((a, b) => a.localeCompare(b, "zh"));
});

const matchedDef = computed(() => defByName.value.get(form.api_name.trim()) ?? null);

const defPreview = (name: string) => {
  const d = defByName.value.get(name);
  return d ? `${d.method} ${d.path}` : "未登记";
};

const isPlainObj = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);

/** 把接口库里的定义填进当前抽屉表单（仅改表单，落盘仍要点保存并走全量校验） */
async function applyDef() {
  const d = matchedDef.value;
  if (!d) return;
  const willBring: string[] = ["方法", "请求路径"];
  if (isPlainObj(d.headers) && Object.keys(d.headers).length) willBring.push("请求头");
  if (isPlainObj(d.params) && Object.keys(d.params).length) willBring.push("查询参数");
  if (d.body != null && d.body !== "") willBring.push("请求体");
  if (d.extract && Object.keys(d.extract).length) willBring.push("变量提取");
  if (d.asserts?.length) willBring.push("推荐断言");
  try {
    await ElMessageBox.confirm(
      `将用接口库中 <b>${d.name}</b> 的定义覆盖当前表单的 ${willBring.join("、")}。<br>
       覆盖后仍需在抽屉里点保存才会写回 Excel，中途取消不影响用例。`,
      "带入推荐配置",
      { type: "info", dangerouslyUseHTMLString: true, confirmButtonText: "覆盖并填入", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  form.method = d.method;
  form.url = d.path;
  if (!form.module.trim() && d.module) form.module = d.module;
  if (isPlainObj(d.headers) && Object.keys(d.headers).length) hdrRows.value = objToRows(d.headers);
  if (isPlainObj(d.params) && Object.keys(d.params).length) prmRows.value = objToRows(d.params);
  if (d.extract && Object.keys(d.extract).length) extRows.value = objToRows(d.extract);
  if (d.asserts?.length) asrtRows.value = assertsToRows(d.asserts);
  if (d.body != null && d.body !== "") form.body = jsonText(d.body);
  ElMessage.success(`已带入 ${willBring.join("、")}，业务参数请按需补全`);
}

const hdrRows = ref<KvRow[]>([]);
const prmRows = ref<KvRow[]>([]);
const extRows = ref<KvRow[]>([]);
const asrtRows = ref<AssertRow[]>([]);

// 字面量含 {{ }}，定义为常量避免写进模板插值
const VAR_PH = "值，支持 {{变量}} 引用";
const PATH_PH = "选常用字段或输入 JSONPath，如 $.data.id";

/* ---------- 断言编辑器中文化（设计：平台 UI 汉化 + 中文预览，Excel 格式不变） ---------- */
const OP_LABELS: Record<string, string> = {
  eq: "等于", neq: "不等于", contains: "包含",
  gt: "大于", ge: "大于等于", lt: "小于", le: "小于等于",
  regex: "正则匹配", not_null: "不为空", len_eq: "长度等于",
};
const opLabel = (op: string) => OP_LABELS[op] || op;

const PATH_PRESETS = [
  { value: "status_code", label: "HTTP 状态码" },
  { value: "$.code", label: "业务响应码" },
  { value: "$.success", label: "成功标志" },
  { value: "$.message", label: "响应消息" },
  { value: "$.data", label: "响应数据（整体）" },
  { value: "$.data.totalCount", label: "数据总数" },
  { value: "response_text", label: "响应原文" },
  { value: "var.", label: "变量池（var.变量名）" },
];

/** 每条断言的中文语义预览 */
function assertHint(row: AssertRow): string {
  const pathCn = PATH_PRESETS.find(p => p.value === row.path)?.label
    || (row.path.startsWith("$.") ? `响应字段 ${row.path}` : row.path || "（待选）");
  const opCn = opLabel(row.op);
  if (row.op === "not_null") return `${pathCn} 不为空`;
  if (!opNeedExpected(row.op)) return `${pathCn} ${opCn}`;
  return `${pathCn} ${opCn} ${row.expected || "（待填）"}`;
}

/** 快捷断言模板：点击即追加常用组合 */
const ASSERT_PRESETS: { label: string; rows: AssertRow[] }[] = [
  { label: "接口调用成功", rows: [
    { op: "eq", path: "status_code", expected: "200" },
    { op: "eq", path: "$.code", expected: "0000" },
  ]},
  { label: "返回数据非空", rows: [
    { op: "eq", path: "status_code", expected: "200" },
    { op: "not_null", path: "$.data", expected: "" },
  ]},
  { label: "请求应报错", rows: [
    { op: "eq", path: "status_code", expected: "200" },
    { op: "neq", path: "$.code", expected: "0000" },
  ]},
];

function applyAssertPreset(preset: typeof ASSERT_PRESETS[0]) {
  asrtRows.value.push(...preset.rows.map(r => ({ ...r })));
}
const varChip = (v: string) => "{{" + v + "}}";

const prioOptions = () => meta.value?.priorities ?? ["P0", "P1", "P2", "P3", "P4", "P5"];
const methodOptions = () => meta.value?.methods ?? ["GET", "POST", "PUT", "DELETE", "PATCH"];

/** 模块候选：自由文本输入，已有模块作为下拉提示（模块不受限定，输入任意名称即归入新模块） */
function queryModules(qs: string, cb: (r: { value: string }[]) => void) {
  const q = (qs || "").trim().toLowerCase();
  const mods = meta.value?.modules ?? [];
  cb(mods.filter((m) => !q || m.toLowerCase().includes(q)).map((m) => ({ value: m })));
}

/* ================= 批量启停 ================= */
const tableRef = ref();          // 表格实例：需要命令式清空勾选（reserve-selection 下仅改 selected 不会同步复选框）
const selected = ref<CaseItem[]>([]);
const batching = ref(false);

/** 清空选择：先调 el-table 的 clearSelection 释放保留勾选，再兜底重置本地计数 */
function clearSelection() {
  tableRef.value?.clearSelection?.();
  selected.value = [];
}

// fixed="right" 操作列 + expand 展开列：数据/翻页/页大小变化后固定列点击热区会错位（按钮看着在、点下去无反应），
// 需 nextTick 后调 doLayout() 让表格重算列布局。watch pagedCases 一并覆盖翻页与筛选两种触发路径。
watch(pagedCases, () => nextTick(() => tableRef.value?.doLayout?.()));

/* ================= 批量导入导出（复用 UI 页的 BatchIoDialog 布局壳：智能导入预览确认 / 直接导入双通道） ================= */
const ioVisible = ref(false);
const ioMode = ref<"smart" | "direct">("smart");
const ioWritable = computed(() => hasPerm("case:write"));
const IO_CHANNELS = [
  { key: "smart", label: "Excel 智能导入（推荐）" },
  { key: "direct", label: "Excel 直接导入" },
];
const IO_ACTIONS = [
  { key: "template", label: "⬇ 下载 Excel 模板" },
  { key: "export", label: "⬆ 导出项目用例" },
];
// 直接导入的结果（弹窗内展示；智能导入的结果在预览表里）
const directResult = ref<{ imported: number; skipped: number; errors: string[] } | null>(null);

function openBatchIo() {
  ioMode.value = "smart";
  smartData.value = null;
  smartSelected.value = [];
  directResult.value = null;
  ioVisible.value = true;
}

function onIoAction(key: string) {
  if (key === "template") doDownloadTemplate();
  else if (key === "export") doExport();
}

// 上传文件分流：智能导入走预览确认流，直接导入即传即导
async function onIoFile(raw: File) {
  if (ioMode.value === "smart") await runSmartPreview(raw);
  else await runDirectImport(raw);
}

async function doExport() {
  try {
    const blob = await api.exportCases({
      priority: filters.priority.length ? filters.priority.join(",") : undefined,
      keyword: filters.keyword || undefined,
      run: filters.onlyEnabled ? true : undefined,
      file: currentFile.value,
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "cases_export.xlsx";
    a.click();
    URL.revokeObjectURL(url);
    ElMessage.success("导出成功");
  } catch { /* 拦截器已提示 */ }
}

async function runDirectImport(file: File) {
  directResult.value = null;
  try {
    const result = await api.importCases(file, currentFile.value);
    const { imported, skipped, errors } = result;
    directResult.value = { imported, skipped, errors };
    if (errors.length) {
      ElMessage.warning(`导入 ${imported} 条，跳过 ${skipped} 条已有，${errors.length} 条格式异常`);
    } else {
      ElMessage.success(`导入 ${imported} 条，跳过 ${skipped} 条已有`);
    }
    await search();
    meta.value = await api.getCaseMeta(currentFile.value);
    loadProjects();   // 项目列表上的用例数同步
  } catch { /* 拦截器已提示 */ }
}

function onSelectionChange(rows: CaseItem[]) {
  selected.value = rows;
}

/* ---------- 模板下载 ---------- */
async function doDownloadTemplate() {
  try {
    const blob = await api.downloadTemplate();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "cases_template.xlsx";
    a.click();
    URL.revokeObjectURL(url);
    ElMessage.success("模板已下载，填写后可直接「导入」或「智能导入」");
  } catch { /* 拦截器已提示 */ }
}

/* ---------- 智能导入（预览确认流：上传 → 转换报告 → 勾选确认 → 落盘），在 BatchIoDialog 智能通道内渲染 ---------- */
const smartLoading = ref(false);
const smartSaving = ref(false);
const smartData = ref<ImportPreviewResp | null>(null);
const smartTableRef = ref();
const smartSelected = ref<ImportPreviewItem[]>([]);

// 转换报告里的字段中文名（后端 field → 展示）
const FIELD_LABEL: Record<string, string> = {
  case_id: "用例编号", module: "模块", module2: "模块(辅助)", api_name: "接口名称", case_desc: "用例描述",
  priority: "优先级", run: "是否执行", method: "请求方式", url: "请求路径", headers: "请求头",
  params: "查询参数", body: "请求体", extract: "变量提取", asserts: "断言", timeout: "超时",
  pre_scripts: "前置脚本", post_scripts: "后置脚本", precondition: "预置条件·参与转换", steps: "操作步骤·参与转换",
  expected: "预期结果·参与转换",
};

async function runSmartPreview(file: File) {
  smartLoading.value = true;
  smartData.value = null;
  smartSelected.value = [];
  try {
    const resp = await api.smartImportPreview(file, currentFile.value);
    smartData.value = resp;
    await nextTick();
    // 默认勾选：可导入且编号不与存量冲突
    for (const it of resp.items) {
      if (it.importable && !it.exists) smartTableRef.value?.toggleRowSelection(it, true);
    }
  } catch { /* 拦截器已提示，留在弹窗内可重试 */ } finally {
    smartLoading.value = false;
  }
}

function smartSelectable(row: ImportPreviewItem) {
  return row.importable && !row.exists;
}

function smartRowClass({ row }: { row: ImportPreviewItem }) {
  if (!row.importable) return "row-err";
  if (row.exists) return "row-exist";
  return row.marks.some((m) => m.level === "warn") ? "row-warn" : "";
}

async function doSmartImport() {
  if (!smartSelected.value.length) {
    ElMessage.info("请至少勾选一条用例");
    return;
  }
  smartSaving.value = true;
  try {
    const { imported, skipped, errors } = await api.smartImportConfirm(smartSelected.value.map((x) => x.case), currentFile.value);
    if (errors.length) {
      ElMessage.warning(`导入 ${imported} 条，跳过 ${skipped} 条已有，${errors.length} 条异常（${errors[0]}）`);
    } else {
      ElMessage.success(`导入 ${imported} 条，跳过 ${skipped} 条已有`);
    }
    ioVisible.value = false;
    await search();
    meta.value = await api.getCaseMeta(currentFile.value);
    loadProjects();
  } catch { /* 拦截器已提示 */ } finally {
    smartSaving.value = false;
  }
}

/** 批量启用/停用：串行逐个写回 Excel（同一文件不能并发写，避开行锁漂移与互相覆盖） */
async function batchRun(val: boolean) {
  const targets = selected.value.filter((c) => c.run !== val);
  if (!targets.length) {
    ElMessage.info(val ? "所选用例均已启用" : "所选用例均已停用");
    return;
  }
  batching.value = true;
  let ok = 0;
  const failed: string[] = [];
  for (const c of targets) {
    try {
      await api.updateCase(c.case_id, { run: val }, currentFile.value);
      ok++;
    } catch {
      failed.push(c.case_id);  // 拦截器已逐条提示后端 detail
    }
  }
  batching.value = false;
  if (failed.length) {
    ElMessage.warning(`已${val ? "启用" : "停用"} ${ok} 条，失败 ${failed.length} 条（${failed.join(", ")}）`);
  } else {
    ElMessage.success(`已${val ? "启用" : "停用"} ${ok} 条用例并写回 Excel`);
  }
  await search();
}

/* ---------- 行数据 ⇄ JSON 值 转换 ---------- */

const objToRows = (o: Record<string, unknown> | null): KvRow[] =>
  Object.entries(o ?? {}).map(([key, value]) => ({ key, value: String(value ?? "") }));

const rowsToObj = (rows: KvRow[], label: string): Record<string, string> | null => {
  const out: Record<string, string> = {};
  for (const r of rows) {
    const k = r.key.trim();
    if (!k) continue;
    if (k in out) { ElMessage.error(`${label}存在重复的键：${k}`); return null; }
    out[k] = r.value;
  }
  return out;
};

/** 期望值字符串 → 单元格 JSON 值（轻量推断数字/布尔，其余按字符串；断言引擎自带跨类型比较） */
const parseExpected = (s: string): unknown => {
  const t = s.trim();
  if (/^-?\d+(\.\d+)?$/.test(t)) return Number(t);
  if (t === "true") return true;
  if (t === "false") return false;
  return t;
};

const stringifyExpected = (v: unknown): string => (v == null ? "" : String(v));

const assertsToRows = (arr: unknown): AssertRow[] =>
  (Array.isArray(arr) ? arr : []).map((a) => {
    const r = Array.isArray(a) ? a : [];
    return { op: String(r[0] ?? ""), path: String(r[1] ?? ""), expected: stringifyExpected(r[2]) };
  });

const opNeedExpected = (op: string): boolean => {
  const info = meta.value?.assert_ops.find((o) => o.op === op);
  return info ? info.need_expected : true;
};

const rowsToAsserts = (): (string | unknown)[][] | null => {
  const out: (string | unknown)[][] = [];
  for (const [i, r] of asrtRows.value.entries()) {
    if (!r.op) { ElMessage.error(`第 ${i + 1} 条断言缺少操作符`); return null; }
    if (!r.path.trim()) { ElMessage.error(`第 ${i + 1} 条断言缺少取值路径`); return null; }
    out.push(opNeedExpected(r.op) ? [r.op, r.path.trim(), parseExpected(r.expected)] : [r.op, r.path.trim()]);
  }
  return out;
};

/** 空对象/空数组与 null 视为等价，避免格式差异造成伪改动 */
const normVal = (v: unknown): string => {
  if (v == null) return "null";
  if (typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 0) return "null";
  if (Array.isArray(v) && v.length === 0) return "null";
  return JSON.stringify(v);
};
const normAsserts = (arr: unknown): string => {
  const list = Array.isArray(arr) ? arr : [];
  return JSON.stringify(list.map((a) => {
    const r = Array.isArray(a) ? a : [];
    return r.length >= 3 ? [String(r[0]).toLowerCase(), String(r[1]), r[2]] : [String(r[0]).toLowerCase(), String(r[1])];
  }));
};

/* ---------- 请求头：key 选中候选时自动填充建议值 ---------- */

const hdrFocus = ref(0);

function onHdrKey(val: string, row: KvRow) {
  const hit = meta.value?.header_suggest.find((o) => o.key === val);
  if (hit && !row.value) row.value = hit.value;
}

function insertVar(v: string) {
  let idx = hdrFocus.value;
  if (!hdrRows.value.length) hdrRows.value.push({ key: "", value: "" });
  idx = Math.min(Math.max(idx, 0), hdrRows.value.length - 1);
  hdrRows.value[idx].value += varChip(v);
}

/* ---------- 打开 / 保存 ---------- */

const jsonText = (v: unknown) => (v == null ? "" : JSON.stringify(v, null, 2));

function openEdit(row: CaseItem) {
  mode.value = "edit";
  editing.value = row;
  Object.assign(form, {
    case_id: row.case_id,
    module: row.module,
    api_name: row.api_name,
    case_desc: row.case_desc,
    priority: row.priority,
    run: row.run,
    method: row.method,
    url: row.url,
    timeout: row.timeout == null ? "" : String(row.timeout),
    body: jsonText(row.body),
  });
  hdrRows.value = objToRows(row.headers);
  prmRows.value = objToRows(row.params);
  extRows.value = objToRows(row.extract);
  asrtRows.value = assertsToRows(row.asserts);
  form.pre_scripts = [...(row.pre_scripts ?? [])];
  form.post_scripts = [...(row.post_scripts ?? [])];
  editVisible.value = true;
}

/** 新增：预置一条最常见的基础断言与默认值，请求配置类字段留空引导填写 */
function openCreate() {
  mode.value = "create";
  editing.value = null;
  copyFrom.value = "";
  Object.assign(form, {
    case_id: "", module: "", api_name: "", case_desc: "", priority: "P2", run: true,
    method: "GET", url: "", timeout: "", body: "", pre_scripts: [], post_scripts: [],
  });
  hdrRows.value = [];
  prmRows.value = [];
  extRows.value = [];
  asrtRows.value = [{ op: "eq", path: "status_code", expected: "200" }];
  editVisible.value = true;
}

/** 复制：按编辑态预填全字段后切到新增态，编号置空强制重新命名 */
function openCopy(row: CaseItem) {
  openEdit(row);
  mode.value = "create";
  editing.value = null;
  copyFrom.value = row.case_id;
  form.case_id = "";
}

/** 采集当前抽屉表单为完整用例对象（新增用）；校验失败返回 null */
function collectAll(): Record<string, unknown> | null {
  const cid = form.case_id.trim();
  if (!cid) { ElMessage.error("用例编号不能为空"); return null; }
  const out: Record<string, unknown> = {};
  for (const key of ["module", "api_name", "priority", "method", "url"] as const) {
    const v = String(form[key]).trim();
    if (!v) { ElMessage.error(`「${key}」不能为空`); return null; }
    out[key] = v;
  }
  out.case_id = cid;
  out.case_desc = form.case_desc.trim() || "新建用例";
  out.run = form.run;
  const to = form.timeout.trim();
  if (to) {
    if (!(Number.isFinite(Number(to)) && Number(to) > 0)) { ElMessage.error("超时必须为正数（秒）"); return null; }
    out.timeout = Number(to);
  }
  if (form.body.trim()) {
    try { out.body = JSON.parse(form.body); }
    catch { ElMessage.error("「请求体」不是合法 JSON，请检查"); return null; }
  }
  const kvSpec: Array<[KvRow[], string, string]> = [
    [hdrRows.value, "headers", "请求头"],
    [prmRows.value, "params", "查询参数"],
    [extRows.value, "extract", "变量提取"],
  ];
  for (const [rows, field, label] of kvSpec) {
    const obj = rowsToObj(rows, label);
    if (obj === null) return null;
    out[field] = Object.keys(obj).length ? obj : null;
  }
  const asserts = rowsToAsserts();
  if (asserts === null) return null;
  if (asserts.length === 0) { ElMessage.error("至少保留一条断言"); return null; }
  out.asserts = asserts;
  if (form.pre_scripts.length) out.pre_scripts = form.pre_scripts;
  if (form.post_scripts.length) out.post_scripts = form.post_scripts;
  return out;
}

/** 只提交与原始行不同的字段（后端 exclude_unset 语义） */
function buildChanges(): Record<string, unknown> | null {
  const row = editing.value!;
  const changes: Record<string, unknown> = {};

  const plain: Array<["module" | "api_name" | "case_desc" | "priority" | "method" | "url", string]> = [
    ["module", row.module], ["api_name", row.api_name], ["case_desc", row.case_desc],
    ["priority", row.priority], ["method", row.method], ["url", row.url],
  ];
  for (const [key, orig] of plain) {
    const v = String(form[key]).trim();
    if (!v) { ElMessage.error(`「${key}」不能为空`); return null; }
    if (v !== orig) changes[key] = v;
  }
  if (form.run !== row.run) changes.run = form.run;

  const to = form.timeout.trim();
  const origTo = row.timeout == null ? "" : String(row.timeout);
  if (to !== origTo) {
    if (!to) changes.timeout = null;
    else if (!(Number.isFinite(Number(to)) && Number(to) > 0)) {
      ElMessage.error("超时必须为正数（秒）"); return null;
    } else changes.timeout = Number(to);
  }

  // JSON 文本域：body
  let bodyVal: unknown = null;
  if (form.body.trim()) {
    try { bodyVal = JSON.parse(form.body); }
    catch { ElMessage.error("「请求体」不是合法 JSON，请检查"); return null; }
  }
  if (normVal(bodyVal) !== normVal(row.body)) changes.body = bodyVal;

  // kv 行编辑器
  const kvSpec: Array<[KvRow[], Record<string, unknown> | null, string, string]> = [
    [hdrRows.value, row.headers, "headers", "请求头"],
    [prmRows.value, row.params, "params", "查询参数"],
    [extRows.value, row.extract, "extract", "变量提取"],
  ];
  for (const [rows, orig, field, label] of kvSpec) {
    const obj = rowsToObj(rows, label);
    if (obj === null) return null;
    if (normVal(obj) !== normVal(orig)) changes[field] = obj;
  }

  // 断言构建器（Excel 断言列不允许为空）
  const asserts = rowsToAsserts();
  if (asserts === null) return null;
  if (asserts.length === 0) { ElMessage.error("至少保留一条断言"); return null; }
  if (normAsserts(asserts) !== normAsserts(row.asserts)) changes.asserts = asserts;

  // 脚本引用：顺序即执行顺序，不能当成集合比对
  const sameList = (a: string[], b: string[]) =>
    a.length === b.length && a.every((v, i) => v === b[i]);
  if (!sameList(form.pre_scripts, row.pre_scripts ?? [])) changes.pre_scripts = form.pre_scripts;
  if (!sameList(form.post_scripts, row.post_scripts ?? [])) changes.post_scripts = form.post_scripts;

  if (Object.keys(changes).length === 0) { ElMessage.info("没有需要保存的改动"); return null; }
  return changes;
}

async function saveEdit() {
  saving.value = true;
  try {
    if (mode.value === "create") {
      const data = collectAll();
      if (!data) return;
      await api.createCase(data, currentFile.value);
      ElMessage.success(`已新增并写回 ${currentProject.value?.name ?? "当前项目"}：${data.case_id}`);
    } else {
      const changes = buildChanges();
      if (!changes || !editing.value) return;
      await api.updateCase(editing.value.case_id, changes, currentFile.value);
      ElMessage.success(`已写回 Excel：${Object.keys(changes).join(", ")}`);
    }
    editVisible.value = false;
    await search();
    meta.value = await api.getCaseMeta(currentFile.value);  // 模块/请求头候选可能因本次改动而新增
    loadProjects();   // 项目用例数随增删变化
  } catch {
    /* 拦截器已提示后端 detail（400 逐行定位 / 409 占用、重号与行锁） */
  } finally {
    saving.value = false;
  }
}

async function removeCase(row: CaseItem) {
  try {
    await api.deleteCase(row.case_id, currentFile.value);
    ElMessage.success(`已删除并写回 ${row.source}：${row.case_id}`);
    await search();
    meta.value = await api.getCaseMeta(currentFile.value);
    loadProjects();
  } catch { /* 拦截器已提示 */ }
}

/* ================= 用例历史（跨批次执行时间线） ================= */
const histVisible = ref(false);
const histLoading = ref(false);
const histRow = ref<CaseItem | null>(null);
const hist = ref<CaseHistoryResp | null>(null);

async function openHistory(row: CaseItem) {
  histRow.value = row;
  hist.value = null;
  histVisible.value = true;
  histLoading.value = true;
  try {
    hist.value = await api.getCaseHistory(row.case_id, 30);
  } catch {
    /* 拦截器已提示 */
  } finally {
    histLoading.value = false;
  }
}

const hStatusType = (s: string) =>
  (({ passed: "success", failed: "danger", broken: "warning", skipped: "info" } as Record<string, any>)[s] ?? "info");
const durFmt = (ms: number) => (ms / 1000).toFixed(2) + "s";
const FAILED = ["failed", "broken"];
const dotColor = (s: string) =>
  (({ passed: "#67c23a", failed: "#f56c6c", broken: "#e6a23c", skipped: "#c0c4cc" } as Record<string, string>)[s] ?? "#909399");
const histTrend = computed(() => {
  const h = hist.value;
  if (!h || !h.found) return "";
  if (h.consec_fail > 0) return `已连续 ${h.consec_fail} 个批次失败`;
  if (h.history[0]?.status === "passed") return "最近一次通过";
  return "";
});
function histRerun() {
  if (histRow.value) router.push({ path: "/execution", query: { keyword: histRow.value.case_id, file: currentFile.value } });
}
function histOpenRun(runId: string) {
  router.push({ path: "/report", query: { runId, caseId: histRow.value?.case_id } });
}

/* ================= 用例依赖管理 ================= */
const depVisible = ref(false);
const depCaseId = ref("");
const depList = ref<string[]>([]);
const depInput = ref("");
const allCaseIds = computed(() => cases.value.map(c => c.case_id));

async function openDeps(row: CaseItem) {
  depCaseId.value = row.case_id;
  depVisible.value = true;
  try {
    const resp = await api.getDeps(row.case_id);
    depList.value = resp.dependencies;
  } catch {
    depList.value = [];
  }
}

async function saveDeps() {
  try {
    await api.setDeps(depCaseId.value, depList.value);
    ElMessage.success("依赖已保存");
    depVisible.value = false;
  } catch { /* 拦截器提示 */ }
}

function addDep() {
  const v = depInput.value.trim();
  if (v && v !== depCaseId.value && !depList.value.includes(v)) {
    depList.value.push(v);
  }
  depInput.value = "";
}

onMounted(async () => {
  // 从工作台（优先级条/模块/KPI）带参跳转而来：预填模块/优先级/关键字
  const q = route.query;
  if (q.module || q.priority || q.keyword) {
    if (q.module) activeModule.value = String(q.module);
    if (q.priority) filters.priority = String(q.priority).split(",").map((s) => s.trim()).filter(Boolean);
    if (q.keyword) filters.keyword = String(q.keyword);
    const parts: string[] = [];
    if (q.module) parts.push(`模块=${q.module}`);
    if (q.priority) parts.push(`优先级=${q.priority}`);
    if (q.keyword) parts.push(`关键字=${q.keyword}`);
    carried.value = parts.join("，");
  }
  await loadProjects();   // 先确定当前项目，再按其文件拉用例
  search();
  if (!isNonApi.value) { try { meta.value = await api.getCaseMeta(currentFile.value); } catch { /* 拦截器已提示 */ } }
  loadScripts();
  loadApiDefs();
  await nextTick();
  tableRef.value?.doLayout?.();   // 首屏固定列布局重算，避免首次渲染操作列热区错位
});
</script>

<template>
  <div class="case-page">
    <!-- 左侧：项目切换 + 模块导航（项目 = 一个 Excel 文件，模块仅在项目内分组，互不污染） -->
    <el-card shadow="never" class="mod-panel">
      <div class="mod-panel-head">用例类型</div>
      <el-radio-group
        :model-value="currentKind"
        size="small"
        class="kind-seg"
        @change="switchKind(($event as 'api' | 'ui' | 'app'))"
      >
        <el-radio-button value="api">接口{{ kindCounts.api ? `(${kindCounts.api})` : "" }}</el-radio-button>
        <el-radio-button value="ui">UI{{ kindCounts.ui ? `(${kindCounts.ui})` : "" }}</el-radio-button>
        <el-radio-button value="app">App{{ kindCounts.app ? `(${kindCounts.app})` : "" }}</el-radio-button>
      </el-radio-group>
      <div class="mod-panel-head" style="margin-top: 10px">用例项目</div>
      <el-select v-model="activeProjectId" style="width: 100%" :loading="!projects.length">
        <el-option v-for="p in projects" :key="p.id" :value="p.id" :label="projectLabel(p)">
          <span class="opt-key">{{ p.name }}{{ p.builtin ? " ⭐" : "" }}</span>
          <span class="opt-val">{{ kindLabel(p.kind) }} · {{ p.case_count }} 条</span>
        </el-option>
      </el-select>
      <div class="proj-file mono" :title="currentFile">{{ currentFile }}</div>
      <div v-if="currentProject?.desc" class="proj-desc" :title="currentProject.desc">{{ currentProject.desc }}</div>
      <div class="proj-ops">
        <el-button v-perm="'case:write'" link type="primary" size="small" @click="openProjCreate">＋ 新建</el-button>
        <el-button v-if="currentProject" v-perm="'case:write'" link type="primary" size="small" @click="openProjEdit">重命名</el-button>
        <el-button v-if="currentProject && !currentProject.builtin" v-perm="'case:delete'" link type="danger" size="small" @click="removeProject">删除</el-button>
      </div>
      <el-divider v-if="!isNonApi" style="margin: 10px 0" />
      <template v-if="!isNonApi">
      <div class="mod-panel-head">模块</div>
      <ul class="mod-list">
        <li :class="{ active: activeModule === '' }" @click="selectModule('')">
          <span class="mod-name">全部用例</span>
          <span class="mod-cnt">{{ total }}</span>
        </li>
        <li v-for="m in modules" :key="m.name" :class="{ active: activeModule === m.name }" @click="selectModule(m.name)">
          <span class="mod-name" :title="m.name">{{ m.name }}</span>
          <span class="mod-cnt">{{ m.count }}</span>
        </li>
      </ul>
      </template>
    </el-card>

    <!-- 右侧：接口项目走原有筛选 + 分页表格；UI/App 项目整体交给对应子视图（自包含拉取/编辑） -->
    <div v-if="!isNonApi" class="case-body">
      <el-alert
        v-if="carried"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
        title="已按工作台条件筛选"
      >
        <template #default>
          <div style="font-size: 12px">{{ carried }}；下方列表与左侧模块已按此范围限定。
            <el-button link type="primary" size="small" @click="clearCarried">清除条件</el-button>
          </div>
        </template>
      </el-alert>
      <el-card shadow="never" style="margin-bottom: 12px">
        <el-form inline @submit.prevent>
          <el-form-item label="优先级">
            <el-select v-model="filters.priority" multiple collapse-tags placeholder="全部" style="width: 180px">
              <el-option v-for="p in prioOptions()" :key="p" :label="p" :value="p" />
            </el-select>
          </el-form-item>
          <el-form-item label="关键字">
            <el-input v-model="filters.keyword" placeholder="编号/接口名/描述/URL" clearable style="width: 200px" />
          </el-form-item>
          <el-form-item label="仅启用">
            <el-switch v-model="filters.onlyEnabled" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="search">查询</el-button>
            <el-button @click="reset">重置</el-button>
            <el-button v-perm="'case:write'" type="success" plain @click="openCreate">＋ 新增用例</el-button>
            <el-button plain @click="openBatchIo">⇪ 批量导入导出</el-button>
            <el-button v-perm="'run:create'" type="warning" plain @click="goExecute">▶ 按当前条件执行</el-button>
          </el-form-item>
        </el-form>
      </el-card>

      <el-card shadow="never">
        <div class="table-head">
          <span class="cur-mod">当前模块：<b>{{ activeModule || "全部用例" }}</b>（{{ visibleCases.length }} 条）</span>
          <div v-if="selected.length" style="display: flex; gap: 8px; align-items: center">
            <span style="color: #606266; font-size: 13px">已选 {{ selected.length }} 条（跨页保留）</span>
            <el-button v-perm="'case:write'" size="small" type="success" plain :loading="batching" @click="batchRun(true)">批量启用</el-button>
            <el-button v-perm="'case:write'" size="small" type="info" plain :loading="batching" @click="batchRun(false)">批量停用</el-button>
            <el-button v-perm="'case:write'" size="small" type="primary" plain @click="openMove">迁移</el-button>
            <el-button size="small" text @click="clearSelection">清空选择</el-button>
          </div>
        </div>
        <el-table ref="tableRef" :data="pagedCases" v-loading="loading" row-key="case_id" size="small" @selection-change="onSelectionChange">
          <el-table-column type="expand">
            <template #default="{ row }">
              <el-descriptions :column="1" border size="small" style="padding: 8px 24px">
                <el-descriptions-item label="Headers">
                  <JsonViewer :data="row.headers" :max-height="200" />
                </el-descriptions-item>
                <el-descriptions-item label="查询参数">
                  <JsonViewer :data="row.params" :max-height="200" />
                </el-descriptions-item>
                <el-descriptions-item label="请求体">
                  <JsonViewer :data="row.body" :max-height="200" />
                </el-descriptions-item>
                <el-descriptions-item label="变量提取">
                  <JsonViewer :data="row.extract" :max-height="200" />
                </el-descriptions-item>
                <el-descriptions-item label="断言">
                  <JsonViewer :data="row.asserts" :max-height="200" />
                </el-descriptions-item>
                <el-descriptions-item label="公共脚本">
                  <span v-if="(row.pre_scripts ?? []).length || (row.post_scripts ?? []).length">
                    前置：{{ (row.pre_scripts ?? []).join(" → ") || "—" }} ｜ 后置：{{ (row.post_scripts ?? []).join(" → ") || "—" }}
                  </span>
                  <span v-else class="dim">未引用</span>
                </el-descriptions-item>
                <el-descriptions-item label="来源">
                  {{ row.source }} 第 {{ row.row }} 行 ｜ 超时 {{ row.timeout ?? "默认" }}
                </el-descriptions-item>
              </el-descriptions>
            </template>
          </el-table-column>
          <el-table-column type="selection" width="45" fixed="left" reserve-selection />
          <el-table-column prop="case_id" label="用例编号" min-width="150">
            <template #default="{ row }"><span class="mono">{{ row.case_id }}</span></template>
          </el-table-column>
          <el-table-column prop="api_name" label="接口名称" min-width="140" />
          <el-table-column prop="case_desc" label="用例描述" min-width="180" show-overflow-tooltip />
          <el-table-column prop="module" label="模块" width="120" />
          <el-table-column label="优先级" width="80">
            <template #default="{ row }"><el-tag :type="prioType(row.priority)" size="small">{{ row.priority }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="method" label="方法" width="80" />
          <el-table-column prop="url" label="请求路径" min-width="160">
            <template #default="{ row }"><span class="mono">{{ row.url }}</span></template>
          </el-table-column>
          <el-table-column label="断言数" width="80">
            <template #default="{ row }">{{ (row.asserts ?? []).length }}</template>
          </el-table-column>
          <el-table-column label="启用" width="70">
            <template #default="{ row }">
              <el-tag :type="row.run ? 'success' : 'info'" size="small">{{ row.run ? "Y" : "N" }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="250" fixed="right">
            <template #default="{ row }">
              <div class="op-cell">
                <el-button size="small" text type="primary" @click="openHistory(row)">历史</el-button>
                <el-button v-perm="'case:write'" size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
                <el-button v-perm="'case:write'" size="small" text type="primary" @click="openCopy(row)">复制</el-button>
                <el-button v-perm="'case:write'" size="small" text type="warning" @click="openDeps(row)">依赖</el-button>
                <el-popconfirm v-perm="'case:delete'" :title="'删除 ' + row.case_id + '？将从 Excel 整行移除'" width="260">
                  <template #reference>
                    <el-button size="small" text type="danger">删除</el-button>
                  </template>
                </el-popconfirm>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="visibleCases.length"
          layout="total, sizes, prev, pager, next, jumper"
          background
          size="small"
          style="margin-top: 12px; justify-content: flex-end"
          @size-change="currentPage = 1"
        />
        <div style="margin-top: 10px; color: #909399; font-size: 13px">
          共 {{ total }} 条 ｜ 当前项目：{{ currentProject?.name ?? "默认项目" }}（{{ currentFile }}）——Excel 仍是唯一用例载体，平台编辑直写回当前项目的 Excel，校验不过不落盘
        </div>
      </el-card>
    </div>
    <UiCasesView v-else-if="isUi" :file="currentFile" :project="currentProject" :projects="projects" @projects-changed="loadProjects" />
    <AppCasesView v-else :file="currentFile" :project="currentProject" :projects="projects" @projects-changed="loadProjects" />

    <!-- 编辑抽屉：下拉候选来自 /api/cases/meta，保存只提交差异字段 -->
    <el-drawer v-model="editVisible" size="52%" :title="mode === 'create' ? (copyFrom ? '复制新增用例' : '新增用例') : `编辑用例 ${editing?.case_id ?? ''}`">
      <el-form label-width="90px" @submit.prevent>
        <el-divider content-position="left">基本信息</el-divider>
        <el-form-item label="用例编号">
          <el-input v-model="form.case_id" :disabled="mode === 'edit'" class="mono"
                    :placeholder="copyFrom ? `新编号（原 ${copyFrom} 的配置已预填）` : '唯一编号，如 album_list_002（创建后不可改）'" />
        </el-form-item>
        <el-form-item label="模块">
          <el-autocomplete
            v-model="form.module"
            :fetch-suggestions="queryModules"
            placeholder="输入任意模块名，或从候选中选已有模块"
            clearable
            style="width: 100%"
          />
          <div class="hint" style="margin: 4px 0 0">模块为自由文本：输入新名称并回车即归入全新模块（不限于下方候选）。</div>
        </el-form-item>
        <el-form-item label="接口名称">
          <el-select v-model="form.api_name" filterable allow-create default-first-option clearable
                     placeholder="选已登记接口可带入推荐配置；也可直接输入新名称" style="width: 100%">
            <el-option v-for="n in apiNameOptions" :key="n" :value="n" :label="n">
              <span class="opt-key">{{ n }}</span>
              <span class="opt-val">{{ defPreview(n) }}</span>
            </el-option>
          </el-select>
          <div class="hint" style="margin: 4px 0 0">
            <template v-if="matchedDef">
              已登记：<span class="mono">{{ matchedDef.method }} {{ matchedDef.path }}</span>
              （{{ SOURCE_LABEL[matchedDef.source] ?? matchedDef.source }}，被 {{ matchedDef.used_by.length }} 条用例引用）
              <el-button link type="primary" size="small" @click="applyDef">带入推荐配置</el-button>
            </template>
            <template v-else-if="form.api_name.trim()">该名称未登记在接口库，仅作为本用例的接口名保存。</template>
            <template v-else>接口名称是用例与接口库的关联锚点：与定义同名才计入覆盖统计，去「接口定义」页可沉淀库。</template>
          </div>
        </el-form-item>
        <el-form-item label="用例描述">
          <el-input v-model="form.case_desc" type="textarea" :rows="2" />
        </el-form-item>
        <el-row :gutter="8">
          <el-col :span="8">
            <el-form-item label="优先级">
              <el-select v-model="form.priority" style="width: 100%">
                <el-option v-for="p in prioOptions()" :key="p" :label="p" :value="p" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="方法">
              <el-select v-model="form.method" style="width: 100%">
                <el-option v-for="m in methodOptions()" :key="m" :label="m" :value="m" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="超时(秒)">
              <el-input v-model="form.timeout" placeholder="留空=默认" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="是否执行">
          <el-switch v-model="form.run" active-text="Y" inactive-text="N" />
        </el-form-item>
        <el-form-item label="请求路径">
          <el-input v-model="form.url" class="mono" />
        </el-form-item>

        <el-divider content-position="left">请求头</el-divider>
        <div class="hint">常用 Header 从下拉选择并自动带出建议值；不在列表中的可直接输入新名称。点击变量标签可在聚焦的值框中插入引用。</div>
        <div v-for="(h, i) in hdrRows" :key="'h' + i" class="kv-row">
          <el-select v-model="h.key" filterable allow-create default-first-option placeholder="Header 名" style="width: 190px" @change="(v: string) => onHdrKey(v, h)">
            <el-option v-for="o in meta?.header_suggest ?? []" :key="o.key" :value="o.key" :label="o.key">
              <span class="opt-key">{{ o.key }}</span>
              <span class="opt-val">{{ o.value }}{{ o.used ? `（${o.used} 条在用）` : "（内置建议）" }}</span>
            </el-option>
          </el-select>
          <el-input v-model="h.value" :placeholder="VAR_PH" class="mono" style="flex: 1" @focus="hdrFocus = i" />
          <el-button text type="danger" @click="hdrRows.splice(i, 1)">✕</el-button>
        </div>
        <el-button size="small" @click="hdrRows.push({ key: '', value: '' })">＋ 添加请求头</el-button>
        <div v-if="meta?.variables?.length" class="var-chips">
          <el-tag v-for="v in meta.variables" :key="v" size="small" class="var-chip" @click="insertVar(v)">{{ varChip(v) }}</el-tag>
        </div>

        <el-divider content-position="left">查询参数</el-divider>
        <div v-for="(p, i) in prmRows" :key="'p' + i" class="kv-row">
          <el-input v-model="p.key" placeholder="参数名" class="mono" style="width: 190px" />
          <el-input v-model="p.value" :placeholder="VAR_PH" class="mono" style="flex: 1" />
          <el-button text type="danger" @click="prmRows.splice(i, 1)">✕</el-button>
        </div>
        <el-button size="small" @click="prmRows.push({ key: '', value: '' })">＋ 添加参数</el-button>

        <el-divider content-position="left">请求体（JSON，留空=清除）</el-divider>
        <el-input v-model="form.body" type="textarea" :rows="5" class="mono" spellcheck="false" />

        <el-divider content-position="left">变量提取</el-divider>
        <div class="hint">变量名 ← 响应 JSONPath，供后续用例 {{ varChip("变量名") }} 引用。</div>
        <div v-for="(x, i) in extRows" :key="'x' + i" class="kv-row">
          <el-input v-model="x.key" placeholder="变量名" class="mono" style="width: 190px" />
          <el-input v-model="x.value" :placeholder="PATH_PH" class="mono" style="flex: 1" />
          <el-button text type="danger" @click="extRows.splice(i, 1)">✕</el-button>
        </div>
        <el-button size="small" @click="extRows.push({ key: '', value: '' })">＋ 添加提取</el-button>

       <el-divider content-position="left">断言（至少一条）</el-divider>
        <!-- 快捷模板：一键追加常用组合 -->
        <div class="assert-presets">
          <span class="hint" style="margin:0;line-height:24px">快捷模板：</span>
          <el-button v-for="p in ASSERT_PRESETS" :key="p.label" size="small" text type="primary"
                     @click="applyAssertPreset(p)">{{ p.label }}</el-button>
        </div>
        <div v-for="(a, i) in asrtRows" :key="'a' + i">
          <div class="kv-row">
            <el-select v-model="a.op" placeholder="验证方式" style="width: 170px">
              <el-option v-for="o in meta?.assert_ops ?? []" :key="o.op" :value="o.op"
                         :label="`${o.desc || opLabel(o.op)}（${o.op}）`" />
            </el-select>
            <el-select v-model="a.path" filterable allow-create default-first-option
                       placeholder="验证什么" style="flex: 1">
              <el-option v-for="p in PATH_PRESETS" :key="p.value" :value="p.value"
                         :label="p.label">
                <span class="opt-key">{{ p.label }}</span>
                <span class="opt-val mono">{{ p.value }}</span>
              </el-option>
            </el-select>
            <el-input v-if="opNeedExpected(a.op)" v-model="a.expected" placeholder="期望值" class="mono" style="width: 170px" />
            <el-button text type="danger" @click="asrtRows.splice(i, 1)">✕</el-button>
          </div>
          <div v-if="a.op && a.path" class="assert-preview">→ {{ assertHint(a) }}</div>
        </div>
        <el-button size="small" @click="asrtRows.push({ op: 'eq', path: '', expected: '' })">＋ 添加断言</el-button>

        <el-divider content-position="left">前后置公共脚本</el-divider>
        <div class="hint">
          前置脚本任一步失败即中断（主请求不发）；后置脚本无论主流程成败都会执行（用来清理造出的数据）。
          多个脚本按选中顺序串行执行，写回 Excel 的「前置脚本 / 后置脚本」列（逗号分隔）。
          <el-button link type="primary" size="small" @click="router.push('/scripts')">去脚本库新建</el-button>
        </div>
        <el-form-item label="前置脚本">
          <el-select v-model="form.pre_scripts" multiple filterable collapse-tags style="width: 100%"
                     placeholder="不引用 = 留空">
            <el-option v-for="s in scriptOptions" :key="s.name" :value="s.name" :label="s.name">
              <span class="opt-key">{{ s.name }}</span>
              <span class="opt-val">{{ s.steps.length }} 步 · {{ s.remark || "无说明" }}</span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="后置脚本">
          <el-select v-model="form.post_scripts" multiple filterable collapse-tags style="width: 100%"
                     placeholder="不引用 = 留空">
            <el-option v-for="s in scriptOptions" :key="s.name" :value="s.name" :label="s.name">
              <span class="opt-key">{{ s.name }}</span>
              <span class="opt-val">{{ s.steps.length }} 步 · {{ s.remark || "无说明" }}</span>
            </el-option>
          </el-select>
        </el-form-item>

        <div class="hint" style="margin: 14px 0 10px">
          <template v-if="mode === 'create'">保存后追加为项目「{{ currentProject?.name ?? currentFile }}」末行{{ copyFrom ? "（复制自 " + copyFrom + "，作为全新用例）" : "" }}；后端跑全文件格式校验，不通过不落盘。</template>
          <template v-else>保存后直写回 {{ editing?.source }} 第 {{ editing?.row }} 行；后端跑全文件格式校验，不通过不落盘。</template>
        </div>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveEdit">{{ mode === "create" ? "新增并写回 Excel" : "保存并写回 Excel" }}</el-button>
      </el-form>
    </el-drawer>

    <!-- 用例历史抽屉：跨批次执行时间线 + 明细（数据源 /api/runs/history/{id}） -->
    <el-drawer v-model="histVisible" size="46%" :title="`用例历史 ${histRow?.case_id ?? ''}`">
      <div v-loading="histLoading">
        <template v-if="hist && hist.found">
          <div class="hist-meta">
            <div class="hist-name">{{ histRow?.api_name }} · {{ histRow?.module }}</div>
            <div class="hist-sub">
              扫描 {{ hist.scanned_batches }} 个批次，该用例出现 {{ hist.found }} 次；
              <el-tag size="small" type="success">通过 {{ hist.passed }}</el-tag>
              <el-tag size="small" type="danger" style="margin-left: 6px">失败 {{ hist.failed }}</el-tag>
              <span v-if="histTrend" class="hist-trend">{{ histTrend }}</span>
            </div>
          </div>
          <div class="hist-bar">
            <el-tooltip v-for="h in [...hist.history].reverse()" :key="'b' + h.run_id" placement="top">
              <template #content>{{ h.run_id }}<br />{{ h.status }} · {{ h.started_at ?? "—" }}</template>
              <div class="hist-dot" :style="{ background: dotColor(h.status) }" @click="histOpenRun(h.run_id)" />
            </el-tooltip>
          </div>
          <div class="hist-legend">旧 ← 时间轴（点色块看该批次该用例明细）→ 新</div>
          <el-table :data="hist.history" size="small" row-key="run_id" style="margin-top: 10px">
            <el-table-column label="状态" width="84">
              <template #default="{ row }"><el-tag :type="hStatusType(row.status)" size="small">{{ row.status }}</el-tag></template>
            </el-table-column>
            <el-table-column label="批次" min-width="210">
              <template #default="{ row }">
                <div class="mono link" @click="histOpenRun(row.run_id)">{{ row.run_id }}</div>
                <div class="hist-time">{{ row.started_at ?? "—" }} · {{ row.source }}</div>
              </template>
            </el-table-column>
            <el-table-column label="结果摘要" min-width="220">
              <template #default="{ row }"><span :class="FAILED.includes(row.status) ? 'txt-fail' : 'txt-ok'">{{ row.summary || row.status }}</span></template>
            </el-table-column>
            <el-table-column label="耗时" width="80">
              <template #default="{ row }">{{ durFmt(row.duration_ms) }}</template>
            </el-table-column>
          </el-table>
          <el-button type="warning" plain size="small" style="margin-top: 14px" @click="histRerun">▶ 重跑该用例</el-button>
        </template>
        <el-empty v-else-if="hist && !hist.found" description="近期批次中未见该用例的执行记录">
          <el-button type="warning" plain @click="histRerun">▶ 立即执行该用例</el-button>
        </el-empty>
      </div>
    </el-drawer>

    <!-- 用例依赖管理弹窗 -->
    <el-dialog v-model="depVisible" :title="`配置依赖 ${depCaseId}`" width="480px">
      <p style="color:#606266; font-size:13px; margin:0 0 12px">执行此用例前，以下前置用例必须全部通过。留空表示无依赖。<br/>按条件/全量执行时会自动纳入这些依赖用例并排到前面；依赖失败或被停用时，本用例将被阻塞（不发请求，报告记 skipped）。</p>
      <div v-for="(d, i) in depList" :key="i" style="display:flex; align-items:center; gap:8px; margin-bottom:6px">
        <el-tag closable @close="depList.splice(i, 1)">{{ d }}</el-tag>
      </div>
      <el-select v-model="depInput" filterable allow-create default-first-option placeholder="输入或选择前置用例 ID"
                 style="width: 100%; margin-top: 8px" @keyup.enter="addDep">
        <el-option v-for="cid in allCaseIds.filter(c => c !== depCaseId && !depList.includes(c))" :key="cid" :label="cid" :value="cid" />
      </el-select>
      <el-button size="small" style="margin-top:8px" @click="addDep">＋ 添加</el-button>
      <template #footer>
        <el-button @click="depVisible = false">取消</el-button>
        <el-button type="primary" @click="saveDeps">保存</el-button>
      </template>
    </el-dialog>

    <!-- 批量导入导出弹窗：复用 UI 页的 BatchIoDialog 布局壳（智能导入预览确认 / 直接导入） -->
    <BatchIoDialog
      :visible="ioVisible"
      :mode="ioMode"
      title="批量导入导出接口用例"
      :channels="IO_CHANNELS"
      :actions="IO_ACTIONS"
      :writable="ioWritable"
      :busy="smartLoading"
      alert-text="Excel：下载模板填写后上传——智能导入先预览转换标注再勾选确认（只写入勾选行），直接导入即传即导；用例编号已存在自动跳过。"
      @update:visible="(v: boolean) => (ioVisible = v)"
      @update:mode="(v: string) => (ioMode = v as 'smart' | 'direct')"
      @action="onIoAction"
      @file="onIoFile"
    >
      <div v-loading="smartLoading">
        <!-- 智能导入：预览表（默认勾选可导入且不冲突的行） -->
        <template v-if="ioMode === 'smart'">
          <el-empty v-if="!smartData && !smartLoading" description="下载模板填写后，点「选择文件上传」解析预览" :image-size="60" />
          <template v-if="smartData">
            <div style="margin-bottom: 8px; font-size: 12px; color: #606266">
              工作表「{{ smartData.sheet }}」解析 {{ smartData.items.length }} 行 ·
              {{ smartData.format === 'standard' ? '标准格式' : '非标准表头，已自动映射转换' }}。
              <b>黄色</b>=已自动补全/猜测（默认值、兜底断言），请核对；<b>红色</b>=无法导入。
            </div>
            <div class="smart-cols">
              <el-tag v-for="cm in smartData.column_map" :key="cm.header" size="small" effect="plain">
                {{ cm.header }} → {{ FIELD_LABEL[cm.field] ?? cm.field }}
              </el-tag>
              <el-tag v-for="h in smartData.unmapped_headers" :key="h" size="small" type="info" effect="plain">
                {{ h }}（已忽略）
              </el-tag>
            </div>
            <el-table ref="smartTableRef" :data="smartData.items" size="small" max-height="440"
                      row-key="row" :row-class-name="smartRowClass" style="margin-top: 8px"
                      @selection-change="(rows: ImportPreviewItem[]) => (smartSelected = rows)">
              <el-table-column type="selection" width="42" fixed="left" :selectable="smartSelectable" reserve-selection />
              <el-table-column label="编号" width="120">
                <template #default="{ row }"><span class="mono">{{ row.case.case_id }}</span></template>
              </el-table-column>
              <el-table-column label="描述" min-width="120" show-overflow-tooltip>
                <template #default="{ row }">{{ row.case.case_desc }}</template>
              </el-table-column>
              <el-table-column label="方法" width="66">
                <template #default="{ row }">{{ row.case.method }}</template>
              </el-table-column>
              <el-table-column label="请求路径" min-width="170" show-overflow-tooltip>
                <template #default="{ row }"><span class="mono">{{ row.case.url }}</span></template>
              </el-table-column>
              <el-table-column label="断言" min-width="150" show-overflow-tooltip>
                <template #default="{ row }"><span class="mono dim">{{ JSON.stringify(row.case.asserts ?? []) }}</span></template>
              </el-table-column>
              <el-table-column label="转换标注" min-width="230">
                <template #default="{ row }">
                  <div v-for="(m, i) in row.marks" :key="i" class="smart-mark" :class="'mk-' + m.level">
                    {{ m.level === 'error' ? '✖' : m.level === 'warn' ? '⚠' : 'ℹ' }} {{ m.msg }}
                  </div>
                  <div v-if="row.exists" class="smart-mark mk-warn">⚠ 编号已存在于目标文件，将自动跳过</div>
                  <span v-if="!row.marks.length && !row.exists" class="smart-mark mk-ok">✔ 完整识别</span>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </template>
        <!-- 直接导入：即传即导，结果落下方结果区 -->
        <template v-else>
          <el-empty v-if="!directResult && !smartLoading"
                    description="点「选择文件上传」即传即导：按用例编号追加，已存在自动跳过，格式异常逐条报因" :image-size="60" />
        </template>
      </div>
    
      <template #result>
        <div v-if="directResult" style="margin-top: 4px; font-size: 13px">
          <el-tag type="success" size="small">成功 {{ directResult.imported }} 条</el-tag>
          <el-tag type="warning" size="small" style="margin-left: 6px">跳过已有 {{ directResult.skipped }} 条</el-tag>
          <el-tag v-if="directResult.errors.length" type="danger" size="small" style="margin-left: 6px">格式异常 {{ directResult.errors.length }} 条</el-tag>
          <ul v-if="directResult.errors.length" style="margin: 6px 0 0; padding-left: 18px; color: #c45656; max-height: 160px; overflow: auto">
            <li v-for="e in directResult.errors" :key="e" class="mono">{{ e }}</li>
          </ul>
        </div>
      </template>
      <template #footer-info>
        <template v-if="ioMode === 'smart' && smartData">已选 {{ smartSelected.length }} 条</template>
      </template>
      <template #footer>
        <el-button v-if="ioMode === 'smart'" type="primary" :loading="smartSaving"
                   :disabled="!smartSelected.length" @click="doSmartImport">确认导入 {{ smartSelected.length }} 条</el-button>
      </template>
    </BatchIoDialog>

    <!-- 项目新建 / 重命名弹窗（后端自动脚手架空用例文件，重名 409） -->
    <el-dialog v-model="projDlgVisible" :title="projEditingId ? '编辑项目' : '新建用例项目'" width="440px">
      <el-form label-width="72px" @submit.prevent>
        <el-form-item label="名称">
          <el-input v-model="projForm.name" maxlength="40" placeholder="被测系统名，如：订单中心" />
        </el-form-item>
        <el-form-item v-if="!projEditingId" label="类型">
          <el-radio-group v-model="projForm.kind">
            <el-radio value="api">接口用例</el-radio>
            <el-radio value="ui">UI 用例</el-radio>
            <el-radio value="app">App 用例</el-radio>
          </el-radio-group>
          <div class="hint" style="margin: 4px 0 0">创建后不可改：接口用例基于 Excel，UI/App 用例基于可视化步骤（分别 JSON / .apjson）。</div>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="projForm.desc" type="textarea" :rows="2" placeholder="选填：项目用途、负责人等" />
        </el-form-item>
      </el-form>
      <div class="hint" style="margin: 0">
        <template v-if="projEditingId">项目文件路径不可变，重命名不影响定时任务/场景引用。</template>
        <template v-else>新建会在 data/cases/ 下生成一张空用例载体（{{ projForm.kind === "ui" ? "JSON 步骤表" : projForm.kind === "app" ? "App 步骤表(.apjson)" : "Excel（仅表头）" }}），切换到该项目后即可新增用例；不同项目的模块彼此隔离。</template>
      </div>
      <template #footer>
        <el-button @click="projDlgVisible = false">取消</el-button>
        <el-button type="primary" :loading="projSaving" @click="saveProj">保存</el-button>
      </template>
    </el-dialog>

    <!-- 用例迁移弹窗（批量）：勾选多条 → 选目标项目 → 串行逐条迁移（目标查重后写入并删源） -->
    <el-dialog v-model="moveVisible" title="迁移用例到其他项目" width="480px">
      <el-form label-width="82px" @submit.prevent>
        <el-form-item label="迁移范围">
          <span>已勾选 <b>{{ selected.length }}</b> 条用例</span>
          <span class="dim" style="margin-left: 8px">{{ selected.slice(0, 3).map(c => c.case_id).join(", ") }}{{ selected.length > 3 ? ` 等 ${selected.length} 条` : "" }}</span>
        </el-form-item>
        <el-form-item label="源项目">
          <span>{{ currentProject?.name ?? currentFile }}</span>
        </el-form-item>
        <el-form-item label="目标项目">
          <el-select v-model="moveForm.toProjectId" style="width: 100%" placeholder="选择要迁入的项目" @change="onMoveTargetChange">
            <el-option v-for="p in moveProjects" :key="p.id" :value="p.id" :label="p.name">
              <span class="opt-key">{{ p.name }}{{ p.builtin ? " ⭐" : "" }}</span>
              <span class="opt-val">{{ p.case_count }} 条</span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="归入模块">
          <el-select v-model="moveForm.new_module" filterable allow-create default-first-option clearable
                     style="width: 100%" placeholder="留空=保留原模块；可选目标项目已有模块或输入新模块">
            <el-option v-for="m in moveTargetModules" :key="m" :value="m" :label="m" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="hint" style="margin: 0">
        迁移会先在目标项目查重并写入（目标已有同编号用例会被拒绝），再从源项目删除；失败条数会计入报告。建议一次不要勾选过多。
      </div>
      <template #footer>
        <el-button @click="moveVisible = false">取消</el-button>
        <el-button type="primary" :loading="moveSaving" @click="doMove">确认迁移 {{ selected.length }} 条</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.json { margin: 0; font-family: Consolas, Monaco, monospace; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
.kv-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.hint { color: #909399; font-size: 12px; margin-bottom: 8px; }
.op-cell { display: flex; align-items: center; gap: 2px; white-space: nowrap; }
.op-cell .el-button + .el-button { margin-left: 0; }
.grp-title { font-weight: 600; font-size: 13px; }
/* 主从布局：左侧模块导航 + 右侧分页表格 */
.case-page { display: flex; align-items: flex-start; gap: 12px; }
.mod-panel { width: 200px; flex: 0 0 200px; }
.mod-panel :deep(.el-card__body) { padding: 8px; }
.mod-panel-head { font-weight: 600; font-size: 13px; color: #303133; padding: 4px 8px 8px; }
.kind-seg { display: flex; width: 100%; }
.kind-seg :deep(.el-radio-button) { flex: 1; }
.kind-seg :deep(.el-radio-button__inner) { width: 100%; padding: 7px 4px; font-size: 12px; }
.mod-list { list-style: none; margin: 0; padding: 0; max-height: calc(100vh - 320px); overflow: auto; }
.mod-list li { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 7px 10px; border-radius: 6px; cursor: pointer; font-size: 13px; color: #606266; }
.mod-list li:hover { background: #f0f2f5; }
.mod-list li.active { background: #409eff; color: #fff; font-weight: 600; }
.mod-list li.active .mod-cnt { background: rgba(255, 255, 255, 0.25); color: #fff; }
.mod-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mod-cnt { flex: 0 0 auto; min-width: 22px; text-align: center; font-size: 12px; line-height: 18px; padding: 0 6px; border-radius: 9px; background: #ecf0f5; color: #909399; }
/* 项目切换器 */
.proj-file { margin-top: 6px; font-size: 11px; color: #a0a4a8; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.proj-desc { margin-top: 4px; font-size: 12px; color: #909399; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.proj-ops { margin-top: 6px; display: flex; gap: 4px; }
.case-body { flex: 1; min-width: 0; }
.table-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.cur-mod { color: #606266; font-size: 13px; }
.opt-key { float: left; }
.opt-val { float: right; color: #a0a4a8; font-size: 12px; margin-left: 16px; }
.var-chips { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }
.var-chip { cursor: pointer; font-family: Consolas, Monaco, monospace; }
/* 用例历史抽屉 */
.hist-meta { margin-bottom: 12px; }
.hist-name { font-weight: 600; font-size: 14px; color: #303133; }
.hist-sub { margin-top: 4px; font-size: 12px; color: #909399; display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
.hist-trend { margin-left: 8px; color: #f56c6c; font-weight: 600; }
.hist-bar { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; padding: 8px; background: #f7f8fa; border-radius: 6px; }
.hist-dot { width: 14px; height: 14px; border-radius: 3px; cursor: pointer; transition: transform 0.12s ease; }
.hist-dot:hover { transform: scale(1.35); }
.hist-legend { margin-top: 4px; font-size: 11px; color: #a0a4a8; }
.hist-time { font-size: 12px; color: #909399; }
.link { color: #409eff; cursor: pointer; }
.txt-fail { color: #f56c6c; }
.txt-ok { color: #67c23a; }
.dim { color: #909399; font-size: 12px; }
.assert-preview { font-size: 12px; color: #909399; margin: -2px 0 6px 4px; padding-left: 4px; }
.assert-presets { margin-bottom: 10px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
/* 智能导入预览 */
.smart-cols { display: flex; flex-wrap: wrap; gap: 6px; }
.smart-mark { font-size: 12px; line-height: 1.6; }
.mk-warn { color: #e6a23c; }
.mk-error { color: #f56c6c; }
.mk-info { color: #9099a6; }
.mk-ok { color: #67c23a; }
:deep(.row-err) { background: #fef0f0 !important; }
:deep(.row-warn) { background: #fdf6ec !important; }
:deep(.row-exist) { color: #a8abb2; }
</style>
