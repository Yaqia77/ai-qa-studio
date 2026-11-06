<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import type { WebhookItem } from "../types";

const items = ref<WebhookItem[]>([]);
const loading = ref(false);

/* 抽屉表单 */
const drawerVisible = ref(false);
const isEdit = ref(false);
const editId = ref("");
const form = reactive({
  name: "",
  url: "",
  type: "custom" as "wechat_work" | "feishu" | "custom",
  events: ["run_fail", "run_error"] as string[],
  enabled: true,
});

const EVENT_OPTIONS = [
  { value: "run_pass", label: "✅ 全部通过" },
  { value: "run_fail", label: "❌ 存在失败" },
  { value: "run_error", label: "⚠️ 执行异常" },
];

const TYPE_OPTIONS = [
  { value: "wechat_work", label: "企业微信" },
  { value: "feishu", label: "飞书" },
  { value: "custom", label: "自定义 JSON" },
];

function typeLabel(t: string) {
  return TYPE_OPTIONS.find((o) => o.value === t)?.label ?? t;
}
function eventLabel(e: string) {
  return EVENT_OPTIONS.find((o) => o.value === e)?.label ?? e;
}

async function loadList() {
  loading.value = true;
  try {
    items.value = await api.listWebhooks();
  } finally {
    loading.value = false;
  }
}
onMounted(loadList);

function openCreate() {
  isEdit.value = false;
  editId.value = "";
  form.name = "";
  form.url = "";
  form.type = "custom";
  form.events = ["run_fail", "run_error"];
  form.enabled = true;
  drawerVisible.value = true;
}

function openEdit(item: WebhookItem) {
  isEdit.value = true;
  editId.value = item.id;
  form.name = item.name;
  form.url = item.url;
  form.type = item.type;
  form.events = [...item.events];
  form.enabled = item.enabled;
  drawerVisible.value = true;
}

async function save() {
  if (!form.name.trim()) { ElMessage.warning("名称不能为空"); return; }
  if (!form.url.trim()) { ElMessage.warning("URL 不能为空"); return; }
  try {
    if (isEdit.value) {
      await api.updateWebhook(editId.value, {
        name: form.name, url: form.url, type: form.type,
        events: form.events, enabled: form.enabled,
      });
      ElMessage.success("已更新");
    } else {
      await api.createWebhook({
        name: form.name, url: form.url, type: form.type,
        events: form.events, enabled: form.enabled,
      });
      ElMessage.success("已创建");
    }
    drawerVisible.value = false;
    loadList();
  } catch { /* 拦截器提示 */ }
}

async function remove(item: WebhookItem) {
  await ElMessageBox.confirm(`确认删除「${item.name}」？`, "提示", { type: "warning" });
  await api.deleteWebhook(item.id);
  ElMessage.success("已删除");
  loadList();
}

async function toggle(item: WebhookItem) {
  await api.updateWebhook(item.id, { enabled: !item.enabled });
  loadList();
}

const testing = ref<string>("");
async function test(item: WebhookItem) {
  testing.value = item.id;
  try {
    const resp = await api.testWebhook(item.id);
    if (resp.ok) {
      ElMessage.success(`测试发送成功 (HTTP ${resp.status_code})`);
    } else {
      ElMessage.warning(`响应码 ${resp.status_code}: ${resp.response.slice(0, 100)}`);
    }
  } catch { /* 拦截器提示 */ } finally {
    testing.value = "";
  }
}
</script>

<template>
  <div>
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px">
      <h3 style="margin: 0">Webhook 通知配置</h3>
      <el-button type="primary" @click="openCreate">＋ 新建通知</el-button>
    </div>

    <el-table :data="items" v-loading="loading" stripe empty-text="暂无通知配置">
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="type" label="类型" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="row.type === 'wechat_work' ? 'success' : row.type === 'feishu' ? '' : 'info'">
            {{ typeLabel(row.type) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="url" label="URL" min-width="220" show-overflow-tooltip />
      <el-table-column label="触发事件" min-width="180">
        <template #default="{ row }">
          <el-tag v-for="ev in row.events" :key="ev" size="small" style="margin: 2px">
            {{ eventLabel(ev) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" size="small" @change="toggle(row)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
          <el-button link type="success" size="small" :loading="testing === row.id" @click="test(row)">测试</el-button>
          <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新建/编辑抽屉 -->
    <el-drawer v-model="drawerVisible" :title="isEdit ? '编辑通知' : '新建通知'" size="420px">
      <el-form label-position="top">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="例如：后端开发群" />
        </el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="form.type">
            <el-radio v-for="opt in TYPE_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="Webhook URL">
          <el-input v-model="form.url" placeholder="https://qyapi.weixin.qq.com/..." />
        </el-form-item>
        <el-form-item label="触发事件">
          <el-checkbox-group v-model="form.events">
            <el-checkbox v-for="ev in EVENT_OPTIONS" :key="ev.value" :value="ev.value">{{ ev.label }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
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
</style>
