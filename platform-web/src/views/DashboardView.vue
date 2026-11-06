<script setup lang="ts">
/** 工作台：用例资产与执行态势一屏概览（全部复用既有接口，纯前端聚合）。 */
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api";
import type { CaseItem, EnvInfo, RunItem } from "../types";

const router = useRouter();
const loading = ref(true);
const cases = ref<CaseItem[]>([]);
const runs = ref<RunItem[]>([]);
const envs = ref<EnvInfo[]>([]);

onMounted(async () => {
  try {
    const [c, r, e] = await Promise.all([
      api.listCases({}),
      api.listRuns(15),
      api.listEnvs(),
    ]);
    cases.value = c.items;
    runs.value = r;
    envs.value = e.envs;
  } finally {
    loading.value = false;
  }
  // 失败聚合与凭证健康不阻塞首屏，异步补上
  analyzeFailures();
  loadSecretHealth();
});

const enabled = computed(() => cases.value.filter((c) => c.run).length);
const modules = computed(() => new Set(cases.value.map((c) => c.module)).size);

/** 优先级分布（P0~P5 固定序，方便条对齐） */
const prioBars = computed(() => {
  const order = ["P0", "P1", "P2", "P3", "P4", "P5"];
  const max = Math.max(1, ...order.map((p) => cases.value.filter((c) => c.priority === p).length));
  return order
    .map((p) => {
      const n = cases.value.filter((c) => c.priority === p).length;
      return { p, n, pct: (n / max) * 100 };
    })
    .filter((b) => b.n > 0);
});

const prioColor: Record<string, string> = {
  P0: "#f56c6c", P1: "#e6a23c", P2: "#409eff", P3: "#909399", P4: "#909399", P5: "#c0c4cc",
};

/** 最近一次已结束的批次（跳过 running，KPI 不被执行中批次污染） */
const lastDone = computed(() =>
  runs.value.find((r) => ["passed", "failed", "error", "cancelled"].includes(r.status)));

const finishedStats = computed(() => {
  const done = runs.value.filter((r) => r.status === "passed" || r.status === "failed");
  const passed = done.filter((r) => r.status === "passed").length;
  return { total: done.length, passed, rate: done.length ? Math.round((passed / done.length) * 100) : null };
});

const statusType = (s: string) =>
  (({ running: "warning", passed: "success", failed: "danger", error: "danger", cancelled: "info" } as Record<string, any>)[s] ?? "info");

/** 批次趋势条：取最近 12 次按时间旧→新排列，颜色即结果 */
const TREND_COLOR: Record<string, string> = {
  passed: "#67c23a", failed: "#f56c6c", broken: "#f56c6c", error: "#e6a23c",
  cancelled: "#909399", running: "#409eff", unknown: "#c0c4cc",
};
const trend = computed(() =>
  runs.value.slice(0, 12).reverse().map((r) => ({
    ...r,
    color: TREND_COLOR[r.status] ?? "#c0c4cc",
  })));

function goReport(run: RunItem) {
  if (run.source === "api" && ["passed", "failed"].includes(run.status)) router.push({ path: "/report", query: { runId: run.run_id } });
  else if (run.report_dir) router.push({ path: "/report", query: { runId: run.run_id } });
}

/** 联动入口：带筛选条件跳用例管理（优先级条/模块/KPI 卡调用） */
function goCases(query: Record<string, string | undefined>) {
  router.push({ path: "/cases", query });
}

/** 重跑该批次条件：用批次 args 里的模块/优先级/关键字预填执行中心 */
function goRerun(run: RunItem) {
  const a = (run.args ?? {}) as Record<string, unknown>;
  router.push({
    path: "/execution",
    query: {
      module: a.module ? String(a.module) : undefined,
      priority: a.priority ? String(a.priority) : undefined,
      keyword: a.keyword ? String(a.keyword) : undefined,
    },
  });
}

/** 本批次可重跑条件的人读摘要（悬停提示用） */
function rerunSummary(run: RunItem): string {
  const a = (run.args ?? {}) as Record<string, unknown>;
  const parts: string[] = [];
  if (a.module) parts.push(`模块=${a.module}`);
  if (a.priority) parts.push(`优先级=${a.priority}`);
  if (a.keyword) parts.push(`关键字=${a.keyword}`);
  return parts.length ? parts.join("，") : "全量（无筛选条件）";
}

/* ================= 失败用例 TOP（近 5 个已完成批次聚合） ================= */
const FAILED_STATUSES = ["failed", "broken"];
const isFail = (s: string) => FAILED_STATUSES.includes(s);
interface FailRow {
  case_id: string; name: string; priority: string;
  summary: string; error: string; consec: number; totalFails: number;
}
const failTop = ref<FailRow[]>([]);
const analyzing = ref(false);
const scanned = ref(0);            // 实际扫描的已完成批次数
const latestFailRunId = ref("");   // 最近一个有失败的批次（供详情跳转）
const latestFailCount = ref(0);    // 最新批次失败数

function buildFailTop(results: ({ run: RunItem; items: any[] } | null)[]): FailRow[] {
  // results 已按新→旧排列；逐批次累计每个用例的连续失败/总失败
  const info = new Map<string, FailRow & { done: boolean }>();
  for (const res of results) {
    if (!res) continue;
    for (const it of res.items) {
      let rec = info.get(it.case_id);
      if (!rec) {
        rec = { case_id: it.case_id, name: it.name, priority: it.priority, summary: "", error: "", consec: 0, totalFails: 0, done: false };
        info.set(it.case_id, rec);
      }
      if (isFail(it.status)) {
        rec.totalFails++;
        if (!rec.done) rec.consec++;            // 从最新往回，遇到一次通过即断链
        rec.summary = it.summary || it.status;
        rec.error = it.error || "";
        rec.name = it.name; rec.priority = it.priority;
      } else {
        rec.done = true;
      }
    }
  }
  return [...info.values()]
    .filter((r) => r.totalFails > 0)
    .sort((a, b) => b.consec - a.consec || b.totalFails - a.totalFails || a.case_id.localeCompare(b.case_id))
    .slice(0, 8);
}

async function analyzeFailures() {
  const finished = runs.value.filter((r) => ["passed", "failed"].includes(r.status)).slice(0, 5);
  scanned.value = finished.length;
  if (!finished.length) { failTop.value = []; return; }
  analyzing.value = true;
  try {
    const results = await Promise.all(
      finished.map((r) => api.getRunCases(r.run_id).then((x) => ({ run: r, items: x.items })).catch(() => null)));
    failTop.value = buildFailTop(results);
    const first = results[0];
    latestFailCount.value = first ? first.items.filter((i: any) => isFail(i.status)).length : 0;
    const withFail = results.find((x) => x && x.items.some((i: any) => isFail(i.status)));
    latestFailRunId.value = withFail ? withFail.run.run_id : "";
  } finally {
    analyzing.value = false;
  }
}

function rerunOne(row: FailRow) {
  router.push({ path: "/execution", query: { keyword: row.case_id } });
}
function viewCase(row: FailRow) {
  if (latestFailRunId.value) router.push({ path: "/report", query: { runId: latestFailRunId.value, caseId: row.case_id } });
}

/* ================= 待办·环境健康（接 /api/secrets） ================= */
const secret = reactive({ loaded: false, exists: true, count: 0, warn: "" });
async function loadSecretHealth() {
  try {
    const s = await api.listSecrets();
    secret.exists = s.exists; secret.count = s.items.length; secret.loaded = true;
    const login = s.items.filter((v) => v.kind === "登录态/凭证");
    if (!s.exists) secret.warn = "尚未配置凭证文件（secret_vars.xlsx）";
    else if (!login.length) secret.warn = "无登录态凭证变量（如 token）";
    else {
      const empty = login.filter((v) => v.empty);
      secret.warn = empty.length ? `登录态凭证为空：${empty.map((v) => v.name).join(", ")}` : "";
    }
  } catch { /* 拦截器已提示 */ }
}
const disabledCount = computed(() => cases.value.length - enabled.value);
</script>

<template>
  <div v-loading="loading">
    <el-row :gutter="12">
      <el-col :span="6">
        <el-card shadow="never" class="kpi kpi-link" @click="goCases({})">
          <div class="kpi-num">{{ cases.length }}</div>
          <div class="kpi-label">用例总数（启用 {{ enabled }}）</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never" class="kpi kpi-link" @click="goCases({})">
          <div class="kpi-num">{{ modules }}</div>
          <div class="kpi-label">模块数</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never" class="kpi" :class="{ 'kpi-link': !!lastDone }" @click="lastDone && goReport(lastDone)">
          <div class="kpi-num" :style="{ color: lastDone?.status === 'passed' ? '#67c23a' : '#f56c6c' }">
            {{ lastDone ? lastDone.status : "—" }}
          </div>
          <div class="kpi-label">最近批次{{ lastDone ? "（" + (lastDone.started_at ?? "") + "）" : "暂无记录" }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never" class="kpi kpi-link" @click="router.push('/execution')">
          <div class="kpi-num">{{ finishedStats.rate == null ? "—" : finishedStats.rate + "%" }}</div>
          <div class="kpi-label">近 {{ finishedStats.total }} 次已完成批次通过率</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" style="margin-top: 12px" header="批次趋势（最近 12 次，旧 → 新）">
      <div class="trend-row">
        <el-tooltip v-for="t in trend" :key="t.run_id" placement="top">
          <template #content>
            {{ t.run_id }}<br />{{ t.status }} · {{ t.started_at ?? "—" }}
            <template v-if="t.finished_at"> → {{ t.finished_at }}</template>
          </template>
          <div class="trend-bar" :style="{ background: t.color }" @click="goReport(t)" />
        </el-tooltip>
        <span v-if="!trend.length" style="color: #909399; font-size: 12px">暂无批次记录</span>
        <div class="trend-legend">
          <span><i style="background: #67c23a" />passed</span>
          <span><i style="background: #f56c6c" />failed</span>
          <span><i style="background: #e6a23c" />error</span>
          <span><i style="background: #909399" />cancelled</span>
          <span><i style="background: #c0c4cc" />unknown</span>
        </div>
      </div>
    </el-card>

    <el-row :gutter="12" style="margin-top: 12px">
      <el-col :span="8">
        <el-card shadow="never" header="待办 · 环境健康">
          <div class="todo-item" :class="secret.warn ? 'todo-warn' : 'todo-ok'">
            <span class="todo-icon">🔑</span>
            <div class="todo-main">
              <b>凭证</b>
              <span v-if="!secret.loaded" class="dim">检测中…</span>
              <span v-else-if="secret.warn" class="dim">{{ secret.warn }}</span>
              <span v-else class="dim">共 {{ secret.count }} 项，登录态正常</span>
            </div>
            <el-button size="small" text type="primary" @click="router.push('/secrets')">去配置</el-button>
          </div>
          <div class="todo-item" :class="failTop.length ? 'todo-warn' : 'todo-ok'">
            <span class="todo-icon">⚠️</span>
            <div class="todo-main">
              <b>执行</b>
              <span v-if="analyzing" class="dim">聚合中…</span>
              <span v-else-if="!scanned" class="dim">暂无已完成批次</span>
              <span v-else-if="failTop.length" class="dim">近 {{ scanned }} 批次 {{ failTop.length }} 个用例未通过{{ latestFailCount ? '，最新批次失败 ' + latestFailCount : '' }}</span>
              <span v-else class="dim">近期批次全部通过</span>
            </div>
            <el-button v-if="latestFailRunId" size="small" text type="primary" @click="router.push({ path: '/report', query: { runId: latestFailRunId } })">看报告</el-button>
          </div>
          <div class="todo-item todo-ok">
            <span class="todo-icon">📋</span>
            <div class="todo-main">
              <b>用例</b>
              <span class="dim">共 {{ cases.length }} 条 · 启用 {{ enabled }} · 停用 {{ disabledCount }} · 模块 {{ modules }}</span>
            </div>
            <el-button size="small" text type="primary" @click="goCases({})">管理</el-button>
          </div>
        </el-card>
      </el-col>
      <el-col :span="16">
        <el-card shadow="never" v-loading="analyzing">
          <template #header>
            失败用例 TOP（近 {{ scanned }} 批次）
            <span class="dim" style="float: right; font-weight: 400; font-size: 12px">连续/累计失败越多越靠前</span>
          </template>
          <el-table :data="failTop" size="small" row-key="case_id">
            <el-table-column label="用例" min-width="210">
              <template #default="{ row }"><div class="mono">{{ row.case_id }}</div><div class="sub">{{ row.name }}</div></template>
            </el-table-column>
            <el-table-column prop="priority" label="优先级" width="70" />
            <el-table-column label="失败原因" min-width="220">
              <template #default="{ row }"><span class="reason">{{ row.summary || row.error || "失败" }}</span></template>
            </el-table-column>
            <el-table-column label="连失/累失" width="95">
              <template #default="{ row }"><el-tag :type="row.consec >= 2 ? 'danger' : 'warning'" size="small">{{ row.consec }} / {{ row.totalFails }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="140" fixed="right">
              <template #default="{ row }">
                <div class="op-cell">
                  <el-button size="small" text type="primary" :disabled="!latestFailRunId" @click="viewCase(row)">详情</el-button>
                  <el-button size="small" text type="warning" @click="rerunOne(row)">重跑</el-button>
                </div>
              </template>
            </el-table-column>
            <template #empty><span class="dim">近期批次无失败用例 👍</span></template>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="12" style="margin-top: 12px">
      <el-col :span="8">
        <el-card shadow="never" header="优先级分布（点击某优先级→查看对应用例）">
          <div v-for="b in prioBars" :key="b.p" class="bar-row bar-link" @click="goCases({ priority: b.p })">
            <span class="bar-label mono">{{ b.p }}</span>
            <div class="bar-track"><div class="bar-fill" :style="{ width: b.pct + '%', background: prioColor[b.p] }" /></div>
            <span class="bar-num">{{ b.n }}</span>
          </div>
          <div style="margin-top: 10px; color: #909399; font-size: 12px">
            环境：
            <el-tag v-for="e in envs" :key="e.name" size="small" style="margin-right: 6px">{{ e.name }}</el-tag>
          </div>
        </el-card>
      </el-col>
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            最近批次
            <el-button size="small" style="float: right" @click="router.push('/execution')">去执行中心</el-button>
          </template>
          <el-table :data="runs.slice(0, 8)" size="small" row-key="run_id">
            <el-table-column label="批次" min-width="200">
              <template #default="{ row }"><span class="mono">{{ row.run_id }}</span></template>
            </el-table-column>
            <el-table-column label="来源" width="70">
              <template #default="{ row }"><el-tag size="small" :type="row.source === 'api' ? 'primary' : 'info'">{{ row.source }}</el-tag></template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }"><el-tag :type="statusType(row.status)" size="small">{{ row.status }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="started_at" label="开始时间" width="150" />
            <el-table-column label="操作" width="150">
              <template #default="{ row }">
                <el-tooltip :content="'重跑条件：' + rerunSummary(row)" placement="top">
                  <el-button size="small" text type="warning" :disabled="row.source !== 'api'" @click="goRerun(row)">重跑</el-button>
                </el-tooltip>
                <el-button size="small" text type="primary" :disabled="!row.report_dir" @click="goReport(row)">报告</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.kpi { text-align: center; }
.kpi-link { cursor: pointer; transition: box-shadow 0.15s ease, transform 0.15s ease; }
.kpi-link:hover { box-shadow: 0 4px 14px rgba(0, 0, 0, 0.12); transform: translateY(-2px); }
.kpi-num { font-size: 30px; font-weight: 700; line-height: 1.3; }
.kpi-label { color: #909399; font-size: 12px; margin-top: 2px; }
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.bar-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.bar-link { cursor: pointer; border-radius: 6px; padding: 2px 4px; margin: 0 -4px 6px; transition: background 0.15s ease; }
.bar-link:hover { background: #f5f7fa; }
.bar-label { width: 26px; }
.bar-track { flex: 1; height: 12px; background: #f0f2f5; border-radius: 6px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 6px; transition: width 0.3s ease; }
.bar-num { width: 26px; text-align: right; font-size: 12px; color: #606266; }
.trend-row { display: flex; align-items: flex-end; gap: 6px; min-height: 42px; }
.trend-bar { width: 26px; height: 30px; border-radius: 4px 4px 2px 2px; cursor: pointer; transition: transform 0.15s ease, opacity 0.15s ease; }
.trend-bar:hover { transform: scaleY(1.25); opacity: 0.85; }
.trend-legend { margin-left: auto; display: flex; gap: 12px; font-size: 12px; color: #909399; }
.trend-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
.dim { color: #909399; font-size: 12px; }
.sub { color: #909399; font-size: 12px; }
.reason { font-size: 12px; color: #f56c6c; }
.op-cell { display: flex; align-items: center; gap: 2px; white-space: nowrap; }
.op-cell .el-button + .el-button { margin-left: 0; }
.todo-item { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 6px; margin-bottom: 8px; }
.todo-item:last-child { margin-bottom: 0; }
.todo-icon { font-size: 18px; }
.todo-main { flex: 1; display: flex; flex-direction: column; gap: 2px; }
.todo-warn { background: #fef0f0; border: 1px solid #fde2e2; }
.todo-ok { background: #f0f9eb; border: 1px solid #e1f3d8; }
</style>
