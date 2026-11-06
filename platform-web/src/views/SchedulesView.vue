<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import type { ScheduleItem, PlanItem, EnvInfo, Project } from "../types";

const items = ref<ScheduleItem[]>([]);
const plans = ref<PlanItem[]>([]);
const loading = ref(false);

/* 用例项目（= 一个 Excel 文件）：自定义配置的目标项目决定跑哪个文件，也决定模块/优先级候选 */
const projects = ref<Project[]>([]);
async function loadProjects() {
  try { projects.value = await api.listProjects(); } catch { /* 拦截器已提示 */ }
}
/** 默认项目文件：优先用与用例管理一致的 localStorage 选择，否则回落到内置默认项目 */
function defaultFile(): string {
  const saved = localStorage.getItem("ia_project");
  return projects.value.find((p) => p.id === saved)?.file
    ?? projects.value.find((p) => p.id === "default")?.file
    ?? "data/api_cases.xlsx";
}

/* 自定义配置的下拉候选：环境来自环境配置（listEnvs），模块/优先级来自用例元数据（getCaseMeta） */
const envs = ref<EnvInfo[]>([]);
const modules = ref<string[]>([]);
const priorities = ref<string[]>([]);
// 优先级为多选，模型用数组，与 run_config.priority 的逗号串在开关抽屉/保存时互转
const priorityList = ref<string[]>([]);

async function loadCandidates() {
  try {
    const [envResp, meta] = await Promise.all([api.listEnvs(), api.getCaseMeta(form.run_config.file || undefined)]);
    envs.value = envResp.envs;
    modules.value = meta.modules;
    priorities.value = meta.priorities;
  } catch { /* 拦截器已提示；候选拉取失败不阻断抽屉打开 */ }
}

/* 抽屉表单 */
const drawerVisible = ref(false);
const isEdit = ref(false);
const editId = ref("");
const form = reactive({
  name: "",
  cron: "0 8 * * *",
  target: "plan" as "plan" | "config",
  plan_id: "",
  run_config: { env: "", module: "", priority: "", file: "" },
  enabled: true,
});

// 切换目标项目：重置已选模块（不同项目模块不互通）并按新项目刷新候选
watch(() => form.run_config.file, () => { form.run_config.module = ""; loadCandidates(); });

/* Cron 预览 */
const previewRuns = ref<string[]>([]);
const previewLoading = ref(false);

/* Cron 可视化生成：频率 + 时间/星期/日 选择，同时保留表达式手动输入 */
const WEEK_OPTIONS = [
  { v: 0, l: "周日" }, { v: 1, l: "周一" }, { v: 2, l: "周二" }, { v: 3, l: "周三" },
  { v: 4, l: "周四" }, { v: 5, l: "周五" }, { v: 6, l: "周六" },
];
const freq = ref<"hourly" | "daily" | "weekly" | "monthly" | "custom">("daily");
const runTime = ref("08:00");       // HH:mm，用于 每天/每周/每月
const minuteOfHour = ref(0);        // 每小时模式：第几分钟
const weekDays = ref<number[]>([1]);
const monthDays = ref<number[]>([1]);

function buildCron(): string {
  const [h, m] = (runTime.value || "08:00").split(":").map(Number);
  switch (freq.value) {
    case "hourly": return `${minuteOfHour.value} * * * *`;
    case "daily": return `${m} ${h} * * *`;
    case "weekly": return `${m} ${h} * * ${(weekDays.value.length ? weekDays.value : [0]).join(",")}`;
    case "monthly": return `${m} ${h} ${(monthDays.value.length ? monthDays.value : [1]).join(",")} * *`;
    default: return form.cron;
  }
}

/** 解析已有 cron 回填可视化控件；仅识别纯数字/逗号列表，范围/步进等一律返回 false（转自定义，保持原值） */
function parseCron(expr: string): boolean {
  const p = (expr || "").trim().split(/\s+/);
  if (p.length !== 5) return false;
  const [min, hour, dom, mon, dow] = p;
  const num = (s: string) => /^\d+$/.test(s);
  const list = (s: string) => /^[\d,]+$/.test(s);
  if (mon !== "*" || !num(min)) return false;
  const set = () => { runTime.value = `${String(hour).padStart(2, "0")}:${String(min).padStart(2, "0")}`; };
  if (hour === "*" && dom === "*" && dow === "*") { minuteOfHour.value = Number(min); freq.value = "hourly"; return true; }
  if (num(hour) && dom === "*" && dow === "*") { set(); freq.value = "daily"; return true; }
  if (num(hour) && dom === "*" && list(dow)) {
    set(); weekDays.value = dow.split(",").map(Number).filter((x) => !Number.isNaN(x)); freq.value = "weekly"; return true;
  }
  if (num(hour) && list(dom) && dow === "*") {
    set(); monthDays.value = dom.split(",").map(Number).filter((x) => !Number.isNaN(x)); freq.value = "monthly"; return true;
  }
  return false;
}

/** 初始化构建器：先定住表达式，再尝试回填控件，无法归类则保留自定义 */
function resetCronBuilder(cron: string) {
  form.cron = cron;
  if (!parseCron(cron)) freq.value = "custom";
}

// 选控变化即回写表达式（自定义模式下不覆盖手动输入）
watch([freq, runTime, minuteOfHour, weekDays, monthDays], () => {
  if (freq.value !== "custom") form.cron = buildCron();
});

/** 手动编辑表达式：切到自定义，停止可视化覆盖 */
function onCronManual() { freq.value = "custom"; }

const cronRead = computed(() => {
  const wk = weekDays.value.map((d) => WEEK_OPTIONS.find((w) => w.v === d)?.l).filter(Boolean).join("、");
  switch (freq.value) {
    case "hourly": return `每小时第 ${minuteOfHour.value} 分钟执行`;
    case "daily": return `每天 ${runTime.value} 执行`;
    case "weekly": return `每周 ${wk || "（未选）"} ${runTime.value} 执行`;
    case "monthly": return `每月 ${(monthDays.value.length ? monthDays.value : [1]).join("、")} 号 ${runTime.value} 执行`;
    default: return "自定义表达式";
  }
});

async function loadList() {
  loading.value = true;
  try {
    const [sch, pl] = await Promise.all([api.listSchedules(), api.listPlans()]);
    items.value = sch;
    plans.value = pl;
  } finally {
    loading.value = false;
  }
}
onMounted(async () => { await loadProjects(); loadList(); });

function openCreate() {
  isEdit.value = false;
  editId.value = "";
  form.name = "";
  form.cron = "0 8 * * *";
  form.target = "plan";
  form.plan_id = "";
  form.run_config = { env: "", module: "", priority: "", file: defaultFile() };
  priorityList.value = [];
  form.enabled = true;
  previewRuns.value = [];
  resetCronBuilder("0 8 * * *");
  loadCandidates();
  drawerVisible.value = true;
}

function openEdit(item: ScheduleItem) {
  isEdit.value = true;
  editId.value = item.id;
  form.name = item.name;
  form.cron = item.cron;
  form.target = item.plan_id ? "plan" : "config";
  form.plan_id = item.plan_id || "";
  form.run_config = { env: "", module: "", priority: "", file: defaultFile(), ...(item.run_config || {}) };
  priorityList.value = String((item.run_config?.priority as string) || "")
    .split(",").map((s) => s.trim()).filter(Boolean);
  form.enabled = item.enabled;
  previewRuns.value = [];
  resetCronBuilder(item.cron);
  loadCandidates();
  drawerVisible.value = true;
}

async function doPreview() {
  if (!form.cron.trim()) return;
  previewLoading.value = true;
  try {
    const resp = await api.previewCron(form.cron, 5);
    previewRuns.value = resp.next_runs;
  } catch {
    previewRuns.value = [];
  } finally {
    previewLoading.value = false;
  }
}

async function save() {
  if (!form.name.trim()) { ElMessage.warning("名称不能为空"); return; }
  if (!form.cron.trim()) { ElMessage.warning("Cron 表达式不能为空"); return; }
  if (form.target === "plan" && !form.plan_id) { ElMessage.warning("请选择场景"); return; }

  const body: any = {
    name: form.name,
    cron: form.cron,
    enabled: form.enabled,
  };
  if (form.target === "plan") {
    body.plan_id = form.plan_id;
  } else {
    const cfg: any = {};
    if (form.run_config.file) cfg.file = form.run_config.file;
    if (form.run_config.env) cfg.env = form.run_config.env;
    if (form.run_config.module) cfg.module = form.run_config.module;
    if (priorityList.value.length) cfg.priority = priorityList.value.join(",");
    body.run_config = cfg;
  }

  try {
    if (isEdit.value) {
      await api.updateSchedule(editId.value, body);
      ElMessage.success("已更新");
    } else {
      await api.createSchedule(body);
      ElMessage.success("已创建");
    }
    drawerVisible.value = false;
    loadList();
  } catch { /* 拦截器提示 */ }
}

async function remove(item: ScheduleItem) {
  await ElMessageBox.confirm(`确认删除「${item.name}」？`, "提示", { type: "warning" });
  await api.deleteSchedule(item.id);
  ElMessage.success("已删除");
  loadList();
}

async function toggle(item: ScheduleItem) {
  await api.updateSchedule(item.id, { enabled: !item.enabled });
  loadList();
}

const running = ref<string>("");
async function manualRun(item: ScheduleItem) {
  running.value = item.id;
  try {
    await api.runSchedule(item.id);
    ElMessage.success("已触发执行");
    loadList();
  } catch { /* 拦截器提示 */ } finally {
    running.value = "";
  }
}

function planName(id: string) {
  return plans.value.find(p => p.id === id)?.name || id || "-";
}
/** 按文件路径回显项目名（定时任务列表展示自定义配置跑的是哪个项目） */
function projectNameByFile(file: unknown) {
  if (typeof file !== "string" || !file) return "默认项目";
  return projects.value.find((p) => p.file === file)?.name ?? file;
}
function fmtTime(t: string | null) {
  if (!t) return "-";
  return t.replace("T", " ");
}
</script>

<template>
  <div>
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px">
      <h3 style="margin: 0">定时任务</h3>
      <el-button type="primary" @click="openCreate">＋ 新建定时任务</el-button>
    </div>

    <el-table :data="items" v-loading="loading" stripe empty-text="暂无定时任务">
      <el-table-column prop="name" label="名称" min-width="130" />
      <el-table-column prop="cron" label="Cron" width="140">
        <template #default="{ row }">
          <code style="font-size: 12px">{{ row.cron }}</code>
        </template>
      </el-table-column>
      <el-table-column label="执行目标" min-width="120">
        <template #default="{ row }">
          <span v-if="row.plan_id">场景: {{ planName(row.plan_id) }}</span>
          <span v-else-if="row.run_config && Object.keys(row.run_config).length">自定义配置 · {{ projectNameByFile(row.run_config.file) }}</span>
          <span v-else style="color:#909399">未配置</span>
        </template>
      </el-table-column>
      <el-table-column label="下次执行" width="160">
        <template #default="{ row }">{{ fmtTime(row.next_run_at) }}</template>
      </el-table-column>
      <el-table-column label="上次执行" width="160">
        <template #default="{ row }">{{ fmtTime(row.last_run_at) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="70">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" size="small" @change="toggle(row)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
          <el-button link type="success" size="small" :loading="running === row.id" @click="manualRun(row)">执行</el-button>
          <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新建/编辑抽屉 -->
    <el-drawer v-model="drawerVisible" :title="isEdit ? '编辑定时任务' : '新建定时任务'" size="460px">
      <el-form label-position="top">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="例如：每日P0冒烟" />
        </el-form-item>
        <el-form-item label="执行频率">
          <el-radio-group v-model="freq" size="small">
            <el-radio-button value="hourly">每小时</el-radio-button>
            <el-radio-button value="daily">每天</el-radio-button>
            <el-radio-button value="weekly">每周</el-radio-button>
            <el-radio-button value="monthly">每月</el-radio-button>
            <el-radio-button value="custom">自定义</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="freq === 'hourly'" label="每小时第几分钟">
          <el-select v-model="minuteOfHour" style="width: 100%">
            <el-option v-for="m in 60" :key="m - 1" :label="`第 ${m - 1} 分`" :value="m - 1" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="freq === 'daily' || freq === 'weekly' || freq === 'monthly'" label="执行时间">
          <el-time-picker v-model="runTime" value-format="HH:mm" format="HH:mm" :clearable="false" placeholder="选择时间" style="width: 100%" />
        </el-form-item>
        <el-form-item v-if="freq === 'weekly'" label="星期">
          <el-checkbox-group v-model="weekDays">
            <el-checkbox v-for="w in WEEK_OPTIONS" :key="w.v" :value="w.v">{{ w.l }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item v-if="freq === 'monthly'" label="每月几日">
          <el-select v-model="monthDays" multiple collapse-tags collapse-tags-tooltip placeholder="选择日期" style="width: 100%">
            <el-option v-for="d in 31" :key="d" :label="`${d} 号`" :value="d" />
          </el-select>
        </el-form-item>
        <el-form-item label="Cron 表达式（分 时 日 月 周）">
          <div style="display: flex; gap: 8px; width: 100%">
            <el-input v-model="form.cron" placeholder="0 8 * * *" style="flex:1" @input="onCronManual" />
            <el-button @click="doPreview" :loading="previewLoading">预览</el-button>
          </div>
          <div class="cron-read">{{ cronRead }}</div>
          <div v-if="previewRuns.length" style="margin-top: 8px; font-size: 12px; color: #606266">
            <div v-for="(t, i) in previewRuns" :key="i">{{ i + 1 }}. {{ t.replace('T', ' ') }}</div>
          </div>
        </el-form-item>
        <el-form-item label="执行目标">
          <el-radio-group v-model="form.target">
            <el-radio value="plan">关联场景</el-radio>
            <el-radio value="config">自定义配置</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="form.target === 'plan'" label="选择场景">
          <el-select v-model="form.plan_id" placeholder="选择已有场景" style="width: 100%">
            <el-option v-for="p in plans" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
        </el-form-item>
        <template v-if="form.target === 'config'">
          <el-form-item label="用例项目">
            <el-select v-model="form.run_config.file" filterable placeholder="选择要定时执行的项目（= 一个用例文件）" style="width: 100%">
              <el-option v-for="p in projects" :key="p.id" :value="p.file" :label="p.name">
                <span class="opt-key">{{ p.name }}{{ p.builtin ? " ⭐" : "" }}</span>
                <span class="opt-val">{{ p.case_count }} 条</span>
              </el-option>
            </el-select>
          </el-form-item>
          <el-form-item label="环境（可选）">
            <el-select v-model="form.run_config.env" clearable filterable placeholder="留空使用默认环境" style="width: 100%">
              <el-option
                v-for="e in envs"
                :key="e.name"
                :label="e.base_url ? `${e.name}（${e.base_url}）` : e.name"
                :value="e.name"
              />
              <template #empty><div style="padding:6px 12px;color:#909399;font-size:12px">无环境，请先到「环境配置」新增</div></template>
            </el-select>
          </el-form-item>
          <el-form-item label="模块（可选）">
            <el-select v-model="form.run_config.module" clearable filterable placeholder="留空不筛选，可下拉选择已有模块" style="width: 100%">
              <el-option v-for="m in modules" :key="m" :label="m" :value="m" />
              <template #empty><div style="padding:6px 12px;color:#909399;font-size:12px">暂无模块，先到「用例管理」录入用例</div></template>
            </el-select>
          </el-form-item>
          <el-form-item label="优先级（可选）">
            <el-select v-model="priorityList" multiple clearable collapse-tags collapse-tags-tooltip placeholder="留空=全量，可多选 P0/P1…" style="width: 100%">
              <el-option v-for="p in priorities" :key="p" :label="p" :value="p" />
            </el-select>
          </el-form-item>
        </template>
        <el-form-item>
          <el-switch v-model="form.enabled" active-text="启用" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="save">{{ isEdit ? '保存' : '创建' }}</el-button>
          <el-button @click="drawerVisible = false">取消</el-button>
        </el-form-item>
      </el-form>
    </el-drawer>
  </div>
</template>

<style scoped>
h3 { font-size: 16px; color: #303133; }
.cron-read { margin-top: 6px; font-size: 12px; color: #409eff; }
.opt-key { float: left; }
.opt-val { float: right; color: #a0a4a8; font-size: 12px; margin-left: 16px; }
</style>
