<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import {
  Odometer, TrendCharts, Tickets, MagicStick, Avatar, Connection, Collection,
  SetUp, VideoPlay, DataAnalysis, Timer, Setting, Key, Lock, Bell, User, UserFilled,
} from "@element-plus/icons-vue";
import { api } from "./api";
import { currentUser, logout } from "./stores/auth";
import type { EnvInfo } from "./types";

const route = useRoute();
const router = useRouter();
const title = computed(() => (route.meta.title as string) ?? "工作台");
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
    <el-aside width="232px" class="side">
      <div class="logo">
        <el-icon class="logo-mark"><MagicStick /></el-icon>
        <span class="logo-text">自动化测试平台</span>
      </div>
      <el-menu router :default-active="activeMenu" class="side-menu">
        <el-menu-item-group title="概览">
          <el-menu-item index="/dashboard"><el-icon><Odometer /></el-icon><span>工作台</span></el-menu-item>
          <el-menu-item index="/stats"><el-icon><TrendCharts /></el-icon><span>统计报表</span></el-menu-item>
        </el-menu-item-group>
        <el-menu-item-group title="测试管理">
          <el-menu-item index="/cases"><el-icon><Tickets /></el-icon><span>用例管理</span></el-menu-item>
          <el-menu-item v-perm="'case:write'" index="/agent-chat"><el-icon><MagicStick /></el-icon><span>AI 助手</span></el-menu-item>
          <el-menu-item index="/ui-sessions"><el-icon><Avatar /></el-icon><span>登录态</span></el-menu-item>
          <el-menu-item v-perm="'api:manage'" index="/defs"><el-icon><Connection /></el-icon><span>接口定义</span></el-menu-item>
          <el-menu-item index="/plans"><el-icon><Collection /></el-icon><span>测试场景</span></el-menu-item>
          <el-menu-item v-perm="'script:manage'" index="/scripts"><el-icon><SetUp /></el-icon><span>公共脚本</span></el-menu-item>
        </el-menu-item-group>
        <el-menu-item-group title="执行与报告">
          <el-menu-item index="/execution"><el-icon><VideoPlay /></el-icon><span>执行中心</span></el-menu-item>
          <el-menu-item index="/report"><el-icon><DataAnalysis /></el-icon><span>报告详情</span></el-menu-item>
          <el-menu-item v-perm="'schedule:manage'" index="/schedules"><el-icon><Timer /></el-icon><span>定时任务</span></el-menu-item>
        </el-menu-item-group>
        <el-menu-item-group title="配置中心">
          <el-menu-item v-perm="'env:manage'" index="/envs"><el-icon><Setting /></el-icon><span>环境配置</span></el-menu-item>
          <el-menu-item v-perm="'secret:manage'" index="/secrets"><el-icon><Key /></el-icon><span>凭证管理</span></el-menu-item>
          <el-menu-item v-perm="'webhook:manage'" index="/notifications"><el-icon><Bell /></el-icon><span>通知配置</span></el-menu-item>
        </el-menu-item-group>
        <el-menu-item-group title="系统管理">
          <el-menu-item v-perm="'user:manage'" index="/users"><el-icon><User /></el-icon><span>用户管理</span></el-menu-item>
          <el-menu-item v-perm="'user:manage'" index="/roles"><el-icon><UserFilled /></el-icon><span>角色权限</span></el-menu-item>
          <el-menu-item v-perm="'audit:view'" index="/audit"><el-icon><Lock /></el-icon><span>操作审计</span></el-menu-item>
        </el-menu-item-group>
      </el-menu>
      <div class="side-foot">Excel 驱动 · 接口 / UI / App 统一自动化</div>
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
:root {
  --side-bg: #1b2431;
  --side-bg-deep: #141c27;
  --brand: #3b7ddd;
}
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif; -webkit-font-smoothing: antialiased; }
.side { background: linear-gradient(180deg, var(--side-bg) 0%, var(--side-bg-deep) 100%); color: #c4ccd8; display: flex; flex-direction: column; }
.logo { height: 60px; display: flex; align-items: center; gap: 10px; padding: 0 20px; color: #fff; border-bottom: 1px solid rgba(255, 255, 255, 0.06); }
.logo-mark { font-size: 22px; color: var(--brand); }
.logo-text { font-weight: 700; font-size: 16px; letter-spacing: 0.3px; }
/* 关键：覆盖 el-menu 自身白底（默认 --el-menu-bg-color:#fff），整侧栏透色融入深色背景 */
.side-menu {
  border-right: none; flex: 1; overflow-y: auto; background: transparent; padding-top: 4px;
  --el-menu-bg-color: transparent;
  --el-menu-text-color: #c4ccd8;
  --el-menu-active-color: #fff;
  --el-menu-hover-bg-color: rgba(255, 255, 255, 0.06);
}
.side-menu .el-menu-item { height: 44px; line-height: 44px; margin: 2px 10px; border-radius: 8px; transition: background-color 0.15s ease, color 0.15s ease; }
.side-menu .el-menu-item .el-icon { font-size: 17px; margin-right: 10px; }
/* 主流选中态：品牌蓝渐变实底 + 白字，左侧高亮条 */
.side-menu .el-menu-item.is-active { color: #fff; background: linear-gradient(90deg, var(--brand), #4d8fe6); font-weight: 600; box-shadow: 0 4px 12px -4px rgba(59, 125, 221, 0.6); }
.side-menu .el-menu-item.is-active:hover { background: linear-gradient(90deg, var(--brand), #4d8fe6); }
.side-menu .el-menu-item:not(.is-active):hover { color: #fff; }
/* 分组标题：置灰小字 */
.side-menu .el-menu-item-group__title {
  padding: 14px 20px 6px; font-size: 12px; color: #7484a0; line-height: 1; letter-spacing: 0.5px;
}
.side-foot { padding: 14px 20px; font-size: 11px; color: #61708a; border-top: 1px solid rgba(255, 255, 255, 0.06); }
.topbar { display: flex; align-items: center; border-bottom: 1px solid #eef0f3; background: #fff; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02); }
.user-chip { display: flex; align-items: center; gap: 6px; cursor: pointer; outline: none; }
.user-name { font-size: 13px; color: #303133; }
.main { background: #f4f6f9; }
</style>
