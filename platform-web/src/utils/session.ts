/**
 * 登录态存取（独立小模块）。
 *
 * 单独抽出是为了打断 api/index.ts 与 stores/auth.ts 的循环依赖：
 * axios 拦截器只需要同步拿到 token，不必引入整个 store。
 */
const TOKEN_KEY = "ia_token";

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* 存储不可用时忽略 */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 忽略 */
  }
}
