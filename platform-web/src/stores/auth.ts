/**
 * 登录态与权限（模块级共享 store，与设计文档 RBAC 对齐）。
 *
 * 后端是安全边界（每个写接口二次校验），前端只据 permissions 做按钮/菜单显隐。
 * token 与用户资料落在 localStorage，刷新页面免重新登录；失效时由 axios 拦截器统一踢回登录页。
 */
import { computed, reactive } from "vue";
import { api } from "../api";
import type { UserInfo } from "../types";
import { clearToken, getToken, setToken } from "../utils/session";

const USER_KEY = "ia_user";

const state = reactive<{ user: UserInfo | null }>({ user: null });

function persistUser(user: UserInfo | null) {
  try {
    if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
    else localStorage.removeItem(USER_KEY);
  } catch {
    /* 忽略 */
  }
}

function readUser(): UserInfo | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as UserInfo) : null;
  } catch {
    return null;
  }
}

export const currentUser = computed(() => state.user);
export const isLoggedIn = computed(() => !!state.user && !!getToken());

/** 是否持有权限码；admin 兜底全通过（后端仍会二次校验）。 */
export function hasPerm(code: string): boolean {
  const u = state.user;
  if (!u) return false;
  if (u.role === "admin") return true;
  return u.permissions.includes(code);
}

/** 应用启动时恢复登录态（同步，不阻塞渲染）。 */
export function restoreSession(): void {
  if (state.user || !getToken()) return;
  state.user = readUser();
}

export async function login(username: string, password: string): Promise<UserInfo> {
  const resp = await api.login({ username, password });
  setToken(resp.token);
  state.user = resp.user;
  persistUser(resp.user);
  return resp.user;
}

export async function logout(): Promise<void> {
  try {
    await api.logout();
  } catch {
    /* 后端吊销失败也要清本地态 */
  }
  clearSession();
}

export function clearSession(): void {
  clearToken();
  state.user = null;
  persistUser(null);
}

/** 拉取最新用户态（校验 token 是否仍有效；无效即清态）。 */
export async function refreshMe(): Promise<UserInfo | null> {
  if (!getToken()) {
    clearSession();
    return null;
  }
  try {
    const me = await api.getMe();
    state.user = me;
    persistUser(me);
    return me;
  } catch {
    clearSession();
    return null;
  }
}
