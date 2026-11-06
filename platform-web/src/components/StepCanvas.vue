<script setup lang="ts">
/**
 * 步骤画布：plan 事件的可视化，节点 = 一条接口用例步骤。
 *
 * 语义对齐平台既有模型，不发明新概念：
 * - 输入参数 = 用例里 {{var}} 引用（变量池），输出映射 = extract 键；
 * - 点击节点 → 抽屉编辑 → 提交走 patch 回对话（人改也过闸，口径唯一），
 *   本组件**只收集改动并 emit**，落库/校验全在父页面与后端。
 */
import { reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import type { ChatPlanStep } from "../types";

const props = defineProps<{
  steps: ChatPlanStep[];
  /** case_id → 草稿用例（case_ready 事件/会话详情灌入），抽屉表单的编辑基线 */
  drafts: Record<string, Record<string, unknown>>;
  /** 流式中禁用提交，避免改一半又被下一轮 plan 覆盖 */
  busy?: boolean;
}>();

const emit = defineEmits<{
  /** 抽屉提交：父页面组装成一轮 patch 对话发回 Agent */
  (e: "patch", caseId: string, body: Record<string, unknown>, label: string): void;
}>();

/* ---------------- 状态色 ---------------- */
const STATUS_META: Record<string, { label: string; color: string; tag: "info" | "warning" | "success" }> = {
  pending: { label: "待生成", color: "#909399", tag: "info" },
  running: { label: "进行中", color: "#e6a23c", tag: "warning" },
  draft: { label: "草稿", color: "#409eff", tag: "info" },
  failed: { label: "未过闸", color: "#f56c6c", tag: "warning" },
  registered: { label: "已落库", color: "#67c23a", tag: "success" },
};

function meta(status: string) {
  return STATUS_META[status] ?? STATUS_META.draft;
}
/* ---------------- 抽屉编辑 ---------------- */
const drawer = ref(false);
const editing = ref<ChatPlanStep | null>(null);
/** 文本类字段（编辑态统一字符串，提交时按字段类型转回） */
const form = reactive({
  case_desc: "",
  priority: "P0",
  run: "Y",
  method: "POST",
  url: "",
  headers: "{}",
  params: "{}",
  body: "{}",
  extract: "{}",
  asserts: "[]",
});

/** JSON 文本字段：失焦即校验，坏 JSON 红字提示，杜绝把非法串提交回 Agent */
const jsonErr = reactive<Record<string, string>>({});
const JSON_FIELDS = ["headers", "params", "body", "extract", "asserts"] as const;

function checkJson(key: string) {
  const raw = (form as Record<string, string>)[key];
  if (!raw.trim()) { jsonErr[key] = ""; return; }
  try {
    JSON.parse(raw);
    jsonErr[key] = "";
  } catch (e) {
    jsonErr[key] = `不是合法 JSON：${(e as Error).message}`;
  }
}

watch(drawer, (v) => { if (!v) JSON_FIELDS.forEach((k) => (jsonErr[k] = "")); });

function openDrawer(step: ChatPlanStep) {
  const draft = props.drafts[step.case_id];
  if (!draft) {
    ElMessage.info("该节点还没有草稿可编辑（生成完成后才能改）");
    return;
  }
  // UI 节点的步骤链不适合当前平铺型表单：一期先保证展示不抛错，编辑退回提示
  if (step.kind === "ui_case") {
    ElMessage.info("UI 草稿请展开卡片查看完整 JSON；结构化编辑步骤将在后续版本开放");
    return;
  }
  editing.value = step;
  form.case_desc = String(draft.case_desc ?? step.name ?? "");
  form.priority = String(draft.priority ?? "P0");
  form.run = draft.run === false || draft.run === "N" ? "N" : "Y";
  form.method = String(draft.method ?? "POST");
  form.url = String(draft.url ?? "");
  for (const k of JSON_FIELDS) {
    const v = draft[k];
    form[k] = v == null ? (k === "asserts" ? "[]" : "{}") : JSON.stringify(v, null, 2);
    jsonErr[k] = "";
  }
  drawer.value = true;
}

function submit() {
  JSON_FIELDS.forEach(checkJson);
  if (Object.values(jsonErr).some(Boolean)) {
    ElMessage.warning("存在非法 JSON 字段，先修正再提交");
    return;
  }
  if (!editing.value) return;
  // 只回传改动字段：与草稿基线一致的部分不动，diff 才干净（后端合并基线后重跑闸门）
  const draft = props.drafts[editing.value.case_id] ?? {};
  const body: Record<string, unknown> = {};
  const put = (key: string, val: unknown) => {
    const base = draft[key];
    const norm = (v: unknown) => (v == null ? "" : typeof v === "string" ? v : JSON.stringify(v));
    if (norm(val) !== norm(base)) body[key] = val;
  };
  put("case_desc", form.case_desc);
  put("priority", form.priority);
  put("run", form.run === "Y");
  put("method", form.method);
  put("url", form.url);
  for (const k of JSON_FIELDS) {
    const raw = form[k].trim();
    if (!raw) continue;
    try {
      const parsed = JSON.parse(raw);
      const base = draft[k] ?? (k === "asserts" ? [] : {});
      // 序列化后比较：键序不同视为改动（宁可多报不可漏报，后端合并重过闸门自会收敛）
      if (JSON.stringify(parsed) !== JSON.stringify(base)) body[k] = parsed;
    } catch { /* checkJson 已拦，不会走到 */ }
  }
  if (!Object.keys(body).length) {
    ElMessage.info("没有检测到改动");
    return;
  }
  emit("patch", editing.value.case_id, body, `手工修改步骤${editing.value.seq}（${form.case_desc || editing.value.name}）`);
  drawer.value = false;
}
</script>

<template>
  <div class="canvas">
    <div class="canvas-head">
      <b>步骤画布</b>
      <span class="hint">{{ steps.length ? `${steps.length} 个节点 · 点击节点可编辑` : "对话产出后实时生成，节点=用例步骤" }}</span>
    </div>

    <el-empty v-if="!steps.length" description="暂无步骤：对话产出后实时生成，节点=用例步骤" :image-size="60"
              style="margin-top: 40px">
      <template #image><span style="font-size: 40px">🗺️</span></template>
    </el-empty>

    <div v-else class="nodes">
      <div v-for="(s, i) in steps" :key="s.case_id || s.seq" class="node-row">
        <div class="node" :class="{ 'node--registered': s.status === 'registered', 'node--ui': s.kind === 'ui_case' }"
             :style="{ borderLeftColor: meta(s.status).color }" @click="openDrawer(s)">
          <div class="node-top">
            <span class="seq">{{ s.seq }}</span>
            <span class="node-name">{{ s.name || s.case_id }}</span>
            <el-tag v-if="s.kind === 'ui_case'" size="small" effect="plain" type="warning">UI</el-tag>
            <el-tag :type="meta(s.status).tag" size="small" effect="light">{{ meta(s.status).label }}</el-tag>
          </div>
          <div v-if="s.kind === 'ui_case'" class="node-api">
            <el-tag size="small" effect="plain">步骤 {{ s.step_count ?? 0 }}</el-tag>
            <code>{{ s.case_id }}</code>
          </div>
          <div v-else class="node-api">
            <el-tag size="small" effect="plain" type="info">{{ s.method }}</el-tag>
            <code>{{ s.url }}</code>
          </div>
          <div class="node-io">
            <span class="io io--in">输入 {{ s.inputs.length ? s.inputs.join(", ") : "—" }}</span>
            <span class="io io--out">输出 {{ s.outputs.length ? s.outputs.join(", ") : "—" }}</span>
          </div>
        </div>
        <div v-if="i < steps.length - 1" class="arrow">↓</div>
      </div>
    </div>

    <!-- 节点编辑抽屉：改完不直接写 JSON，而是回一轮 patch 对话重过闸门 -->
    <el-drawer v-model="drawer" :title="`编辑步骤 ${editing?.seq ?? ''}`" size="440px">
      <el-form label-position="top" :disabled="busy">
        <el-form-item label="用例描述">
          <el-input v-model="form.case_desc" maxlength="200" />
        </el-form-item>
        <el-row :gutter="10">
          <el-col :span="8"><el-form-item label="优先级">
            <el-select v-model="form.priority" style="width: 100%">
              <el-option v-for="p in ['P0', 'P1', 'P2', 'P3']" :key="p" :label="p" :value="p" />
            </el-select>
          </el-form-item></el-col>
          <el-col :span="8"><el-form-item label="执行">
            <el-select v-model="form.run" style="width: 100%">
              <el-option label="Y" value="Y" /><el-option label="N" value="N" />
            </el-select>
          </el-form-item></el-col>
          <el-col :span="8"><el-form-item label="方法">
            <el-select v-model="form.method" style="width: 100%">
              <el-option v-for="m in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH']" :key="m" :label="m" :value="m" />
            </el-select>
          </el-form-item></el-col>
        </el-row>
        <el-form-item label="URL"><el-input v-model="form.url" /></el-form-item>
        <el-form-item v-for="k in JSON_FIELDS" :key="k" :label="k">
          <el-input v-model="form[k]" type="textarea" :rows="k === 'asserts' ? 4 : 3"
                    :class="{ 'field-bad': jsonErr[k] }" @blur="checkJson(k)" />
          <div v-if="jsonErr[k]" style="color: #f56c6c; font-size: 12px">{{ jsonErr[k] }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="drawer = false">取消</el-button>
        <el-button type="primary" :disabled="busy" @click="submit">
          {{ busy ? "对话进行中…" : "提交修改（重过校验闸门）" }}
        </el-button>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.canvas { display: flex; flex-direction: column; height: 100%; overflow: hidden; }
.canvas-head { padding: 10px 14px; border-bottom: 1px solid #e4e7ed; display: flex; align-items: baseline; gap: 10px; }
.canvas-head .hint { color: #909399; font-size: 12px; }
.nodes { flex: 1; overflow: auto; padding: 14px; }
.node {
  border: 1px solid #e4e7ed; border-left: 4px solid #409eff; border-radius: 8px;
  padding: 10px 12px; cursor: pointer; background: #fff; transition: box-shadow 0.15s;
}
.node:hover { box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08); }
.node--registered { background: #f6fbf3; }
.node--ui { border-left-color: #e6a23c; }
.node-top { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.seq {
  width: 20px; height: 20px; border-radius: 50%; background: #409eff; color: #fff;
  font-size: 12px; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.node-name { font-weight: 600; font-size: 13px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node-api { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.node-api code { font-size: 12px; color: #606266; }
.node-io { display: flex; gap: 12px; font-size: 12px; }
.io--in { color: #b88230; }
.io--out { color: #3a8f5f; }
.arrow { text-align: center; color: #c0c4cc; line-height: 18px; font-size: 14px; }
.field-bad :deep(.el-textarea__inner) { border-color: #f56c6c; }
</style>
