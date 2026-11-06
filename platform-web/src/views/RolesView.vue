<script setup lang="ts">
/**
 * 角色与权限（设计文档 8.2）：权限码目录由后端固定，界面只改「码 → 角色」的分配。
 *
 * 保存即热生效——后端 require_perm 每次请求实时读矩阵，已登录会话无需重新登录；
 * 被锁死的格子（如 admin 的 user:manage）禁勾选，防止把平台改到无人可管。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import { currentUser, refreshMe } from "../stores/auth";
import type { PermMatrixResp } from "../types";

const data = ref<PermMatrixResp | null>(null);
const draft = reactive<Record<string, string[]>>({});
const loading = ref(false);
const saving = ref(false);
const dirty = ref(false);

async function load() {
  loading.value = true;
  try {
    const resp = await api.getPermissions();
    data.value = resp;
    for (const k of Object.keys(draft)) delete draft[k];
    for (const [code, roles] of Object.entries(resp.matrix)) draft[code] = [...roles];
    dirty.value = false;
  } finally {
    loading.value = false;
  }
}
onMounted(load);

/** 勾选/取消；锁定格由模板的 disabled 拦下 */
function toggle(code: string, role: string, on: boolean) {
  const cur = new Set(draft[code] ?? []);
  if (on) cur.add(role);
  else cur.delete(role);
  draft[code] = [...cur];
  dirty.value = true;
}

const lockedOf = (code: string) => data.value?.rules.locked_role[code] ?? "";

/** 变更清单：与后端 diff 同口径，保存前给人看清楚到底动了什么 */
const changedRows = computed(() => {
  const out: string[] = [];
  for (const item of data.value?.catalog ?? []) {
    const before = new Set(item.roles);
    const after = new Set(draft[item.code] ?? []);
    const add = [...after].filter((r) => !before.has(r));
    const del = [...before].filter((r) => !after.has(r));
    if (add.length || del.length) {
      out.push(`${item.name}${add.length ? " +" + add.join("/") : ""}${del.length ? " -" + del.join("/") : ""}`);
    }
  }
  return out;
});

async function save() {
  if (!changedRows.value.length) {
    ElMessage.info("没有需要保存的改动");
    return;
  }
  try {
    await ElMessageBox.confirm(
      `将变更 ${changedRows.value.length} 项权限，保存后立即生效（含当前已登录的会话）。`,
      "确认修改权限矩阵",
      { type: "warning", confirmButtonText: "保存", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  saving.value = true;
  try {
    const resp = await api.updatePermissions({ ...draft });
    ElMessage.success(`已保存，变更 ${resp.changed.length} 项`);
    await refreshMe(); // 自己所属角色的权限可能已变，顶栏与按钮立刻对齐
    load();
  } catch {
    /* 拦截器已提示（护栏拒绝会带具体原因） */
  } finally {
    saving.value = false;
  }
}

async function resetDefault() {
  try {
    await ElMessageBox.confirm(
      "恢复为代码内置的默认矩阵，当前自定义分配将被整体覆盖，立即生效。",
      "恢复默认权限",
      { type: "warning", confirmButtonText: "恢复默认", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    const resp = await api.resetPermissions();
    ElMessage.success(`已恢复默认（变更 ${resp.changed.length} 项）`);
    await refreshMe();
    load();
  } catch {
    /* 拦截器已提示 */
  }
}

const meRoleName = computed(() => currentUser.value?.role_name ?? "");
const lockedTipCount = computed(() => Object.keys(data.value?.rules.locked_role ?? {}).length);
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <span class="page-title">
        权限码目录由后端固定（共 {{ data?.catalog.length ?? 0 }} 项），此处仅调整授予的角色 · 我的角色：{{ meRoleName }}
      </span>
      <div class="header-ops">
        <el-button v-if="dirty" size="small" @click="load">放弃修改</el-button>
        <el-button size="small" plain @click="resetDefault">恢复默认</el-button>
        <el-button type="primary" size="small" :disabled="!dirty" :loading="saving" @click="save">
          保存{{ changedRows.length ? `（${changedRows.length} 项变更）` : "" }}
        </el-button>
      </div>
    </div>

    <el-alert v-if="changedRows.length" type="warning" :closable="false" class="diff-alert">
      <div class="diff-title">待保存的变更：</div>
      <div v-for="c in changedRows" :key="c">· {{ c }}</div>
    </el-alert>

    <el-table :data="data?.catalog ?? []" border size="small" row-key="code">
      <el-table-column label="权限" min-width="240">
        <template #default="{ row }">
          <div class="p-name">{{ row.name }}</div>
          <div class="p-code mono">{{ row.code }}</div>
          <div class="p-desc">{{ row.desc }}</div>
        </template>
      </el-table-column>
      <el-table-column
        v-for="r in data?.roles ?? []"
        :key="r.value"
        :label="r.label"
        width="112"
        align="center"
      >
        <template #default="{ row }">
          <el-tooltip
            v-if="lockedOf(row.code) === r.value"
            content="防锁死护栏：该权限必须保留给管理员，不可撤销"
          >
            <el-checkbox :model-value="true" disabled />
          </el-tooltip>
          <el-checkbox
            v-else
            :model-value="(draft[row.code] ?? []).includes(r.value)"
            @change="(v: boolean) => toggle(row.code, r.value, v)"
          />
        </template>
      </el-table-column>
    </el-table>

    <div class="foot-tip">
      生效说明：接口侧的 require_perm 按新矩阵实时判定，被收回权限的会话下一个请求即 403；
      菜单与按钮读取的是登录时下发的权限快照，刷新页面（或重新登录）即与后端一致。
      本次改动会自动写入操作审计。
      <template v-if="lockedTipCount"> · 已启用 {{ lockedTipCount }} 项防锁死护栏</template>
    </div>
  </div>
</template>

<style scoped>
.page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.page-title { font-size: 13px; color: #909399; }
.header-ops { display: flex; gap: 8px; }
.diff-alert { margin-bottom: 10px; line-height: 1.8; }
.diff-title { font-weight: 600; }
.mono { font-family: Consolas, Monaco, monospace; }
.p-name { font-size: 13px; font-weight: 600; color: #303133; }
.p-code { font-size: 11px; color: #909399; }
.p-desc { font-size: 11px; color: #a8abb2; line-height: 1.5; }
.foot-tip { margin-top: 10px; font-size: 12px; color: #909399; line-height: 1.7; }
</style>
