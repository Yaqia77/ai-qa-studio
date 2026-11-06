<script setup lang="ts">
/**
 * UI 登录态会话管理（storage_state 复用）：全局注册表的列表 / 采集 / 增删改。
 *
 * 登录态 = 一次成功登录后导出的 Playwright storage_state（cookie + localStorage），
 * 被 UI 用例通过 session 字段引用后「开机即已登录」，从而删除重复登录步骤。
 * 安全边界：本页只读写元数据与派生状态，任何接口都不回传 storage_state 实体内容。
 * 写操作（新建/编辑/删除/采集）需 session:manage（v-perm 门控按钮）；查看仅需登录态。
 */
import { computed, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import type { UiSession, UiSessionMeta } from "../types";

const loading = ref(false);
const saving = ref(false);
const collectingId = ref("");
const sessions = ref<UiSession[]>([]);
const meta = ref<UiSessionMeta | null>(null);

async function load() {
  loading.value = true;
  try {
    sessions.value = await api.listUiSessions();
    meta.value = await api.getUiSessionMeta();
  } catch {
    sessions.value = [];
  } finally {
    loading.value = false;
  }
}
load();

const statusMap: Record<string, { label: string; type: string }> = {
  valid: { label: "🟢 有效", type: "success" },
  expired: { label: "🟡 已过期", type: "warning" },
  not_collected: { label: "⚪ 未采集", type: "info" },
  failed: { label: "🔴 采集失败", type: "danger" },
};
function statusInfo(s?: string) {
  return statusMap[s ?? "not_collected"] ?? statusMap.not_collected;
}
function remainText(row: UiSession) {
  if (row.ttl_seconds <= 0) return "永不过期";
  if (row.status === "valid") {
    const sec = row.remaining_seconds ?? 0;
    return sec >= 60 ? `剩 ${Math.floor(sec / 60)} 分 ${sec % 60} 秒` : `剩 ${sec} 秒`;
  }
  if (row.status === "expired") return "已过期（下次复用自动重采）";
  return "";
}

/* ================= 采集 ================= */
async function doCollect(row: UiSession) {
  collectingId.value = row.id;
  try {
    const r = await api.collectUiSession(row.id);
    if (r.ok) ElMessage.success(`登录态「${row.name}」采集成功`);
    else ElMessage.error(`采集失败：${r.error ?? "未知原因"}`);
    await load();
  } catch {
    /* 拦截器已提示 */
  } finally {
    collectingId.value = "";
  }
}

/* ================= 新建 / 编辑抽屉 ================= */
const editVisible = ref(false);
const mode = ref<"create" | "edit">("create");
const editingId = ref("");
const form = reactive({
  id: "", name: "", desc: "", env: "", browser: "chromium",
  headless: true, file: "", capture_case: "", ttl_seconds: 3600,
});

const envOptions = computed(() => meta.value?.envs ?? []);
const browserOptions = computed(() => meta.value?.browsers ?? ["chromium"]);
const projectOptions = computed(() => meta.value?.ui_projects ?? []);
const captureCaseOptions = computed(
  () => projectOptions.value.find((p) => p.file === form.file)?.cases ?? []
);

function blank() {
  mode.value = "create";
  editingId.value = "";
  Object.assign(form, {
    id: "", name: "", desc: "", env: envOptions.value[0] ?? "", browser: "chromium",
    headless: true, file: projectOptions.value[0]?.file ?? "", capture_case: "", ttl_seconds: 3600,
  });
  editVisible.value = true;
}

function openEdit(row: UiSession) {
  mode.value = "edit";
  editingId.value = row.id;
  Object.assign(form, {
    id: row.id, name: row.name, desc: row.desc, env: row.env, browser: row.browser,
    headless: row.headless, file: row.file, capture_case: row.capture_case, ttl_seconds: row.ttl_seconds,
  });
  editVisible.value = true;
}

async function save() {
  if (!form.id.trim()) { ElMessage.error("会话 id 不能为空"); return; }
  if (!form.env) { ElMessage.error("必须绑定环境 env"); return; }
  if (!form.file) { ElMessage.error("必须选择采集用例所在的 UI 项目"); return; }
  if (!form.capture_case) { ElMessage.error("必须选择一条采集用例 capture_case"); return; }
  const payload: Record<string, unknown> = {
    id: form.id.trim(), name: form.name.trim() || "新建登录态", desc: form.desc.trim(),
    env: form.env, browser: form.browser, headless: form.headless,
    file: form.file, capture_case: form.capture_case.trim(),
    ttl_seconds: Number(form.ttl_seconds) || 0,
  };
  saving.value = true;
  try {
    if (mode.value === "create") {
      await api.createUiSession(payload);
      ElMessage.success("登录态已创建（状态：未采集，请点『立即采集』生成）");
    } else {
      // id 不可改：更新只传可编辑字段
      const { id: _omit, ...changes } = payload;
      await api.updateUiSession(editingId.value, changes);
      ElMessage.success("登录态已保存");
    }
    editVisible.value = false;
    await load();
  } catch {
    /* 拦截器已提示（env 非法/id 冲突等后端 400/409） */
  } finally {
    saving.value = false;
  }
}

async function remove(row: UiSession) {
  try {
    await api.deleteUiSession(row.id);
    ElMessage.success(`登录态「${row.name}」已删除`);
    await load();
  } catch {
    /* 被 UI 用例引用时后端 409，拦截器已提示 */
  }
}

function onFileChange() {
  form.capture_case = "";  // 换项目后采集用例需重选
}
</script>

<template>
  <div class="sess-body">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
      登录态 = 一次成功登录后导出的 storage_state（cookie + localStorage）。UI 用例引用后「开机即已登录」，
      无需在步骤里重复登录。会话实体属高敏数据：不进 Git、不回传前端、不写日志。
    </el-alert>

    <el-card shadow="never" style="margin-bottom: 12px">
      <div class="bar">
        <span class="tip">共 {{ sessions.length }} 个登录态会话</span>
        <div>
          <el-button @click="load">刷新</el-button>
          <el-button v-perm="'session:manage'" type="success" plain @click="blank">＋ 新建登录态</el-button>
        </div>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-table :data="sessions" v-loading="loading" row-key="id" size="small">
        <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
        <el-table-column prop="id" label="会话 id" min-width="150">
          <template #default="{ row }"><span class="mono">{{ row.id }}</span></template>
        </el-table-column>
        <el-table-column prop="env" label="环境" width="90" />
        <el-table-column label="采集用例" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono">{{ row.capture_case }}</span>
            <span class="dim"> @ {{ row.file }}</span>
          </template>
        </el-table-column>
        <el-table-column label="TTL" width="90">
          <template #default="{ row }">{{ row.ttl_seconds > 0 ? row.ttl_seconds + ' 秒' : '不过期' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tooltip :content="row.last_error || '无错误记录'" :disabled="row.status !== 'failed'" placement="top">
              <el-tag :type="statusInfo(row.status).type" size="small">{{ statusInfo(row.status).label }}</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="有效期" min-width="150">
          <template #default="{ row }"><span class="dim">{{ remainText(row) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <div class="op-cell">
              <el-button v-perm="'session:manage'" size="small" text type="warning"
                :loading="collectingId === row.id" @click="doCollect(row)">立即采集</el-button>
              <el-button v-perm="'session:manage'" size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
              <el-popconfirm v-perm="'session:manage'" :title="'删除 ' + row.id + '？连带清理实体文件'" width="240">
                <template #reference><el-button size="small" text type="danger">删除</el-button></template>
              </el-popconfirm>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <div style="padding: 20px; color: #909399">还没有登录态会话，点右上「＋ 新建登录态」创建并采集。</div>
        </template>
      </el-table>
    </el-card>

    <!-- 新建 / 编辑抽屉 -->
    <el-drawer v-model="editVisible" size="46%" :title="mode === 'create' ? '新建登录态会话' : `编辑登录态 ${editingId}`">
      <el-form label-width="96px" @submit.prevent>
        <el-form-item label="会话 id">
          <el-input v-model="form.id" :disabled="mode === 'edit'" class="mono"
            placeholder="唯一名，如 album_test_login（不含 / \ ? #；创建后不可改）" />
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="如：相册测试环境登录态" />
        </el-form-item>
        <el-form-item label="环境">
          <el-select v-model="form.env" style="width: 100%" placeholder="绑定 config.yaml 的环境名">
            <el-option v-for="e in envOptions" :key="e" :label="e" :value="e" />
          </el-select>
          <div class="hint">cookie 归属域名，切环境各留一份不串味；改环境会作废旧登录态并需被引用时禁止。</div>
        </el-form-item>
        <el-form-item label="浏览器">
          <el-select v-model="form.browser" style="width: 100%">
            <el-option v-for="b in browserOptions" :key="b" :label="b" :value="b" />
          </el-select>
        </el-form-item>
        <el-form-item label="采集无头">
          <el-switch v-model="form.headless" />
        </el-form-item>
        <el-form-item label="采集项目">
          <el-select v-model="form.file" style="width: 100%" placeholder="采集用例所在的 UI 项目" @change="onFileChange">
            <el-option v-for="p in projectOptions" :key="p.file" :value="p.file" :label="`${p.name}（${p.file}）`" />
          </el-select>
          <div v-if="!projectOptions.length" class="hint">当前没有 UI 项目，请先在「用例管理」建 UI 项目并写一条登录用例。</div>
        </el-form-item>
        <el-form-item label="采集用例">
          <el-select v-model="form.capture_case" filterable style="width: 100%"
            :disabled="!form.file" placeholder="选一条会登录成功的 UI 用例（登录后导出登录态）">
            <el-option v-for="c in captureCaseOptions" :key="c.case_id" :value="c.case_id"
              :label="`${c.case_id} ${c.name}`" />
          </el-select>
        </el-form-item>
        <el-form-item label="有效期(秒)">
          <el-input-number v-model="form.ttl_seconds" :min="0" :step="600" controls-position="right" />
          <div class="hint">复用有效期；&lt;=0 视为永不过期（不自动重采，仅手动采集）。过期后复用前会在进程内自动重采一次。</div>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.desc" type="textarea" :rows="2" />
        </el-form-item>

        <div class="hint" style="margin: 6px 0 12px">
          采集用例若需账号密码，走既有 <span class="mono">{{ '\{\{username\}\}' }}</span> / 凭证机制；采集成功的登录态等同实时凭证，绝不外泄。
        </div>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ mode === "create" ? "新建" : "保存" }}</el-button>
      </el-form>
    </el-drawer>
  </div>
</template>

<style scoped>
.sess-body { padding: 4px; }
.bar { display: flex; align-items: center; justify-content: space-between; }
.tip { color: #606266; font-size: 13px; }
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.dim { color: #909399; font-size: 12px; }
.hint { color: #909399; font-size: 12px; margin-top: 4px; line-height: 1.5; }
.op-cell { display: flex; align-items: center; gap: 2px; white-space: nowrap; }
.op-cell .el-button + .el-button { margin-left: 0; }
</style>
