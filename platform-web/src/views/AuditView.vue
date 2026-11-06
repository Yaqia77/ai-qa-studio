<script setup lang="ts">
/**
 * 操作审计（设计文档 8.3 / 企业级硬性要求 #2）：谁在什么时候改了什么，结果如何。
 * 菜单与路由需 audit:view（admin/tl）；「清理历史」按钮仅 audit:manage（admin）可见。
 * 请求摘要由后端脱敏后下发（密码/token 之类别不落盘），前端只展示。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import { currentUser, hasPerm } from "../stores/auth";
import type { AuditItem } from "../types";

const logs = ref<AuditItem[]>([]);
const total = ref(0);
const operators = ref<string[]>([]);
const loading = ref(false);

/** days=0 不限时间；limit 为本次拉取上限（后端最多 2000） */
const query = reactive({ username: "", keyword: "", days: 7, only_failed: false, limit: 200 });

const DAY_OPTIONS = [
  { value: 1, label: "最近 1 天" },
  { value: 7, label: "最近 7 天" },
  { value: 30, label: "最近 30 天" },
  { value: 90, label: "最近 90 天" },
  { value: 0, label: "全部时间" },
];

async function load() {
  loading.value = true;
  try {
    const resp = await api.listAuditLogs({ ...query });
    logs.value = resp.items;
    total.value = resp.total;
  } finally {
    loading.value = false;
  }
}

async function loadOperators() {
  try {
    operators.value = (await api.auditOperators()).operators;
  } catch {
    /* 拦截器已提示 */
  }
}

onMounted(() => {
  load();
  loadOperators();
});

function reset() {
  Object.assign(query, { username: "", keyword: "", days: 7, only_failed: false, limit: 200 });
  load();
}

/** 导出当前筛选条件（同参数交给后端，避免前端二次过滤造成口径不一致） */
function exportCsv() {
  const url = api.auditExportUrl({
    username: query.username,
    keyword: query.keyword,
    days: query.days,
    only_failed: query.only_failed,
  });
  const a = document.createElement("a");
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  ElMessage.success("CSV 导出已开始下载");
}

/** 清理历史（仅 admin）：保留最近 N 天，0 为清空 */
async function purge() {
  let input: string;
  try {
    const r = await ElMessageBox.prompt("保留最近多少天的审计日志？输入 0 表示清空全部", "清理审计日志", {
      inputValue: "90",
      confirmButtonText: "确定",
      cancelButtonText: "取消",
      inputValidator: (v) => (/^\d+$/.test(v) ? true : "请输入非负整数"),
    });
    input = r.value;
  } catch {
    return;
  }
  try {
    const resp = await api.purgeAudit(Number(input));
    ElMessage.success(Number(input) > 0 ? `已清理 ${resp.removed} 条（保留最近 ${input} 天）` : `已清空 ${resp.removed} 条审计日志`);
    load();
  } catch {
    /* 拦截器已提示 */
  }
}

/* ---------- 详情抽屉 ---------- */
const detail = ref<AuditItem | null>(null);
const drawer = ref(false);
function openDetail(row: AuditItem) {
  detail.value = row;
  drawer.value = true;
}

const canManage = computed(() => hasPerm("audit:manage"));
const methodTag = (m: string) =>
  m === "DELETE" ? "danger" : m === "PUT" || m === "PATCH" ? "warning" : m === "POST" ? "success" : "info";
const fmtTs = (ts: string) => (ts || "").replace("T", " ");
/** 操作人列：自己操作的行高亮一下，便于回看“我改过什么” */
const isMe = (row: AuditItem) => row.username === currentUser.value?.username;
</script>

<template>
  <div>
    <div class="page-header">
      <span class="page-title">
        共留痕 {{ total }} 条 · 当前展示 {{ logs.length }} 条（写操作 POST/PUT/PATCH/DELETE 与登录尝试，敏感字段已脱敏）
      </span>
      <div class="header-ops">
        <el-button size="small" @click="exportCsv">导出 CSV</el-button>
        <el-button v-if="canManage" size="small" type="danger" plain @click="purge">清理历史</el-button>
      </div>
    </div>

    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent>
        <el-form-item label="操作人">
          <el-select v-model="query.username" clearable filterable placeholder="全部" style="width: 150px">
            <el-option v-for="u in operators" :key="u" :label="u" :value="u" />
          </el-select>
        </el-form-item>
        <el-form-item label="时间范围">
          <el-select v-model="query.days" style="width: 130px">
            <el-option v-for="d in DAY_OPTIONS" :key="d.value" :label="d.label" :value="d.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="条数上限">
          <el-select v-model="query.limit" style="width: 110px">
            <el-option :value="100" label="100" />
            <el-option :value="200" label="200" />
            <el-option :value="500" label="500" />
            <el-option :value="2000" label="2000" />
          </el-select>
        </el-form-item>
        <el-form-item label="关键字">
          <el-input
            v-model="query.keyword"
            placeholder="动作 / 路径 / 对象 / 账号"
            clearable
            style="width: 200px"
            @keyup.enter="load"
          />
        </el-form-item>
        <el-form-item label="仅看失败">
          <el-switch v-model="query.only_failed" @change="load" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="load">查询</el-button>
          <el-button @click="reset">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-table v-loading="loading" :data="logs" size="small" border class="audit-table">
      <el-table-column label="时间" width="150">
        <template #default="{ row }"><span class="mono">{{ fmtTs(row.ts) }}</span></template>
      </el-table-column>
      <el-table-column label="操作人" width="150">
        <template #default="{ row }">
          <span class="mono">{{ row.username }}</span>
          <el-tag v-if="isMe(row)" size="small" type="success" effect="plain" style="margin-left: 4px">我</el-tag>
          <div class="sub">{{ row.role }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="action" label="动作" min-width="170" show-overflow-tooltip />
      <el-table-column label="对象" min-width="130" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono">{{ row.target || "—" }}</span></template>
      </el-table-column>
      <el-table-column label="方法" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="methodTag(row.method)" effect="plain">{{ row.method }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="路径" min-width="200" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono path">{{ row.path }}</span></template>
      </el-table-column>
      <el-table-column label="结果" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.ok ? 'success' : 'danger'">
            {{ row.ok ? "成功" : "失败" }} {{ row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="来源 IP" width="120">
        <template #default="{ row }"><span class="mono">{{ row.ip || "—" }}</span></template>
      </el-table-column>
      <el-table-column label="操作" width="70" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <div class="empty-tip">暂无审计记录：有写操作（含登录尝试、越权尝试）后这里就会积累留痕</div>
      </template>
    </el-table>

    <el-drawer v-model="drawer" title="审计详情" size="480px" direction="rtl">
      <el-descriptions v-if="detail" :column="1" border size="small">
        <el-descriptions-item label="时间">{{ fmtTs(detail.ts) }}</el-descriptions-item>
        <el-descriptions-item label="操作人">
          {{ detail.display_name || "—" }}（{{ detail.username }}） · 角色 {{ detail.role }}
        </el-descriptions-item>
        <el-descriptions-item label="动作">{{ detail.action }}</el-descriptions-item>
        <el-descriptions-item label="对象">{{ detail.target || "—" }}</el-descriptions-item>
        <el-descriptions-item label="请求">
          <span class="mono">{{ detail.method }} {{ detail.path }}</span>
        </el-descriptions-item>
        <el-descriptions-item v-if="detail.query" label="Query">
          <span class="mono">{{ detail.query }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="结果">
          <el-tag size="small" :type="detail.ok ? 'success' : 'danger'">
            {{ detail.ok ? "成功" : "失败" }} · {{ detail.status }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="来源 IP">{{ detail.ip || "—" }}</el-descriptions-item>
        <el-descriptions-item label="请求摘要（已脱敏）">
          <pre class="detail-body">{{ detail.detail || "（无请求体）" }}</pre>
        </el-descriptions-item>
      </el-descriptions>
    </el-drawer>
  </div>
</template>

<style scoped>
.page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.page-title { font-size: 13px; color: #909399; }
.header-ops { display: flex; gap: 8px; }
.filter-card { margin-bottom: 12px; }
.filter-card :deep(.el-form-item) { margin-bottom: 0; }
.audit-table :deep(.el-table__cell) { vertical-align: middle; }
.mono { font-family: Consolas, Monaco, monospace; }
.path { font-size: 12px; color: #606266; }
.sub { font-size: 11px; color: #a8abb2; line-height: 1.4; }
.empty-tip { padding: 20px 0; color: #909399; font-size: 13px; }
.detail-body {
  margin: 0; white-space: pre-wrap; word-break: break-all;
  font-family: Consolas, Monaco, monospace; font-size: 12px; line-height: 1.6; color: #303133;
}
</style>
