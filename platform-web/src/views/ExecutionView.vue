<script setup lang="ts">
/** 执行中心：发起批次 → 轮询日志 → 查看/打开中文 Allure 报告。 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import type { EnvInfo, Project, RunItem } from "../types";

/* 项目（= 一个 Excel 用例文件）：与用例管理共用 localStorage 的当前项目选择 */
const PROJ_KEY = "ia_project";
const projects = ref<Project[]>([]);
const activeProjectId = ref(localStorage.getItem(PROJ_KEY) || "default");
const currentProject = computed(() =>
  projects.value.find((p) => p.id === activeProjectId.value) ??
  projects.value.find((p) => p.id === "default") ?? null);
const selectedFile = computed(() => currentProject.value?.file ?? "data/api_cases.xlsx");

const form = reactive({
  env: "",
  keyword: "",
});
// 并行：仅 UI 项目可选 worker 数（1=串行缺省）；接口/App 批次后端忽略该参数
const workersSel = ref(1);
// 筛选条件可选化：优先级多选、模块单选（候选均来自所选项目的用例元数据）
const priorityList = ref<string[]>([]);
const moduleSel = ref("");
const metaModules = ref<string[]>([]);
const metaPrios = ref<string[]>([]);

async function loadProjects() {
  try { projects.value = await api.listProjects(); } catch { /* 拦截器已提示 */ }
  if (projects.value.length && !projects.value.some((p) => p.id === activeProjectId.value)) {
    activeProjectId.value = "default";
  }
}

async function loadMeta() {
  // 按项目类型选源：接口→/cases/meta(Excel)、UI→/ui-cases/meta、App→/app-cases/meta；
  // 三者都暴露 modules/priorities，避免对 .json/.apjson 误调 Excel 接口（旧 500 根因）。
  const kind = currentProject.value?.kind ?? "api";
  try {
    const m = kind === "ui" ? await api.getUiCaseMeta(selectedFile.value)
      : kind === "app" ? await api.getAppCaseMeta(selectedFile.value)
      : await api.getCaseMeta(selectedFile.value);
    metaModules.value = m.modules;
    metaPrios.value = m.priorities;
  } catch { metaModules.value = []; metaPrios.value = []; }
}

const envs = ref<EnvInfo[]>([]);
const launching = ref(false);
const current = ref<RunItem | null>(null);
const logText = ref("");
const runs = ref<RunItem[]>([]);
let timer: number | undefined;

const router = useRouter();
const route = useRoute();
const carried = ref("");   // 从用例管理携带过来的筛选条件描述（非空则顶部提示）

/** 清除预填条件（仅清从用例管理带来的三个过滤项，不动环境/项目） */
function clearCarried() {
  moduleSel.value = "";
  priorityList.value = [];
  form.keyword = "";
  carried.value = "";
  rangeMode.value = "filter";
  router.replace({ path: "/execution" });
}

/* ================= 范围模式：按条件筛选 / 勾选用例（树形多选） ================= */
const rangeMode = ref<"filter" | "pick">("filter");
interface TreeNode { id: string; label: string; module?: string; priority?: string; children?: TreeNode[] }
const treeRef = ref();
const treeData = ref<TreeNode[]>([]);
const treeLoading = ref(false);
const treeFilter = ref("");
const checkedIds = ref<string[]>([]);

async function loadTree() {
  if (treeData.value.length) return;      // 首次切到勾选模式才加载，后续复用
  treeLoading.value = true;
  try {
    // 按项目类型选端点：接口用 api_name 作标签，UI/App 用 name（三域列表契约同名 case_id/module/priority）
    const kind = currentProject.value?.kind ?? "api";
    const leaves: { id: string; label: string; module: string; priority: string }[] = [];
    if (kind === "ui") {
      const resp = await api.listUiCases({ run: true, file: selectedFile.value });
      for (const c of resp.items) leaves.push({ id: c.case_id, label: `${c.case_id} · ${c.name}`, module: c.module, priority: c.priority });
    } else if (kind === "app") {
      const resp = await api.listAppCases({ run: true, file: selectedFile.value });
      for (const c of resp.items) leaves.push({ id: c.case_id, label: `${c.case_id} · ${c.name}`, module: c.module, priority: c.priority });
    } else {
      const resp = await api.listCases({ run: true, file: selectedFile.value });
      for (const c of resp.items) leaves.push({ id: c.case_id, label: `${c.case_id} · ${c.api_name}`, module: c.module, priority: c.priority });
    }
    const byMod = new Map<string, TreeNode[]>();
    for (const leaf of leaves) {
      if (!byMod.has(leaf.module)) byMod.set(leaf.module, []);
      byMod.get(leaf.module)!.push(leaf);
    }
    treeData.value = [...byMod.entries()]
      .sort((a, b) => a[0].localeCompare(b[0], "zh"))
      .map(([mod, children]) => ({ id: `mod:${mod}`, label: `${mod}（${children.length}）`, module: mod, children }));
  } finally {
    treeLoading.value = false;
  }
}

function filterNode(q: string, data: TreeNode): boolean {
  if (!q) return true;
  const s = q.toLowerCase();
  return data.label.toLowerCase().includes(s) || (data.module ?? "").toLowerCase().includes(s);
}

/** 收集已勾选的叶子（用例）id；模块父节点自动排除 */
function collectIds(): string[] {
  const nodes: TreeNode[] = treeRef.value?.getCheckedNodes(true) ?? [];
  return nodes.filter((n) => !n.id.startsWith("mod:")).map((n) => n.id);
}
function onCheck() {
  checkedIds.value = collectIds();
}
watch(treeFilter, (v) => treeRef.value?.filter(v));
watch(rangeMode, (m) => { if (m === "pick") loadTree(); });
watch(activeProjectId, (v) => localStorage.setItem(PROJ_KEY, v));
// 切换项目：刷新模块/优先级候选；勾选模式的用例树按新项目重建
watch(selectedFile, () => {
  loadMeta();
  treeData.value = [];
  checkedIds.value = [];
  if (rangeMode.value === "pick") loadTree();
});
const pickedCount = computed(() => checkedIds.value.length);

/* ================= 运行前变量覆盖 ================= */
interface VarRow { key: string; value: string }
const varRows = ref<VarRow[]>([]);
const advOpen = ref<string[]>([]);
function addVarRow() { varRows.value.push({ key: "", value: "" }); }
function delVarRow(i: number) { varRows.value.splice(i, 1); }
function buildVars(): Record<string, unknown> {
  const o: Record<string, unknown> = {};
  for (const r of varRows.value) {
    const k = r.key.trim();
    if (k) o[k] = r.value;
  }
  return o;
}

const statusType = (s: string) =>
  (({ running: "warning", passed: "success", failed: "danger", error: "danger", cancelled: "info", unknown: "info" } as Record<string, any>)[s] ?? "info");

async function loadRuns() {
  runs.value = await api.listRuns(20);
}

async function start() {
  const body: Parameters<typeof api.createRun>[0] = { env: form.env || undefined, file: selectedFile.value };
  if (rangeMode.value === "pick") {
    const ids = collectIds();
    if (!ids.length) {
      ElMessage.warning("请至少勾选一条用例（或切回“按条件筛选”）");
      return;
    }
    body.cases = ids;
  } else {
    body.priority = priorityList.value.length ? priorityList.value.join(",") : undefined;
    body.module = moduleSel.value || undefined;
    body.keyword = form.keyword || undefined;
  }
  const vars = buildVars();
  if (Object.keys(vars).length) body.vars = vars;
  if (currentProject.value?.kind === "ui" && workersSel.value > 1) body.workers = workersSel.value;

  launching.value = true;
  try {
    const run = await api.createRun(body);
    current.value = run;
    logText.value = "";
    ElMessage.success(`批次已受理：${run.run_id}`);
    startPolling(run.run_id);
    loadRuns();
  } finally {
    launching.value = false;
  }
}

function startPolling(runId: string) {
  stopPolling();
  timer = window.setInterval(async () => {
    try {
      const [detail, log] = await Promise.all([api.getRun(runId), api.getRunLog(runId)]);
      current.value = detail;
      logText.value = log.log;
      if (detail.status !== "running") {
        stopPolling();
        loadRuns();
        ElMessage[detail.status === "passed" ? "success" : "warning"](`批次结束：${detail.status}`);
      }
    } catch {
      stopPolling();
    }
  }, 2000);
}

function stopPolling() {
  if (timer) {
    clearInterval(timer);
    timer = undefined;
  }
}

async function cancelCurrent() {
  if (!current.value) return;
  const r = await api.cancelRun(current.value.run_id);
  ElMessage[r.cancelling ? "warning" : "info"](r.cancelling ? "已终止批次进程树" : `批次已结束：${r.status}`);
  // 立刻刷新一次，不等下一轮轮询
  try { current.value = await api.getRun(current.value.run_id); } catch { /* 拦截器已提示 */ }
  loadRuns();
}

function openReport(runId: string) {
  // 模板里没有 window 作用域，统一走这里
  window.open(api.reportUrl(runId), "_blank");
}

function toReportPage(runId: string) {
  // 站内报告页：平台原生明细（掩码后）+ Allure 内嵌
  router.push({ path: "/report", query: { runId } });
}

/** 删除批次报告（需 report:delete 权限，后端二次校验）；正在跑的批次不允许删 */
async function removeRun(row: RunItem) {
  try {
    await ElMessageBox.confirm(
      `确认删除批次 ${row.run_id}？其报告目录与明细将一并删除，不可恢复。`,
      "删除报告",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch { return; /* 用户取消 */ }
  try {
    await api.deleteRun(row.run_id);
    ElMessage.success("批次报告已删除");
    if (current.value?.run_id === row.run_id) { current.value = null; logText.value = ""; }
    loadRuns();
  } catch { /* 拦截器已提示 */ }
}

function watchRun(run: RunItem) {
  if (run.source !== "api") {
    // cli 历史批次没有内存日志，直接进站内报告页
    toReportPage(run.run_id);
    return;
  }
  current.value = run;
  startPolling(run.run_id);
}

onMounted(async () => {
  loadRuns();
  await loadProjects();   // 先确定项目，候选与执行文件才正确
  const q = route.query;
  // 从用例管理/报告页携带 file：定位到对应项目
  if (q.file) {
    const p = projects.value.find((x) => x.file === (q.file as string));
    if (p) activeProjectId.value = p.id;
  }
  // 预填模块/优先级/关键字
  if (q.module || q.priority || q.keyword) {
    moduleSel.value = (q.module as string) || "";
    priorityList.value = ((q.priority as string) || "").split(",").map((s) => s.trim()).filter(Boolean);
    form.keyword = (q.keyword as string) || "";
    const parts: string[] = [];
    if (q.module) parts.push(`模块=${q.module}`);
    if (q.priority) parts.push(`优先级=${q.priority}`);
    if (q.keyword) parts.push(`关键字=${q.keyword}`);
    carried.value = parts.join("，");
  }
  loadMeta();
  const resp = await api.listEnvs();
  envs.value = resp.envs;
  form.env = resp.default_env ?? "";
});

onBeforeUnmount(stopPolling);
</script>

<template>
  <div>
    <el-row :gutter="12">
      <el-col :span="8">
        <el-card shadow="never" header="发起执行">
          <el-alert
            v-if="carried"
            type="warning"
            :closable="false"
            show-icon
            style="margin-bottom: 12px"
            title="已按用例管理的筛选条件预填"
          >
            <template #default>
              <div style="font-size: 12px">{{ carried }}；确认环境后点“开始执行”即按此范围跑。
                <el-button link type="primary" size="small" @click="clearCarried">清除条件</el-button>
              </div>
            </template>
          </el-alert>
          <el-form label-width="72px" @submit.prevent>
            <el-form-item label="环境">
              <el-select v-model="form.env" style="width: 100%">
                <el-option v-for="e in envs" :key="e.name" :label="`${e.name}（${e.base_url}）`" :value="e.name" />
              </el-select>
            </el-form-item>
            <el-form-item label="范围">
              <el-radio-group v-model="rangeMode" size="small">
                <el-radio-button label="filter">按条件筛选</el-radio-button>
                <el-radio-button label="pick">勾选用例</el-radio-button>
              </el-radio-group>
            </el-form-item>

            <template v-if="rangeMode === 'filter'">
              <el-form-item label="优先级">
                <el-select v-model="priorityList" multiple collapse-tags placeholder="全部（留空=全量）" style="width: 100%">
                  <el-option v-for="p in metaPrios" :key="p" :label="p" :value="p" />
                </el-select>
              </el-form-item>
              <el-form-item label="模块">
                <el-select v-model="moduleSel" filterable clearable placeholder="全部（留空=不筛选）" style="width: 100%">
                  <el-option v-for="m in metaModules" :key="m" :label="m" :value="m" />
                </el-select>
              </el-form-item>
              <el-form-item label="关键字">
                <el-input v-model="form.keyword" placeholder="编号/接口名/描述/URL" />
              </el-form-item>
            </template>
            <div v-else class="pick-wrap">
              <div class="pick-bar">
                <el-input v-model="treeFilter" size="small" placeholder="过滤模块/编号/接口名" clearable />
                <span class="pick-count">已选 {{ pickedCount }} 条</span>
              </div>
              <el-tree
                ref="treeRef"
                v-loading="treeLoading"
                :data="treeData"
                show-checkbox
                node-key="id"
                :props="{ children: 'children', label: 'label' }"
                :filter-node-method="filterNode"
                default-expand-all
                class="pick-tree"
                @check="onCheck"
              />
              <div class="pick-tip">勾选模式使用当前所选项目，仅列已启用（run=Y）用例；执行顺序按用例依赖（Excel 行序）。</div>
            </div>

            <el-form-item label="用例项目">
              <el-select v-model="activeProjectId" style="width: 100%">
                <el-option v-for="p in projects" :key="p.id" :value="p.id" :label="p.name">
                  <span class="opt-key">{{ p.name }}{{ p.builtin ? " ⭐" : "" }}</span>
                  <span class="opt-val">{{ p.case_count }} 条</span>
                </el-option>
              </el-select>
              <div class="pick-tip" style="margin-top: 4px; word-break: break-all">{{ selectedFile }}</div>
            </el-form-item>

            <el-form-item v-if="currentProject?.kind === 'ui'" label="并行执行">
              <el-input-number v-model="workersSel" :min="1" :max="4" size="small" />
              <div class="pick-tip" style="margin-top: 4px">
                1=串行（缺省）；&gt;1 多 worker 按浏览器分组×分片并行，含依赖用例的批次自动降级串行；实际上限受本机 CPU 核数约束。
              </div>
            </el-form-item>

            <el-collapse v-model="advOpen" class="vars-collapse">
              <el-collapse-item name="vars">
                <template #title>
                  变量覆盖（可选，运行前生效）
                  <el-tag v-if="varRows.length" size="small" type="warning" style="margin-left: 6px">{{ varRows.length }}</el-tag>
                </template>
                <div v-for="(r, i) in varRows" :key="i" class="var-row">
                  <el-input v-model="r.key" size="small" placeholder="变量名" />
                  <el-input v-model="r.value" size="small" placeholder="值" />
                  <el-button size="small" text type="danger" @click="delVarRow(i)">删</el-button>
                </div>
                <el-button size="small" plain @click="addVarRow">+ 添加变量</el-button>
                <div class="vars-hint">覆盖同名 secret/Excel/YAML 变量（如 base_url、token）；运行时提取值仍优先。</div>
              </el-collapse-item>
            </el-collapse>

            <el-button v-perm="'run:create'" type="primary" :loading="launching" style="width: 100%; margin-top: 10px" @click="start">
              ▶ 开始执行<template v-if="rangeMode === 'pick' && pickedCount">（{{ pickedCount }} 条）</template>
            </el-button>
          </el-form>
        </el-card>

        <el-card v-if="current" shadow="never" style="margin-top: 12px">
          <template #header>
            当前批次
            <el-tag :type="statusType(current.status)" size="small" style="margin-left: 8px">{{ current.status }}</el-tag>
            <span v-if="current.exit_code !== null" style="float: right; color: #909399; font-size: 12px">exit={{ current.exit_code }}</span>
          </template>
          <div class="mono" style="font-size: 12px; margin-bottom: 6px; word-break: break-all">{{ current.run_id }}</div>
          <el-button
            v-if="current.status === 'running'"
            v-perm="'run:create'"
            size="small"
            type="danger"
            plain
            style="margin-bottom: 6px"
            @click="cancelCurrent"
          >
            ⏹ 取消执行
          </el-button>
          <pre class="console">{{ logText || "等待日志…" }}</pre>
          <el-button
            v-if="current.report_dir && ['passed', 'failed'].includes(current.status)"
            type="primary"
            size="small"
            style="margin-top: 8px"
            @click="toReportPage(current.run_id)"
          >
            📊 查看批次报告
          </el-button>
        </el-card>
      </el-col>

      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            批次列表
            <el-button size="small" style="float: right" @click="loadRuns">刷新</el-button>
          </template>
          <el-table :data="runs" size="small" row-key="run_id">
            <el-table-column label="批次" min-width="200">
              <template #default="{ row }"><span class="mono">{{ row.run_id }}</span></template>
            </el-table-column>
            <el-table-column label="来源" width="70">
              <template #default="{ row }">
                <el-tag size="small" :type="row.source === 'api' ? 'primary' : 'info'">{{ row.source }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }"><el-tag :type="statusType(row.status)" size="small">{{ row.status }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="started_at" label="开始时间" width="150" />
            <el-table-column label="操作" width="270">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click="watchRun(row)">
                  {{ row.source === "api" ? "看日志" : "明细" }}
                </el-button>
                <el-button
                  v-if="row.source === 'api' && ['passed', 'failed'].includes(row.status)"
                  size="small"
                  text
                  type="primary"
                  @click="toReportPage(row.run_id)"
                >
                  明细
                </el-button>
                <el-button
                  v-if="row.report_dir"
                  size="small"
                  text
                  @click="openReport(row.run_id)"
                >
                  Allure
                </el-button>
                <el-button
                  v-perm="'report:delete'"
                  size="small"
                  text
                  type="danger"
                  :disabled="row.status === 'running' || row.status === 'pending'"
                  @click="removeRun(row)"
                >
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <div style="margin-top: 8px; color: #909399; font-size: 12px">
            api = 平台发起（内存跟踪，可看实时日志）；cli = 本地/CI 历史运行（reports/ 目录认领，可直接出报告）
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.mono { font-family: Consolas, Monaco, monospace; }
.console {
  background: #1e2530; color: #9ab; border-radius: 6px; padding: 10px 12px;
  font-family: Consolas, Monaco, monospace; font-size: 12px; line-height: 1.6;
  height: 300px; overflow: auto; white-space: pre-wrap; word-break: break-all; margin: 0;
}
.pick-wrap { margin-bottom: 12px; }
.pick-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.pick-count { font-size: 12px; color: #409eff; white-space: nowrap; }
.pick-tree {
  max-height: 300px; overflow: auto; border: 1px solid #ebeef5; border-radius: 6px; padding: 4px;
}
.pick-tip { margin-top: 6px; font-size: 12px; color: #909399; line-height: 1.5; }
.vars-collapse { margin-top: 4px; }
.vars-collapse :deep(.el-collapse-item__header) { font-size: 13px; height: 34px; line-height: 34px; }
.var-row { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.var-row .el-input { flex: 1; }
.vars-hint { margin-top: 6px; font-size: 12px; color: #909399; line-height: 1.5; }
.opt-key { float: left; }
.opt-val { float: right; color: #a0a4a8; font-size: 12px; margin-left: 16px; }
</style>
