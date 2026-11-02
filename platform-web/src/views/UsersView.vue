<script setup lang="ts">
/**
 * 用户管理（仅 admin 可见/可访问）：账号增删改 + 角色分配 + 启停 + 重置密码。
 * 权限码→角色的具体分配只在「角色权限」页维护（后端 data/permissions.json 单一来源），
 * 本页不再复刻一份矩阵，避免两处口径打架。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import { currentUser } from "../stores/auth";
import type { UserInfo } from "../types";

const users = ref<UserInfo[]>([]);
const loading = ref(false);

const ROLE_OPTIONS = [
  { value: "admin", label: "管理员 Admin" },
  { value: "tl", label: "测试负责人 TL" },
  { value: "qa", label: "测试工程师 QA" },
  { value: "dev", label: "开发 Dev" },
  { value: "viewer", label: "只读访客 Viewer" },
  { value: "agent_svc", label: "Agent 服务账号" },
];
const roleName = (r: string) => ROLE_OPTIONS.find((o) => o.value === r)?.label ?? r;

/** 角色定位说明（具体权限项见「角色权限」页，此处不复述矩阵） */
const ROLE_DESC: Record<string, string> = {
  admin: "平台最高权限，唯一可管理用户与权限分配",
  tl: "负责人：删除类与配置类高危操作",
  qa: "主力：用例、场景、执行",
  dev: "开发：查看 + 触发执行",
  viewer: "只读访客：全站仅查看",
  agent_svc: "qa-agent 服务账号：只读 + 用例写入，由 tools/init_agent_svc.py 创建",
};

const roleUserCount = computed(() => {
  const map: Record<string, number> = {};
  for (const u of users.value) map[u.role] = (map[u.role] ?? 0) + 1;
  return map;
});

async function load() {
  loading.value = true;
  try {
    users.value = await api.listUsers();
  } finally {
    loading.value = false;
  }
}
onMounted(load);

/* ---------- 新建/编辑 ---------- */
const dialog = ref(false);
const mode = ref<"create" | "edit">("create");
const saving = ref(false);
const editTarget = ref("");
const form = reactive({ username: "", password: "", display_name: "", role: "qa", enabled: true });

function openCreate() {
  mode.value = "create";
  editTarget.value = "";
  Object.assign(form, { username: "", password: "", display_name: "", role: "qa", enabled: true });
  dialog.value = true;
}

function openEdit(u: UserInfo) {
  mode.value = "edit";
  editTarget.value = u.username;
  Object.assign(form, {
    username: u.username, password: "", display_name: u.display_name,
    role: u.role, enabled: u.enabled,
  });
  dialog.value = true;
}

async function save() {
  if (!form.username.trim()) {
    ElMessage.warning("用户名不能为空");
    return;
  }
  saving.value = true;
  try {
    if (mode.value === "create") {
      if (form.password.length < 6) {
        ElMessage.warning("密码至少 6 位");
        saving.value = false;
        return;
      }
      await api.createUser({
        username: form.username.trim(), password: form.password,
        display_name: form.display_name, role: form.role,
      });
      ElMessage.success("用户已创建");
    } else {
      const body: Record<string, unknown> = {
        display_name: form.display_name, role: form.role, enabled: form.enabled,
      };
      if (form.password) body.password = form.password;
      await api.updateUser(editTarget.value, body);
      ElMessage.success("用户已更新");
    }
    dialog.value = false;
    load();
  } catch { /* 拦截器已提示 */ } finally {
    saving.value = false;
  }
}

async function remove(u: UserInfo) {
  try {
    await ElMessageBox.confirm(
      `确认删除用户「${u.display_name || u.username}」？`, "删除用户",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
    await api.deleteUser(u.username);
    ElMessage.success("已删除");
    load();
  } catch { /* 取消或拦截器已提示 */ }
}

const isSelf = (u: UserInfo) => u.username === currentUser.value?.username;
const dialogTitle = computed(() => (mode.value === "create" ? "新建用户" : `编辑用户：${editTarget.value}`));
</script>

<template>
  <div>
    <div class="page-header">
      <span class="page-title">共 {{ users.length }} 个账号 · 权限分配在「角色权限」页维护</span>
      <el-button type="primary" @click="openCreate">+ 新建用户</el-button>
    </div>

    <el-row :gutter="12">
      <el-col :span="17">
        <el-table v-loading="loading" :data="users" size="small" border>
          <el-table-column label="用户名" min-width="120">
            <template #default="{ row }">
              <span class="mono">{{ row.username }}</span>
              <el-tag v-if="isSelf(row)" size="small" type="success" effect="plain" style="margin-left: 6px">当前</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="display_name" label="姓名" min-width="120" />
          <el-table-column label="角色" width="140">
            <template #default="{ row }">
              <el-tag size="small" :type="row.role === 'admin' ? 'danger' : 'info'">{{ row.role_name }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.enabled ? 'success' : 'warning'">
                {{ row.enabled ? "启用" : "停用" }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="更新时间" width="150">
            <template #default="{ row }">{{ (row.updated_at ?? "").replace("T", " ").slice(0, 16) || "—" }}</template>
          </el-table-column>
          <el-table-column label="操作" width="130" fixed="right">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" text type="danger" :disabled="isSelf(row) || row.username === 'admin'" @click="remove(row)">
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-col>

      <el-col :span="7">
        <el-card shadow="never" class="perm-card">
          <template #header>
            <div class="card-head">
              <b>角色说明</b>
              <el-button text type="primary" size="small" @click="$router.push('/roles')">去分配权限 ›</el-button>
            </div>
          </template>
          <div v-for="r in ROLE_OPTIONS" :key="r.value" class="perm-row">
            <div class="perm-row-top">
              <el-tag size="small" :type="r.value === 'admin' ? 'danger' : 'info'" effect="plain">{{ r.label }}</el-tag>
              <span class="perm-count">{{ roleUserCount[r.value] ?? 0 }} 人</span>
            </div>
            <div class="perm-desc">{{ ROLE_DESC[r.value] }}</div>
          </div>
          <el-alert type="warning" :closable="false" style="margin-top: 10px">
            默认账号密码为初始化播种值，生产部署请立即重置。
          </el-alert>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="dialog" :title="dialogTitle" width="460px">
      <el-form label-width="88px" @submit.prevent>
        <el-form-item label="用户名" required>
          <el-input v-model="form.username" :disabled="mode === 'edit'" placeholder="登录账号，如 zhangsan" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="form.display_name" placeholder="显示名（可选）" maxlength="30" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width: 100%">
            <el-option v-for="r in ROLE_OPTIONS" :key="r.value" :label="r.label" :value="r.value" />
          </el-select>
        </el-form-item>
        <el-form-item :label="mode === 'edit' ? '重置密码' : '密码'" :required="mode === 'create'">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            :placeholder="mode === 'edit' ? '留空表示不修改' : '至少 6 位'"
          />
        </el-form-item>
        <el-form-item v-if="mode === 'edit'" label="启用">
          <el-switch v-model="form.enabled" :disabled="isSelf({ username: form.username } as UserInfo)" />
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
.page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.page-title { font-size: 13px; color: #909399; }
.mono { font-family: Consolas, Monaco, monospace; }
.perm-card { font-size: 12px; }
.card-head { display: flex; align-items: center; justify-content: space-between; }
.perm-row { margin-bottom: 12px; }
.perm-row-top { display: flex; align-items: center; justify-content: space-between; }
.perm-count { color: #909399; }
.perm-desc { margin-top: 4px; color: #606266; line-height: 1.6; }
</style>
