<script setup lang="ts">
/**
 * 公共脚本（设计文档 2.4）：前后置处理器 = 声明式步骤链，不是一段任意 Python。
 *
 * 表单由后端 /api/scripts/meta 驱动（动作白名单、字段集合、各类上限都取自同一份定义），
 * 所以「页面能填的」与「执行期认的」永远一致；非法定义一律由后端 400 兜底并回具体原因。
 * 引用保护：被用例「前置脚本 / 后置脚本」列引用的脚本不可删除、不可改名（后端 409）。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import type { ScriptItem, ScriptMeta, ScriptStep } from "../types";

const items = ref<ScriptItem[]>([]);
const meta = ref<ScriptMeta | null>(null);
const loading = ref(false);

interface KvRow { key: string; value: string }
interface AssertRow { op: string; path: string; expected: string }

/** 步骤的编辑态：复杂值（headers/body/vars）以文本或行数组承载，保存时再序列化 */
interface StepDraft {
  action: string;
  desc: string;
  method: string;
  url: string;
  timeout: string;
  body: string;
  hdrRows: KvRow[];
  prmRows: KvRow[];
  extRows: KvRow[];
  asrtRows: AssertRow[];
  varRows: KvRow[];
  seconds: string;
}

const ACTION_ICON: Record<string, string> = { request: "🌐", sleep: "⏳", set_var: "🏷", assert: "✅" };
const OP_DESC: Record<string, string> = {
  eq: "等于", neq: "不等于", contains: "包含", gt: "大于", ge: "大于等于",
  lt: "小于", le: "小于等于", regex: "正则匹配", not_null: "非空", len_eq: "长度等于",
};

const VAR_PH = "值，支持 {{变量}} 引用";
const PATH_PH = "status_code / $.data.id / var.token";

/* ================= 列表 ================= */
async function loadList() {
  loading.value = true;
  try {
    items.value = await api.listScripts();
  } finally {
    loading.value = false;
  }
}

onMounted(async () => {
  loadList();
  try {
    meta.value = await api.getScriptMeta();
  } catch { /* 元数据取不到时表单退化为全字段可填，落盘仍由后端校验 */ }
});

const actionOptions = computed(
  () => meta.value?.actions ?? [
    { action: "request", desc: "发一次请求（可提取变量、可带断言）", fields: [] },
    { action: "sleep", desc: "等待若干秒（用于异步任务落库）", fields: [] },
    { action: "set_var", desc: "写入变量池（值支持 {{变量}} 占位符）", fields: [] },
    { action: "assert", desc: "断言上一条响应或变量池（var.xxx）", fields: [] },
  ]
);
const stepLimit = () => meta.value?.step_limit ?? 20;
const sleepLimit = () => meta.value?.sleep_limit ?? 30;
const nameMax = () => meta.value?.name_max ?? 50;
const forbiddenChars = () => meta.value?.name_forbidden ?? "，, \t\"'/\\[]{}";
const methodOptions = () => meta.value?.methods ?? ["GET", "POST", "PUT", "DELETE", "PATCH"];
const assertOps = () => meta.value?.assert_ops ?? ["eq", "neq", "contains", "gt", "ge", "lt", "le", "regex", "not_null", "len_eq"];
const opNeedExpected = (op: string) =>
  meta.value ? meta.value.ops_need_expected.includes(op) : op !== "not_null";

/** 字段是否属于该动作：meta 缺失时放行（真校验在后端落盘那一刻） */
function hasField(action: string, field: string) {
  const hit = meta.value?.actions.find((a) => a.action === action);
  return hit ? hit.fields.includes(field) : true;
}

const actionLabel = (a: string) => `${ACTION_ICON[a] ?? "•"} ${a}`;
const actionDesc = (a: string) => actionOptions.value.find((o) => o.action === a)?.desc ?? "";
const stepPreview = (s: ScriptItem) => s.steps.map((st) => st.action).join(" → ");

/* ================= 抽屉：新建 / 编辑 ================= */
const drawerVisible = ref(false);
const isEdit = ref(false);
const saving = ref(false);
const origName = ref("");
const origUsed = ref<string[]>([]);
const form = reactive({ name: "", remark: "" });
const steps = ref<StepDraft[]>([]);

/* 「粘贴 JSON 导入」：把整段步骤定义一次贴入，解析回填成步骤后仍走同套校验保存 */
const importVisible = ref(false);
const importText = ref("");

const errMsg = (m: string) => { ElMessage.error(m); return null; };

const objToRows = (o: Record<string, unknown> | undefined | null): KvRow[] =>
  Object.entries(o ?? {}).map(([key, value]) => ({
    key,
    value: typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? ""),
  }));

function rowsToObj(rows: KvRow[], label: string): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const r of rows) {
    const k = r.key.trim();
    if (!k) continue;
    if (k in out) return errMsg(`${label} 存在重复的键：${k}`);
    out[k] = r.value;
  }
  return out;
}

const parseExpected = (s: string): unknown => {
  const t = s.trim();
  if (/^-?\d+(\.\d+)?$/.test(t)) return Number(t);
  if (t === "true") return true;
  if (t === "false") return false;
  return t;
};

const rulesToRows = (arr: unknown): AssertRow[] =>
  (Array.isArray(arr) ? arr : []).map((a) => {
    const r = Array.isArray(a) ? a : [];
    return { op: String(r[0] ?? ""), path: String(r[1] ?? ""), expected: r[2] == null ? "" : String(r[2]) };
  });

function rowsToRules(rows: AssertRow[], where: string): unknown[][] | null {
  const out: unknown[][] = [];
  for (const [i, r] of rows.entries()) {
    if (!r.op) return errMsg(`${where} 第 ${i + 1} 条断言缺少操作符`);
    if (!r.path.trim()) return errMsg(`${where} 第 ${i + 1} 条断言缺少取值路径`);
    out.push(opNeedExpected(r.op) ? [r.op, r.path.trim(), parseExpected(r.expected)] : [r.op, r.path.trim()]);
  }
  return out;
}

const jsonText = (v: unknown) => (v == null || v === "" ? "" : typeof v === "string" ? v : JSON.stringify(v, null, 2));

function emptyStep(action = "request"): StepDraft {
  return {
    action, desc: "", method: "GET", url: "", timeout: "", body: "",
    hdrRows: [], prmRows: [], extRows: [], asrtRows: [], varRows: [], seconds: "1",
  };
}

function toDraft(s: ScriptStep): StepDraft {
  const d = emptyStep(s.action);
  d.desc = s.desc ?? "";
  d.method = s.method ?? "GET";
  d.url = s.url ?? "";
  d.timeout = s.timeout == null ? "" : String(s.timeout);
  d.body = jsonText(s.body);
  d.hdrRows = objToRows(s.headers);
  d.prmRows = objToRows(s.params);
  d.extRows = objToRows(s.extract);
  d.varRows = objToRows(s.vars);
  d.asrtRows = rulesToRows(s.assert);
  d.seconds = s.seconds == null ? "1" : String(s.seconds);
  return d;
}

/** 编辑态 → 后端定义：只输出该动作支持的字段，空值一律省略（与 core.scripts 的归一化口径一致） */
function toServer(d: StepDraft, i: number): ScriptStep | null {
  const where = `第 ${i + 1} 步（${d.action}）`;
  const out: ScriptStep = { action: d.action };
  if (d.desc.trim()) out.desc = d.desc.trim();

  if (d.action === "request") {
    if (!d.method) return errMsg(`${where} 请选择请求方式`);
    if (!d.url.trim()) return errMsg(`${where} 请求路径不能为空`);
    out.method = d.method;
    out.url = d.url.trim();
    const kvSpec: Array<[KvRow[], string, keyof ScriptStep]> = [
      [d.hdrRows, "请求头", "headers"],
      [d.prmRows, "查询参数", "params"],
      [d.extRows, "变量提取", "extract"],
    ];
    for (const [rows, label, key] of kvSpec) {
      if (!hasField(d.action, key as string)) continue;
      const obj = rowsToObj(rows, `${where} ${label}`);
      if (obj === null) return null;
      if (Object.keys(obj).length) (out as Record<string, unknown>)[key] = obj;
    }
    if (d.body.trim() && hasField(d.action, "body")) {
      try { out.body = JSON.parse(d.body); }
      catch { return errMsg(`${where} 请求体不是合法 JSON`); }
    }
    if (d.timeout.trim() && hasField(d.action, "timeout")) {
      if (!(Number.isFinite(Number(d.timeout)) && Number(d.timeout) > 0)) {
        return errMsg(`${where} 超时必须为正数（秒）`);
      }
      out.timeout = Number(d.timeout);
    }
    const rules = rowsToRules(d.asrtRows, where);
    if (rules === null) return null;
    if (rules.length) out.assert = rules;
    return out;
  }

  if (d.action === "sleep") {
    const v = Number(d.seconds);
    if (!Number.isFinite(v)) return errMsg(`${where} 等待秒数必须是数字`);
    if (v < 0 || v > sleepLimit()) return errMsg(`${where} 等待秒数须在 0~${sleepLimit()} 之间`);
    out.seconds = v;
    return out;
  }

  if (d.action === "set_var") {
    const obj = rowsToObj(d.varRows, `${where} 变量`);
    if (obj === null) return null;
    if (!Object.keys(obj).length) return errMsg(`${where} 至少填写一个变量（名/值）`);
    out.vars = obj;
    return out;
  }

  const rules = rowsToRules(d.asrtRows, where);
  if (rules === null) return null;
  if (!rules.length) return errMsg(`${where} 至少需要一条断言规则`);
  out.assert = rules;
  return out;
}

function openCreate(preset?: ScriptStep[]) {
  isEdit.value = false;
  origName.value = "";
  origUsed.value = [];
  form.name = "";
  form.remark = "";
  steps.value = preset?.length
    ? preset.map(toDraft)
    : [toDraft({ action: "request", method: "GET", url: "/api/health", desc: "占位：改成你的登录/造数请求" } as ScriptStep)];
  drawerVisible.value = true;
}

function openEdit(row: ScriptItem) {
  isEdit.value = true;
  origName.value = row.name;
  origUsed.value = [...row.used_by];
  form.name = row.name;
  form.remark = row.remark;
  steps.value = row.steps.map(toDraft);
  drawerVisible.value = true;
}

function copyAsNew(row: ScriptItem) {
  openCreate(row.steps);
  form.name = `${row.name}_副本`;
  form.remark = row.remark;
}

/** 粘贴新建：清空占位步，直接弹导入框（作为列表页的另一种「新增方式」） */
function createByImport() {
  openCreate();
  steps.value = [];
  importText.value = "";
  importVisible.value = true;
}

/** 从现有步骤链生成 JSON 并填入导入框（便于复制/改写/版本化） */
function exportToJson() {
  const arr: ScriptStep[] = [];
  for (const [i, d] of steps.value.entries()) {
    const s = toServer(d, i);
    if (s === null) return; // toServer 已提示错误
    arr.push(s);
  }
  importText.value = JSON.stringify(arr, null, 2);
  importVisible.value = true;
}

const IMPORT_SAMPLE = `[
  {
    "action": "request",
    "desc": "三方鉴权取 neauth",
    "method": "POST",
    "url": "https://.../neauth/query",
    "headers": { "Content-Type": "application/json" },
    "body": { "fromAppKey": "...", "toAppKey": "..." },
    "extract": { "x_yun_app_key": "$.data.x-yun-app-key", "x_yun_neauth": "$.data.x-yun-neauth" }
  }
]`;

/** 从索引处的括号/花括号，返回其配对闭合位置（跳过字符串字面量）；找不到返回 -1 */
function matchBalanced(text: string, openIdx: number): number {
  const openCh = text[openIdx];
  const closeCh = openCh === "{" ? "}" : openCh === "[" ? "]" : openCh === "(" ? ")" : "";
  if (!closeCh) return -1;
  let depth = 0;
  let inStr: string | null = null;
  for (let i = openIdx; i < text.length; i++) {
    const c = text[i];
    if (inStr) {
      if (c === "\\") { i++; continue; }
      if (c === inStr) inStr = null;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") { inStr = c; continue; }
    if (c === openCh) depth++;
    else if (c === closeCh) { depth--; if (depth === 0) return i; }
  }
  return -1;
}

/** 按顶层分隔符切开（嵌套括号/字符串内的分隔符不切） */
function splitTopLevel(s: string, sep: string): string[] {
  const parts: string[] = [];
  let depth = 0; let inStr: string | null = null; let cur = "";
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (inStr) { cur += c; if (c === "\\") { cur += s[++i]; continue; } if (c === inStr) inStr = null; continue; }
    if (c === '"' || c === "'" || c === "`") { inStr = c; cur += c; continue; }
    if (c === "{" || c === "[" || c === "(") { depth++; cur += c; continue; }
    if (c === "}" || c === "]" || c === ")") { depth--; cur += c; continue; }
    if (c === sep && depth === 0) { parts.push(cur); cur = ""; continue; }
    cur += c;
  }
  parts.push(cur);
  return parts;
}

/** 取 `key: "value"` / `key: 'value'` 的字符串值 */
function matchStrVal(t: string, keyRe: RegExp): string | undefined {
  const m = keyRe.exec(t);
  if (!m) return undefined;
  const sm = /^\s*(['"])(.*?)\1/.exec(t.slice(m.index + m[0].length));
  return sm ? sm[2] : undefined;
}

/** 把一段 `"k": "v"` / `k: 'v'` 文本解成字符串键值对 */
function parseStrPairs(t: string): Record<string, string> {
  const out: Record<string, string> = {};
  const re = /['"]?([\w-]+)['"]?\s*:\s*(['"])(.*?)\2/g;
  let m;
  while ((m = re.exec(t))) out[m[1]] = m[3];
  return out;
}

/** 宽松地将 JS 对象字面量修正为 JSON（补引号键 / 单引号转双 / 去尾逗号） */
function jsObjToJson(text: string): string {
  let out = text.replace(/([{,]\s*)([A-Za-z_$][\w$]*)\s*:/g, '$1"$2":');
  out = out.replace(/,\s*([}\]])/g, "$1");
  out = out.replace(/'([^']*)'/g, '"$1"');
  return out;
}

function tryParseJsObj(text: string): unknown {
  try { return JSON.parse(text); } catch { /* 继续宽松解析 */ }
  try { return JSON.parse(jsObjToJson(text)); } catch { return undefined; }
}

/** 把 pm 取值表达式转 JSONPath：bb['data']['x-yun-key'] → $.data['x-yun-key']；res.json().data.id → $.data.id */
function pmExprToJsonPath(expr: string): string {
  let tail = expr;
  const j = expr.search(/\.json\s*\(/);
  if (j >= 0) tail = expr.slice(j);
  else tail = expr.replace(/^[A-Za-z_$][\w$]*/, "");
  const segs: string[] = [];
  const re = /\[\s*(['"])(.*?)\1\s*\]|\.([A-Za-z_$][\w$]*)/g;
  let m;
  while ((m = re.exec(tail))) {
    const seg = m[2] != null ? m[2] : m[3];
    if (seg === "json") continue;
    segs.push(seg);
  }
  if (!segs.length) return "$";
  return "$" + segs.map((s) => (/^\w+$/.test(s) ? "." + s : "['" + s + "']")).join("");
}

/** 从回调体里抽 pm.environment/variables.set("name", expr) → 提取映射（变量名非法字符转下划线） */
function parseApifoxExtracts(cb: string): Record<string, string> {
  const out: Record<string, string> = {};
  const re = /pm\.(?:environment|variables)\.set\s*\(/g;
  let m;
  while ((m = re.exec(cb))) {
    const op = m.index + m[0].length - 1;
    const cl = matchBalanced(cb, op);
    if (cl < 0) continue;
    const args = splitTopLevel(cb.slice(op + 1, cl), ",");
    if (args.length < 2) continue;
    const nameM = /^\s*(['"])(.*?)\1\s*$/.exec(args[0]);
    if (!nameM) continue;
    const key = nameM[2].replace(/\W/g, "_").replace(/^(\d)/, "_$1");
    const jp = pmExprToJsonPath(args[1].trim());
    if (key && jp) out[key] = jp;
  }
  return out;
}

/** 解析单个请求对象字面量 → method/url/headers/body */
function parseApifoxRequest(t: string): ScriptStep | null {
  const url = matchStrVal(t, /\burl\s*:/);
  if (!url) return null;
  const method = (matchStrVal(t, /\bmethod\s*:/) || "GET").toUpperCase();
  const step: ScriptStep = { action: "request", desc: "从 Apifox/Postman 脚本导入", method, url };
  const hm = /\bheaders?\s*:\s*\{/.exec(t);
  if (hm) {
    const ob = t.indexOf("{", hm.index + hm[0].length - 1);
    const cb = matchBalanced(t, ob);
    if (cb >= 0) {
      const h = parseStrPairs(t.slice(ob + 1, cb));
      if (Object.keys(h).length) step.headers = h;
    }
  }
  const bm = /\bbody\s*:\s*\{/.exec(t);
  if (bm) {
    const ob = t.indexOf("{", bm.index + bm[0].length - 1);
    const cb = matchBalanced(t, ob);
    if (cb >= 0) {
      const bodyText = t.slice(ob, cb + 1);
      const jm = /raw\s*:\s*JSON\.stringify\s*\(/.exec(bodyText);
      if (jm) {
        const op = bodyText.indexOf("(", jm.index + jm[0].length - 1);
        const cl = matchBalanced(bodyText, op);
        if (cl >= 0) { const v = tryParseJsObj(bodyText.slice(op + 1, cl)); if (v !== undefined) step.body = v; }
      } else {
        const raw = matchStrVal(bodyText, /\braw\s*:/);
        if (raw) { const v = tryParseJsObj(raw); step.body = v !== undefined ? v : raw; }
      }
    }
  }
  return step;
}

/** 将整段 Apifox/Postman 脚本转为步骤链（每个 pm.sendRequest 一步）；识别不了返回 null */
function convertApifoxScript(src: string): ScriptStep[] | null {
  const re = /pm\.sendRequest\s*\(/g;
  const steps: ScriptStep[] = [];
  let m;
  while ((m = re.exec(src))) {
    const op = m.index + m[0].length - 1;
    const cl = matchBalanced(src, op);
    if (cl < 0) continue;
    const args = splitTopLevel(src.slice(op + 1, cl), ",");
    const reqRef = (args[0] || "").trim();
    const cbText = args[1] || "";
    let reqText = "";
    if (reqRef.startsWith("{")) reqText = reqRef;
    else {
      const idm = reqRef.match(/[A-Za-z_$][\w$]*/);
      if (idm) {
        const dm = new RegExp("(?:const|let|var)?\\s*" + idm[0] + "\\s*=\\s*\\{").exec(src);
        if (dm) {
          const ob = src.indexOf("{", dm.index + dm[0].length - 1);
          const cb = matchBalanced(src, ob);
          if (cb >= 0) reqText = src.slice(ob, cb + 1);
        }
      }
    }
    if (!reqText) continue;
    const step = parseApifoxRequest(reqText);
    if (!step) continue;
    const extract = parseApifoxExtracts(cbText);
    if (Object.keys(extract).length) step.extract = extract;
    steps.push(step);
  }
  return steps.length ? steps : null;
}

/** 统一的回填入口：结构校验 + 转草稿 + 应用名称/说明 */
function fillSteps(arr: ScriptStep[], name = "", remark = "") {
  if (!arr.length) { ElMessage.error("解析结果为空步骤"); return; }
  if (arr.length > stepLimit()) { ElMessage.error(`步骤数超上限 ${stepLimit()}（拆分成多条脚本）`); return; }
  const validActions = actionOptions.value.map((a) => a.action);
  for (const [i, s] of arr.entries()) {
    if (!s || typeof s !== "object") { ElMessage.error(`第 ${i + 1} 步不是 JSON 对象`); return; }
    if (!validActions.includes(s.action)) {
      ElMessage.error(`第 ${i + 1} 步动作「${s.action ?? "(空)"}」不支持，可用：${validActions.join(" / ")}`);
      return;
    }
  }
  steps.value = arr.map(toDraft);
  if (name.trim() && !isEdit.value && !renameLocked.value) form.name = name.trim();
  if (remark.trim()) form.remark = remark.trim();
  importVisible.value = false;
  ElMessage.success(`已填入 ${arr.length} 步，请核对后点「保存脚本」`);
}

/** 解析粘贴内容并回填。优先当 JSON（数组/脚本对象/单步）；失败则尝试当 Apifox/Postman JS 脚本转换 */
function applyImport() {
  const raw = importText.value.trim();
  if (!raw) { ElMessage.error("请先粘贴脚本内容"); return; }
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    const jsSteps = convertApifoxScript(raw);
    if (jsSteps) { fillSteps(jsSteps); return; }
    ElMessage.error("既不是合法 JSON，也无法识别为 Apifox/Postman 脚本（需含 pm.sendRequest）");
    return;
  }
  const obj = data as Record<string, unknown>;
  let stepsArr: unknown;
  let metaName = "";
  let metaRemark = "";
  if (Array.isArray(data)) {
    stepsArr = data;
  } else if (data && typeof data === "object") {
    if (Array.isArray(obj.steps)) {
      stepsArr = obj.steps;
      metaName = typeof obj.name === "string" ? obj.name : "";
      metaRemark = typeof obj.remark === "string" ? obj.remark : "";
    } else if (typeof obj.action === "string") {
      stepsArr = [obj];
    } else {
      ElMessage.error("JSON 需为步骤数组，或含 steps 字段的脚本对象");
      return;
    }
  } else {
    ElMessage.error("JSON 需为步骤数组或脚本对象");
    return;
  }
  fillSteps(stepsArr as ScriptStep[], metaName, metaRemark);
}

const renameLocked = computed(() => isEdit.value && origUsed.value.length > 0);

function addStep() {
  if (steps.value.length >= stepLimit()) {
    ElMessage.warning(`单脚本最多 ${stepLimit()} 步（超出请把流程拆成多条脚本，用例按顺序引用）`);
    return;
  }
  steps.value.push(emptyStep("request"));
}

function moveStep(i: number, delta: number) {
  const j = i + delta;
  if (j < 0 || j >= steps.value.length) return;
  const [s] = steps.value.splice(i, 1);
  steps.value.splice(j, 0, s);
}

async function save() {
  const name = form.name.trim();
  if (!name) { ElMessage.error("脚本名不能为空"); return; }
  if (name.length > nameMax()) { ElMessage.error(`脚本名不能超过 ${nameMax()} 字符`); return; }
  const bad = [...forbiddenChars()].find((ch) => name.includes(ch));
  if (bad) { ElMessage.error(`脚本名不能包含「${bad}」（用例的 Excel 列以逗号分隔引用脚本名）`); return; }
  if (!steps.value.length) { ElMessage.error("至少需要一个步骤"); return; }

  const payload: ScriptStep[] = [];
  for (const [i, d] of steps.value.entries()) {
    const s = toServer(d, i);
    if (s === null) return;
    payload.push(s);
  }

  saving.value = true;
  try {
    if (isEdit.value) {
      await api.updateScript(origName.value, { name, remark: form.remark.trim(), steps: payload });
      ElMessage.success(`已保存脚本 ${name}（${payload.length} 步）`);
    } else {
      await api.createScript({ name, remark: form.remark.trim(), steps: payload });
      ElMessage.success(`已创建脚本 ${name}（${payload.length} 步）`);
    }
    drawerVisible.value = false;
    await loadList();
  } catch {
    /* 拦截器已提示后端 detail（定义非法 400 / 改名破坏引用 409 / 无权限 403） */
  } finally {
    saving.value = false;
  }
}

async function remove(row: ScriptItem) {
  if (row.used_by.length) {
    ElMessage.warning(`脚本 ${row.name} 被 ${row.used_by.length} 条用例引用，需先清空用例的脚本列`);
    return;
  }
  try {
    await ElMessageBox.confirm(
      `确认删除脚本「${row.name}」？删除后无法恢复（当前无用例引用）。`,
      "删除公共脚本",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch { return; }
  try {
    await api.deleteScript(row.name);
    ElMessage.success(`已删除 ${row.name}`);
    await loadList();
  } catch { /* 拦截器已提示 */ }
}
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <div class="head-tip">
        <b>公共脚本</b>：把「登录取 token、造数、清理数据」沉淀成可复用的步骤链，用例在
        <span class="mono">前置脚本 / 后置脚本</span> 两列按名引用；前置失败即中断（不发主请求），后置无论主流程成败都会执行。
      </div>
      <div class="header-ops">
        <el-button size="small" @click="loadList">刷新</el-button>
        <el-button v-perm="'script:manage'" size="small" @click="createByImport">✂ 粘贴新建</el-button>
        <el-button v-perm="'script:manage'" type="primary" size="small" @click="openCreate()">＋ 新建脚本</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" class="rule-alert">
      安全边界：步骤只支持声明式动作（{{ actionOptions.map((a) => a.action).join(" / ") }}），不执行任意代码；
      token/password 之类的变量值在报告与日志里一律掩码。单脚本上限 {{ stepLimit() }} 步，单步等待上限 {{ sleepLimit() }} 秒。
    </el-alert>

    <el-table :data="items" size="small" row-key="name">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="step-view">
            <div v-for="(s, i) in row.steps" :key="i" class="step-item">
              <el-tag size="small" effect="plain">{{ i + 1 }}</el-tag>
              <span class="step-act mono">{{ s.action }}</span>
              <span v-if="s.desc" class="step-desc">{{ s.desc }}</span>
              <span class="step-body mono">
                <template v-if="s.action === 'request'">{{ s.method }} {{ s.url }}</template>
                <template v-else-if="s.action === 'sleep'">等待 {{ s.seconds }}s</template>
                <template v-else-if="s.action === 'set_var'">{{ Object.keys(s.vars || {}).join(", ") }}</template>
                <template v-else>{{ (s.assert || []).length }} 条断言</template>
              </span>
            </div>
            <div v-if="row.used_by.length" class="used-tip">
              被 {{ row.used_by.length }} 条用例引用：{{ row.used_by.join(", ") }}
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="脚本名" min-width="180">
        <template #default="{ row }"><span class="mono">{{ row.name }}</span></template>
      </el-table-column>
      <el-table-column prop="remark" label="用途说明" min-width="200" show-overflow-tooltip />
      <el-table-column label="步骤链" min-width="240">
        <template #default="{ row }">
          <span class="mono step-chain">{{ stepPreview(row) }}</span>
          <span class="step-count">（{{ row.steps.length }} 步）</span>
        </template>
      </el-table-column>
      <el-table-column label="引用" width="120">
        <template #default="{ row }">
          <el-tag v-if="row.used_by.length" size="small" type="warning">{{ row.used_by.length }} 条用例</el-tag>
          <el-tag v-else size="small" type="info" effect="plain">未引用</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="updated_at" label="更新时间" width="180" />
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <div class="op-cell">
            <el-button v-perm="'script:manage'" size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button v-perm="'script:manage'" size="small" text type="primary" @click="copyAsNew(row)">复制</el-button>
            <el-tooltip v-if="row.used_by.length" content="被用例引用，删除会被后端拒绝；请先清空用例的脚本列" placement="top">
              <el-button v-perm="'script:manage'" size="small" text type="danger" disabled>删除</el-button>
            </el-tooltip>
            <el-button v-else v-perm="'script:manage'" size="small" text type="danger" @click="remove(row)">删除</el-button>
          </div>
        </template>
      </el-table-column>
      <template #empty>
        <div style="padding: 20px; color: #909399">
          还没有公共脚本。新建一条「登录取 token」，再在用例里把它填进「前置脚本」列，即可跨用例复用。
        </div>
      </template>
    </el-table>

    <!-- 步骤链编辑器抽屉 -->
    <el-drawer v-model="drawerVisible" size="58%" :title="isEdit ? `编辑脚本 ${origName}` : '新建脚本'">
      <el-form label-width="80px" @submit.prevent>
        <el-form-item label="脚本名">
          <el-input v-model="form.name" class="mono" :disabled="renameLocked"
                    placeholder="唯一名称，用例按名引用（不能含逗号/空格，建议中文或下划线）" />
          <div class="hint">
            用例的「前置脚本 / 后置脚本」列以逗号分隔脚本名，故名称内不能出现
            <span class="mono">{{ forbiddenChars().replace(/\s/g, " ") || "，, /\"'[]{}" }}</span>。
            <template v-if="renameLocked">
              <b style="color: #e6a23c">本脚本被 {{ origUsed.length }} 条用例引用，改名会断链，已锁定。</b>
            </template>
          </div>
        </el-form-item>
        <el-form-item label="用途说明">
          <el-input v-model="form.remark" type="textarea" :rows="2" placeholder="这条脚本解决什么复用问题" />
        </el-form-item>

        <el-divider content-position="left">
          步骤链（{{ steps.length }} / {{ stepLimit() }} 步 · 按序执行）
        </el-divider>

        <div class="step-tools">
          <el-button size="small" text type="primary" @click="importText = ''; importVisible = true">✂ 粘贴 JSON 导入</el-button>
          <el-button size="small" text :disabled="!steps.length" @click="exportToJson">⬇ 导出为 JSON</el-button>
          <span class="dim">不想逐步填？把整段步骤定义贴进来，自动解析成下面的步骤。</span>
        </div>

        <div v-for="(s, i) in steps" :key="i" class="step-card">
          <div class="step-head">
            <el-tag size="small" effect="dark" type="primary">{{ i + 1 }}</el-tag>
            <el-select v-model="s.action" style="width: 150px">
              <el-option v-for="o in actionOptions" :key="o.action" :value="o.action" :label="actionLabel(o.action)">
                <span class="opt-key">{{ actionLabel(o.action) }}</span>
                <span class="opt-val">{{ o.desc }}</span>
              </el-option>
            </el-select>
            <el-input v-model="s.desc" placeholder="该步做什么（可选，写清楚点）" style="flex: 1" />
            <el-button size="small" text :disabled="i === 0" @click="moveStep(i, -1)">↑</el-button>
            <el-button size="small" text :disabled="i === steps.length - 1" @click="moveStep(i, 1)">↓</el-button>
            <el-button size="small" text type="danger" @click="steps.splice(i, 1)">✕</el-button>
          </div>
          <div class="step-lead">{{ actionDesc(s.action) }}</div>

          <!-- request -->
          <template v-if="s.action === 'request'">
            <div class="kv-row">
              <el-select v-model="s.method" placeholder="方法" style="width: 110px">
                <el-option v-for="m in methodOptions()" :key="m" :value="m" :label="m" />
              </el-select>
              <el-input v-model="s.url" class="mono" placeholder="请求路径，如 /auth/login 或 {{base_url}}/x" style="flex: 1" />
              <el-input v-model="s.timeout" class="mono" placeholder="超时(秒)" style="width: 110px" />
            </div>
            <div v-for="(h, hi) in s.hdrRows" :key="'h' + hi" class="kv-row">
              <el-input v-model="h.key" placeholder="Header 名" class="mono" style="width: 190px" />
              <el-input v-model="h.value" :placeholder="VAR_PH" class="mono" style="flex: 1" />
              <el-button text type="danger" @click="s.hdrRows.splice(hi, 1)">✕</el-button>
            </div>
            <el-button size="small" text @click="s.hdrRows.push({ key: '', value: '' })">＋ 请求头</el-button>
            <div v-for="(p, pi) in s.prmRows" :key="'p' + pi" class="kv-row">
              <el-input v-model="p.key" placeholder="参数名" class="mono" style="width: 190px" />
              <el-input v-model="p.value" :placeholder="VAR_PH" class="mono" style="flex: 1" />
              <el-button text type="danger" @click="s.prmRows.splice(pi, 1)">✕</el-button>
            </div>
            <el-button size="small" text @click="s.prmRows.push({ key: '', value: '' })">＋ 查询参数</el-button>
            <div class="sub-label">请求体（JSON，留空 = 不发送）</div>
            <el-input v-model="s.body" type="textarea" :rows="3" class="mono" spellcheck="false" />
            <div class="sub-label">变量提取（变量名 ← 响应 JSONPath，写入本用例的变量池）</div>
            <div v-for="(x, xi) in s.extRows" :key="'x' + xi" class="kv-row">
              <el-input v-model="x.key" placeholder="变量名" class="mono" style="width: 190px" />
              <el-input v-model="x.value" :placeholder="PATH_PH" class="mono" style="flex: 1" />
              <el-button text type="danger" @click="s.extRows.splice(xi, 1)">✕</el-button>
            </div>
            <el-button size="small" text @click="s.extRows.push({ key: '', value: '' })">＋ 提取</el-button>
            <div class="sub-label">断言（可选，对这一步的响应生效）</div>
            <div v-for="(a, ai) in s.asrtRows" :key="'a' + ai" class="kv-row">
              <el-select v-model="a.op" placeholder="操作符" style="width: 170px">
                <el-option v-for="o in assertOps()" :key="o" :value="o" :label="`${o} ${OP_DESC[o] ?? ''}`" />
              </el-select>
              <el-input v-model="a.path" :placeholder="PATH_PH" class="mono" style="flex: 1" />
              <el-input v-if="opNeedExpected(a.op)" v-model="a.expected" placeholder="期望值" class="mono" style="width: 170px" />
              <el-button text type="danger" @click="s.asrtRows.splice(ai, 1)">✕</el-button>
            </div>
            <el-button size="small" text @click="s.asrtRows.push({ op: 'eq', path: 'status_code', expected: '200' })">＋ 断言</el-button>
          </template>

          <!-- sleep -->
          <template v-else-if="s.action === 'sleep'">
            <div class="kv-row">
              <el-input v-model="s.seconds" class="mono" :placeholder="`秒，0~${sleepLimit()}`" style="width: 190px" />
              <span class="dim">用于等待异步任务落库；超过上限说明流程该拆分。</span>
            </div>
          </template>

          <!-- set_var -->
          <template v-else-if="s.action === 'set_var'">
            <div v-for="(v, vi) in s.varRows" :key="'v' + vi" class="kv-row">
              <el-input v-model="v.key" placeholder="变量名" class="mono" style="width: 190px" />
              <el-input v-model="v.value" :placeholder="VAR_PH" class="mono" style="flex: 1" />
              <el-button text type="danger" @click="s.varRows.splice(vi, 1)">✕</el-button>
            </div>
            <el-button size="small" text @click="s.varRows.push({ key: '', value: '' })">＋ 变量</el-button>
            <div class="hint" style="margin-top: 6px">
              变量名含 token/password/secret/key/auth/cookie 时，报告与日志只保留掩码，绝不落盘明文。
              <template v-if="meta?.variables?.length"> 已有变量：{{ meta.variables.join(", ") }}</template>
            </div>
          </template>

          <!-- assert -->
          <template v-else>
            <div v-for="(a, ai) in s.asrtRows" :key="'a' + ai" class="kv-row">
              <el-select v-model="a.op" placeholder="操作符" style="width: 170px">
                <el-option v-for="o in assertOps()" :key="o" :value="o" :label="`${o} ${OP_DESC[o] ?? ''}`" />
              </el-select>
              <el-input v-model="a.path" :placeholder="PATH_PH" class="mono" style="flex: 1" />
              <el-input v-if="opNeedExpected(a.op)" v-model="a.expected" placeholder="期望值" class="mono" style="width: 170px" />
              <el-button text type="danger" @click="s.asrtRows.splice(ai, 1)">✕</el-button>
            </div>
            <el-button size="small" text @click="s.asrtRows.push({ op: 'eq', path: 'status_code', expected: '200' })">＋ 断言</el-button>
            <div class="hint" style="margin-top: 6px">
              校验对象是上一条 request 步的响应；路径以 <span class="mono">var.</span> 开头时取变量池（不依赖响应）。
            </div>
          </template>
        </div>

        <el-button size="small" style="margin-bottom: 12px" @click="addStep">＋ 添加步骤</el-button>

        <div class="hint" style="margin: 6px 0 12px">
          保存即落盘 data/scripts.json；用例在 Excel「前置脚本 / 后置脚本」列写脚本名（逗号分隔）即引用。
          执行期仍会用同一套校验，改动后建议先跑一条引用它的用例。
        </div>
        <el-button @click="drawerVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存脚本</el-button>
      </el-form>

      <!-- 粘贴 JSON 导入对话框 -->
      <el-dialog v-model="importVisible" append-to-body width="640px"
                 :title="isEdit ? '粘贴 JSON 覆盖当前步骤' : '粘贴 JSON 导入'">
        <div class="hint" style="margin-bottom: 8px">
          支持两类粘体：① <span class="mono">JSON</span>（步骤数组 / <span class="mono">{ name, remark, steps:[...] }</span> / 单步对象）；
          ② <span class="mono">Apifox/Postman 脚本</span>（自动识别 <span class="mono">pm.sendRequest</span> 与 <span class="mono">pm.environment.set</span>，连字符变量名自动转下划线）。
          解析后回填成步骤供你核对，仍需点「保存脚本」才落盘；动作与字段合法性由后端同一套校验兜底。
        </div>
        <el-input v-model="importText" type="textarea" :rows="14" class="mono" spellcheck="false"
                  :placeholder="IMPORT_SAMPLE" />
        <template #footer>
          <el-button @click="importVisible = false">取消</el-button>
          <el-button @click="importText = IMPORT_SAMPLE">填入示例</el-button>
          <el-button type="primary" @click="applyImport">解析并填入</el-button>
        </template>
      </el-dialog>
    </el-drawer>
  </div>
</template>

<style scoped>
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.head-tip { font-size: 13px; color: #606266; line-height: 1.7; }
.header-ops { display: flex; gap: 8px; flex: 0 0 auto; }
.rule-alert { margin-bottom: 12px; line-height: 1.7; font-size: 12px; }
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.step-chain { color: #303133; }
.step-count { color: #909399; font-size: 12px; margin-left: 4px; }
.op-cell { display: flex; align-items: center; gap: 2px; white-space: nowrap; }
.op-cell .el-button + .el-button { margin-left: 0; }
.hint { color: #909399; font-size: 12px; line-height: 1.6; }
.dim { color: #909399; font-size: 12px; }
.opt-key { float: left; }
.opt-val { float: right; color: #a0a4a8; font-size: 12px; margin-left: 16px; }
/* 步骤卡片 */
.step-card { border: 1px solid #e4e7ed; border-radius: 8px; padding: 10px 12px; margin-bottom: 10px; background: #fafbfc; }
.step-tools { display: flex; align-items: center; gap: 10px; margin: -4px 0 12px; }
.step-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.step-lead { color: #a8abb2; font-size: 12px; margin-bottom: 8px; }
.kv-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.sub-label { color: #909399; font-size: 12px; margin: 10px 0 6px; }
/* 表格展开区 */
.step-view { padding: 6px 24px; }
.step-item { display: flex; align-items: center; gap: 10px; padding: 4px 0; border-bottom: 1px dashed #ebeef5; }
.step-act { color: #409eff; min-width: 70px; }
.step-desc { color: #606266; }
.step-body { color: #909399; margin-left: auto; }
.used-tip { margin-top: 8px; font-size: 12px; color: #e6a23c; }
</style>
