<script setup lang="ts">
/**
 * 统计报表：纯前端聚合，跨批次通过率趋势 / 模块分布 / 优先级分布 / 耗时 TOP。
 * 数据源：listRuns(100) + getRunCases(×N) + listCases({})。
 */
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api";
import type { RunCaseSummary, RunItem } from "../types";

const router = useRouter();

/* ============ 数据加载 ============ */
const batchLimit = ref(20);
const loading = ref(false);
// batchCases 按时间新→旧排列（与 listRuns 返回顺序一致）
const batchCases = ref<{ run: RunItem; items: RunCaseSummary[] }[]>([]);
const caseModuleMap = ref<Map<string, string>>(new Map());

async function load() {
  loading.value = true;
  try {
    // case_id → module 映射（全量用例）
    const allCases = await api.listCases({});
    const m = new Map<string, string>();
    for (const c of allCases.items) m.set(c.case_id, c.module);
    caseModuleMap.value = m;

    // 近 N 个已完成批次
    const runs = await api.listRuns(100);
    const finished = runs
      .filter((r) => ["passed", "failed"].includes(r.status))
      .slice(0, batchLimit.value);

    const results = await Promise.all(
      finished.map((r) => api.getRunCases(r.run_id).then((x) => x.items).catch(() => null)),
    );
    batchCases.value = finished
      .map((run, i) => ({ run, items: results[i] ?? [] }))
      .filter((x) => x.items.length > 0);
  } finally {
    loading.value = false;
  }
}

/* ============ KPI ============ */
const FAILED = new Set(["failed", "broken"]);

const kpi = computed(() => {
  let total = 0, passed = 0, failed = 0;
  for (const bc of batchCases.value) {
    total += bc.items.length;
    passed += bc.items.filter((i) => i.status === "passed").length;
    failed += bc.items.filter((i) => FAILED.has(i.status)).length;
  }
  return {
    batches: batchCases.value.length,
    total,
    passed,
    failed,
    passRate: total ? Math.round((passed / total) * 100) : 0,
  };
});

/* ============ 趋势（旧 → 新，用于图表） ============ */
interface TrendPoint {
  runId: string;
  label: string;
  total: number;
  passed: number;
  failed: number;
  rate: number;
}

const trendData = computed<TrendPoint[]>(() =>
  batchCases.value
    .slice()
    .reverse()
    .map((bc) => {
      const total = bc.items.length;
      const passed = bc.items.filter((i) => i.status === "passed").length;
      const failed = bc.items.filter((i) => FAILED.has(i.status)).length;
      return {
        runId: bc.run.run_id,
        label: (bc.run.started_at ?? "").replace("T", " ").slice(5, 16),
        total,
        passed,
        failed,
        rate: total ? passed / total : 0,
      };
    }),
);

/* ============ 趋势可视化大屏（SVG 发光柱状，无第三方依赖） ============ */
const TARGET = 0.8; // 通过率目标线
// 柱状图几何：viewBox 0 0 680 240，绘图区 左44/右16/上24/下34
const BAR_W = 680, BAR_H = 240;
const BX0 = 44, BX1 = 664, BY0 = 24, BY1 = 206;
function barY(rate: number): number {
  return BY0 + (1 - rate) * (BY1 - BY0);
}

// 每批一根柱：宽度按批数自适应，高度=通过率
const bars = computed(() => {
  const d = trendData.value;
  const n = d.length;
  if (!n) return [];
  const slot = (BX1 - BX0) / n;
  const bw = Math.min(38, slot * 0.62);
  return d.map((p, i) => {
    const cx = BX0 + slot * (i + 0.5);
    const h = Math.max(2, p.rate * (BY1 - BY0));
    return { ...p, cx, bw, x: cx - bw / 2, y: BY1 - h, h, ok: p.rate >= TARGET };
  });
});

// Y 轴刻度（0/25/50/75/100%）
const yTicks = [1, 0.75, 0.5, 0.25, 0];

// X 轴标签采样：≤8 批全标，否则均匀取 6 个（含首尾）
const labelIdx = computed(() => {
  const n = trendData.value.length;
  const s = new Set<number>();
  if (n <= 8) { for (let i = 0; i < n; i++) s.add(i); return s; }
  for (let k = 0; k < 6; k++) s.add(Math.round((k / 5) * (n - 1)));
  return s;
});

// 大屏左侧发光指标
const avgRate = computed(() => {
  const d = trendData.value;
  return d.length ? d.reduce((s, p) => s + p.rate, 0) / d.length : 0;
});
const latestRate = computed(() => trendData.value[trendData.value.length - 1]?.rate ?? 0);
const prevRate = computed(() => trendData.value[trendData.value.length - 2]?.rate ?? 0);
const delta = computed(() => Math.round((latestRate.value - prevRate.value) * 100));
const okBatches = computed(() => trendData.value.filter((p) => p.rate >= TARGET).length);

// 综合通过率环形（conic-gradient），颜色随档位变化
const ringStyle = computed(() => {
  const r = kpi.value.passRate;
  const color = r >= 80 ? "#2f9e44" : r >= 60 ? "#f5a623" : "#e03131";
  return { background: `conic-gradient(${color} ${r * 3.6}deg, #eef0f3 0deg)` };
});

/* ============ 模块分布 ============ */
const moduleStats = computed(() => {
  const map = new Map<string, { total: number; passed: number; failed: number }>();
  for (const bc of batchCases.value) {
    for (const item of bc.items) {
      const mod = caseModuleMap.value.get(item.case_id) ?? "未分类";
      const rec = map.get(mod) ?? { total: 0, passed: 0, failed: 0 };
      rec.total++;
      if (item.status === "passed") rec.passed++;
      else if (FAILED.has(item.status)) rec.failed++;
      map.set(mod, rec);
    }
  }
  return [...map.entries()]
    .map(([mod, v]) => ({ mod, ...v, passRate: v.total ? v.passed / v.total : 0 }))
    .sort((a, b) => b.failed - a.failed || b.total - a.total);
});

/* ============ 优先级分布 ============ */
const priorityStats = computed(() => {
  const map = new Map<string, { total: number; passed: number; failed: number }>();
  for (const bc of batchCases.value) {
    for (const item of bc.items) {
      const p = item.priority || "?";
      const rec = map.get(p) ?? { total: 0, passed: 0, failed: 0 };
      rec.total++;
      if (item.status === "passed") rec.passed++;
      else if (FAILED.has(item.status)) rec.failed++;
      map.set(p, rec);
    }
  }
  return [...map.entries()]
    .map(([priority, v]) => ({ priority, ...v, passRate: v.total ? v.passed / v.total : 0 }))
    .sort((a, b) => a.priority.localeCompare(b.priority));
});

/* ============ 耗时 TOP（最新批次） ============ */
const durationTop = computed(() => {
  if (!batchCases.value.length) return [];
  const latest = batchCases.value[0];
  return latest.items
    .slice()
    .sort((a, b) => b.duration_ms - a.duration_ms)
    .slice(0, 8)
    .map((i) => ({
      case_id: i.case_id,
      name: i.name,
      status: i.status,
      duration_ms: i.duration_ms,
      mod: caseModuleMap.value.get(i.case_id) ?? "未分类",
    }));
});

const durMax = computed(() =>
  Math.max(1, ...durationTop.value.map((d) => d.duration_ms)),
);

const fmtDur = (ms: number) => (ms >= 1000 ? (ms / 1000).toFixed(2) + "s" : ms + "ms");
const fmtRate = (r: number) => Math.round(r * 100) + "%";
const toReport = (runId: string) => router.push({ path: "/report", query: { runId } });

/* ============ Flaky 趋势（观测与诊断） ============ */
interface FlakyRow {
  case_id: string;
  name: string;
  runs: number;
  flaky: number;    // 重试后通过次数
  failed: number;   // 最终失败次数
  retried: number;  // 发生过重试的次数
  maxAttempts: number;
}
// 复用已加载的 batchCases（含结构化 attempts/retried/flaky），按 case_id 跨批次聚合
const flakyRows = computed<FlakyRow[]>(() => {
  const map = new Map<string, FlakyRow>();
  for (const bc of batchCases.value) {
    for (const it of bc.items) {
      // batchCases 新→旧，首次命中即以最新 name；后续同用例只累计计数
      const rec = map.get(it.case_id) ?? {
        case_id: it.case_id, name: it.name, runs: 0, flaky: 0, failed: 0, retried: 0, maxAttempts: 1,
      };
      rec.runs++;
      if (it.flaky) rec.flaky++;
      if (it.retried) rec.retried++;
      if (FAILED.has(it.status)) rec.failed++;
      const at = it.attempts ?? 1;
      if (at > rec.maxAttempts) rec.maxAttempts = at;
      map.set(it.case_id, rec);
    }
  }
  return [...map.values()]
    .filter((r) => r.retried > 0 || r.failed > 0) // 只列可疑项（重试过或失败过）
    .sort((a, b) => b.flaky - a.flaky || b.failed - a.failed || b.retried - a.retried);
});
const flakyCaseCount = computed(() => flakyRows.value.filter((r) => r.flaky > 0).length);
const flakyMax = computed(() => Math.max(1, ...flakyRows.value.map((r) => r.flaky)));

onMounted(load);
</script>

<template>
  <div v-loading="loading" class="stats-page">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <span class="page-title">统计报表</span>
        <span class="page-sub">跨批次质量概览 · 趋势 / 模块 / 优先级 / 耗时</span>
      </div>
      <div class="toolbar-right">
        <el-select v-model="batchLimit" size="small" style="width: 110px" @change="load">
          <el-option :value="10" label="近 10 批" />
          <el-option :value="20" label="近 20 批" />
          <el-option :value="50" label="近 50 批" />
        </el-select>
        <el-button size="small" plain :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <!-- KPI -->
    <div class="kpi-grid">
      <div class="kpi-card kpi-blue">
        <div class="kpi-icon">📦</div>
        <div class="kpi-body">
          <div class="kpi-num">{{ kpi.batches }}</div>
          <div class="kpi-lbl">已完成批次</div>
        </div>
      </div>
      <div class="kpi-card kpi-purple">
        <div class="kpi-icon">⚡</div>
        <div class="kpi-body">
          <div class="kpi-num">{{ kpi.total }}</div>
          <div class="kpi-lbl">总执行次数</div>
        </div>
      </div>
      <div class="kpi-card kpi-green">
        <div class="kpi-ring" :style="ringStyle">
          <span class="ring-txt">{{ kpi.passRate }}%</span>
        </div>
        <div class="kpi-body">
          <div class="kpi-num" style="color: #2f9e44">{{ kpi.passed }}<span class="kpi-unit">/{{ kpi.total }}</span></div>
          <div class="kpi-lbl">综合通过率</div>
        </div>
      </div>
      <div class="kpi-card kpi-red">
        <div class="kpi-icon">⚠️</div>
        <div class="kpi-body">
          <div class="kpi-num" style="color: #e03131">{{ kpi.failed }}</div>
          <div class="kpi-lbl">失败总次数</div>
        </div>
      </div>
    </div>

    <!-- 通过率趋势 -->
    <section class="panel">
      <header class="panel-head">
        <span class="head-bar"></span>
        <h3>通过率趋势</h3>
        <span class="head-meta">近 {{ trendData.length }} 批 · 目标线 {{ Math.round(TARGET * 100) }}%</span>
        <span class="head-hint">点击柱体查看该批次报告</span>
      </header>
      <div class="panel-body">
        <div v-if="trendData.length" class="screen-body">
          <!-- 左侧指标 -->
          <div class="screen-stats">
            <div class="ss-main">
              <div class="ss-big" :class="latestRate >= TARGET ? 'txt-ok' : 'txt-fail'">{{ fmtRate(latestRate) }}</div>
              <div class="ss-lbl">最新通过率</div>
              <div class="ss-delta" :class="delta >= 0 ? 'up' : 'down'">
                {{ delta >= 0 ? '▲' : '▼' }} {{ Math.abs(delta) }}% 较上批
              </div>
            </div>
            <div class="ss-list">
              <div class="ss-item"><span class="ss-k">平均通过率</span><span class="ss-v">{{ fmtRate(avgRate) }}</span></div>
              <div class="ss-item"><span class="ss-k">达标批次</span><span class="ss-v">{{ okBatches }}/{{ trendData.length }}</span></div>
              <div class="ss-item"><span class="ss-k">总执行</span><span class="ss-v">{{ kpi.total }}</span></div>
              <div class="ss-item"><span class="ss-k">失败次数</span><span class="ss-v warn">{{ kpi.failed }}</span></div>
            </div>
          </div>

          <!-- 右侧柱状趋势 -->
          <div class="screen-chart">
            <svg :viewBox="`0 0 ${BAR_W} ${BAR_H}`" class="bar-svg" preserveAspectRatio="xMidYMid meet">
              <defs>
                <linearGradient id="barOk" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stop-color="#57c17a" />
                  <stop offset="100%" stop-color="#2f9e44" stop-opacity="0.55" />
                </linearGradient>
                <linearGradient id="barBad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stop-color="#ff8f8f" />
                  <stop offset="100%" stop-color="#e03131" stop-opacity="0.55" />
                </linearGradient>
              </defs>
              <!-- 网格 + Y 标签 -->
              <line v-for="t in yTicks" :key="'sg' + t" :x1="BX0" :y1="barY(t)" :x2="BX1" :y2="barY(t)" stroke="#eef1f6" stroke-width="1" />
              <text v-for="t in yTicks" :key="'syl' + t" :x="BX0 - 8" :y="barY(t) + 3" text-anchor="end" font-size="10" fill="#b6bcc7">{{ Math.round(t * 100) }}%</text>
              <!-- 目标线 -->
              <line :x1="BX0" :y1="barY(TARGET)" :x2="BX1" :y2="barY(TARGET)" stroke="#f5a623" stroke-width="1" stroke-dasharray="5 4" opacity="0.8" />
              <!-- 柱体 -->
              <g v-for="(b, i) in bars" :key="b.runId" class="bar-g" @click="toReport(b.runId)">
                <rect :x="b.x" :y="b.y" :width="b.bw" :height="b.h" rx="3" :fill="b.ok ? 'url(#barOk)' : 'url(#barBad)'" class="bar-rect" />
                <text v-if="labelIdx.has(i)" :x="b.cx" :y="BY1 + 16" text-anchor="middle" font-size="9" fill="#b6bcc7">{{ b.label }}</text>
                <title>{{ b.label }}  {{ fmtRate(b.rate) }}（{{ b.passed }}/{{ b.total }}）</title>
              </g>
              <!-- 基线 -->
              <line :x1="BX0" :y1="BY1" :x2="BX1" :y2="BY1" stroke="#e4e7ed" stroke-width="1" />
            </svg>
          </div>
        </div>
        <el-empty v-else description="暂无已完成批次数据" :image-size="60" />
      </div>
    </section>

    <!-- 模块 + 优先级/耗时 -->
    <div class="grid-2">
      <section class="panel">
        <header class="panel-head">
          <span class="head-bar"></span>
          <h3>模块通过率</h3>
          <span class="head-meta">跨批次累计</span>
        </header>
        <div class="panel-body">
          <div v-if="moduleStats.length" class="bar-list">
            <div v-for="s in moduleStats.slice(0, 12)" :key="s.mod" class="bar-row">
              <span class="bar-name" :title="s.mod">{{ s.mod }}</span>
              <div class="bar-track">
                <div class="bar-fill" :class="s.passRate >= TARGET ? 'fill-ok' : 'fill-bad'"
                     :style="{ width: Math.max(4, s.passRate * 100) + '%' }"></div>
              </div>
              <span class="bar-rate" :class="s.passRate >= TARGET ? 'txt-ok' : 'txt-fail'">{{ fmtRate(s.passRate) }}</span>
              <span class="bar-cnt">{{ s.total }}</span>
            </div>
          </div>
          <el-empty v-else :image-size="50" description="无数据" />
        </div>
      </section>

      <div class="col-stack">
        <section class="panel">
          <header class="panel-head">
            <span class="head-bar"></span>
            <h3>优先级通过率</h3>
          </header>
          <div class="panel-body">
            <div v-if="priorityStats.length" class="pri-grid">
              <div v-for="s in priorityStats" :key="s.priority" class="pri-item">
                <div class="pri-badge" :class="s.passRate >= TARGET ? 'pri-ok' : 'pri-bad'">{{ s.priority }}</div>
                <div class="pri-rate" :class="s.passRate >= TARGET ? 'txt-ok' : 'txt-fail'">{{ fmtRate(s.passRate) }}</div>
                <div class="pri-cnt">{{ s.passed }}/{{ s.total }}</div>
              </div>
            </div>
            <el-empty v-else :image-size="50" description="无数据" />
          </div>
        </section>

        <section class="panel">
          <header class="panel-head">
            <span class="head-bar"></span>
            <h3>最新批次耗时 TOP</h3>
          </header>
          <div class="panel-body">
            <div v-if="durationTop.length" class="dur-list">
              <div v-for="d in durationTop" :key="d.case_id" class="dur-row">
                <span class="dur-dot" :class="d.status === 'passed' ? 'dot-ok' : 'dot-bad'"></span>
                <span class="dur-name" :title="`${d.case_id} · ${d.name}`">{{ d.case_id }}</span>
                <div class="dur-track">
                  <div class="dur-bar" :class="d.status !== 'passed' ? 'dur-bar-warn' : ''"
                       :style="{ width: (d.duration_ms / durMax) * 100 + '%' }"></div>
                </div>
                <span class="dur-val">{{ fmtDur(d.duration_ms) }}</span>
              </div>
            </div>
            <el-empty v-else :image-size="50" description="无数据" />
          </div>
        </section>
      </div>
    </div>

    <!-- Flaky 趋势（观测与诊断）：靠重试蒙过的不稳定用例排行 -->
    <section class="panel">
      <header class="panel-head">
        <span class="head-bar"></span>
        <h3>Flaky 趋势</h3>
        <span class="head-meta">近 {{ batchCases.length }} 批 · flaky 用例 {{ flakyCaseCount }} 个</span>
        <span class="head-hint">flaky = 靠重试才通过的不稳定用例，建议排查首次失败根因</span>
      </header>
      <div class="panel-body">
        <div v-if="flakyRows.length" class="flaky-list">
          <div v-for="r in flakyRows.slice(0, 15)" :key="r.case_id" class="flaky-row">
            <span class="flaky-name" :title="`${r.case_id} · ${r.name}`">{{ r.case_id }}</span>
            <div class="bar-track">
              <div class="bar-fill fill-flaky" :style="{ width: Math.max(4, (r.flaky / flakyMax) * 100) + '%' }"></div>
            </div>
            <span class="flaky-metric"><b class="flaky-warn">{{ r.flaky }}</b> flaky</span>
            <span class="flaky-metric dim2">{{ r.retried }} 重试</span>
            <span class="flaky-metric" :class="r.failed ? 'txt-fail' : 'dim2'">{{ r.failed }} 失败</span>
            <span class="flaky-cnt">{{ r.runs }} 次</span>
          </div>
        </div>
        <el-empty v-else :image-size="50" description="近期无重试/失败用例，稳定性良好" />
      </div>
    </section>
  </div>
</template>

<style scoped>
.stats-page { display: flex; flex-direction: column; gap: 16px; padding: 4px; }

/* 工具栏 */
.toolbar { display: flex; align-items: center; justify-content: space-between; }
.toolbar-left { display: flex; flex-direction: column; gap: 2px; }
.page-title { font-size: 18px; font-weight: 700; color: #1f2733; }
.page-sub { font-size: 12px; color: #98a1ad; }
.toolbar-right { display: flex; align-items: center; gap: 8px; }

/* KPI */
.kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
.kpi-card {
  position: relative; display: flex; align-items: center; gap: 14px;
  background: #fff; border-radius: 14px; padding: 18px 20px; overflow: hidden;
  box-shadow: 0 1px 2px rgba(0, 0, 0, .04), 0 8px 24px rgba(31, 45, 61, .06);
  border: 1px solid #f0f2f5; transition: transform .15s ease, box-shadow .15s ease;
}
.kpi-card:hover { transform: translateY(-2px); box-shadow: 0 6px 14px rgba(31, 45, 61, .10); }
.kpi-card::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; }
.kpi-blue::before { background: #4d8dff; }
.kpi-purple::before { background: #8b5cf6; }
.kpi-green::before { background: #2f9e44; }
.kpi-red::before { background: #e03131; }
.kpi-icon { width: 52px; height: 52px; border-radius: 14px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 24px; }
.kpi-blue .kpi-icon { background: rgba(77, 141, 255, .12); }
.kpi-purple .kpi-icon { background: rgba(139, 92, 246, .12); }
.kpi-red .kpi-icon { background: rgba(224, 49, 49, .10); }
.kpi-ring { width: 52px; height: 52px; border-radius: 50%; flex-shrink: 0; display: flex; align-items: center; justify-content: center; position: relative; }
.kpi-ring::after { content: ""; position: absolute; inset: 6px; background: #fff; border-radius: 50%; }
.ring-txt { position: relative; z-index: 1; font-size: 12px; font-weight: 700; color: #2f9e44; }
.kpi-body { display: flex; flex-direction: column; gap: 2px; }
.kpi-num { font-size: 26px; font-weight: 700; line-height: 1.15; color: #1f2733; }
.kpi-unit { font-size: 14px; font-weight: 500; color: #a8afba; margin-left: 2px; }
.kpi-lbl { font-size: 12px; color: #98a1ad; }

/* 面板 */
.panel { background: #fff; border-radius: 14px; border: 1px solid #f0f2f5; box-shadow: 0 1px 2px rgba(0, 0, 0, .04), 0 8px 24px rgba(31, 45, 61, .05); }
.panel-head { display: flex; align-items: center; gap: 8px; padding: 14px 18px 0; }
.head-bar { width: 3px; height: 14px; border-radius: 2px; background: #4d8dff; }
.panel-head h3 { margin: 0; font-size: 14px; font-weight: 600; color: #1f2733; }
.head-meta { font-size: 12px; color: #a8afba; }
.head-hint { margin-left: auto; font-size: 11px; color: #c0c6d0; }
.panel-body { padding: 14px 18px 18px; }

/* 趋势（浅色，与整页统一） */
.screen-body { display: grid; grid-template-columns: 190px 1fr; gap: 22px; align-items: center; }
.screen-stats { display: flex; flex-direction: column; gap: 12px; }
.ss-big { font-size: 40px; font-weight: 800; line-height: 1; font-variant-numeric: tabular-nums; }
.ss-lbl { font-size: 12px; color: #98a1ad; margin-top: 4px; }
.ss-delta { font-size: 12px; margin-top: 6px; font-weight: 600; }
.ss-delta.up { color: #2f9e44; }
.ss-delta.down { color: #e03131; }
.ss-list { display: flex; flex-direction: column; gap: 8px; border-top: 1px solid #f0f2f5; padding-top: 12px; }
.ss-item { display: flex; align-items: center; justify-content: space-between; font-size: 12.5px; }
.ss-k { color: #98a1ad; }
.ss-v { font-weight: 700; color: #1f2733; font-variant-numeric: tabular-nums; }
.ss-v.warn { color: #e03131; }
.screen-chart { min-width: 0; }
.bar-svg { width: 100%; height: auto; display: block; }
.bar-g { cursor: pointer; }
.bar-rect { transition: opacity .12s ease; }
.bar-g:hover .bar-rect { opacity: .8; }

/* 两列布局 */
.grid-2 { display: grid; grid-template-columns: 1.35fr 1fr; gap: 16px; align-items: start; }
.col-stack { display: flex; flex-direction: column; gap: 16px; }

/* 横向条 */
.bar-list { display: flex; flex-direction: column; gap: 10px; }
.bar-row { display: flex; align-items: center; gap: 10px; }
.bar-name { font-size: 12px; color: #5b6470; width: 104px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 0; }
.bar-track { flex: 1; height: 8px; background: #f2f4f7; border-radius: 6px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 6px; transition: width .4s ease; }
.fill-ok { background: linear-gradient(90deg, #57c17a, #2f9e44); }
.fill-bad { background: linear-gradient(90deg, #ff8f8f, #e03131); }
.bar-rate { font-size: 12px; width: 42px; text-align: right; flex-shrink: 0; font-weight: 600; }
.bar-cnt { font-size: 11px; color: #b6bcc7; width: 26px; text-align: right; flex-shrink: 0; }

/* 优先级网格 */
.pri-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(84px, 1fr)); gap: 10px; }
.pri-item { display: flex; flex-direction: column; align-items: center; gap: 3px; padding: 12px 6px; border-radius: 10px; background: #fafbfc; border: 1px solid #f0f2f5; }
.pri-badge { width: 26px; height: 26px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 700; color: #fff; }
.pri-ok { background: #2f9e44; }
.pri-bad { background: #e03131; }
.pri-rate { font-size: 15px; font-weight: 700; }
.pri-cnt { font-size: 11px; color: #a8afba; }

/* 耗时 */
.dur-list { display: flex; flex-direction: column; gap: 9px; }
.dur-row { display: flex; align-items: center; gap: 8px; }
.dur-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dot-ok { background: #2f9e44; }
.dot-bad { background: #e03131; }
.dur-name { font-family: Consolas, Monaco, monospace; font-size: 11px; color: #5b6470; width: 128px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 0; }
.dur-track { flex: 1; height: 8px; background: #f2f4f7; border-radius: 6px; overflow: hidden; }
.dur-bar { background: linear-gradient(90deg, #7aa7ff, #4d8dff); height: 100%; border-radius: 6px; }
.dur-bar-warn { background: linear-gradient(90deg, #ffc078, #f59f00); }
.dur-val { font-size: 11px; color: #98a1ad; width: 52px; text-align: right; flex-shrink: 0; }

/* 颜色 */
.txt-ok { color: #2f9e44; }
.txt-fail { color: #e03131; }

/* Flaky 趋势 */
.flaky-list { display: flex; flex-direction: column; gap: 9px; }
.flaky-row { display: flex; align-items: center; gap: 10px; }
.flaky-name { font-family: Consolas, Monaco, monospace; font-size: 11px; color: #5b6470; width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 0; }
.flaky-metric { font-size: 12px; color: #5b6470; white-space: nowrap; }
.flaky-metric b { font-size: 14px; font-weight: 700; }
.flaky-warn { color: #f59f00; }
.dim2 { color: #b6bcc7; }
.flaky-cnt { font-size: 11px; color: #a8afba; width: 42px; text-align: right; flex-shrink: 0; }
.fill-flaky { background: linear-gradient(90deg, #ffd43b, #f59f00); }

@media (max-width: 1200px) {
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  .grid-2 { grid-template-columns: 1fr; }
  .screen-body { grid-template-columns: 1fr; }
}
</style>
