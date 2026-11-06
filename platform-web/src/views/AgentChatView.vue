<script setup lang="ts">
/**
 * AI 助手：豆包/DeepSeek 式聊天布局——左侧会话栏、居中消息流、底部输入卡片，步骤画布按需从右侧展开。
 *
 * 多会话并行：每个会话是独立视图（消息/打字机/事件流各自一份），切换不中断后台流；
 * 生成中可继续发送——消息进“等待发送”队列，轮到自动发出，也可手动强制推送（中止本轮立即发）。
 *
 * 链路职责：
 * - 对话走 qa-agent /v1/chat/stream（SSE，X-Agent-Key）；模型逐段回传，前端做打字机平滑揭示；
 * - 落库红线在后端（auto 模式也必经平台权威过检），前端只按事件渲染，不做任何"可否落库"判断；
 * - 节点编辑不直接改 JSON：StepCanvas emit patch → 组一轮 {message, patch} 对话回 Agent（人改也过闸）。
 * 会话历史唯一来源是 qa-agent（GET /v1/chat/sessions*），本地不做第二存储。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox, type UploadFile } from "element-plus";
import { ArrowDown, Check, ChatLineSquare, Close, Connection, Delete, Document, MagicStick, Paperclip, Plus, Promotion, VideoPause } from "@element-plus/icons-vue";
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
  // Agentic 过程可视化：本轮的思考条目（trace/step/cite）与耗时
  process?: ProcEntry[];
  procOpen?: boolean;
  elapsed?: number;
  // kind=ask：选过选项后回显“已选择”，未选且流结束时提示等待
  answered?: string;
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
  // 用户气泡附带的附件名（仅展示用，历史恢复不重建）
  files?: string[];
}

/** 思考过程条目：trace=后端阶段注记，step=环节起止，cite=知识引用 */
interface ProcEntry {
  kind: "trace" | "step" | "cite";
  phase?: string;
  text: string;
  ok?: boolean;
}

let msgSeq = 0;
const input = ref("");
const streamBox = ref<HTMLElement | null>(null);

/* ---------------- 多会话并行：每个会话一份独立视图，后台流继续写入互不干扰 ---------------- */
interface QueueItem {
  message: string;
  patch?: { case_id: string; body: Record<string, unknown> };
  files: { name: string; content: string }[];
}
interface SessionView {
  key: string;                          // views 里的键：新会话用临时键，done 后搬到真实 sid
  sid: string;                          // 后端 session_id（新会话未落定时为空）
  messages: ChatMsg[];
  steps: ChatPlanStep[];
  drafts: Record<string, Record<string, unknown>>;
  citationsMap: Record<string, { title: string; source_uri: string }>;
  activity: string;                     // step_start/finish 的轻量进度条，不落消息流
  busy: boolean;
  queue: QueueItem[];                   // 生成中发的消息：等待发送，轮末自动接发
  typingBuf: string;                    // 打字机：已到达未展示
  typingMsg: ChatMsg | null;            // 本轮唯一在打字的气泡
  procMsg: ChatMsg | null;              // 本轮思考条目挂在这个气泡上
  turnEnded: boolean;                   // 流已结束：揭示完即收尾
  turnSeq: number;                      // 轮次代号：强制推送让旧轮的收尾逻辑失效
  abortCtl: AbortController | null;
  turnStart: number;
  elapsed: number;                      // 本轮耗时（思考过程块头部“处理中 Xs”）
}
function makeView(key: string, sid = ""): SessionView {
  return { key, sid, messages: [], steps: [], drafts: {}, citationsMap: {}, activity: "", busy: false,
    queue: [], typingBuf: "", typingMsg: null, procMsg: null, turnEnded: false, turnSeq: 0,
    abortCtl: null, turnStart: 0, elapsed: 0 };
}
const views = reactive<Record<string, SessionView>>({});
let viewSeq = 0;
const activeKey = ref("");
// 兜底空视图：首帧渲染、done 键搬迁、会话删除的瞬时间隙里 activeKey 可能指不到任何视图，
// 若 cur 返回 undefined，模板与 watch 求值会直接 TypeError 崩掉整页（并连带打断后续路由渲染）
const idleView = makeView("__idle");
const cur = computed(() => views[activeKey.value] ?? idleView);
// 模板与读取处沿用旧名称，统一由当前视图派生（写入处一律操作视图对象，保证切走后事件仍写对目标）
const messages = computed(() => cur.value.messages);
const steps = computed(() => cur.value.steps);
const drafts = computed(() => cur.value.drafts);
const busy = computed(() => cur.value.busy);
const activity = computed(() => cur.value.activity);
const sid = computed(() => cur.value.sid);
const elapsedSec = computed(() => cur.value.elapsed);

/* ---------------- 接口文档附件（上传 → 本轮对话携带） ---------------- */
const attachedFiles = ref<{ name: string; content: string }[]>([]);
const MAX_ATTACH = 5;            // 与后端 doc_generate.MAX_ATTACHMENTS 同口径
const MAX_ATTACH_BYTES = 2_000_000;  // 与后端单附件 2MB 上限同口径

function onFilePick(uploadFile: UploadFile) {
  const raw = uploadFile.raw;
  if (!raw) return;
  if (attachedFiles.value.length >= MAX_ATTACH) {
    ElMessage.warning(`单轮最多 ${MAX_ATTACH} 份文档，请分批发送`); return;
  }
  if (raw.size > MAX_ATTACH_BYTES) {
    ElMessage.warning(`「${raw.name}」超过 2MB 上限，请拆分后再传`); return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const content = String(reader.result ?? "");
    if (!content.trim()) { ElMessage.warning(`「${raw.name}」内容为空`); return; }
    attachedFiles.value = [...attachedFiles.value, { name: raw.name, content }];
  };
  reader.onerror = () => ElMessage.error(`读取「${raw.name}」失败`);
  reader.readAsText(raw, "utf-8");
}

/* ---------------- 顶部：项目 / 落库模式 / Agent 状态 ---------------- */
const projects = ref<Project[]>([]);
const project = ref<Project | null>(null);
const autoRegister = ref<"auto" | "ask" | "off">("auto");
const agentOnline = ref<boolean | null>(null);
// 联网搜索：webSearch 是本轮开关；总闸未放开时 webSearchSupported=false，开关置灰
const webSearch = ref(false);
const webSearchSupported = ref(true);
// 模型选择：health.models 提供候选；未配置时退回 default_model 单项（选择器始终可见，用户能看到当前用的模型）
const models = ref<string[]>([]);
const defaultModel = ref("");
const selectedModel = ref("");
const modelOptions = computed(() =>
  models.value.length ? models.value : (defaultModel.value ? [defaultModel.value] : []));
// 步骤画布：默认收起不占聊天宽度，产出步骤后自动展开，可手动开合
const canvasOpen = ref(false);

async function loadProjects() {
  projects.value = (await api.listProjects()).filter((p) => p.kind === "api");
  project.value = projects.value[0] ?? null;
}

async function checkAgent() {
  try {
    const h = await api.agentHealth();
    agentOnline.value = h.skills?.includes("case_generate") ?? false;
    webSearchSupported.value = !!h.web_search_enabled;
    if (!webSearchSupported.value) webSearch.value = false;   // 总闸关掉时复位开关，避免“看着开了其实没开”
    models.value = h.models ?? [];
    defaultModel.value = h.default_model ?? "";
    const opts = modelOptions.value;
    if (opts.length && !opts.includes(selectedModel.value)) selectedModel.value = opts[0];
  } catch {
    agentOnline.value = false;   // 拦截器已提示原因，这里只落状态
    webSearchSupported.value = false;
    models.value = [];
    defaultModel.value = "";
  }
}

/* ---------------- 会话历史（qa-agent 侧存储） ---------------- */
const sessions = ref<ChatSessionInfo[]>([]);

async function loadSessions() {
  try {
    sessions.value = await api.agentChatSessions();
  } catch { sessions.value = []; }
}

function newViewWithWelcome(): SessionView {
  const v = makeView(`new-${++viewSeq}`);
  v.messages.push({
    id: ++msgSeq, role: "agent", kind: "text",
    text: "你好！我是测试平台的智能助手，即问即答——日常沟通、测试答疑都可以直接问我，例如：\n" +
      "· 「这个接口应该测哪些场景」「帮我看看这段断言写得对不对」\n" +
      "· 「给『统一相册列表(分页)』生成 5 条用例」 · 「基于 AL-01 补全断言」 · 「再扩展三条同风格的」\n" +
      "· 也可以点输入框的「接口文档」（OpenAPI/Swagger/自由格式），一键登记接口并生成用例\n" +
      "需要产出用例时，会自动走平台权威校验后写入用例管理，无需指定项目。",
  });
  return v;
}

function newSession() {
  const v = newViewWithWelcome();
  views[v.key] = v;
  activeKey.value = v.key;
}

// 初始会话必须在 setup 内同步建好：首帧渲染与 watch(messages) 求值都早于 onMounted，
// 放在 onMounted（且排在 await 之后）会让 cur 首帧就是 undefined，整页崩溃
newSession();

/** 切换会话：已在内存的视图（含进行中的流）直接切回，后台流不中断；否则拉历史建新视图 */
async function switchTo(id: string) {
  if (views[id]) { activeKey.value = id; return; }
  const d = await api.agentChatSession(id);
  const v = makeView(id, id);
  v.steps = d.plan ?? [];
  v.drafts = d.drafts ?? {};
  // 历史轮次只存文本摘要：气泡可回看，卡片明细不重建（画布/草稿已从 plan/drafts 恢复）
  for (const t of d.turns) {
    if (t.role === "user") {
      v.messages.push({ id: ++msgSeq, role: "user", kind: "text", text: t.content || "（结构化修改）" });
    } else {
      v.messages.push({ id: ++msgSeq, role: "agent", kind: "text", text: t.content });
    }
  }
  views[id] = v;
  activeKey.value = id;
}

async function removeSession(id?: string) {
  const target = id || cur.value.sid;
  if (!target) { ElMessage.info("当前没有进行中的会话"); return; }
  await ElMessageBox.confirm("删除后该会话历史不可恢复（用例/画布不受影响），确认删除？", "删除会话",
    { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  await api.agentChatDelete(target);
  const v = views[target];
  if (v) {
    v.turnSeq++; v.abortCtl?.abort(); v.abortCtl = null;   // 中止该会话的进行中流
    if (v.key === activeKey.value) newSession();
    delete views[target];
  }
  await loadSessions();
  ElMessage.success("会话已删除");
}

/* ---------------- 事件分派 → 渲染（全部面向视图：切走后事件仍写回自己会话） ---------------- */
function pushProc(v: SessionView, e: ProcEntry) {
  const m = v.procMsg;
  if (!m) return;
  m.process = [...(m.process ?? []), e];
  if (m.procOpen === undefined) m.procOpen = true;   // 流式期间默认展开，结束后用户可收
}

function lastStreamingText(v: SessionView): ChatMsg | null {
  const m = v.messages[v.messages.length - 1];
  return m && m.role === "agent" && m.kind === "text" && m.streaming ? m : null;
}

function pushAgentText(v: SessionView, text: string): ChatMsg {
  v.messages.push({ id: ++msgSeq, role: "agent", kind: "text", text, streaming: true });
  // 必须拿数组里的响应式代理而非 raw 对象：改 raw 不触发重渲染，打字机会“卡住”不上屏
  return v.messages[v.messages.length - 1];
}

/* ---------------- 打字机平滑（一轮一个气泡，展示节奏与到达节奏解耦） ---------------- */
// 有的网关整段缓冲后才吐 SSE 帧（到达呈突发），真流式时帧粒度也参差：
// token 一律进视图缓冲按 tick 揭示；气泡从发送起就绑定本轮，直到流结束且揭示完才收尾，
// 绝不因缓冲暂时为空就提前关泡——那会把一段回答拆成多条气泡。
const TYPE_TICK_MS = 16;
let tickTimer: number | null = null;

/** 全局单 tick 驱动所有会话的揭示与计时：后台会话的流式效果也在走，切回来无缝衔接 */
function globalTick() {
  const now = Date.now();
  for (const k in views) {
    const v = views[k];
    if (v.busy) v.elapsed = Math.round((now - v.turnStart) / 1000);
    if (!v.typingMsg) continue;
    if (v.typingBuf) {
      const n = Math.max(2, Math.ceil(v.typingBuf.length / 50));   // 积压多则快、少则慢，自然收敛
      v.typingMsg.text = (v.typingMsg.text ?? "") + v.typingBuf.slice(0, n);
      v.typingBuf = v.typingBuf.slice(n);
    } else if (v.turnEnded) {
      finishTyping(v);
    }
  }
}

/** 收尾：空占位泡（没等到任何 token 也没记到思考条目）直接移除，不留“思考中”残影 */
function finishTyping(v: SessionView) {
  const m = v.typingMsg;
  if (m) {
    if (m.text || m.process?.length) m.streaming = false;
    else {
      const i = v.messages.indexOf(m);
      if (i >= 0) v.messages.splice(i, 1); else m.streaming = false;
    }
  }
  v.typingBuf = ""; v.typingMsg = null; v.turnEnded = false;
}

/** 立即揭示剩余文本并收尾（结构性事件/新轮次/失败时调用，保证不乱序、不留残字） */
function flushTyping(v: SessionView) {
  if (v.typingMsg && v.typingBuf) v.typingMsg.text = (v.typingMsg.text ?? "") + v.typingBuf;
  finishTyping(v);
}

/** registered 事件：把对应 case_id 的卡片翻成「已落库 ✓」；平台让号同步改卡片编号与草稿 key */
function markRegistered(v: SessionView, caseIds: string[], file: string, renamed: { from: string; to: string }[] = []) {
  const toOld = new Map(renamed.map((r) => [r.to, r.from]));   // 事件带的是新号，卡片还是旧号
  const toNew = new Map(renamed.map((r) => [r.from, r.to]));
  for (const m of v.messages) {
    if (m.kind !== "case" || !m.caseId) continue;
    const matchId = toOld.get(m.caseId) ?? m.caseId;
    if (!caseIds.includes(m.caseId) && !caseIds.includes(matchId)) continue;
    m.registered = true;
    m.registeredFile = file;
    const newId = toNew.get(m.caseId);
    if (newId) {
      const d = { ...v.drafts };
      if (d[m.caseId]) {
        d[newId] = { ...(d[m.caseId] as Record<string, unknown>), case_id: newId };
        delete d[m.caseId];
      }
      v.drafts = d;
      m.caseId = newId;
      m.caseData = { ...m.caseData, case_id: newId };
    }
  }
}

function onEvent(v: SessionView, ev: ChatEvent) {
  switch (ev.type) {
    case "token": {
      // 全部 token 只进当前轮的缓冲；缓冲短暂清空不关泡（拆气泡的根因），后续 token 续写同一气泡
      if (!v.typingMsg) v.typingMsg = lastStreamingText(v) ?? pushAgentText(v, "");
      v.typingBuf += ev.payload.text;
      v.turnEnded = false;
      break;
    }
    case "trace":
      pushProc(v, { kind: "trace", phase: ev.payload.phase, text: ev.payload.detail });
      break;
    case "plan":
      v.steps = ev.payload.steps;
      break;
    case "step_start":
      v.activity = `步骤 ${ev.payload.seq}：${ev.payload.summary}`;
      pushProc(v, { kind: "step", text: ev.payload.summary });
      break;
    case "step_finish":
      // 后端节点状态闭集只有 draft/registered；失败靠活动条+error 事件显性化，不伪造状态
      v.activity = ev.payload.ok ? "" : `步骤 ${ev.payload.seq} 失败：${ev.payload.summary}`;
      pushProc(v, { kind: "step", text: ev.payload.summary, ok: !!ev.payload.ok });
      break;
    case "case_ready": {
      flushTyping(v);
      const cur2 = lastStreamingText(v);
      if (cur2) cur2.streaming = false;   // 卡片前的思考文本收尾，后续 token 另起气泡
      const c = ev.payload.case;
      const caseId = String(c.case_id ?? "");
      if (caseId) v.drafts = { ...v.drafts, [caseId]: c };
      v.messages.push({
        id: ++msgSeq, role: "agent", kind: "case",
        caseData: c, caseId,
        validation: ev.payload.validation,
        diff: ev.payload.diff?.changed ?? [],
        citationIds: ev.payload.citations ?? [],
      });
      break;
    }
    case "registered":
      markRegistered(v, ev.payload.case_ids, ev.payload.file, ev.payload.renamed ?? []);
      break;
    case "ask": {
      flushTyping(v);
      const cur2 = lastStreamingText(v);
      if (cur2) cur2.streaming = false;
      v.messages.push({
        id: ++msgSeq, role: "agent", kind: "ask",
        question: ev.payload.question, options: ev.payload.options ?? [],
      });
      break;
    }
    case "citation":
      v.citationsMap = {
        ...v.citationsMap,
        [ev.payload.kb_id]: { title: ev.payload.title || ev.payload.kb_id, source_uri: ev.payload.source_uri },
      };
      pushProc(v, { kind: "cite", text: ev.payload.title || ev.payload.kb_id });
      break;
    case "error":
      flushTyping(v);
      v.messages.push({
        id: ++msgSeq, role: "agent", kind: "error",
        detail: ev.payload.detail, code: ev.payload.code, traceId: ev.payload.trace_id,
      });
      break;
    case "done": {
      const newSid = ev.payload.session_id;
      if (newSid && v.sid !== newSid) {
        const oldKey = v.key;
        v.sid = newSid; v.key = newSid;
        views[newSid] = v;                 // 键搬到真实 sid：导轨点击即可切回这个进行中的视图
        if (activeKey.value === oldKey) activeKey.value = newSid;   // 先切键再删旧键，不留空窗
        if (oldKey !== newSid) delete views[oldKey];
      }
      if (v.procMsg) v.procMsg.elapsed = Math.max(1, v.elapsed);
      v.turnEnded = true;
      if (!v.typingBuf) finishTyping(v);   // 无积压立即收；有积压由 tick 揭示完再收
      break;
    }
  }
}

/* ---------------- 发送（生成中不拦截：排队“等待发送”，轮末自动接发，也可强制推送） ---------------- */
async function send(text?: string, patch?: { case_id: string; body: Record<string, unknown> }) {
  const v = cur.value;
  const files = attachedFiles.value;
  const rawMsg = (text ?? input.value).trim();
  // 只上传了文档没打字：补一句默认指令（后端 message 必填，路由靠 attachments 判 doc 分支）
  const message = rawMsg || (files.length ? "请登记附件接口文档并生成用例" : "");
  if (!message) { ElMessage.info("请输入内容或上传接口文档"); return; }
  if (!patch) { input.value = ""; attachedFiles.value = []; }
  const carry = patch ? [] : [...files];
  if (v.busy || v.queue.length) {   // 生成中或已有排队：保持顺序进队列，轮末自动接发（豆包式）
    v.queue.push({ message, patch, files: carry });
    return;
  }
  await runTurn(v, message, patch, carry);
}

/** 把视图键搬到真实 sid：导轨点击即可切回这个进行中的视图 */
function bindSid(v: SessionView, newSid: string) {
  if (!newSid || v.sid === newSid) return;
  const oldKey = v.key;
  v.sid = newSid; v.key = newSid;
  views[newSid] = v;
  if (activeKey.value === oldKey) activeKey.value = newSid;   // 先切键再删旧键，不留空窗
  if (oldKey !== newSid) delete views[oldKey];
}

async function runTurn(v: SessionView, message: string, patch?: { case_id: string; body: Record<string, unknown> },
                       files: { name: string; content: string }[] = []) {
  flushTyping(v);   // 上一轮若还在揭示，先补全收尾，避免新旧气泡串字
  v.messages.push({ id: ++msgSeq, role: "user", kind: "text", text: message, files: files.map((f) => f.name) });
  v.busy = true;
  v.activity = "已发送，等待 Agent 响应…";
  v.turnEnded = false;
  v.typingMsg = pushAgentText(v, "");   // 占位“思考中”气泡：首 token 前的长等待也有即时反馈
  v.procMsg = v.typingMsg;
  v.turnStart = Date.now();
  v.elapsed = 0;
  const seq = ++v.turnSeq;              // 轮次代号：强制推送后旧轮收尾逻辑整体失效
  v.abortCtl = new AbortController();   // 停止按钮的取消句柄
  try {
    const handle = await postChatStream(
      {
        session_id: v.sid || undefined,
        message,
        platform: "interface-auto",
        project_id: project.value?.id ?? "",
        auto_register: autoRegister.value,
        web_search: webSearch.value,
        model: selectedModel.value || undefined,
        patch: patch ?? null,
        attachments: patch ? undefined : (files.length ? files : undefined),
      },
      (ev) => onEvent(v, ev),
      v.abortCtl.signal,
    );
    if (handle.sessionId) bindSid(v, handle.sessionId);
  } catch (e) {
    if (v.turnSeq !== seq) { /* 被强制推送接管，静默 */ }
    else if ((e as Error)?.name === "AbortError") {
      ElMessage.info("已停止本次生成");   // 手动停止不是故障，不补 error 气泡
    } else {
      // 流开始前的失败仍是统一 JSON：收掉占位泡后补一条 error 气泡，SSE 帧内的 error 事件走 onEvent
      flushTyping(v);
      v.messages.push({ id: ++msgSeq, role: "agent", kind: "error", detail: (e as Error).message, code: "request_failed", traceId: "" });
    }
  } finally {
    if (v.turnSeq !== seq) return;       // 已被强制推送接管：不碰新轮状态、不重复接发
    v.abortCtl = null;
    // 流已关：有积压交给 tick 揭示完收尾；没积压就地收占位泡（断流无 done 时不留残影）
    if (v.typingBuf) v.turnEnded = true;
    else finishTyping(v);
    v.busy = false;
    v.activity = "";
    void loadSessions();
    if (v.queue.length) {                // 等待队列自动接发（FIFO）
      const it = v.queue.shift()!;
      void runTurn(v, it.message, it.patch, it.files);
    }
  }
}

function askOption(opt: string, m?: ChatMsg) {
  if (m) m.answered = opt;   // 卡片定格“已选择”，选项按钮不再可点
  void send(opt);
}

/** 停止本轮生成：取消 fetch 流，已揭示的内容保留，不报错误 */
function stopStream() {
  cur.value.abortCtl?.abort();
}

/** 强制推送（豆包式）：中止当前生成，立即发这条排队消息；已揭示内容保留 */
function forcePush(i: number) {
  const v = cur.value;
  const it = v.queue.splice(i, 1)[0];
  if (!it) return;
  v.turnSeq++;                           // 旧轮 finally 作废，避免它覆盖新轮状态/重复接发
  v.abortCtl?.abort(); v.abortCtl = null;
  void runTurn(v, it.message, it.patch, it.files);
}

function cancelQueue(i: number) {
  cur.value.queue.splice(i, 1);
}

/** 画布抽屉提交：结构化 patch 也是一轮对话（后端合并基线→重过闸门→更新卡片与画布） */
function onCanvasPatch(caseId: string, body: Record<string, unknown>, label: string) {
  void send(label, { case_id: caseId, body });
}

/* ---------------- 展示辅助 ---------------- */
function citeLabel(kbId: string): string {
  return cur.value.citationsMap[kbId]?.title || kbId;
}
function citeUrl(kbId: string): string {
  return cur.value.citationsMap[kbId]?.source_uri || "";
}

/** 极简 Markdown 渲染：先转义再替换粗体/行内代码/标题，不引入额外依赖（模型回复常带这些标记） */
function mdLite(text: string): string {
  const esc = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/^#{1,4}\s+(.*)$/gm, "<b>$1</b>");
}

watch(messages, () => {
  void nextTick(() => {
    if (streamBox.value) streamBox.value.scrollTop = streamBox.value.scrollHeight;
  });
}, { deep: true });

// 画布有步骤了就自动展开（生成/修改产出时不打断用户）
watch(steps, (v) => { if (v.length) canvasOpen.value = true; });

onUnmounted(() => {
  if (tickTimer !== null) window.clearInterval(tickTimer);
});

onMounted(async () => {
  void loadProjects();
  void loadSessions();
  void checkAgent();
  tickTimer = window.setInterval(globalTick, TYPE_TICK_MS);   // 全局单 tick 驱动所有视图
});
</script>

<template>
  <div class="chat-page">
    <!-- 左侧会话栏（豆包/DeepSeek 式）：新建入口 + 历史会话列表 -->
    <aside class="rail">
      <div class="rail-head">
        <b>AI 助手</b>
        <el-tag v-if="agentOnline === true" type="success" size="small" effect="plain">在线</el-tag>
        <el-tag v-else-if="agentOnline === false" type="danger" size="small" effect="plain">不可达</el-tag>
        <el-tag v-else size="small" type="info" effect="plain">检测中</el-tag>
      </div>
      <el-button class="rail-new" type="primary" plain :icon="Plus" @click="newSession">新建对话</el-button>
      <div class="rail-list">
        <div v-for="s in sessions" :key="s.id" class="rail-item" :class="{ active: s.id === sid }"
             @click="switchTo(s.id)">
          <el-icon><ChatLineSquare /></el-icon>
          <span class="rail-title">{{ s.title }}</span>
          <span class="rail-turns">{{ Math.floor(s.turns / 2) }} 轮</span>
          <el-icon class="rail-del" title="删除会话" @click.stop="removeSession(s.id)"><Delete /></el-icon>
        </div>
        <div v-if="!sessions.length" class="rail-empty">暂无历史会话</div>
      </div>
      <div class="rail-foot">
        <el-link :underline="false" @click="checkAgent">重新检测 Agent 状态</el-link>
      </div>
    </aside>

    <!-- 主列：极简顶栏 + 居中消息流 + 底部输入卡片 -->
    <div class="main">
      <div class="topbar">
        <span class="tb-sub">日常沟通 · 测试答疑 · 用例生成，都可以在这里对话完成</span>
        <el-badge :value="steps.length" :hidden="!steps.length">
          <el-button size="small" :type="canvasOpen ? 'primary' : 'default'" plain
                     @click="canvasOpen = !canvasOpen">步骤画布</el-button>
        </el-badge>
      </div>

        <div ref="streamBox" class="stream">
          <div v-for="m in messages" :key="m.id" class="msg" :class="`msg--${m.role}`">
            <!-- 用户气泡 -->
            <div v-if="m.role === 'user'" class="bubble bubble--user">
              {{ m.text }}
              <div v-if="m.files?.length" class="user-files">
                <span v-for="(f, i) in m.files" :key="`${f}-${i}`"><el-icon><Document /></el-icon> {{ f }}</span>
              </div>
            </div>

            <!-- Agent 文本：头像 + 思考过程块 + 极简 Markdown；无字时显“思考中”动画，有字后显流式光标 -->
            <template v-else-if="m.kind === 'text'">
              <div class="avatar">AI</div>
              <div class="bubble bubble--agent">
                <!-- 思考过程（demo.svg Agentic 可视化）：trace/step/cite 聚成可折叠块，头部实时计时 -->
                <div v-if="m.process?.length" class="proc">
                  <div class="proc-head" @click="m.procOpen = !m.procOpen">
                    <el-icon><MagicStick /></el-icon>思考过程
                    <el-icon class="proc-arrow" :class="{ open: m.procOpen }"><ArrowDown /></el-icon>
                    <span class="proc-time">{{ m.elapsed != null ? `已处理 ${m.elapsed}s` : `处理中 ${elapsedSec}s` }}</span>
                  </div>
                  <div v-show="m.procOpen" class="proc-body">
                    <div v-for="(p, i) in m.process" :key="i" class="proc-item">
                      <el-icon class="proc-ico" :class="{ 'proc-ico--think': p.kind === 'trace', 'proc-ico--step': p.kind === 'step', 'proc-ico--cite': p.kind === 'cite' }">
                        <component :is="p.kind === 'cite' ? Document : (p.kind === 'step' ? (p.ok === false ? Close : Check) : VideoPause)" />
                      </el-icon>
                      <span class="proc-text">{{ p.text }}</span>
                    </div>
                  </div>
                </div>
                <span v-if="m.streaming && !m.text && !m.process?.length" class="thinking"><i /><i /><i /></span>
                <template v-else>
                  <span class="md" v-html="mdLite(m.text ?? '')"></span>
                  <span v-if="m.streaming" class="cursor">▍</span>
                </template>
              </div>
            </template>

            <!-- 用例卡片：校验徽标 + 问题 + diff 对照 + 溯源 + 落库状态 -->
            <template v-else-if="m.kind === 'case'">
              <div class="avatar">AI</div>
              <div class="bubble bubble--agent case-card">
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
            </template>

            <!-- 追问卡片：选择后定格“已选择”，未选且本轮已结束时提示等待（demo.svg 交互确认样式） -->
            <template v-else-if="m.kind === 'ask'">
              <div class="avatar">AI</div>
              <div class="bubble bubble--agent ask-card">
              <div>{{ m.question }}</div>
              <div v-if="m.answered" class="ask-answered">
                <el-icon><Check /></el-icon>已选择："{{ m.answered }}"
              </div>
              <div v-else-if="!busy" class="ask-waiting">正在等待你的操作…</div>
              <div v-else class="ask-opts">
                <el-button v-for="o in m.options" :key="o" size="small" :disabled="busy" @click="askOption(o, m)">
                  {{ o }}
                </el-button>
              </div>
              </div>
            </template>

            <!-- 错误条 -->
            <template v-else>
              <div class="avatar avatar--err">!</div>
              <div class="err-bar">
                <b>出错</b>（{{ m.code }}）：{{ m.detail }}
                <span v-if="m.traceId" class="trace">trace {{ m.traceId }}</span>
              </div>
            </template>
          </div>
        </div>

        <div v-if="busy && activity" class="activity">{{ activity }}</div>

        <!-- 输入区：圆角卡片，工具条内置在卡片里（豆包/DeepSeek 式）；生成中不锁输入，排队可见可强制推送 -->
        <div class="composer">
          <div v-if="cur.queue.length" class="queue-card">
            <div v-for="(q, i) in cur.queue" :key="i" class="queue-item">
              <span class="queue-tag">等待发送</span>
              <span class="queue-text">{{ q.message }}</span>
              <el-button size="small" type="primary" text bg @click="forcePush(i)">强制推送</el-button>
              <el-button size="small" text @click="cancelQueue(i)">取消</el-button>
            </div>
          </div>
          <div class="composer-card">
            <div v-if="attachedFiles.length" class="attach-row">
              <el-tag v-for="(f, i) in attachedFiles" :key="f.name + i" size="small" closable type="info"
                      @close="attachedFiles.splice(i, 1)">{{ f.name }}</el-tag>
            </div>
            <el-input v-model="input" type="textarea" :autosize="{ minRows: 1, maxRows: 6 }" resize="none"
                      placeholder="输入任何问题：日常沟通、测试答疑，或描述要生成的用例（回车发送，Shift+回车换行；生成中发送会排队）"
                      @keydown.enter.exact.prevent="send()" />
            <div class="card-tools">
              <div class="tool-left">
                <el-select v-model="selectedModel" size="small" class="tool-ctl" placeholder="模型"
                           :disabled="!modelOptions.length">
                  <el-option v-for="m in modelOptions" :key="m" :label="m" :value="m" />
                </el-select>
                <el-tooltip :disabled="webSearchSupported" content="服务端未放开联网总闸" placement="top">
                  <button type="button" class="tool-toggle" :class="{ on: webSearch }"
                          :disabled="!webSearchSupported" @click="webSearch = !webSearch">
                    <el-icon><Connection /></el-icon>联网搜索
                  </button>
                </el-tooltip>
                <el-upload :show-file-list="false" :auto-upload="false" :multiple="true"
                           accept=".json,.yaml,.yml,.md,.txt" :on-change="onFilePick" action="#">
                  <button type="button" class="tool-toggle"><el-icon><Paperclip /></el-icon>接口文档</button>
                </el-upload>
              </div>
              <button v-if="!busy" type="button" class="send-btn"
                      :disabled="agentOnline === false" @click="send()">
                <el-icon><Promotion /></el-icon>
              </button>
              <button v-else type="button" class="send-btn send-btn--stop" title="停止生成" @click="stopStream()">
                <el-icon><VideoPause /></el-icon>
              </button>
            </div>
          </div>
          <div class="composer-hint">回答由 AI 生成；用例落库前均经平台权威校验，重要信息请人工复核</div>
        </div>
    </div>

      <!-- 步骤画布右侧抽屉：默认收起，产出步骤自动展开 -->
      <transition name="canvas">
        <aside v-show="canvasOpen" class="canvas-panel">
          <div class="canvas-head">
            <b>步骤画布</b>
            <span class="canvas-sub">对话产出后实时生成，节点=用例步骤</span>
            <el-button text size="small" @click="canvasOpen = false">收起</el-button>
          </div>
          <div class="canvas-body">
            <StepCanvas :steps="steps" :drafts="drafts" :busy="busy" @patch="onCanvasPatch" />
          </div>
        </aside>
      </transition>
  </div>
</template>

<style scoped>
.chat-page { display: flex; height: calc(100vh - 112px); min-height: 520px; background: #f7f8fa; }

/* 左侧会话栏 */
.rail {
  width: 232px; flex-shrink: 0; display: flex; flex-direction: column;
  background: #fafbfd; border-right: 1px solid #e8ebf0;
}
.rail-head { display: flex; align-items: center; gap: 8px; padding: 14px 14px 10px; font-size: 15px; color: #1f2733; }
.rail-new { margin: 0 14px 10px; }
.rail-list { flex: 1; overflow: auto; padding: 0 8px; }
.rail-item {
  display: flex; align-items: center; gap: 6px; padding: 8px; margin-bottom: 2px;
  border-radius: 8px; cursor: pointer; font-size: 13px; color: #4b5563;
}
.rail-item:hover { background: #eef1f6; }
.rail-item.active { background: #e7f0fe; color: #1d63e0; }
.rail-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rail-turns { font-size: 11px; color: #9aa3af; flex-shrink: 0; }
.rail-del { opacity: 0; color: #c45656; flex-shrink: 0; }
.rail-item:hover .rail-del { opacity: 1; }
.rail-empty { padding: 16px 8px; font-size: 12px; color: #a8b0bb; text-align: center; }
.rail-foot { padding: 10px 14px; border-top: 1px solid #eceff4; font-size: 12px; }

/* 主列 */
.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.topbar {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 10px 20px; border-bottom: 1px solid #eceff4; background: #fff;
}
.tb-sub { font-size: 12px; color: #909399; }
.stream { flex: 1; overflow: auto; padding: 22px 24px; }
.msg { display: flex; margin: 0 auto 14px; max-width: 880px; width: 100%; }
.msg--user { justify-content: flex-end; }
.msg--agent { justify-content: flex-start; }
.avatar {
  width: 30px; height: 30px; border-radius: 8px; flex-shrink: 0; margin-right: 10px;
  background: linear-gradient(135deg, #409eff, #6c63ff); color: #fff;
  font-size: 11px; font-weight: 700; display: flex; align-items: center; justify-content: center;
}
.avatar--err { background: #f56c6c; }
.bubble { max-width: 78%; padding: 10px 14px; border-radius: 10px; font-size: 13px; line-height: 1.7; }
.bubble--user { background: #409eff; color: #fff; border-radius: 10px 10px 2px 10px; }
.user-files { margin-top: 4px; font-size: 12px; opacity: 0.9; }
.user-files span { display: inline-flex; align-items: center; gap: 4px; margin-right: 8px; }
.bubble--agent { background: #fff; border: 1px solid #e8ebf0; color: #303133; border-radius: 10px 10px 10px 2px; box-shadow: 0 1px 2px rgba(31, 39, 51, 0.05); }
.md { white-space: pre-wrap; word-break: break-word; }
.md :deep(code) { background: #f2f4f8; border-radius: 4px; padding: 1px 5px; font-family: Consolas, Monaco, monospace; font-size: 12px; }
.cursor { animation: blink 1s infinite; color: #409eff; }
@keyframes blink { 50% { opacity: 0; } }
.thinking { display: inline-flex; align-items: center; gap: 5px; height: 20px; }
.thinking i { width: 6px; height: 6px; border-radius: 50%; background: #b7c2d5; animation: bounce 1.2s infinite; }
.thinking i:nth-child(2) { animation-delay: 0.15s; }
.thinking i:nth-child(3) { animation-delay: 0.3s; }
@keyframes bounce { 0%, 60%, 100% { transform: translateY(0); opacity: 0.5; } 30% { transform: translateY(-5px); opacity: 1; } }
/* 思考过程块（可折叠）：trace/step/cite 条目 + 头部计时 */
.proc { border: 1px solid #e8ebf0; border-radius: 8px; margin-bottom: 8px; background: #fafbfd; overflow: hidden; }
.proc-head {
  display: flex; align-items: center; gap: 6px; padding: 7px 10px; cursor: pointer;
  font-size: 12px; color: #4b5563; user-select: none;
}
.proc-head:hover { background: #eef1f6; }
.proc-arrow { margin-left: 2px; transition: transform 0.15s; font-size: 12px; color: #9aa3af; }
.proc-arrow.open { transform: rotate(180deg); }
.proc-time { margin-left: auto; color: #9aa3af; font-size: 11px; }
.proc-body { padding: 2px 10px 8px; border-top: 1px dashed #e8ebf0; }
.proc-item { display: flex; align-items: baseline; gap: 6px; font-size: 12px; color: #606266; padding: 3px 0; }
.proc-ico { align-self: center; font-size: 13px; color: #9aa3af; flex-shrink: 0; }
.proc-ico--think { color: #6c63ff; }
.proc-ico--step { color: #409eff; }
.proc-ico--cite { color: #529b2e; }
.proc-text { flex: 1; }
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
.ask-answered { display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 12px; color: #529b2e; }
.ask-waiting { margin-top: 8px; font-size: 12px; color: #909399; }
.err-bar {
  background: #fef0f0; border: 1px solid #fde2e2; color: #c45656; border-radius: 8px;
  padding: 8px 12px; font-size: 12px; max-width: 78%;
}
.err-bar .trace { color: #909399; margin-left: 8px; }
.activity { padding: 4px 16px; font-size: 12px; color: #e6a23c; background: #fdf6ec; border-top: 1px solid #f5dab1; }

/* 等待发送队列（生成中发的消息排队，可强制推送/取消） */
.queue-card {
  max-width: 860px; margin: 0 auto 8px; background: #f2f6ff; border: 1px solid #d8e4fb;
  border-radius: 12px; padding: 6px 12px;
}
.queue-item { display: flex; align-items: center; gap: 8px; padding: 3px 0; font-size: 13px; color: #374151; }
.queue-tag { flex-shrink: 0; font-size: 11px; color: #1d63e0; background: #e0ebfd; border-radius: 6px; padding: 1px 7px; }
.queue-text { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 底部输入卡片 */
.composer { padding: 10px 24px 12px; }
.composer-card {
  max-width: 860px; margin: 0 auto; background: #fff;
  border: 1px solid #dfe4ec; border-radius: 16px; padding: 10px 12px 8px;
  box-shadow: 0 2px 10px rgba(31, 39, 51, 0.06); transition: border-color 0.15s;
}
.composer-card:focus-within { border-color: #409eff; }
.composer-card :deep(.el-textarea__inner) { box-shadow: none; padding: 4px 6px; font-size: 14px; }
.attach-row { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.attach-row .el-tag { cursor: default; }
.card-tools { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
.tool-left { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tool-ctl { width: 128px; }
.tool-ctl :deep(.el-select__wrapper) { box-shadow: none; border: 1px solid #e3e8f0; border-radius: 8px; font-size: 12px; }
.tool-toggle {
  display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px;
  border: 1px solid #e3e8f0; border-radius: 8px; background: #fff; color: #606266;
  font-size: 12px; cursor: pointer;
}
.tool-toggle:hover:not(:disabled) { border-color: #409eff; color: #409eff; }
.tool-toggle.on { background: #e7f0fe; border-color: #409eff; color: #1d63e0; }
.tool-toggle:disabled { opacity: 0.5; cursor: not-allowed; }
.tool-left :deep(.el-upload) { display: inline-block; }
.send-btn {
  width: 34px; height: 34px; border-radius: 50%; border: none; flex-shrink: 0;
  background: #409eff; color: #fff; font-size: 16px; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
}
.send-btn:hover:not(:disabled) { background: #2f86e6; }
.send-btn:disabled { background: #c6d6ea; cursor: not-allowed; }
.send-btn--stop { background: #6c63ff; }
.send-btn--stop:hover { background: #5a52d9; }
.composer-hint { max-width: 860px; margin: 6px auto 0; text-align: center; font-size: 11px; color: #a8b0bb; }

/* 步骤画布抽屉 */
.canvas-panel {
  width: 430px; min-width: 360px; border-left: 1px solid #e4e7ed; background: #fafbfc;
  display: flex; flex-direction: column; overflow: hidden;
}
.canvas-head {
  display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  border-bottom: 1px solid #e4e7ed; background: #fff; flex-shrink: 0;
}
.canvas-head b { font-size: 14px; color: #1f2733; }
.canvas-sub { flex: 1; font-size: 12px; color: #909399; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.canvas-body { flex: 1; overflow: auto; }
.canvas-enter-active, .canvas-leave-active { transition: width 0.18s ease, min-width 0.18s ease; }
.canvas-enter-from, .canvas-leave-to { width: 0; min-width: 0; }
</style>
