<script setup lang="ts">
/**
 * UI 用例管理（项目 kind=ui）：可视化步骤用例的列表 / 筛选 / 增删改 / 跨项目迁移。
 *
 * 与接口用例（CasesView 的 Excel 形态）分轨：数据全部走 /api/ui-cases，载体是 JSON。
 * 本页自包含拉取逻辑，CasesView 在选中 UI 项目时整体委托给它，接口链路互不影响。
 */
import { computed, nextTick, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { hasPerm } from "../stores/auth";
import type { Project, UiCaseItem, UiCaseMeta, UiExtract, UiImportPreviewResp, UiStep, UiSession } from "../types";
import UiStepEditor from "../components/UiStepEditor.vue";
import BatchIoDialog from "../components/BatchIoDialog.vue";

const props = defineProps<{
  file: string;
  project: Project | null;
  projects: Project[];
}>();
const emit = defineEmits<{ (e: "projects-changed"): void }>();

const router = useRouter();

interface KvRow { key: string; value: string }

const loading = ref(false);
const cases = ref<UiCaseItem[]>([]);
const total = ref(0);
const meta = ref<UiCaseMeta | null>(null);
const variables = ref<string[]>([]);
const sessions = ref<UiSession[]>([]);   // 可引用的登录态会话
const filters = reactive({ priority: [] as string[], keyword: "", onlyEnabled: false });
const activeModule = ref("");   // "" = 全部
const pageSize = ref(10);
const currentPage = ref(1);

const prioType = (p: string) =>
  ({ P0: "danger", P1: "warning", P2: "", P3: "info", P4: "info", P5: "info" }[p] ?? "");

async function loadMeta() {
  if (!props.file) return;
  try {
    meta.value = await api.getUiCaseMeta(props.file);
    variables.value = [];
    const v = await api.getUiCaseVariables(props.file);
    variables.value = [...(v.excel_variables ?? []), ...(v.secret_variables ?? [])];
  } catch { /* 拦截器已提示 */ }
  // 会话库是全局资源，与项目无关；拉一次供「引用登录态」下拉
  try { sessions.value = await api.listUiSessions(); } catch { sessions.value = []; }
}

async function search() {
  if (!props.file) return;
  loading.value = true;
  try {
    const resp = await api.listUiCases({
      file: props.file,
      module: activeModule.value || undefined,
      priority: filters.priority.length ? filters.priority.join(",") : undefined,
      keyword: filters.keyword.trim() || undefined,
      run: filters.onlyEnabled ? true : undefined,
    });
    cases.value = resp.items;
    total.value = resp.total;
  } catch {
    cases.value = [];
    total.value = 0;
  } finally {
    loading.value = false;
  }
}

/** 模块计数（基于已加载列表，按 zh 排序）；UI 列表已由后端按 module 过滤，这里统计全项目 */
const modules = computed(() => {
  const c = new Map<string, number>();
  for (const it of cases.value) c.set(it.module, (c.get(it.module) ?? 0) + 1);
  return [...c.entries()].map(([name, count]) => ({ name, count })).sort((a, b) => a.name.localeCompare(b.name, "zh"));
});

const sortedCases = computed(() =>
  [...cases.value].sort((a, b) => a.module.localeCompare(b.module, "zh") || a.case_id.localeCompare(b.case_id))
);
const pagedCases = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value;
  return sortedCases.value.slice(start, start + pageSize.value);
});

// 固定列（选择/操作）在数据变化后重算布局，避免翻页热区错位（同 CasesView 实践）
const tableRef = ref<{ doLayout: () => void } | null>(null);
watch(pagedCases, () => nextTick(() => tableRef.value?.doLayout()));

function selectModule(m: string) {
  activeModule.value = m;
  currentPage.value = 1;
  search();
}
function reset() {
  filters.priority = [];
  filters.keyword = "";
  filters.onlyEnabled = false;
  activeModule.value = "";
  currentPage.value = 1;
  search();
}

/* ================= 选择 + 迁移 ================= */
const selected = ref<UiCaseItem[]>([]);
function onSelectionChange(rows: UiCaseItem[]) {
  selected.value = rows;
}
function clearSelection() {
  selected.value = [];
}

const uiProjects = computed(() => props.projects.filter((p) => p.kind === "ui" && p.file !== props.file));
const moveVisible = ref(false);
const moveSaving = ref(false);
const moveForm = reactive({ toProjectId: "", new_module: "" });

function openMove() {
  if (!selected.value.length) { ElMessage.warning("请先勾选要迁移的用例"); return; }
  moveForm.toProjectId = "";
  moveForm.new_module = "";
  moveVisible.value = true;
}

async function doMove() {
  const target = props.projects.find((p) => p.id === moveForm.toProjectId);
  if (!target) { ElMessage.warning("请选择目标 UI 项目"); return; }
  moveSaving.value = true;
  let ok = 0;
  const failed: string[] = [];
  for (const c of selected.value) {
    try {
      await api.moveUiCase(c.case_id, { to_file: target.file, new_module: moveForm.new_module.trim() || undefined }, props.file);
      ok++;
    } catch {
      failed.push(c.case_id);
    }
  }
  moveSaving.value = false;
  if (failed.length) ElMessage.warning(`已迁移 ${ok} 条到「${target.name}」，失败 ${failed.length} 条（${failed.join(", ")}）`);
  else ElMessage.success(`已迁移 ${ok} 条用例到「${target.name}」`);
  moveVisible.value = false;
  clearSelection();
  await search();
  await loadMeta();
  emit("projects-changed");
}

/* ================= 批量导入导出（Excel 一步一行 / JSON 双通道 + 按通道导出） ================= */
const importVisible = ref(false);
const importMode = ref<"excel" | "json">("excel");
const importSaving = ref(false);
const ioWritable = computed(() => hasPerm("case:write"));
// 提示条按通道切换，避免 JSON 通道展示 Excel 专属说明
const ioAlertText = computed(() => importMode.value === "excel"
  ? "Excel：一步一行、同编号连续行属同一用例，动作可填中文（打开/点击/输入…）；编号重复自动跳过，字段非法逐条报因、不阻断其他条，单次上限 64 条。"
  : "JSON：粘贴模板 JSON（含 cases 数组）或上传 .json，逐条校验后落库；编号重复自动跳过，非法逐条报因，单次上限 64 条。");
// 导出说明两通道通用，附在尾部
const ioAlertFull = computed(() => ioAlertText.value + "「⬆ 导出项目用例」按当前通道格式与筛选条件导出，编辑后可直接回导。");
const importResult = ref<{ imported: number; imported_ids: string[]; skipped: string[]; errors: string[] } | null>(null);

// JSON 通道
const importRaw = ref("");
// Excel 通道
const excelPreview = ref<UiImportPreviewResp | null>(null);
const excelParsing = ref(false);

function resetImport() {
  importRaw.value = "";
  excelPreview.value = null;
  importResult.value = null;
  importSaving.value = false;
  excelParsing.value = false;
}

function openImport() {
  resetImport();
  importMode.value = "excel";
  importVisible.value = true;
}

function switchMode(m: "excel" | "json") {
  if (importMode.value === m) return;
  importMode.value = m;
  importResult.value = null;
}

async function downloadTemplate(fmt: string) {
  try {
    const blob = await api.downloadUiCaseTemplate(fmt as "json" | "excel");
    const isExcel = fmt === "excel";
    const url = URL.createObjectURL(new Blob([blob], {
      type: isExcel ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : "application/json",
    }));
    const a = document.createElement("a");
    a.href = url;
    a.download = isExcel ? "ui_cases_template.xlsx" : "ui_cases_template.json";
    a.click();
    URL.revokeObjectURL(url);
  } catch { /* 拦截器已提示 */ }
}

// 弹窗内文字动作：模板下载（excel/json）或导出项目用例
function onIoAction(key: string) {
  if (key === "export") doExport();
  else downloadTemplate(key);
}

// 导出项目用例：按当前通道格式（Excel 通道→一步一行 xlsx，JSON 通道→同构 json），带当前筛选条件
async function doExport() {
  try {
    const fmt = importMode.value;
    const blob = await api.exportUiCases(props.file, fmt, {
      module: activeModule.value || undefined,
      priority: filters.priority.length ? filters.priority.join(",") : undefined,
      keyword: filters.keyword.trim() || undefined,
      run: filters.onlyEnabled ? true : undefined,
    });
    const url = URL.createObjectURL(new Blob([blob], {
      type: fmt === "excel" ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : "application/json",
    }));
    const a = document.createElement("a");
    a.href = url;
    a.download = fmt === "excel" ? "ui_cases_export.xlsx" : "ui_cases_export.json";
    a.click();
    URL.revokeObjectURL(url);
    ElMessage.success(fmt === "excel"
      ? "已按当前筛选导出 Excel（一步一行，编辑后可直接回导）"
      : "已按当前筛选导出 JSON（与导入结构同构，无损可回导）");
  } catch { /* 拦截器已提示 */ }
}

// 解析粘贴/上传的 JSON：兼容 {cases:[…]} 模板形态与裸数组 […]
const importParsed = computed<{ ok: boolean; cases: Record<string, unknown>[]; err: string }>(() => {
  const text = importRaw.value.trim();
  if (!text) return { ok: false, cases: [], err: "" };
  try {
    const data = JSON.parse(text);
    const arr = Array.isArray(data) ? data : data?.cases;
    if (!Array.isArray(arr)) return { ok: false, cases: [], err: "JSON 需为用例数组，或含 cases 数组的对象（同模板结构）" };
    return { ok: true, cases: arr, err: "" };
  } catch (e) {
    return { ok: false, cases: [], err: `JSON 解析失败：${(e as Error).message}` };
  }
});

// 上传文件分流：按后缀——.xlsx/.xlsm 走 Excel 预览，.json 走 JSON 粘贴框（BatchIoDialog 壳透传原始 File）
async function onIoFile(rawFile: File) {
  const lower = rawFile.name.toLowerCase();
  if (lower.endsWith(".xlsx") || lower.endsWith(".xlsm")) {
    switchMode("excel");
    importResult.value = null;
    excelParsing.value = true;
    excelPreview.value = null;
    try {
      excelPreview.value = await api.previewUiExcel(rawFile, props.file);
    } catch { /* 拦截器已提示 */ } finally {
      excelParsing.value = false;
    }
  } else if (lower.endsWith(".json")) {
    switchMode("json");
    rawFile.text().then((t) => { importRaw.value = t; }).catch(() => ElMessage.error("读取文件失败"));
  } else {
    ElMessage.warning("只支持 .xlsx 或 .json 文件");
  }
}

const excelImportable = computed(() =>
  (excelPreview.value?.items ?? []).filter((i) => i.importable && !i.exists)
);

async function doImport() {
  if (importMode.value === "excel") {
    const cases = excelImportable.value.map((i) => i.case as Record<string, unknown>);
    if (!cases.length) { ElMessage.error("没有可导入的用例（请检查预览中的标红项）"); return; }
    importSaving.value = true;
    try {
      const resp = await api.importUiCases(cases, props.file);
      importResult.value = resp;
      if (resp.imported) {
        await search(); await loadMeta(); emit("projects-changed");
        excelPreview.value = null;  // 导入后清空预览，避免重复提交
      }
    } catch { /* 拦截器已提示 */ } finally {
      importSaving.value = false;
    }
    return;
  }
  // JSON 通道
  const parsed = importParsed.value;
  if (!parsed.ok) { ElMessage.error(parsed.err || "请先粘贴或上传 JSON"); return; }
  if (!parsed.cases.length) { ElMessage.error("cases 为空数组，没有可导入的用例"); return; }
  importSaving.value = true;
  try {
    const resp = await api.importUiCases(parsed.cases, props.file);
    importResult.value = resp;
    if (resp.imported) { await search(); await loadMeta(); emit("projects-changed"); }
  } catch { /* 拦截器已提示 */ } finally {
    importSaving.value = false;
  }
}

function markLevel(m: { level: string }): "info" | "warning" | "danger" {
  return m.level === "error" ? "danger" : m.level === "warn" ? "warning" : "info";
}

/* ================= 编辑抽屉 ================= */
const editVisible = ref(false);
const saving = ref(false);
const mode = ref<"create" | "edit">("create");
const editingId = ref("");
const form = reactive({
  case_id: "", module: "", name: "", desc: "", priority: "P2",
  run: true, browser: "chromium", headless: true, session: "", retry: 0,
});
const steps = ref<UiStep[]>([]);
const extractRows = ref<KvRow[]>([]);

const browserOptions = computed(() => meta.value?.browsers ?? ["chromium"]);
const prioOptions = computed(() => meta.value?.priorities ?? ["P0", "P1", "P2", "P3", "P4", "P5"]);
const moduleOptions = computed(() => meta.value?.modules ?? []);

// 会话状态中文标签（供「引用登录态」下拉标注可用性）
function statusLabel(s?: string) {
  return { valid: "🟢有效", expired: "🟡已过期", not_collected: "⚪未采集", failed: "🔴采集失败" }[s ?? ""] ?? "—";
}

function blankCase() {
  mode.value = "create";
  editingId.value = "";
  Object.assign(form, { case_id: "", module: activeModule.value || "", name: "", desc: "", priority: "P2", run: true, browser: "chromium", headless: true, session: "", retry: 0 });
  steps.value = [{ action: "goto", value: "/" }];
  extractRows.value = [];
  editVisible.value = true;
}

function fillFrom(c: UiCaseItem) {
  mode.value = "edit";
  editingId.value = c.case_id;
  Object.assign(form, {
    case_id: c.case_id, module: c.module, name: c.name, desc: c.desc,
    priority: c.priority, run: c.run, browser: c.browser, headless: c.headless,
    session: c.session ?? "", retry: c.retry ?? 0,
  });
  steps.value = (c.steps ?? []).map((s) => ({ ...s }));
  extractRows.value = (c.extract ?? []).map((e) => ({ key: e.var, value: e.source }));
  editVisible.value = true;
}

async function openEdit(row: UiCaseItem) {
  try {
    fillFrom(await api.getUiCase(row.case_id, props.file));
  } catch { /* 拦截器已提示 */ }
}

function openCopy(row: UiCaseItem) {
  fillFrom({ ...row, case_id: "" });
  mode.value = "create";
  editingId.value = "";
}

async function removeCase(row: UiCaseItem) {
  try {
    await api.deleteUiCase(row.case_id, props.file);
    ElMessage.success(`用例「${row.case_id}」已删除`);
    await search();
    await loadMeta();
    emit("projects-changed");
  } catch { /* 拦截器已提示 */ }
}

async function saveCase() {
  if (!form.case_id.trim()) { ElMessage.error("用例编号不能为空"); return; }
  if (!steps.value.length) { ElMessage.error("至少需要一个步骤"); return; }
  const extract: UiExtract[] = [];
  for (const r of extractRows.value) {
    const k = r.key.trim();
    if (!k) continue;
    if (!r.value.trim()) { ElMessage.error(`变量提取「${k}」缺少来源`); return; }
    extract.push({ var: k, source: r.value.trim() });
  }
  const payload: Record<string, unknown> = {
    case_id: form.case_id.trim(), module: form.module.trim() || "未分类",
    name: form.name.trim() || "新建UI用例", desc: form.desc.trim(),
    priority: form.priority, run: form.run, browser: form.browser, headless: form.headless,
    steps: steps.value, extract, session: form.session.trim() || null,
    retry: form.retry ?? 0,
  };
  saving.value = true;
  try {
    if (mode.value === "create") {
      await api.createUiCase(payload, props.file);
      ElMessage.success("用例已创建并落盘");
    } else {
      await api.updateUiCase(editingId.value, payload, props.file);
      ElMessage.success("用例已保存并落盘");
    }
    editVisible.value = false;
    await search();
    await loadMeta();
    emit("projects-changed");
  } catch { /* 拦截器已提示（校验不过后端 400 不落盘） */ } finally {
    saving.value = false;
  }
}

async function goExecute() {
  const mod = activeModule.value;
  const kw = filters.keyword.trim();
  const pr = filters.priority.length ? filters.priority.join(",") : undefined;
  const q: Record<string, string> = { file: props.file };
  if (mod) q.module = mod;
  if (kw) q.keyword = kw;
  if (pr) q.priority = pr;
  router.push({ path: "/execution", query: q });
}

// 切换项目文件：重新拉取列表 + 元数据，清空分页与选择
watch(
  () => props.file,
  () => {
    activeModule.value = "";
    currentPage.value = 1;
    clearSelection();
    loadMeta();
    search();
  }
);

// 首次挂载由父级 onMounted 触发，这里确保有 file 时拉一次
watch(
  () => [props.file, props.projects.length],
  () => { if (props.file && !cases.value.length && !meta.value) { loadMeta(); search(); } },
  { immediate: true }
);
</script>

<template>
  <div class="ui-case-body">
    <el-card shadow="never" style="margin-bottom: 12px">
      <el-form inline @submit.prevent>
        <el-form-item label="优先级">
          <el-select v-model="filters.priority" multiple collapse-tags placeholder="全部" style="width: 160px">
            <el-option v-for="p in prioOptions" :key="p" :label="p" :value="p" />
          </el-select>
        </el-form-item>
        <el-form-item label="关键字">
          <el-input v-model="filters.keyword" placeholder="编号/名称/描述" clearable style="width: 200px" @keyup.enter="search" />
        </el-form-item>
        <el-form-item label="仅启用">
          <el-switch v-model="filters.onlyEnabled" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="search">查询</el-button>
          <el-button @click="reset">重置</el-button>
          <el-button v-perm="'case:write'" type="success" plain @click="blankCase">＋ 新增 UI 用例</el-button>
          <el-button plain @click="openImport">⇪ 批量导入导出</el-button>
          <el-button v-perm="'run:create'" type="warning" plain @click="goExecute">▶ 按当前条件执行</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <div class="table-head">
        <span class="cur-mod">
          当前项目：<b>{{ project?.name ?? "UI 项目" }}</b>（共 {{ total }} 条）
          <template v-if="modules.length">
            ｜
            <el-link :type="activeModule === '' ? 'primary' : 'default'" :underline="false" style="margin: 0 4px" @click="selectModule('')">全部</el-link>
            <el-link v-for="m in modules" :key="m.name" :type="activeModule === m.name ? 'primary' : 'default'" :underline="false" style="margin: 0 4px" @click="selectModule(m.name)">{{ m.name }}({{ m.count }})</el-link>
          </template>
        </span>
        <div v-if="selected.length" style="display: flex; gap: 8px; align-items: center">
          <span style="color: #606266; font-size: 13px">已选 {{ selected.length }} 条</span>
          <el-button v-perm="'case:write'" size="small" type="primary" plain @click="openMove">迁移</el-button>
          <el-button size="small" text @click="clearSelection">清空选择</el-button>
        </div>
      </div>

      <el-table ref="tableRef" :data="pagedCases" v-loading="loading" row-key="case_id" size="small" @selection-change="onSelectionChange">
        <el-table-column type="selection" width="45" fixed="left" reserve-selection />
        <el-table-column prop="case_id" label="用例编号" min-width="150">
          <template #default="{ row }"><span class="mono">{{ row.case_id }}</span></template>
        </el-table-column>
        <el-table-column prop="name" label="用例名称" min-width="160" show-overflow-tooltip />
        <el-table-column prop="module" label="模块" width="120" />
        <el-table-column prop="desc" label="描述" min-width="160" show-overflow-tooltip />
        <el-table-column label="优先级" width="80">
          <template #default="{ row }"><el-tag :type="prioType(row.priority)" size="small">{{ row.priority }}</el-tag></template>
        </el-table-column>
        <el-table-column label="步骤数" width="80">
          <template #default="{ row }">{{ (row.steps ?? []).length }}</template>
        </el-table-column>
        <el-table-column label="浏览器" width="100">
          <template #default="{ row }"><span class="mono">{{ row.browser }}</span></template>
        </el-table-column>
        <el-table-column label="启用" width="70">
          <template #default="{ row }"><el-tag :type="row.run ? 'success' : 'info'" size="small">{{ row.run ? "Y" : "N" }}</el-tag></template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <div class="op-cell">
              <el-button v-perm="'case:write'" size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button v-perm="'case:write'" size="small" text type="primary" @click="openCopy(row)">复制</el-button>
              <el-popconfirm v-perm="'case:delete'" :title="'删除 ' + row.case_id + '？' " width="220">
                <template #reference><el-button size="small" text type="danger">删除</el-button></template>
              </el-popconfirm>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50, 100]"
        :total="sortedCases.length"
        layout="total, sizes, prev, pager, next, jumper"
        background size="small" style="margin-top: 12px; justify-content: flex-end"
        @size-change="currentPage = 1"
      />
      <div style="margin-top: 10px; color: #909399; font-size: 13px">
        共 {{ total }} 条 ｜ 当前项目：{{ project?.name ?? "UI 项目" }}（{{ file }}）——UI 用例以 JSON 为载体，编辑保存后经后端校验通过才落盘。
      </div>
    </el-card>

    <!-- 新增 / 编辑抽屉：基本信息 + 步骤编辑器 + 变量提取 -->
    <el-drawer v-model="editVisible" size="60%" :title="mode === 'create' ? '新增 UI 用例' : `编辑 UI 用例 ${editingId}`">
      <el-form label-width="90px" @submit.prevent>
        <el-divider content-position="left">基本信息</el-divider>
        <el-row :gutter="8">
          <el-col :span="12">
            <el-form-item label="用例编号">
              <el-input v-model="form.case_id" :disabled="mode === 'edit'" class="mono" placeholder="唯一编号，如 ui_login_001（创建后不可改）" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="用例名称">
              <el-input v-model="form.name" placeholder="如：用户登录成功" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="8">
          <el-col :span="12">
            <el-form-item label="模块">
              <el-select v-model="form.module" filterable allow-create default-first-option clearable placeholder="选择或输入模块" style="width: 100%">
                <el-option v-for="m in moduleOptions" :key="m" :value="m" :label="m" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="优先级">
              <el-select v-model="form.priority" style="width: 100%">
                <el-option v-for="p in prioOptions" :key="p" :label="p" :value="p" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="描述">
          <el-input v-model="form.desc" type="textarea" :rows="2" placeholder="验证什么场景" />
        </el-form-item>
        <el-row :gutter="8">
          <el-col :span="6">
            <el-form-item label="浏览器">
              <el-select v-model="form.browser" style="width: 100%">
                <el-option v-for="b in browserOptions" :key="b" :label="b" :value="b" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="无头模式">
              <el-switch v-model="form.headless" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="是否执行">
              <el-switch v-model="form.run" active-text="Y" inactive-text="N" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="失败重试">
              <el-input-number v-model="form.retry" :min="0" :max="3" controls-position="right" style="width: 100%" />
            </el-form-item>
          </el-col>
        </el-row>
        <div class="hint" style="margin: -6px 0 10px">
          重试 0=失败即判负（默认）；&gt;0 时最多重跑对应次数，任一次通过即判通过，报告会标注「重试 k 次后通过」（登录态采集不重试）。
        </div>

        <el-form-item label="引用登录态">
          <el-select v-model="form.session" clearable filterable placeholder="不引用（用例自带登录步骤）" style="width: 100%">
            <el-option v-for="s in sessions" :key="s.id" :value="s.id"
              :label="`${s.name}（${s.env}/${s.id}｜${statusLabel(s.status)}）`" />
          </el-select>
          <div v-if="form.session" class="hint" style="margin: 6px 0 0">
            本用例开机即带登录态（加载所选会话的 storage_state），步骤里无需重复「打开登录页→填账号→点登录」。
          </div>
          <div v-else-if="!sessions.length" class="hint" style="margin: 6px 0 0">
            尚无可用登录态会话，可在侧栏「🔐 登录态」页采集后回来引用。
          </div>
        </el-form-item>

        <el-divider content-position="left">操作步骤（按序执行，失败即中断并截图）</el-divider>
        <UiStepEditor v-model="steps" :actions="meta?.actions ?? []" :variables="variables" />

        <el-divider content-position="left">变量提取（供后续用例引用）</el-divider>
        <div class="hint">来源写法：<span class="mono">text|.order-id</span> / <span class="mono">input_value|#kw</span> / <span class="mono">cookie:session</span> / <span class="mono">url</span>。</div>
        <div v-for="(x, i) in extractRows" :key="'x' + i" class="kv-row">
          <el-input v-model="x.key" placeholder="变量名" class="mono" style="width: 190px" />
          <el-input v-model="x.value" placeholder="来源，如 text|.welcome" class="mono" style="flex: 1" />
          <el-button text type="danger" @click="extractRows.splice(i, 1)">✕</el-button>
        </div>
        <el-button size="small" @click="extractRows.push({ key: '', value: '' })">＋ 添加提取</el-button>

        <div class="hint" style="margin: 14px 0 10px">
          <template v-if="mode === 'create'">保存后追加进「{{ project?.name ?? file }}」的 JSON；后端跑全量步骤校验，不通过不落盘。</template>
          <template v-else>保存后整体替换用例 {{ editingId }}；后端跑全量步骤校验，不通过不落盘。</template>
        </div>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveCase">{{ mode === "create" ? "新增并落盘" : "保存并落盘" }}</el-button>
      </el-form>
    </el-drawer>

    <!-- 迁移弹窗：仅可在 UI 项目之间迁移 -->
    <el-dialog v-model="moveVisible" title="迁移 UI 用例到其他 UI 项目" width="460px">
      <el-form label-width="82px" @submit.prevent>
        <el-form-item label="迁移范围">
          <span>已勾选 <b>{{ selected.length }}</b> 条用例</span>
          <span class="dim" style="margin-left: 8px">{{ selected.slice(0, 3).map((c) => c.case_id).join(", ") }}{{ selected.length > 3 ? ` 等 ${selected.length} 条` : "" }}</span>
        </el-form-item>
        <el-form-item label="目标项目">
          <el-select v-model="moveForm.toProjectId" style="width: 100%" placeholder="选择要迁入的 UI 项目">
            <el-option v-for="p in uiProjects" :key="p.id" :value="p.id" :label="p.name">
              <span class="opt-key">{{ p.name }}</span>
              <span class="opt-val">{{ p.case_count }} 条</span>
            </el-option>
          </el-select>
          <div v-if="!uiProjects.length" class="hint" style="margin: 4px 0 0">当前没有其他 UI 项目，请先新建。</div>
        </el-form-item>
        <el-form-item label="归入模块">
          <el-input v-model="moveForm.new_module" placeholder="留空=保留原模块；可填目标项目的模块名" />
        </el-form-item>
      </el-form>
      <div class="hint" style="margin: 0">迁移先写目标（同编号会被拒绝），再从源删除；失败条数会计入提示。</div>
      <template #footer>
        <el-button @click="moveVisible = false">取消</el-button>
        <el-button type="primary" :loading="moveSaving" @click="doMove">确认迁移 {{ selected.length }} 条</el-button>
      </template>
    </el-dialog>

    <!-- 批量导入导出弹窗：Excel 一步一行（新手首选）/ JSON 双通道 + 按通道导出——布局复用 BatchIoDialog 共享壳 -->
    <BatchIoDialog
      :visible="importVisible"
      :mode="importMode"
      title="批量导入导出 UI 用例"
      :channels="[{ key: 'excel', label: 'Excel 表格（推荐）' }, { key: 'json', label: 'JSON' }]"
      :actions="[{ key: 'excel', label: '⬇ 下载 Excel 模板' }, { key: 'json', label: '⬇ 下载 JSON 模板' }, { key: 'export', label: '⬆ 导出项目用例' }]"
      accept=".xlsx,.xlsm,.json,application/json"
      :busy="excelParsing"
      :writable="ioWritable"
      :alert-text="ioAlertFull"
      @update:visible="(v: boolean) => (importVisible = v)"
      @update:mode="(v: string) => switchMode(v as 'excel' | 'json')"
      @action="onIoAction"
      @file="onIoFile"
    >
      <!-- Excel 预览表 -->
      <div v-if="importMode === 'excel'" v-loading="excelParsing">
        <el-empty v-if="!excelPreview && !excelParsing" description="下载模板填写后，点「选择文件上传」解析预览" :image-size="60" />
        <template v-if="excelPreview">
          <div style="margin-bottom: 8px; font-size: 13px; color: #606266">
            共解析 {{ excelPreview.total }} 条用例，
            <span style="color: #67c23a">可导入 {{ excelImportable.length }} 条</span>
            <span v-if="excelPreview.items.some((i) => i.exists)" style="color: #e6a23c">；已存在 {{ excelPreview.items.filter((i) => i.exists).length }} 条（将跳过）</span>
            <span v-if="excelPreview.items.some((i) => !i.importable)" style="color: #f56c6c">；非法 {{ excelPreview.items.filter((i) => !i.importable).length }} 条</span>
          </div>
          <el-table :data="excelPreview.items" size="small" max-height="320" border>
            <el-table-column prop="case_id" label="用例编号" width="150">
              <template #default="{ row }"><span class="mono">{{ row.case_id }}</span></template>
            </el-table-column>
            <el-table-column prop="name" label="名称" min-width="120" show-overflow-tooltip />
            <el-table-column prop="steps_count" label="步骤数" width="70" align="center" />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag v-if="!row.importable" size="small" type="danger">非法</el-tag>
                <el-tag v-else-if="row.exists" size="small" type="warning">已存在</el-tag>
                <el-tag v-else size="small" type="success">可导入</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="标注" min-width="220">
              <template #default="{ row }">
                <div v-if="!row.marks.length" class="dim">—</div>
                <div v-for="(m, mi) in row.marks" :key="mi" style="font-size: 12px">
                  <el-tag :type="markLevel(m)" size="small">{{ m.field }}</el-tag>
                  <span :style="{ color: m.level === 'error' ? '#f56c6c' : m.level === 'warn' ? '#e6a23c' : '#909399', marginLeft: '4px' }">{{ m.msg }}</span>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </div>

      <!-- JSON 粘贴框 -->
      <div v-else>
        <span v-if="importRaw.trim()" style="font-size: 13px; display: flex; align-items: center; gap: 6px; margin-bottom: 6px">
          <template v-if="importParsed.ok">
            <el-tag size="small" type="success">可解析：{{ importParsed.cases.length }} 条</el-tag>
            <el-link type="info" :underline="false" @click="importRaw = ''">清空重填</el-link>
          </template>
          <el-tag v-else size="small" type="danger">{{ importParsed.err }}</el-tag>
        </span>
        <el-input v-model="importRaw" type="textarea" :rows="10" class="mono" placeholder="粘贴模板 JSON（含 cases 数组），或用上方文件选择" />
      </div>

      <template #result>
        <div v-if="importResult" style="margin-top: 10px; font-size: 13px">
          <el-tag type="success" size="small">成功 {{ importResult.imported }} 条</el-tag>
          <el-tag v-if="importResult.skipped.length" type="warning" size="small" style="margin-left: 6px">编号重复跳过 {{ importResult.skipped.length }} 条（{{ importResult.skipped.join(", ") }}）</el-tag>
          <el-tag v-if="importResult.errors.length" type="danger" size="small" style="margin-left: 6px">非法 {{ importResult.errors.length }} 条</el-tag>
          <ul v-if="importResult.errors.length" style="margin: 6px 0 0; padding-left: 18px; color: #c45656; max-height: 160px; overflow: auto">
            <li v-for="e in importResult.errors" :key="e" class="mono">{{ e }}</li>
          </ul>
        </div>
      </template>
      <template #footer>
        <template v-if="ioWritable">
          <el-button v-if="importMode === 'excel'" type="primary" :loading="importSaving"
                     :disabled="!excelImportable.length"
                     @click="doImport">导入 {{ excelImportable.length }} 条</el-button>
          <el-button v-else type="primary" :loading="importSaving"
                     :disabled="!importParsed.ok || !importParsed.cases.length"
                     @click="doImport">导入 {{ importParsed.ok ? importParsed.cases.length : 0 }} 条</el-button>
        </template>
      </template>
    </BatchIoDialog>
  </div>
</template>

<style scoped>
.ui-case-body { flex: 1; min-width: 0; }
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.hint { color: #909399; font-size: 12px; margin-bottom: 8px; }
.table-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.cur-mod { color: #606266; font-size: 13px; }
.op-cell { display: flex; align-items: center; gap: 2px; white-space: nowrap; }
.op-cell .el-button + .el-button { margin-left: 0; }
.kv-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.opt-key { float: left; }
.opt-val { float: right; color: #a0a4a8; font-size: 12px; margin-left: 16px; }
.dim { color: #909399; font-size: 12px; }
</style>
