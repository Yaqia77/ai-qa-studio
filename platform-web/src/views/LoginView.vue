<script setup lang="ts">
/**
 * 登录页：用户名 + 密码，成功后按 redirect 回跳。
 * 默认账号由后端首次启动播种（admin/tl/qa/dev/viewer），生产部署需改密。
 * 失败体验：卡内红色告警（含后端 detail）+ 输入框抖动 + 全局 toast（拦截器补发），
 * 密码错误不再是「点了没反应」。
 */
import { reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Document, Lock, MagicStick, TrendCharts, User, VideoPlay } from "@element-plus/icons-vue";
import { login } from "../stores/auth";

const route = useRoute();
const router = useRouter();
const submitting = ref(false);
// 卡内错误告警 + 抖动开关（shake 靠 class 增删触发动画）
const errorMsg = ref("");
const shake = ref(false);

const form = reactive({ username: "", password: "" });
const formRef = ref();
const rules = {
  username: [{ required: true, message: "请输入用户名", trigger: "blur" }],
  password: [{ required: true, message: "请输入密码", trigger: "blur" }],
};

async function submit() {
  const ok = await formRef.value?.validate().catch(() => false);
  if (!ok) return;
  submitting.value = true;
  errorMsg.value = "";
  try {
    const user = await login(form.username.trim(), form.password);
    ElMessage.success(`欢迎回来，${user.display_name || user.username}`);
    const redirect = (route.query.redirect as string) || "/dashboard";
    router.replace(redirect);
  } catch (e: any) {
    // 拦截器对登录页 401 已补 toast；这里再落一条卡内告警，保证错误始终可见
    const detail = e?.response?.data?.detail;
    errorMsg.value =
      typeof detail === "string" ? detail : "登录失败，请检查用户名和密码";
    shake.value = true;
    setTimeout(() => (shake.value = false), 500);
    form.password = ""; // 密码错误后清空，避免带着错密码反复提交
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="login-wrap">
    <!-- 背景装饰：柔光圆斑 + 细网格 -->
    <div class="blob blob-a"></div>
    <div class="blob blob-b"></div>
    <div class="blob blob-c"></div>

    <div class="login-stage">
      <!-- 左侧品牌区（窄屏隐藏） -->
      <div class="brand">
        <div class="brand-logo">● Interface Auto</div>
        <h1 class="brand-title">Excel 驱动的一站式<br />自动化测试平台</h1>
        <p class="brand-desc">接口 · UI · AI 助手｜用例即 Excel · 配置即环境 · 执行即报告</p>
        <ul class="brand-feats">
          <li><el-icon><Document /></el-icon>用例管理 · 智能导入 · Excel 批量维护</li>
          <li><el-icon><VideoPlay /></el-icon>执行中心 · 定时任务 · 依赖编排</li>
          <li><el-icon><TrendCharts /></el-icon>Allure 报告 · 统计报表 · 质量趋势</li>
          <li><el-icon><MagicStick /></el-icon>AI 助手 · 对话式生成用例 · 即问即答</li>
          <li><el-icon><Lock /></el-icon>RBAC 权限 · 操作审计 · 凭证隔离</li>
        </ul>
      </div>

      <!-- 右侧登录卡 -->
      <el-card class="login-card" :class="{ shake }" shadow="always">
        <div class="login-logo">● 欢迎登录</div>
        <div class="login-sub">Interface Auto 一站式自动化测试平台</div>

        <el-alert
          v-if="errorMsg"
          type="error"
          :closable="false"
          show-icon
          class="login-error"
          :title="errorMsg"
        />

        <el-form ref="formRef" :model="form" :rules="rules" size="large" @submit.prevent="submit">
          <el-form-item prop="username">
            <el-input
              v-model="form.username"
              placeholder="用户名"
              :prefix-icon="User"
              clearable
              autofocus
              @input="errorMsg = ''"
              @keyup.enter="submit"
            />
          </el-form-item>
          <el-form-item prop="password">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="密码"
              :prefix-icon="Lock"
              show-password
              @input="errorMsg = ''"
              @keyup.enter="submit"
            />
          </el-form-item>
          <el-button type="primary" class="login-btn" :loading="submitting" @click="submit">
            {{ submitting ? "登录中…" : "登 录" }}
          </el-button>
        </el-form>

        <el-collapse class="login-adv">
          <el-collapse-item name="hint">
            <template #title><span class="adv-title">默认角色账号</span></template>
            <div class="login-tip">
              <div>首次启动自动播种，生产环境请立即改密：</div>
              <div class="mono">admin/admin123 · tl/tl123 · qa/qa123 · dev/dev123 · viewer/viewer123</div>
            </div>
          </el-collapse-item>
        </el-collapse>
      </el-card>
    </div>

    <div class="login-foot">Interface Auto · 接口 · UI · AI 助手</div>
  </div>
</template>

<style scoped>
.login-wrap {
  position: relative;
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  background: linear-gradient(135deg, #16202e 0%, #1f2b3e 40%, #2c405c 75%, #3a5f8a 100%);
}
/* 背景柔光圆斑 */
.blob { position: absolute; border-radius: 50%; filter: blur(90px); opacity: 0.45; }
.blob-a { width: 480px; height: 480px; left: -120px; top: -140px; background: #409eff; }
.blob-b { width: 420px; height: 420px; right: -100px; bottom: -160px; background: #2f9e44; opacity: 0.22; }
.blob-c { width: 300px; height: 300px; right: 22%; top: 8%; background: #f5a623; opacity: 0.14; }

.login-stage {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 72px;
  padding: 0 32px;
}

/* 品牌区 */
.brand { color: #fff; max-width: 430px; }
.brand-logo { font-size: 15px; letter-spacing: 2px; color: #9db6d8; font-weight: 600; }
.brand-title { font-size: 34px; line-height: 1.35; margin: 14px 0 10px; font-weight: 700; }
.brand-desc { font-size: 14px; color: #a8bdd9; margin: 0 0 26px; letter-spacing: 1px; }
.brand-feats { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 12px; }
.brand-feats li {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; color: #cdd9ea; padding: 9px 14px;
  background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.09);
  border-radius: 8px; backdrop-filter: blur(4px);
}
.brand-feats .el-icon { color: #7fb3ff; font-size: 15px; flex-shrink: 0; }

/* 登录卡 */
.login-card { width: 400px; border-radius: 14px; padding: 10px 14px 4px; border: none; flex-shrink: 0; }
.login-card.shake { animation: shake 0.45s ease; }
@keyframes shake {
  0%, 100% { transform: translateX(0); }
  20% { transform: translateX(-9px); }
  40% { transform: translateX(8px); }
  60% { transform: translateX(-5px); }
  80% { transform: translateX(3px); }
}
.login-logo { font-size: 20px; font-weight: 700; color: #1f2733; text-align: center; }
.login-sub { font-size: 12px; color: #909399; text-align: center; margin: 6px 0 20px; }
.login-error { margin-bottom: 16px; }
.login-btn { width: 100%; margin-top: 4px; height: 42px; font-size: 15px; letter-spacing: 6px; }
.login-adv { margin-top: 14px; border-top: none; }
.login-adv :deep(.el-collapse-item__header) { height: 34px; line-height: 34px; border-bottom: none; justify-content: center; }
.login-adv :deep(.el-collapse-item__wrap) { border-bottom: none; }
.adv-title { font-size: 12px; color: #a0a6b0; }
.login-tip { font-size: 12px; color: #909399; line-height: 1.8; }
.mono { font-family: Consolas, Monaco, monospace; word-break: break-all; color: #b3b9c4; }

.login-foot {
  position: absolute; bottom: 16px; left: 0; right: 0; text-align: center;
  font-size: 11px; color: #6b7a92; z-index: 1;
}

/* 窄屏：隐藏品牌区，单卡居中 */
@media (max-width: 900px) {
  .login-stage { gap: 0; }
  .brand { display: none; }
}
</style>
