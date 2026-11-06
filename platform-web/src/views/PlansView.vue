<script setup lang="ts">
/**
 * 测试场景管理：将常用用例集合保存为可复用场景，一键发起执行。
 * 卡片列表 + 当前运行条 + 新建/编辑抽屉（树形勾选）。
 */
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import type { EnvInfo, PlanItem } from "../types";
import { planRunStore, trackRun, dismissRun, hasActiveRun, ensurePlanRunStore } from "../stores/planRun";

const router = useRouter();

/* ============ 数据加载 ============ */
const plans = ref<PlanItem[]>([]);
const loading = ref(false);
const envs = ref<EnvInfo[]>([]);

async function loadPlans() {
  loading.value = true;
  try {
    plans.value = await api.listPlans();
  } finally {
    loading.value = false;
  }
}

/* ============ 运行状态追踪（共享 store，切换页面不丢失） ============ */
// 进行中的运行置顶，其次按开始时间倒序
const trackedRuns = computed(() =>
  [...planRunStore.runs].sort((a, b) => {
    const aActive = a.status === "running" || a.status === "pending" ? 0 : 1;
    const bActive = b.status === "running" || b.status === "pending" ? 0 : 1;
    if (aActive !== bActive) return aActive - bActive;
    return (b.startedAt ?? b.finishedAt ?? "").localeCompare(a.startedAt ?? a.finishedAt ?? "");
  }),
);

// 某场景是否正在运行（用于禁用其执行按钮，避免重复发起）
const planRunning = (plan: PlanItem) => hasActiveRun(plan.id);

async function doRun(plan: PlanItem) {
  if (planRunning(plan)) {
    ElMessage.warning("该场景已有运行中的批次");
    return;
  }
  try {
    const run = await api.runPlan(plan.id);
    trackRun(plan.id, plan.name, run.run_id);
    ElMessage.success(`批次已发起：${run.run_id}`);
    loadPlans();
  } catch { /* 拦截器已提示 */ }
}

async function cancelRun(runId: string) {
  try {
    await api.cancelRun(runId);
    ElMessage.warning("已请求取消");
  } catch { /* 拦截器已提示 */ }
}

function toReport(runId: string) {
  router.push({ path: "/report", query: { runId } });
}

/* ============ 状态标签 ============ */
const statusType = (s: string) =>
  (({ running: "warning", passed: "success", failed: "danger", error: "danger", cancelled: "info" } as Record<string, any>)[s] ?? "info");

const fmtTime = (iso: string | null) => {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16);
};

/* ============ 新建/编辑抽屉 ============ */
const drawer = ref(false);
const drawerMode = ref<"create" | "edit">("create");
const editPlanId = ref("");
const formName = ref("");
const formDesc = ref("");
const formEnv = ref("");
const saving = ref(false);

function openCreate() {
  drawerMode.value = "create";
  editPlanId.value = "";
  formName.value = "";
  formDesc.value = "";
  formEnv.value = envs.value[0]?.name ?? "";
  checkedIds.value = [];
  drawer.value = true;
  loadTree();
}

async function openEdit(plan: PlanItem) {
  drawerMode.value = "edit";
  editPlanId.value = plan.id;
  formName.value = plan.name;
  formDesc.value = plan.desc;
  formEnv.value = plan.env || (envs.value[0]?.name ?? "");
  drawer.value = true;
  await loadTree();
  await nextTick();
  treeRef.value?.setCheckedKeys(plan.case_ids);
  checkedIds.value = [...plan.case_ids];
}

async function save() {
  const ids = collectIds();
  if (!formName.value.trim()) {
    ElMessage.warning("场景名称不能为空");
    return;
  }
  if (!ids.length) {
    ElMessage.warning("请至少勾选一条用例");
    return;
  }
  saving.value = true;
  try {
    const body = { name: formName.value.trim(), desc: formDesc.value, case_ids: ids, env: formEnv.value };
    if (drawerMode.value === "create") {
      await api.createPlan(body);
    } else {
      await api.updatePlan(editPlanId.value, body);
    }
    ElMessage.success(drawerMode.value === "create" ? "场景已创建" : "场景已保存");
    drawer.value = false;
    loadPlans();
  } catch { /* 拦截器已提示 */ } finally {
    saving.value = false;
  }
}

async function removePlan(plan: PlanItem) {
  try {
    await ElMessageBox.confirm(
      `确认删除场景「${plan.name}」？此操作不可恢复。`,
      "删除场景",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
    await api.deletePlan(plan.id);
    ElMessage.success(`已删除：${plan.name}`);
    loadPlans();
  } catch { /* 用户取消或拦截器已提示 */ }
}

/* ============ 用例树（同执行中心 pick 模式） ============ */
interface TreeNode {
  id: string;
  label: string;
  module?: string;
  children?: TreeNode[];
}
const treeRef = ref();
const treeData = ref<TreeNode[]>([]);
const treeLoading = ref(false);
const treeFilter = ref("");
const checkedIds = ref<string[]>([]);

async function loadTree() {
  if (treeData.value.length) return;
  treeLoading.value = true;
  try {
    const resp = await api.listCases({ run: true });
    const byMod = new Map<string, TreeNode[]>();
    for (const c of resp.items) {
      const leaf: TreeNode = { id: c.case_id, label: `${c.case_id} · ${c.api_name}` };
      if (!byMod.has(c.module)) byMod.set(c.module, []);
      byMod.get(c.module)!.push(leaf);
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

function collectIds(): string[] {
  const nodes: TreeNode[] = treeRef.value?.getCheckedNodes(true) ?? [];
  return nodes.filter((n) => !n.id.startsWith("mod:")).map((n) => n.id);
}

function onCheck() {
  checkedIds.value = collectIds();
}

const pickedCount = computed(() => checkedIds.value.length);

watch(treeFilter, (v) => treeRef.value?.filter(v));

/* ============ 生命周期 ============ */
onMounted(async () => {
  ensurePlanRunStore(); // 首屏即从 localStorage 恢复历史运行并接管轮询
  await loadPlans();
  const resp = await api.listEnvs();
  envs.value = resp.envs;
  // planRunStore 为模块级单例，首次访问自动从 localStorage 恢复并接管轮询，
  // 组件卸载时不中断，因此无需在此手动启动/停止。
});
</script>

<template>
  <div>
    <!-- 顶部工具栏 -->
    <div class="page-header">
      <span class="page-title">共 {{ plans.length }} 个场景</span>
      <el-button v-perm="'plan:manage'" type="primary" @click="openCreate">+ 新建场景</el-button>
    </div>

    <!-- 运行追踪条（共享 store：切换页面/刷新都不丢失，进行中置顶） -->
    <el-card v-if="trackedRuns.length" shadow="never" class="run-bar">
      <div
        v-for="(r, idx) in trackedRuns"
        :key="r.runId"
        class="run-bar-inner"
        :class="{ 'run-bar-row': idx > 0 }"
      >
        <span class="run-dot" :class="{ 'is-running': r.status === 'running' || r.status === 'pending' }"></span>
        <span class="run-plan-name">{{ r.planName }}</span>
        <el-tag :type="statusType(r.status)" size="small">{{ r.status }}</el-tag>
        <span class="run-id mono">{{ r.runId }}</span>
        <span v-if="r.finishedAt" class="run-time">{{ fmtTime(r.finishedAt) }}</span>
        <div class="run-actions">
          <el-button
            v-if="r.status === 'running' || r.status === 'pending'"
            size="small" type="danger" plain
            @click="cancelRun(r.runId)"
          >⏹ 取消</el-button>
          <el-button
            v-if="['passed', 'failed', 'error'].includes(r.status)"
            size="small" type="primary"
            @click="toReport(r.runId)"
          >📊 查看报告</el-button>
          <el-button
            v-if="r.status !== 'running' && r.status !== 'pending'"
            size="small" text
            @click="dismissRun(r.runId)"
          >✕ 收起</el-button>
        </div>
      </div>
    </el-card>

    <!-- 空状态 -->
    <el-empty v-if="!loading && !plans.length" description="还没有场景，点右上角新建" />

    <!-- 场景卡片网格 -->
    <el-row v-loading="loading" :gutter="12">
      <el-col
        v-for="plan in plans"
        :key="plan.id"
        :xs="24"
        :sm="12"
        :md="8"
        :lg="6"
      >
        <el-card shadow="hover" class="plan-card">
          <template #header>
            <div class="card-head">
              <span class="card-name" :title="plan.name">{{ plan.name }}</span>
              <el-tag
                v-if="planRunning(plan)"
                type="warning"
                size="small"
              >运行中</el-tag>
            </div>
          </template>

          <div class="card-desc">{{ plan.desc || "（无描述）" }}</div>

          <div class="card-tags">
            <el-tag size="small" type="info">{{ plan.case_ids.length }} 条用例</el-tag>
            <el-tag v-if="plan.env" size="small">{{ plan.env }}</el-tag>
          </div>

          <div class="card-last">
            最近执行：{{ fmtTime(plan.last_run_at) }}
            <el-button
              v-if="plan.last_run_id"
              class="card-report-btn"
              size="small"
              text
              type="primary"
              @click="toReport(plan.last_run_id!)"
            >📊 查看上次报告</el-button>
          </div>

          <div class="card-actions">
            <el-button
              v-perm="'run:create'"
              size="small"
              type="primary"
              :disabled="planRunning(plan)"
              @click="doRun(plan)"
            >▶ 执行</el-button>
            <el-button v-perm="'plan:manage'" size="small" @click="openEdit(plan)">✏ 编辑</el-button>
            <el-button v-perm="'plan:manage'" size="small" type="danger" plain @click="removePlan(plan)">🗑</el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 新建/编辑抽屉 -->
    <el-drawer
      v-model="drawer"
      size="52%"
      :title="drawerMode === 'create' ? '新建场景' : '编辑场景'"
      @close="treeFilter = ''"
    >
      <el-form label-width="72px" @submit.prevent>
        <el-form-item label="名称" required>
          <el-input v-model="formName" placeholder="如：冒烟测试" maxlength="60" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="formDesc"
            type="textarea"
            :rows="2"
            placeholder="场景用途说明（可选）"
            maxlength="200"
          />
        </el-form-item>
        <el-form-item label="偏好环境">
          <el-select v-model="formEnv" clearable placeholder="默认环境" style="width: 100%">
            <el-option
              v-for="e in envs"
              :key="e.name"
              :label="`${e.name}（${e.base_url}）`"
              :value="e.name"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="用例">
          <div class="tree-wrap">
            <div class="tree-bar">
              <el-input v-model="treeFilter" size="small" placeholder="过滤模块/编号/接口名" clearable />
              <span class="tree-count">已选 {{ pickedCount }} 条</span>
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
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="drawer = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">
          {{ drawerMode === "create" ? "创建场景" : "保存修改" }}
        </el-button>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.page-title { font-size: 13px; color: #909399; }

.run-bar { margin-bottom: 12px; border-left: 3px solid #409eff; }
.run-bar-inner { display: flex; align-items: center; gap: 10px; }
.run-bar-row { margin-top: 10px; padding-top: 10px; border-top: 1px dashed #ebeef5; }
.run-actions { margin-left: auto; display: flex; gap: 8px; }
.run-time { font-size: 12px; color: #a0a4a8; }
.run-dot { width: 10px; height: 10px; border-radius: 50%; background: #909399; flex-shrink: 0; }
.run-dot.is-running { background: #409eff; animation: pulse 1s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
.run-plan-name { font-weight: 600; font-size: 13px; }
.run-id { font-size: 12px; color: #909399; }

.plan-card { margin-bottom: 12px; }
.card-head { display: flex; align-items: center; gap: 6px; }
.card-name { font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-desc {
  font-size: 12px; color: #606266; line-height: 1.5;
  height: 36px; overflow: hidden;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  margin-bottom: 8px;
}
.card-tags { display: flex; gap: 6px; margin-bottom: 6px; }
.card-last { font-size: 11px; color: #a0a4a8; margin-bottom: 10px; }
.card-report-btn { margin-left: 6px; height: auto; padding: 0; font-size: 11px; vertical-align: baseline; }
.card-actions { display: flex; gap: 6px; flex-wrap: wrap; }

.tree-wrap { width: 100%; }
.tree-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.tree-count { font-size: 12px; color: #409eff; white-space: nowrap; }
.pick-tree {
  max-height: 320px; overflow: auto;
  border: 1px solid #ebeef5; border-radius: 6px; padding: 4px;
}

.mono { font-family: Consolas, Monaco, monospace; }
</style>
