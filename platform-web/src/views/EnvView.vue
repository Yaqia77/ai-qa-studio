<script setup lang="ts">
/** 环境配置：config.yaml 的可视化编辑视图。保存整体写回 env 段并即时生效（load_config 无缓存）。
 *  注意：写回只重写 default_env 行与 env: 区块，其余章节及注释原样保留；env 区块内的行内注释会丢失。 */
import { onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import type { EnvsResp } from "../types";

const loading = ref(true);
const resp = ref<EnvsResp | null>(null);

async function reload() {
  resp.value = await api.listEnvs();
}

onMounted(async () => {
  try {
    await reload();
  } finally {
    loading.value = false;
  }
});

/* ---------- 编辑抽屉 ---------- */
interface HeaderRow {
  key: string;
  value: string;
}
interface EnvForm {
  name: string;
  base_url: string;
  timeout: string; // 文本承载，允许留空；保存时转 number|null
  verify_ssl: boolean;
  headers: HeaderRow[];
}

const drawer = ref(false);
const saving = ref(false);
const form = reactive<{ default_env: string; envs: EnvForm[] }>({
  default_env: "",
  envs: [],
});

function toRows(h?: Record<string, string>): HeaderRow[] {
  return Object.entries(h ?? {}).map(([key, value]) => ({ key, value }));
}

function openEdit() {
  if (!resp.value) return;
  form.default_env = resp.value.default_env ?? "";
  form.envs = (resp.value.envs ?? []).map((e) => ({
    name: e.name,
    base_url: e.base_url ?? "",
    timeout: e.timeout == null ? "" : String(e.timeout),
    verify_ssl: e.verify_ssl ?? true,
    headers: toRows(e.default_headers),
  }));
  if (!form.envs.length) addEnv();
  drawer.value = true;
}

function addEnv() {
  const name = `env_${form.envs.length + 1}`;
  form.envs.push({ name, base_url: "https://", timeout: "15", verify_ssl: true, headers: [] });
  if (!form.default_env) form.default_env = name;
}

function removeEnv(idx: number) {
  const removed = form.envs[idx];
  form.envs.splice(idx, 1);
  if (form.default_env === removed?.name) {
    form.default_env = form.envs[0]?.name ?? "";
  }
}

/** 收集请求头行为对象；键为空视为忽略，重复键报错 */
function collectHeaders(rows: HeaderRow[], envName: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rows) {
    const k = r.key.trim();
    if (!k) continue;
    if (k in out) throw new Error(`环境 ${envName} 的请求头键重复：${k}`);
    out[k] = r.value;
  }
  return out;
}

async function save() {
  // 前端快校验（后端为准，这里只做友好拦截）
  if (!form.envs.length) return ElMessage.warning("至少需要一个环境");
  const names = new Set<string>();
  for (const e of form.envs) {
    const nm = e.name.trim();
    if (!nm) return ElMessage.warning("环境名不能为空");
    if (/[\r\n]/.test(nm)) return ElMessage.warning(`环境名非法：${nm}`);
    if (names.has(nm)) return ElMessage.warning(`环境名重复：${nm}`);
    names.add(nm);
    if (!/^https?:\/\/\S+$/.test(e.base_url.trim()))
      return ElMessage.warning(`环境 ${nm} 的 base_url 必须以 http(s):// 开头`);
    if (e.timeout !== "") {
      const t = Number(e.timeout);
      if (!Number.isFinite(t) || t <= 0) return ElMessage.warning(`环境 ${nm} 的超时必须大于 0`);
    }
  }
  if (!names.has(form.default_env)) return ElMessage.warning("默认环境必须是环境之一");

  let envs;
  try {
    envs = form.envs.map((e) => ({
      name: e.name.trim(),
      base_url: e.base_url.trim(),
      timeout: e.timeout.trim() === "" ? null : Number(e.timeout),
      verify_ssl: e.verify_ssl,
      default_headers: collectHeaders(e.headers, e.name.trim()),
    }));
  } catch (err) {
    return ElMessage.warning((err as Error).message);
  }

  saving.value = true;
  try {
    resp.value = await api.updateEnvs({ default_env: form.default_env, envs });
    ElMessage.success("已保存到 config/config.yaml，下一次执行即生效");
    drawer.value = false;
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div v-loading="loading">
    <div style="margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center">
      <span class="hint">环境与会话配置来自 config/config.yaml；编辑保存后整体写回 env 段，下一次执行/拉取即生效（无需重启）。</span>
      <el-button type="primary" @click="openEdit">✏️ 编辑配置</el-button>
    </div>

    <el-row :gutter="12">
      <el-col :span="8" v-for="e in resp?.envs ?? []" :key="e.name">
        <el-card shadow="never" style="margin-bottom: 12px">
          <template #header>
            <b>{{ e.name }}</b>
            <el-tag v-if="e.name === resp?.default_env" type="success" size="small" style="margin-left: 8px">默认</el-tag>
          </template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="base_url">
              <span class="mono">{{ e.base_url ?? "—" }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="超时">
              {{ e.timeout ?? "框架默认" }} 秒
            </el-descriptions-item>
            <el-descriptions-item label="SSL 校验">
              <el-tag :type="e.verify_ssl ? 'success' : 'warning'" size="small">
                {{ e.verify_ssl ? "开启" : "关闭（自签/ TLS 拦截）" }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="默认请求头">
              <div v-if="!Object.keys(e.default_headers ?? {}).length" style="color: #909399">（无）</div>
              <div v-for="(v, k) in e.default_headers" :key="k" class="mono hdr-line">
                {{ k }}: <span style="color: #606266">{{ v }}</span>
              </div>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>

      <el-col :span="8">
        <el-card shadow="never" header="全局配置" style="margin-bottom: 12px">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="报告产物">
              <el-tag v-if="resp?.report?.html" size="small" style="margin-right: 6px">pytest-html</el-tag>
              <el-tag v-if="resp?.report?.allure" size="small">allure-results</el-tag>
              <span v-if="!resp?.report?.html && !resp?.report?.allure" style="color: #909399">（均未开启）</span>
            </el-descriptions-item>
            <el-descriptions-item label="日志级别">
              <span class="mono">{{ resp?.log?.level ?? "INFO" }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="凭证变量文件">
              <span class="mono">{{ resp?.secret_vars_file ?? "—" }}</span>
              <div class="hint">文件内容不经过接口回显，仅展示路径</div>
            </el-descriptions-item>
          </el-descriptions>
          <div class="hint" style="margin-top: 10px">
            平台仅编辑环境段；report/log/凭证文件路径等全局项仍需在 config.yaml 手动维护。用例内 headers 与默认头合并时用例优先。
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-drawer v-model="drawer" title="编辑环境配置" size="640px">
      <el-form label-width="90px" label-position="left">
        <el-form-item label="默认环境">
          <el-select v-model="form.default_env" placeholder="选择默认环境" style="width: 220px">
            <el-option v-for="e in form.envs" :key="e.name" :label="e.name" :value="e.name" />
          </el-select>
        </el-form-item>
      </el-form>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
        title="保存将整体重写 config.yaml 的 env 段；其余配置与注释保留，但环境内的行内注释会丢失。"
      />

      <el-card
        v-for="(e, idx) in form.envs"
        :key="idx"
        shadow="never"
        style="margin-bottom: 12px"
        class="env-edit"
      >
        <template #header>
          <span>环境 #{{ idx + 1 }}</span>
          <el-popconfirm
            v-if="form.envs.length > 1"
            title="删除该环境？"
            confirm-button-text="删除"
            cancel-button-text="取消"
            @confirm="removeEnv(idx)"
          >
            <template #reference>
              <el-button link type="danger" style="float: right">删除</el-button>
            </template>
          </el-popconfirm>
        </template>
        <el-form label-width="90px" label-position="left">
          <el-form-item label="环境名">
            <el-input v-model="e.name" placeholder="唯一标识，如 test" />
          </el-form-item>
          <el-form-item label="base_url">
            <el-input v-model="e.base_url" placeholder="https://example.com/api" />
          </el-form-item>
          <el-form-item label="超时(秒)">
            <el-input v-model="e.timeout" placeholder="留空用框架默认" style="width: 160px" />
          </el-form-item>
          <el-form-item label="SSL 校验">
            <el-switch v-model="e.verify_ssl" active-text="开启" inactive-text="关闭" />
          </el-form-item>
          <el-form-item label="默认请求头">
            <div style="width: 100%">
              <div v-for="(h, hi) in e.headers" :key="hi" style="display: flex; gap: 6px; margin-bottom: 6px">
                <el-input v-model="h.key" placeholder="Header 名" style="width: 180px" />
                <el-input v-model="h.value" placeholder="值（可用 {{VAR}} 引用变量）" style="flex: 1" />
                <el-button link type="danger" @click="e.headers.splice(hi, 1)">删</el-button>
              </div>
              <el-button link type="primary" @click="e.headers.push({ key: '', value: '' })">+ 添加请求头</el-button>
            </div>
          </el-form-item>
        </el-form>
      </el-card>

      <el-button @click="addEnv" style="width: 100%; margin-bottom: 8px">+ 新增环境</el-button>

      <template #footer>
        <el-button @click="drawer = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存到配置文件</el-button>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.hdr-line { margin-bottom: 2px; word-break: break-all; }
.hint { color: #909399; font-size: 12px; }
.env-edit :deep(.el-card__header) { padding: 8px 12px; font-weight: 600; }
</style>
