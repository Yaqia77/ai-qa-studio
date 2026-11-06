/**
 * 测试场景运行追踪（模块级共享 store）。
 *
 * 独立于组件生命周期：切换页面不会丢失执行信息，轮询在后台持续；
 * 通过 localStorage 持久化，浏览器刷新后仍能恢复历史记录。
 * 结束的记录保留一段时间（FINISHED_TTL），便于切换页面回来后查看结果。
 */
import { reactive } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";

export interface TrackedRun {
  planId: string;
  planName: string;
  runId: string;
  status: string;
  startedAt: string | null;
  finishedAt: string | null;
}

const LS_KEY = "ia_plan_tracked";
const MAX_KEEP = 20; // 最多保留的追踪条数
const FINISHED_TTL = 60 * 60 * 1000; // 已结束记录保留 60 分钟

export const planRunStore = reactive<{ runs: TrackedRun[] }>({ runs: [] });

let timer: number | undefined;
let initialized = false;

const isActive = (s: string) => s === "running" || s === "pending";

function persist() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(planRunStore.runs.slice(0, MAX_KEEP)));
  } catch {
    /* 存储不可用时忽略 */
  }
}

/** 首次访问时从 localStorage 恢复，并清理过期记录。 */
function init() {
  if (initialized) return;
  initialized = true;
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) {
      const arr = JSON.parse(raw) as TrackedRun[];
      const now = Date.now();
      planRunStore.runs = arr
        .filter(
          (r) =>
            r.runId &&
            (isActive(r.status) ||
              (r.finishedAt ? now - new Date(r.finishedAt).getTime() < FINISHED_TTL : true)),
        )
        .slice(0, MAX_KEEP);
    }
  } catch {
    /* 数据损坏则忽略 */
  }
  poll();
  ensureTimer();
}

/** 显式初始化（组件挂载时调用）：从 localStorage 恢复历史并接管轮询。幂等。 */
export function ensurePlanRunStore() {
  init();
}

/** 新增一条运行追踪（发起执行后调用）。 */
export function trackRun(planId: string, planName: string, runId: string) {
  init();
  planRunStore.runs = planRunStore.runs.filter((r) => r.runId !== runId);
  planRunStore.runs.unshift({
    planId,
    planName,
    runId,
    status: "running",
    startedAt: null,
    finishedAt: null,
  });
  planRunStore.runs = planRunStore.runs.slice(0, MAX_KEEP);
  persist();
  ensureTimer();
  poll();
}

/** 收起（移除）某条追踪记录。 */
export function dismissRun(runId: string) {
  planRunStore.runs = planRunStore.runs.filter((r) => r.runId !== runId);
  persist();
}

/** 某场景当前是否有进行中的运行。 */
export function hasActiveRun(planId: string): boolean {
  init();
  return planRunStore.runs.some((r) => r.planId === planId && isActive(r.status));
}

function ensureTimer() {
  const active = planRunStore.runs.some((r) => isActive(r.status));
  if (active && timer === undefined) {
    timer = window.setInterval(poll, 2500);
  } else if (!active && timer !== undefined) {
    clearInterval(timer);
    timer = undefined;
  }
}

/** 拉取所有进行中运行的最新状态；结束时提示并持久化。 */
async function poll() {
  const active = planRunStore.runs.filter((r) => isActive(r.status));
  if (!active.length) {
    ensureTimer();
    return;
  }
  for (const r of active) {
    const before = r.status;
    try {
      const detail = await api.getRun(r.runId);
      r.status = detail.status;
      r.startedAt = detail.started_at;
      r.finishedAt = detail.finished_at;
    } catch {
      /* 单条查询失败保持原状态，下轮重试 */
      continue;
    }
    if (isActive(before) && !isActive(r.status)) {
      ElMessage[r.status === "passed" ? "success" : "warning"](
        `场景「${r.planName}」执行结束：${r.status}`,
      );
    }
  }
  persist();
  ensureTimer();
}
