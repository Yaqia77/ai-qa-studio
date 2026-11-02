<script setup lang="ts">
/**
 * AI 生成（二期对话式）：聊天流 + 左侧步骤画布，替换一期表单页。
 *
 * 链路职责与一期一致、只是形态变了：
 * - 对话走 qa-agent /v1/chat/stream（SSE，X-Agent-Key）；产出草稿在事件里流式回显；
 * - 落库红线在后端（auto 模式也必经平台权威过检），前端只按事件渲染，不做任何"可否落库"判断；
 * - 节点编辑不直接改 JSON：StepCanvas emit patch → 组一轮 {message, patch} 对话回 Agent（人改也过闸）。
 * 会话历史唯一来源是 qa-agent（GET /v1/chat/sessions*），本地不做第二存储。
 */
import { nextTick, onMounted, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import { postChatStream } from "../utils/sse";
import StepCanvas from "../components/StepCanvas.vue";
import JsonViewer from "../components/JsonViewer.vue";
import type { ChatEvent, ChatPlanStep, ChatSessionInfo, GenerationDiff, Project } from "../types";

/* ---------------- 消息模型 ---------------- */
interface ChatMsg {
  id: number;
  role: "user" | "agent";
  kind: "text" | "case" | "ask" | "error";
  text?: string;
  streaming?: boolean;
  // kind=case：case_ready 卡片，registered 事件到达后翻转状态
  caseData?: Record<string, unknown>;
  caseId?: string;
  validation?: { passed?: boolean; problems?: string[] };
  diff?: GenerationDiff["changed"];
  citationIds?: string[];
  registered?: boolean;
  registeredFile?: string;
  // kind=ask
  question?: string;
  options?: string[];
  // kind=error
  detail?: string;
  code?: string;
  traceId?: string;
}

let msgSeq = 0;
const messages = ref<ChatMsg[]>([]);
const steps = ref<ChatPlanStep[]>([]);
const drafts = ref<Record<string, Record<string, unknown>>>({});
const citationsMap = ref<Record<string, { title: string; source_uri: string }>>({});
const busy = ref(false);
const input = ref("");
const streamBox = ref<HTMLElement | null>(null);
const activity = ref("");   // step_start/finish 的轻量进度条，不落消息流

/* ---------------- 顶部：项目 / 落库模式 / Agent 状态 ---------------- */
const projects = ref<Project[]>([]);
const project = ref<Project | null>(null);
const autoRegister = ref<"auto" | "ask" | "off">("auto");
const agentOnline = ref<boolean | null>(null);

async function loadProjects() {
  projects.value = (await api.listProjects()).filter((p) => p.kind === "api");
  project.value = projects.value[0] ?? null;
}

async function checkAgent() {
  try {
    const h = await api.agentHealth();
    agentOnline.value = h.skills?.includes("case_generate") ?? false;
  } catch {
    agentOnline.value = false;   // 拦截器已提示原因，这里只落状态
  }
}

/* ---------------- 会话历史（qa-agent 侧存储） ---------------- */
const sessions = ref<ChatSessionInfo[]>([]);
const sid = ref("");

async function loadSessions() {
  try {
    sessions.value = await api.agentChatSessions();
  } catch { sessions.value = []; }
}

function clearAll() {
  messages.value = [];
  steps.value = [];
  drafts.value = {};
  citationsMap.value = {};
  sid.value = "";
  activity.value = "";
}

function newSession() {
  if (busy.value) { ElMessage.warning("对话进行中，先等待本轮结束"); return; }
  clearAll();
}

async function restoreSession(id: string) {
  if (busy.value) { ElMessage.warning("对话进行中，先等待本轮结束"); return; }
  const d = await api.agentChatSession(id);
  clearAll();
  sid.value = d.id;
  project.value = projects.value.find((p) => p.id === d.project_id) ?? project.value;
  steps.value = d.plan ?? [];
  drafts.value = d.drafts ?? {};
  // 历史轮次只存文本摘要：气泡可回看，卡片明细不重建（画布/草稿已从 plan/drafts 恢复）
  for (const t of d.turns) {
    if (t.role === "user") {
      messages.value.push({ id: ++msgSeq, role: "user", kind: "text", text: t.content || "（结构化修改）" });
    } else {
      messages.value.push({ id: ++msgSeq, role: "agent", kind: "text", text: t.content });
    }
  }
  ElMessage.success(`已恢复会话「${d.title}」（${d.turns.length / 2} 轮）`);
}

async function removeSession() {
  if (!sid.value) { ElMessage.info("当前没有进行中的会话"); return; }
  await ElMessageBox.confirm("删除后该会话历史不可恢复（用例/画布不受影响），确认删除？", "删除会话",
    { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  await api.agentChatDelete(sid.value);
  clearAll();
  await loadSessions();
  ElMessage.success("会话已删除");
}

/* ---------------- 事件分派 → 渲染 ---------------- */
function lastStreamingText(): ChatMsg | null {
  const m = messages.value[messages.value.length - 1];
  return m && m.role === "agent" && m.kind === "text" && m.streaming ? m : null;
}

function pushAgentText(text: string): ChatMsg {
  const m: ChatMsg = { id: ++msgSeq, role: "agent", kind: "text", text, streaming: true };
  messages.value.push(m);
  return m;
}

/** registered 事件：把对应 case_id 的卡片翻成「已落库 ✓」 */
function markRegistered(caseIds: string[], file: string) {
  for (const m of messages.value) {
    if (m.kind === "case" && m.caseId && caseIds.includes(m.caseId)) {
      m.registered = true;
      m.registeredFile = file;
    }
  }
}

function onEvent(ev: ChatEvent) {
  switch (ev.type) {
    case "token": {
      const cur = lastStreamingText();
      if (cur) cur.text = (cur.text ?? "") + ev.payload.text;
      else pushAgentText(ev.payload.text);
      break;
    }
    case "plan":
      steps.value = ev.payload.steps;
      break;
    case "step_start":
      activity.value = `步骤 ${ev.payload.seq}：${ev.payload.summary}`;
      break;
    case "step_finish":
      // 后端节点状态闭集只有 draft/registered；失败靠活动条+error 事件显性化，不伪造状态
      activity.value = ev.payload.ok ? "" : `步骤 ${ev.payload.seq} 失败：${ev.payload.summary}`;
      break;
    case "case_ready": {
      const cur = lastStreamingText();
      if (cur) cur.streaming = false;   // 卡片前的思考文本收尾，后续 token 另起气泡
      const c = ev.payload.case;
      const caseId = String(c.case_id ?? "");
      if (caseId) drafts.value = { ...drafts.value, [caseId]: c };
      messages.value.push({
        id: ++msgSeq, role: "agent", kind: "case",
        caseData: c, caseId,
        validation: ev.payload.validation,
        diff: ev.payload.diff?.changed ?? [],
        citationIds: ev.payload.citations ?? [],
      });
      break;
    }
    case "registered":
      markRegistered(ev.payload.case_ids, ev.payload.file);
      break;
    case "ask": {
      const cur = lastStreamingText();
      if (cur) cur.streaming = false;
      messages.value.push({
        id: ++msgSeq, role: "agent", kind: "ask",
        question: ev.payload.question, options: ev.payload.options ?? [],
      });
      break;
    }
    case "citation":
      citationsMap.value = {
        ...citationsMap.value,
        [ev.payload.kb_id]: { title: ev.payload.title || ev.payload.kb_id, source_uri: ev.payload.source_uri },
      };
      break;
    case "error":
      messages.value.push({
        id: ++msgSeq, role: "agent", kind: "error",
        detail: ev.payload.detail, code: ev.payload.code, traceId: ev.payload.trace_id,
      });
      break;
    case "done":
      if (ev.payload.session_id) sid.value = ev.payload.session_id;
      { const cur = lastStreamingText(); if (cur) cur.streaming = false; }
      break;
  }
}

/* ---------------- 发送 ---------------- */
async function send(text?: string, patch?: { case_id: string; body: Record<string, unknown> }) {
  const message = (text ?? input.value).trim();
  if (!message) { ElMessage.info("请输入内容"); return; }
  if (busy.value) return;
  messages.value.push({ id: ++msgSeq, role: "user", kind: "text", text: message });
  if (!patch) input.value = "";
  busy.value = true;
  activity.value = "已发送，等待 Agent 响应…";
  try {
    const handle = await postChatStream(
      {
        session_id: sid.value || undefined,
        message,
        platform: "interface-auto",
        project_id: project.value?.id ?? "",
        auto_register: autoRegister.value,
        patch: patch ?? null,
      },
      onEvent,
    );
    if (handle.sessionId) sid.value = handle.sessionId;
  } catch (e) {
    // 流开始前的失败仍是统一 JSON：这里补一条 error 气泡，SSE 帧内的 error 事件走 onEvent
    messages.value.push({ id: ++msgSeq, role: "agent", kind: "error", detail: (e as Error).message, code: "request_failed", traceId: "" });
  } finally {
    busy.value = false;
    activity.value = "";
    void loadSessions();
  }
}

function askOption(opt: string) {
  void send(opt);
}

/** 画布抽屉提交：结构化 patch 也是一轮对话（后端合并基线→重过闸门→更新卡片与画布） */
function onCanvasPatch(caseId: string, body: Record<string, unknown>, label: string) {
  void send(label, { case_id: caseId, body });
}

/* ---------------- 展示辅助 ---------------- */
function citeLabel(kbId: string): string {
  return citationsMap.value[kbId]?.title || kbId;
}
function citeUrl(kbId: string): string {
  return citationsMap.value[kbId]?.source_uri || "";
}

watch(messages, () => {
  void nextTick(() => {
    if (streamBox.value) streamBox.value.scrollTop = streamBox.value.scrollHeight;
  });
}, { deep: true });

onMounted(async () => {
  await loadProjects();
  void loadSessions();
  void checkAgent();
  messages.value.push({
    id: ++msgSeq, role: "agent", kind: "text",
    text: "你好！用自然语言描述要做什么即可，例如：\n" +
      "· 「给『统一相册列表(分页)』生成 5 条用例」\n" +
      "· 「基于 AL-01 补全断言」 · 「再扩展三条同风格的」\n" +
      "产出会通过平台权威校验后自动写入用例管理（可在上方切换为仅草稿）。",
  });
});
</script>

<template>
  <div class="chat-page">
    <!-- 顶栏：项目 / 落库模式 / 会话 / Agent 状态 -->
    <div class="topbar">
      <b>AI 生成（对话式）</b>
      <el-tag v-if="agentOnline === true" type="success" size="small">Agent 在线</el-tag>
      <el-tag v-else-if="agentOnline === false" type="danger" size="small">Agent 不可达</el-tag>
      <el-tag v-else size="small">检测中…</el-tag>
      <el-link :underline="false" style="margin-left: 2px" @click="checkAgent">重新检测</el-link>

      <span class="top-label">目标项目</span>
      <el-select :model-value="project?.id" size="small" style="width: 180px" placeholder="选择接口项目"
                 @change="(id: string) => (project = projects.find((p) => p.id === id) ?? project)">
        <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
      </el-select>

      <span class="top-label">落库模式</span>
      <el-radio-group v-model="autoRegister" size="small">
        <el-radio-button value="auto">过检自动落库</el-radio-button>
        <el-radio-button value="ask">仅草稿</el-radio-button>
        <el-radio-button value="off">关闭</el-radio-button>
      </el-radio-group>

      <span class="top-label">历史会话</span>
      <el-select v-model="sid" size="small" style="width: 200px" placeholder="恢复会话" clearable
                 @change="(v: string) => v && restoreSession(v)">
        <el-option v-for="s in sessions" :key="s.id" :label="`${s.title}（${Math.floor(s.turns / 2)} 轮）`" :value="s.id" />
      </el-select>
      <el-button size="small" @click="newSession">新建</el-button>
      <el-button size="small" type="danger" plain :disabled="!sid" @click="removeSession">删除</el-button>
    </div>

    <div class="body">
      <!-- 左 40%：步骤画布 -->
      <div class="left">
        <StepCanvas :steps="steps" :drafts="drafts" :busy="busy" @patch="onCanvasPatch" />
      </div>

      <!-- 右 60%：聊天流 -->
      <div class="right">
        <div ref="streamBox" class="stream">
          <div v-for="m in messages" :key="m.id" class="msg" :class="`msg--${m.role}`">
            <!-- 用户气泡 -->
            <div v-if="m.role === 'user'" class="bubble bubble--user">{{ m.text }}</div>

            <!-- Agent 文本 -->
            <div v-else-if="m.kind === 'text'" class="bubble bubble--agent">
              <span style="white-space: pre-wrap">{{ m.text }}</span>
              <span v-if="m.streaming" class="cursor">▍</span>
            </div>

            <!-- 用例卡片：校验徽标 + 问题 + diff 对照 + 溯源 + 落库状态 -->
            <div v-else-if="m.kind === 'case'" class="bubble bubble--agent case-card">
              <div class="case-head">
                <el-tag size="small" effect="dark" :type="m.validation?.passed ? 'success' : 'danger'">
                  {{ m.validation?.passed ? "过检 ✓" : "未过闸" }}
                </el-tag>
                <b>{{ m.caseId }}</b>
                <span class="case-desc">{{ String(m.caseData?.case_desc ?? "") }}</span>
                <el-tag v-if="m.registered" size="small" type="success" class="reg-tag">已落库 ✓</el-tag>
                <el-tag v-else-if="autoRegister === 'auto'" size="small" type="info" class="reg-tag">未落库</el-tag>
              </div>
              <div class="case-api">
                <el-tag size="small" effect="plain" type="info">{{ String(m.caseData?.method ?? "") }}</el-tag>
                <code>{{ String(m.caseData?.url ?? "") }}</code>
              </div>
              <ul v-if="m.validation?.problems?.length" class="problems">
                <li v-for="(p, i) in m.validation.problems" :key="i">{{ p }}</li>
              </ul>
              <div v-if="m.diff?.length" class="diff">
                <div class="diff-title">相对基准的改动：</div>
                <div v-for="d in m.diff" :key="d.field" class="diff-row">
                  <code>{{ d.field }}</code>
                  <span class="from">{{ JSON.stringify(d.from ?? null) }}</span>
                  <span>→</span>
                  <span class="to">{{ JSON.stringify(d.to ?? null) }}</span>
                </div>
              </div>
              <div v-if="m.citationIds?.length" class="cites">
                依据：
                <a v-for="c in m.citationIds" :key="c" :href="citeUrl(c) || undefined" target="_blank" rel="noopener">
                  {{ citeLabel(c) }}
                </a>
              </div>
              <el-collapse class="case-json">
                <el-collapse-item title="查看草稿 JSON" :name="m.id">
                  <JsonViewer :data="m.caseData" :max-height="260" />
                </el-collapse-item>
              </el-collapse>
              <div v-if="!m.registered && autoRegister === 'auto'" class="reg-note">
                未自动落库：本次产出未同时满足「非降级 + 平台权威 + 单条过闸」红线（详见上方问题列表或错误提示）
              </div>
            </div>

            <!-- 追问卡片 -->
            <div v-else-if="m.kind === 'ask'" class="bubble bubble--agent ask-card">
              <div>{{ m.question }}</div>
              <div class="ask-opts">
                <el-button v-for="o in m.options" :key="o" size="small" :disabled="busy" @click="askOption(o)">
                  {{ o }}
                </el-button>
              </div>
            </div>

            <!-- 错误条 -->
            <div v-else class="err-bar">
              <b>出错</b>（{{ m.code }}）：{{ m.detail }}
              <span v-if="m.traceId" class="trace">trace {{ m.traceId }}</span>
            </div>
          </div>
        </div>

        <div v-if="busy && activity" class="activity">{{ activity }}</div>

        <!-- 输入区 -->
        <div class="composer">
          <el-input v-model="input" type="textarea" :rows="3" resize="none" :disabled="busy"
                    placeholder="描述要生成的用例（回车发送，Shift+回车换行）；也可以直接点左侧画布节点手工修改"
                    @keydown.enter.exact.prevent="send()" />
          <div class="composer-actions">
            <span class="tip">
              {{ busy ? "流式响应中…" : `落库模式：${autoRegister === 'auto' ? '过检自动落库' : autoRegister === 'ask' ? '仅草稿（后端 ask）' : '关闭'}` }}
            </span>
            <el-button type="primary" :loading="busy" :disabled="agentOnline === false" @click="send()">
              发送
            </el-button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-page { display: flex; flex-direction: column; height: calc(100vh - 112px); min-height: 520px; }
.topbar {
  display: flex; align-items: center; gap: 10px; padding: 10px 14px;
  border-bottom: 1px solid #e4e7ed; background: #fff; flex-wrap: wrap;
}
.top-label { color: #606266; font-size: 13px; margin-left: 8px; }
.body { flex: 1; display: flex; min-height: 0; }
.left { width: 40%; min-width: 320px; border-right: 1px solid #e4e7ed; background: #fafbfc; }
.right { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.stream { flex: 1; overflow: auto; padding: 16px; }
.msg { display: flex; margin-bottom: 12px; }
.msg--user { justify-content: flex-end; }
.msg--agent { justify-content: flex-start; }
.bubble { max-width: 82%; padding: 9px 12px; border-radius: 10px; font-size: 13px; line-height: 1.65; }
.bubble--user { background: #ecf5ff; color: #2b6cb0; border: 1px solid #d9ecff; }
.bubble--agent { background: #fff; border: 1px solid #e4e7ed; color: #303133; }
.cursor { animation: blink 1s infinite; color: #409eff; }
@keyframes blink { 50% { opacity: 0; } }
.case-card { width: 100%; min-width: 320px; }
.case-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.case-desc { flex: 1; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.reg-tag { flex-shrink: 0; }
.case-api { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.case-api code { font-size: 12px; color: #606266; }
.problems { margin: 4px 0 6px; padding-left: 18px; color: #f56c6c; font-size: 12px; }
.diff { background: #f8f9fb; border-radius: 6px; padding: 8px 10px; margin-bottom: 6px; font-size: 12px; }
.diff-title { color: #909399; margin-bottom: 4px; }
.diff-row { display: flex; align-items: center; gap: 8px; margin: 2px 0; }
.diff-row .from { color: #c45656; text-decoration: line-through; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.diff-row .to { color: #529b2e; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cites { font-size: 12px; color: #909399; margin-bottom: 4px; }
.cites a { color: #409eff; margin-right: 8px; }
.case-json { margin-top: 4px; }
.case-json :deep(.el-collapse-item__header) { font-size: 12px; color: #909399; height: 28px; line-height: 28px; border: none; }
.reg-note { margin-top: 6px; font-size: 12px; color: #e6a23c; }
.ask-card .ask-opts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.err-bar {
  background: #fef0f0; border: 1px solid #fde2e2; color: #c45656; border-radius: 8px;
  padding: 8px 12px; font-size: 12px; max-width: 92%;
}
.err-bar .trace { color: #909399; margin-left: 8px; }
.activity { padding: 4px 16px; font-size: 12px; color: #e6a23c; background: #fdf6ec; border-top: 1px solid #f5dab1; }
.composer { border-top: 1px solid #e4e7ed; padding: 10px 14px; background: #fff; }
.composer-actions { display: flex; align-items: center; justify-content: space-between; margin-top: 8px; }
.composer-actions .tip { color: #909399; font-size: 12px; }
</style>
