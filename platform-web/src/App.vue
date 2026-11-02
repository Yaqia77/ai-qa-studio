<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { api } from "./api";
import { currentUser, logout } from "./stores/auth";
import type { EnvInfo } from "./types";

const route = useRoute();
const router = useRouter();
const title = computed(() => (route.meta.title as string) ?? "interface-auto");
// 菜单选中态归属：/report 等二级页通过 meta.active 指定高亮的菜单项
const activeMenu = computed(() => (route.meta.active as string) ?? route.path);
// 登录页不带平台外壳（侧栏/顶栏）
const barePage = computed(() => route.meta.public === true);

// 顶栏环境指示（全局共享的简化版：当前仅展示，切换在任务表单里做）
const envs = ref<EnvInfo[]>([]);
const currentEnv = ref<string>("");

async function loadEnv() {
  if (!currentUser.value) return;   // 未登录不拉环境数据
  try {
    const resp = await api.listEnvs();
    envs.value = resp.envs;
    currentEnv.value = resp.default_env ?? resp.envs[0]?.name ?? "";
  } catch {
    /* 拦截器已提示 */
  }
}

onMounted(loadEnv);
// 登录页不带平台外壳，App 只挂载一次；登录后需补拉一次环境指示
watch(currentUser, loadEnv);

async function doLogout() {
  await logout();
  ElMessage.success("已退出登录");
  router.replace("/login");
}

function onUserCommand(cmd: string) {
  if (cmd === "logout") doLogout();
}
</script>

<template>
  <!-- 登录等无壳页：不渲染侧栏/顶栏 -->
  <router-view v-if="barePage" />

  <el-container v-else style="height: 100vh">
    <el-aside width="220px" class="side">
      <div class="logo">● Interface Auto 平台</div>
      <el-menu router :default-active="activeMenu" class="side-menu">
        <el-menu-item-group title="概览">
          <el-menu-item index="/dashboard">📊 工作台</el-menu-item>
          <el-menu-item index="/stats">📉 统计报表</el-menu-item>
        </el-menu-item-group>
        <el-menu-item-group title="测试管理">
          <el-menu-item index="/cases">📋 用例管理</el-menu-item>
          <el-menu-item v-perm="'case:write'" index="/agent-gen">🤖 AI 生成</el-menu-item>
          <el-menu-item index="/ui-sessions">🔐 登录态</el-menu-item>
          <el-menu-item v-perm="'api:manage'" index="/defs">📚 接口定义</el-menu-item>
          <el-menu-item index="/plans">📁 测试场景</el-menu-item>
          <el-menu-item v-perm="'script:manage'" index="/scripts">🧩 公共脚本</el-menu-item>
        </el-menu-item-group>
        <el-menu-item-group title="执行与报告">
          <el-menu-item index="/execution">🚀 执行中心</el-menu-item>
          <el-menu-item index="/report">📈 报告详情</el-menu-item>
          <el-menu-item v-perm="'schedule:manage'" index="/schedules">⏰ 定时任务</el-menu-item>
        </el-menu-item-group>
        <el-menu-item-group title="配置中心">
          <el-menu-item v-perm="'env:manage'" index="/envs">🌐 环境配置</el-menu-item>
          <el-menu-item v-perm="'secret:manage'" index="/secrets">🔑 凭证管理</el-menu-item>
          <el-menu-item v-perm="'webhook:manage'" index="/notifications">🔔 通知配置</el-menu-item>
        </el-menu-item-group>
        <el-menu-item-group title="系统管理">
          <el-menu-item v-perm="'user:manage'" index="/users">👥 用户管理</el-menu-item>
          <el-menu-item v-perm="'user:manage'" index="/roles">🛡️ 角色权限</el-menu-item>
          <el-menu-item v-perm="'audit:view'" index="/audit">📜 操作审计</el-menu-item>
        </el-menu-item-group>
      </el-menu>
      <div class="side-foot">对接 FastAPI 后端 · Excel 为唯一用例载体</div>
    </el-aside>
    <el-container>
      <el-header class="topbar">
        <b>{{ title }}</b>
        <div style="margin-left: auto; display: flex; align-items: center; gap: 10px">
          <el-tag v-if="currentEnv" size="small">环境：{{ currentEnv }}</el-tag>
          <span v-if="envs.length > 1" style="color: #909399; font-size: 12px">
            （共 {{ envs.length }} 个环境）
          </span>
          <el-dropdown v-if="currentUser" trigger="click" @command="onUserCommand">
            <span class="user-chip">
              <el-tag size="small" :type="currentUser.role === 'admin' ? 'danger' : 'info'" effect="dark">
                {{ currentUser.role_name }}
              </el-tag>
              <span class="user-name">{{ currentUser.display_name || currentUser.username }}</span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style>
body { margin: 0; font-family: "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif; }
.side { background: #1f2b3e; color: #bfcbd9; display: flex; flex-direction: column; }
.logo { height: 56px; display: flex; align-items: center; padding: 0 14px; font-weight: 600; color: #fff; background: #16202e; font-size: 14px; letter-spacing: 0.5px; }
/* 关键：覆盖 el-menu 自身白底（默认 --el-menu-bg-color:#fff），整侧栏透色融入深色背景 */
.side-menu {
  border-right: none; flex: 1; background: transparent;
  --el-menu-bg-color: transparent;
  --el-menu-text-color: #c3ccd9;
  --el-menu-active-color: #fff;
  --el-menu-hover-bg-color: rgba(255, 255, 255, 0.07);
}
/* 企业后台经典选中态：主题蓝实底 + 白字 */
.side-menu .el-menu-item { height: 46px; line-height: 46px; margin: 4px 8px; border-radius: 6px; transition: background-color 0.15s ease, color 0.15s ease; }
.side-menu .el-menu-item.is-active { color: #fff; background: #409eff; font-weight: 600; }
.side-menu .el-menu-item.is-active:hover { background: #409eff; }
.side-menu .el-menu-item:not(.is-active):hover { color: #fff; }
/* 分组标题：置灰小字，遵循原型 .menu-group */
.side-menu .el-menu-item-group__title {
  padding: 12px 16px 6px; font-size: 12px; color: #7c8aa3; line-height: 1;
}
.side-foot { padding: 12px 14px; font-size: 11px; color: #63718a; border-top: 1px solid #16202e; }
.topbar { display: flex; align-items: center; border-bottom: 1px solid #e4e7ed; background: #fff; }
.user-chip { display: flex; align-items: center; gap: 6px; cursor: pointer; outline: none; }
.user-name { font-size: 13px; color: #303133; }
.main { background: #f5f7fa; }
</style>
