import { createApp } from "vue";
import ElementPlus from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import "element-plus/dist/index.css";
import App from "./App.vue";
import router from "./router";
import { perm } from "./directives/perm";
import { refreshMe, restoreSession } from "./stores/auth";

// 登录态需在首次渲染前恢复，v-perm 才能在挂载时就判定显隐
restoreSession();

createApp(App)
  .use(router)
  .use(ElementPlus, { locale: zhCn })
  .directive("perm", perm)
  .mount("#app");

// 后台校验 token 并拉最新权限集（角色被改动后下次跳页即生效）；失效时拦截器会踢回登录页
void refreshMe();
