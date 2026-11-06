<script setup lang="ts">
/** 凭证管理：secret_vars.xlsx「全局变量」的可视化维护。值仅掩码回显、绝不明文；编辑留空即不改原值。 */
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import type { SecretVar } from "../types";

const loading = ref(false);
const exists = ref(true);
const items = ref<SecretVar[]>([]);
const kw = ref("");

const filtered = computed(() => {
  const q = kw.value.trim().toLowerCase();
  return q ? items.value.filter((v) => v.name.toLowerCase().includes(q) || v.remark.toLowerCase().includes(q)) : items.value;
});

const kindType = (k: string) =>
  (({ "登录态/凭证": "warning", "密文": "danger", "固定值": "info" } as Record<string, any>)[k] ?? "info");
// 变量引用写法 {{name}}：放脚本来构造，避免模板插值定界符冲突
const varRef = (n: string) => "{{" + n + "}}";

async function reload() {
  loading.value = true;
  try {
    const resp = await api.listSecrets();
    exists.value = resp.exists;
    items.value = resp.items;
  } finally {
    loading.value = false;
  }
}
onMounted(reload);

/* ---------- 新增 / 编辑 ---------- */
const dialog = ref(false);
const saving = ref(false);
const editing = ref<SecretVar | null>(null);   // null=新增
const form = reactive<{ name: string; value: string; remark: string }>({ name: "", value: "", remark: "" });

function openCreate() {
  editing.value = null;
  form.name = ""; form.value = ""; form.remark = "";
  dialog.value = true;
}
function openEdit(row: SecretVar) {
  editing.value = row;
  form.name = row.name;
  form.value = "";                 // 原值不回显，留空表示保持不变
  form.remark = row.remark;
  dialog.value = true;
}

async function save() {
  const name = form.name.trim();
  if (!name) return ElMessage.warning("变量名不能为空");
  if (!editing.value && !form.value) return ElMessage.warning("新增凭证必须填写值");
  saving.value = true;
  try {
    if (editing.value) {
      await api.updateSecret(name, { value: form.value || undefined, remark: form.remark });
      ElMessage.success(form.value ? `已更新 ${name} 的值` : `已更新 ${name} 的备注`);
    } else {
      await api.createSecret({ name, value: form.value, remark: form.remark });
      ElMessage.success(`已新增凭证 ${name}`);
    }
    dialog.value = false;
    await reload();
  } finally {
    saving.value = false;
  }
}

async function remove(row: SecretVar) {
  try {
    await ElMessageBox.confirm(
      `确认删除凭证变量 ${row.name}？该操作会从 secret_vars.xlsx 移除整行，引用它的用例会报「变量未定义」。`,
      "删除凭证", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  } catch { return; }
  await api.deleteSecret(row.name);
  ElMessage.success(`已删除 ${row.name}`);
  await reload();
}
</script>

<template>
  <div v-loading="loading">
    <div class="head">
      <div>
        <div class="page-title">凭证管理</div>
        <div class="page-desc">
          密文存储 · 界面仅回显掩码 · 支持编辑值 / 备注与增删。对应框架 secret_vars 变量池，优先级高于用例全局变量表。
        </div>
      </div>
      <el-button type="primary" @click="openCreate">＋ 新增凭证</el-button>
    </div>

    <el-alert
      v-if="!exists"
      type="info"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
      title="尚无凭证文件；新增第一条将自动创建 data/secret_vars.xlsx（该文件已被 .gitignore 忽略，不入库）。"
    />

    <el-card shadow="never">
      <div class="filter-bar">
        <el-input v-model="kw" placeholder="搜索变量名 / 备注" clearable style="width: 240px" />
        <span class="count">共 {{ items.length }} 个变量</span>
      </div>

      <el-table :data="filtered" size="small" row-key="name">
        <el-table-column label="变量名（引用方式）" min-width="180">
          <template #default="{ row }"><span class="mono">{{ varRef(row.name) }}</span></template>
        </el-table-column>
        <el-table-column label="类型" width="130">
          <template #default="{ row }"><el-tag :type="kindType(row.kind)" size="small">{{ row.kind }}</el-tag></template>
        </el-table-column>
        <el-table-column label="值（掩码）" min-width="220">
          <template #default="{ row }">
            <span v-if="row.empty" class="empty">(空)</span>
            <span v-else class="mono">{{ row.masked_value }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="remark" label="备注" min-width="200" show-overflow-tooltip />
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <div class="op-cell">
              <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <span style="color: #909399">{{ exists ? "暂无凭证变量" : "尚未创建凭证文件，点右上「新增凭证」开始" }}</span>
        </template>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialog"
      :title="editing ? '编辑凭证 · ' + editing.name : '新增凭证'"
      width="520px"
      append-to-body
    >
      <el-form label-width="88px">
        <el-form-item label="变量名">
          <el-input v-model="form.name" :disabled="!!editing" placeholder="如 token / user_id" />
          <div v-if="editing" class="hint">变量名作为定位锚点不可改；需改名请「新增新名 + 删除旧行」。</div>
        </el-form-item>
        <el-form-item label="值">
          <el-input
            v-model="form.value"
            type="password"
            show-password
            :placeholder="editing ? '留空表示保持原值不变' : '填写凭证值（保存后不再明文回显）'"
          />
          <div v-if="editing" class="hint">
            当前值：{{ editing.empty ? "(空)" : editing.masked_value }}
          </div>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.remark" placeholder="用途说明，如 登录态 token（会覆盖同名全局变量）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 12px; }
.page-title { font-size: 18px; font-weight: 600; }
.page-desc { color: #909399; font-size: 13px; margin-top: 4px; max-width: 720px; }
.filter-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.count { margin-left: auto; color: #909399; font-size: 12px; }
.mono { font-family: Consolas, Monaco, monospace; }
.empty { color: #c0c4cc; }
.op-cell { display: flex; align-items: center; gap: 2px; white-space: nowrap; }
.op-cell .el-button + .el-button { margin-left: 0; }
.hint { color: #909399; font-size: 12px; margin-top: 2px; }
</style>
