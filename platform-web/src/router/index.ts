import { createRouter, createWebHistory } from "vue-router";
import { hasPerm } from "../stores/auth";
import { getToken } from "../utils/session";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/dashboard" },
    {
      path: "/login",
      name: "login",
      component: () => import("../views/LoginView.vue"),
      meta: { title: "登录", public: true },
    },
    {
      path: "/dashboard",
      name: "dashboard",
      component: () => import("../views/DashboardView.vue"),
      meta: { title: "工作台" },
    },
    {
      path: "/stats",
      name: "stats",
      component: () => import("../views/StatsView.vue"),
      meta: { title: "统计报表" },
    },
    {
      path: "/cases",
      name: "cases",
      component: () => import("../views/CasesView.vue"),
      meta: { title: "用例管理" },
    },
    {
      // AI 生成：菜单级 case:write 拦截；预览/落库接口后端还有 require_perm 二次校验
      path: "/agent-gen",
      name: "agent-gen",
      component: () => import("../views/AgentGenView.vue"),
      meta: { title: "AI 生成", perm: "case:write" },
    },
    {
      // 登录态页：查看仅需登录态，写操作（新建/编辑/删除/采集）靠页内 v-perm='session:manage' 门控
      path: "/ui-sessions",
      name: "ui-sessions",
      component: () => import("../views/UiSessionsView.vue"),
      meta: { title: "登录态" },
    },
    {
      path: "/defs",
      name: "defs",
      component: () => import("../views/ApiDefsView.vue"),
      meta: { title: "接口定义", perm: "api:manage" },
    },
    {
      path: "/plans",
      name: "plans",
      component: () => import("../views/PlansView.vue"),
      meta: { title: "测试场景" },
    },
    {
      path: "/scripts",
      name: "scripts",
      component: () => import("../views/ScriptsView.vue"),
      meta: { title: "公共脚本", perm: "script:manage" },
    },
    {
      path: "/execution",
      name: "execution",
      component: () => import("../views/ExecutionView.vue"),
      meta: { title: "执行中心" },
    },
    {
      path: "/envs",
      name: "envs",
      component: () => import("../views/EnvView.vue"),
      meta: { title: "环境配置", perm: "env:manage" },
    },
    {
      path: "/secrets",
      name: "secrets",
      component: () => import("../views/SecretsView.vue"),
      meta: { title: "凭证管理", perm: "secret:manage" },
    },
    {
      path: "/notifications",
      name: "notifications",
      component: () => import("../views/NotificationsView.vue"),
      meta: { title: "通知配置", perm: "webhook:manage" },
    },
    {
      path: "/schedules",
      name: "schedules",
      component: () => import("../views/SchedulesView.vue"),
      meta: { title: "定时任务", perm: "schedule:manage" },
    },
    {
      path: "/users",
      name: "users",
      component: () => import("../views/UsersView.vue"),
      meta: { title: "用户管理", perm: "user:manage" },
    },
    {
      path: "/roles",
      name: "roles",
      component: () => import("../views/RolesView.vue"),
      meta: { title: "角色权限", perm: "user:manage" },
    },
    {
      path: "/audit",
      name: "audit",
      component: () => import("../views/AuditView.vue"),
      meta: { title: "操作审计", perm: "audit:view" },
    },
    {
      // run_id 可能含 "/"（cli 历史批次 reports/run_xxx），用 query 传参规避动态路由歧义；
      // 无 runId 时报告页落「批次列表」选择页，作为侧栏独立入口。
      path: "/report",
      name: "report",
      component: () => import("../views/ReportView.vue"),
      meta: { title: "报告详情" },
    },
  ],
});

/**
 * 菜单级路由守卫（设计文档 RBAC 第一道粒度）：
 * 未登录 → 登录页；登录后访问登录页 → 工作台；meta.perm 不满足 → 回工作台。
 * 后端接口仍有 require_perm 二次校验，此处仅为体验层拦截。
 */
router.beforeEach((to) => {
  const authed = !!getToken();
  if (to.meta.public) {
    return authed && to.path === "/login" ? "/dashboard" : true;
  }
  if (!authed) {
    return { path: "/login", query: to.fullPath !== "/dashboard" ? { redirect: to.fullPath } : {} };
  }
  const permCode = to.meta.perm as string | undefined;
  if (permCode && !hasPerm(permCode)) {
    return "/dashboard";
  }
  return true;
});

export default router;
