<script setup lang="ts">
/**
 * App 用例管理（项目 kind=app，预留位/脚手架）：可视化步骤用例的列表 / 筛选 / 增删改 / 跨项目迁移。
 *
 * 与 UI 用例（UiCasesView）同构，只是把"浏览器/无头"换成"平台(android/ios)+被测应用包名"，
 * 数据全部走 /api/app-cases，载体是 .apjson。本页自包含拉取逻辑，CasesView 在选中 App 项目时整体委托给它。
 * 驱动（Appium）尚未接入：用例可编辑维护、可被执行链路收集，执行阶段统一跳过。
 */
import { computed, nextTick, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { api } from "../api";
import type { AppCaseItem, AppCaseMeta, AppExtract, Project, UiStep } from "../types";
import UiStepEditor from "../components/UiStepEditor.vue";

const props = defineProps<{
  file: string;
  project: Project | null;
  projects: Project[];
}>();
const emit = defineEmits<{ (e: "projects-changed"): void }>();

const router = useRouter();

interface KvRow { key: string; value: string }

const loading = ref(false);
const cases = ref<AppCaseItem[]>([]);
const total = ref(0);
const meta = ref<AppCaseMeta | null>(null);
const variables = ref<string[]>([]);
const filters = reactive({ priority: [] as string[], keyword: "", onlyEnabled: false });
const activeModule = ref("");   // "" = 全部
const pageSize = ref(10);
const currentPage = ref(1);

const prioType = (p: string) =>
  ({ P0: "danger", P1: "warning", P2: "", P3: "info", P4: "info", P5: "info" }[p] ?? "");

async function loadMeta() {
  if (!props.file) return;
  try {
    meta.value = await api.getAppCaseMeta(props.file);
    variables.value = [];
    const v = await api.getAppCaseVariables(props.file);
    variables.value = [...(v.excel_variables ?? []), ...(v.secret_variables ?? [])];
  } catch { /* 拦截器已提示 */ }
}

async function search() {
  if (!props.file) return;
  loading.value = true;
  try {
    const resp = await api.listAppCases({
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

/** 模块计数（基于已加载列表，按 zh 排序） */
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
const selected = ref<AppCaseItem[]>([]);
function onSelectionChange(rows: AppCaseItem[]) {
  selected.value = rows;
}
function clearSelection() {
  selected.value = [];
}

const appProjects = computed(() => props.projects.filter((p) => p.kind === "app" && p.file !== props.file));
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
  if (!target) { ElMessage.warning("请选择目标 App 项目"); return; }
  moveSaving.value = true;
  let ok = 0;
  const failed: string[] = [];
  for (const c of selected.value) {
    try {
      await api.moveAppCase(c.case_id, { to_file: target.file, new_module: moveForm.new_module.trim() || undefined }, props.file);
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

/* ================= 编辑抽屉 ================= */
const editVisible = ref(false);
const saving = ref(false);
const mode = ref<"create" | "edit">("create");
const editingId = ref("");
const form = reactive({
  case_id: "", module: "", name: "", desc: "", priority: "P2",
  run: true, platform: "android", app: "",
});
const steps = ref<UiStep[]>([]);
const extractRows = ref<KvRow[]>([]);

const platformOptions = computed(() => meta.value?.platforms ?? ["android", "ios"]);
const prioOptions = computed(() => meta.value?.priorities ?? ["P0", "P1", "P2", "P3", "P4", "P5"]);
const moduleOptions = computed(() => meta.value?.modules ?? []);

function blankCase() {
  mode.value = "create";
  editingId.value = "";
  Object.assign(form, { case_id: "", module: activeModule.value || "", name: "", desc: "", priority: "P2", run: true, platform: "android", app: "" });
  steps.value = [{ action: "launch", value: "" }];
  extractRows.value = [];
  editVisible.value = true;
}

function fillFrom(c: AppCaseItem) {
  mode.value = "edit";
  editingId.value = c.case_id;
  Object.assign(form, {
    case_id: c.case_id, module: c.module, name: c.name, desc: c.desc,
    priority: c.priority, run: c.run, platform: c.platform, app: c.app,
  });
  steps.value = (c.steps ?? []).map((s) => ({ ...s }));
  extractRows.value = (c.extract ?? []).map((e) => ({ key: e.var, value: e.source }));
  editVisible.value = true;
}

async function openEdit(row: AppCaseItem) {
  try {
    fillFrom(await api.getAppCase(row.case_id, props.file));
  } catch { /* 拦截器已提示 */ }
}

function openCopy(row: AppCaseItem) {
  fillFrom({ ...row, case_id: "" });
  mode.value = "create";
  editingId.value = "";
}

async function removeCase(row: AppCaseItem) {
  try {
    await api.deleteAppCase(row.case_id, props.file);
    ElMessage.success(`用例「${row.case_id}」已删除`);
    await search();
    await loadMeta();
    emit("projects-changed");
  } catch { /* 拦截器已提示 */ }
}

async function saveCase() {
  if (!form.case_id.trim()) { ElMessage.error("用例编号不能为空"); return; }
  if (!steps.value.length) { ElMessage.error("至少需要一个步骤"); return; }
  const extract: AppExtract[] = [];
  for (const r of extractRows.value) {
    const k = r.key.trim();
    if (!k) continue;
    if (!r.value.trim()) { ElMessage.error(`变量提取「${k}」缺少来源`); return; }
    extract.push({ var: k, source: r.value.trim() });
  }
  const payload: Record<string, unknown> = {
    case_id: form.case_id.trim(), module: form.module.trim() || "未分类",
    name: form.name.trim() || "新建App用例", desc: form.desc.trim(),
    priority: form.priority, run: form.run, platform: form.platform, app: form.app.trim(),
    steps: steps.value, extract,
  };
  saving.value = true;
  try {
    if (mode.value === "create") {
      await api.createAppCase(payload, props.file);
      ElMessage.success("用例已创建并落盘");
    } else {
      await api.updateAppCase(editingId.value, payload, props.file);
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
  <div class="app-case-body">
    <el-alert
      type="info"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
      title="App 自动化为预留脚手架：用例可编辑维护、可被执行链路收集，实际驱动（Appium）接入后即可运行"
    />
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
          <el-button v-perm="'case:write'" type="success" plain @click="blankCase">＋ 新增 App 用例</el-button>
          <el-button v-perm="'run:create'" type="warning" plain @click="goExecute">▶ 按当前条件执行</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <div class="table-head">
        <span class="cur-mod">
          当前项目：<b>{{ project?.name ?? "App 项目" }}</b>（共 {{ total }} 条）
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
        <el-table-column label="平台" width="90">
          <template #default="{ row }"><span class="mono">{{ row.platform }}</span></template>
        </el-table-column>
        <el-table-column prop="app" label="被测应用" min-width="140" show-overflow-tooltip />
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
        共 {{ total }} 条 ｜ 当前项目：{{ project?.name ?? "App 项目" }}（{{ file }}）——App 用例以 .apjson 为载体，编辑保存后经后端校验通过才落盘。
      </div>
    </el-card>

    <!-- 新增 / 编辑抽屉：基本信息 + 步骤编辑器 + 变量提取 -->
    <el-drawer v-model="editVisible" size="60%" :title="mode === 'create' ? '新增 App 用例' : `编辑 App 用例 ${editingId}`">
      <el-form label-width="90px" @submit.prevent>
        <el-divider content-position="left">基本信息</el-divider>
        <el-row :gutter="8">
          <el-col :span="12">
            <el-form-item label="用例编号">
              <el-input v-model="form.case_id" :disabled="mode === 'edit'" class="mono" placeholder="唯一编号，如 app_login_001（创建后不可改）" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="用例名称">
              <el-input v-model="form.name" placeholder="如：App 登录成功" />
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
          <el-col :span="8">
            <el-form-item label="平台">
              <el-select v-model="form.platform" style="width: 100%">
                <el-option v-for="b in platformOptions" :key="b" :label="b" :value="b" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="被测应用">
              <el-input v-model="form.app" class="mono" placeholder="包名 / BundleID" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="是否执行">
              <el-switch v-model="form.run" active-text="Y" inactive-text="N" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">操作步骤（按序执行，失败即中断并截图）</el-divider>
        <UiStepEditor v-model="steps" :actions="meta?.actions ?? []" :variables="variables" />

        <el-divider content-position="left">变量提取（供后续用例引用）</el-divider>
        <div class="hint">来源写法：<span class="mono">text|id=welcome</span> / <span class="mono">attr:name|id=price</span>。</div>
        <div v-for="(x, i) in extractRows" :key="'x' + i" class="kv-row">
          <el-input v-model="x.key" placeholder="变量名" class="mono" style="width: 190px" />
          <el-input v-model="x.value" placeholder="来源，如 text|id=welcome" class="mono" style="flex: 1" />
          <el-button text type="danger" @click="extractRows.splice(i, 1)">✕</el-button>
        </div>
        <el-button size="small" @click="extractRows.push({ key: '', value: '' })">＋ 添加提取</el-button>

        <div class="hint" style="margin: 14px 0 10px">
          <template v-if="mode === 'create'">保存后追加进「{{ project?.name ?? file }}」的 .apjson；后端跑全量步骤校验，不通过不落盘。</template>
          <template v-else>保存后整体替换用例 {{ editingId }}；后端跑全量步骤校验，不通过不落盘。</template>
        </div>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveCase">{{ mode === "create" ? "新增并落盘" : "保存并落盘" }}</el-button>
      </el-form>
    </el-drawer>

    <!-- 迁移弹窗：仅可在 App 项目之间迁移 -->
    <el-dialog v-model="moveVisible" title="迁移 App 用例到其他 App 项目" width="460px">
      <el-form label-width="82px" @submit.prevent>
        <el-form-item label="迁移范围">
          <span>已勾选 <b>{{ selected.length }}</b> 条用例</span>
          <span class="dim" style="margin-left: 8px">{{ selected.slice(0, 3).map((c) => c.case_id).join(", ") }}{{ selected.length > 3 ? ` 等 ${selected.length} 条` : "" }}</span>
        </el-form-item>
        <el-form-item label="目标项目">
          <el-select v-model="moveForm.toProjectId" style="width: 100%" placeholder="选择要迁入的 App 项目">
            <el-option v-for="p in appProjects" :key="p.id" :value="p.id" :label="p.name">
              <span class="opt-key">{{ p.name }}</span>
              <span class="opt-val">{{ p.case_count }} 条</span>
            </el-option>
          </el-select>
          <div v-if="!appProjects.length" class="hint" style="margin: 4px 0 0">当前没有其他 App 项目，请先新建。</div>
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
  </div>
</template>

<style scoped>
.app-case-body { flex: 1; min-width: 0; }
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
