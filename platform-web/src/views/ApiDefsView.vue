<script setup lang="ts">
/**
 * 接口定义管理（设计文档 2.2）：接口库沉淀 + OpenAPI/Swagger 导入 + 生成用例用例骨架。
 *
 * 一句话定位：这里管的是「元数据」不是「执行体」——用例行始终自带 方法/URL/断言，
 * 删掉一条定义不会让任何批次失败；它换来的是同名接口的覆盖视图、断言建议与一键骨架。
 * 引用保护：接口名称被用例同名引用时不可改名、不可删除（后端 409），防止覆盖统计静默失联。
 * 表单常量（方法/操作符/长度上限/导入限额）全部来自 /api/defs/meta，与后端校验同源。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import type {
  AggregateResult, ApiDefCreateReq, ApiDefItem, ApiDefMeta,
  OpenApiImportResult, SkeletonCase,
} from "../types";

const router = useRouter();

const items = ref<ApiDefItem[]>([]);
const meta = ref<ApiDefMeta | null>(null);
const loading = ref(false);
const filters = reactive({ keyword: "", module: "" });

interface KvRow { key: string; value: string }
interface AssertRow { op: string; path: string; expected: string }

const OP_DESC: Record<string, string> = {
  eq: "等于", neq: "不等于", contains: "包含", gt: "大于", ge: "大于等于",
  lt: "小于", le: "小于等于", regex: "正则匹配", not_null: "非空", len_eq: "长度等于",
};
const METHOD_TYPE: Record<string, "success" | "primary" | "warning" | "danger" | "info"> = {
  GET: "success", POST: "primary", PUT: "warning", DELETE: "danger", PATCH: "info",
};
const SOURCE_LABEL: Record<string, string> = { manual: "手工录入", excel: "Excel 聚合", openapi: "OpenAPI 导入" };
const SOURCE_TYPE: Record<string, "success" | "warning" | "info"> = { manual: "info", excel: "success", openapi: "warning" };

const PATH_PH = "以 / 开头（base_url 由环境提供），路径参数写 {{id}}";
const VAR_PH = "值，支持 {{变量}} 引用";
const ASSERT_PATH_PH = "status_code / $.data.code / var.token";
// 占位符字面量走变量输出，避开模板里 {{ }} 插值语法本身的歧义
const PH_ID = "{{id}}";
const PH_VAR = "{{变量}}";
const PH_TOKEN = "{{token}}";
const SKELETON_MAX_ROWS = 20;
// 接口名要能安全写进 /api/defs/{name}：含这些字符会被服务端当成多一段路径（后端同名拒收）
const NAME_BAD_CHARS = ["/", "\\", "?", "#"];

/* ================= 元数据兜底（拿不到 meta 也不至于把表单锁死） ================= */
const methods = computed(() => meta.value?.methods ?? ["DELETE", "GET", "PATCH", "POST", "PUT"]);
const assertOps = computed(() =>
  meta.value?.assert_ops?.map((o) => o.op)
  ?? ["contains", "eq", "ge", "gt", "le", "len_eq", "lt", "neq", "not_null", "regex"]);
const nameMax = () => meta.value?.name_max ?? 60;
const remarkMax = () => meta.value?.remark_max ?? 200;
const assertMax = () => meta.value?.assert_max ?? 10;
const specLimitMb = () => meta.value?.spec_limit_mb ?? 4;
const defaultAsserts = (): AssertRow[] =>
  (meta.value?.default_asserts ?? [["eq", "status_code", 200]]).map((r) => ({
    op: String(r[0] ?? "eq"), path: String(r[1] ?? "status_code"), expected: r[2] == null ? "" : String(r[2]),
  }));
const opNeedExpected = (op: string) =>
  meta.value ? meta.value.ops_need_expected.includes(op) : op !== "not_null";

/* ================= 列表 ================= */
async function loadList() {
  loading.value = true;
  try {
    items.value = await api.listApiDefs();
  } finally {
    loading.value = false;
  }
}

onMounted(async () => {
  loadList();
  try {
    meta.value = await api.getApiDefMeta();
  } catch { /* 元数据缺失只影响候选提示，不阻塞浏览 */ }
});

const moduleOptions = computed(() => {
  const set = new Set<string>(meta.value?.modules ?? []);
  items.value.forEach((i) => set.add(i.module));
  return [...set].filter(Boolean).sort();
});

const filtered = computed(() => {
  const kw = filters.keyword.trim().toLowerCase();
  return items.value.filter((i) => {
    if (filters.module && i.module !== filters.module) return false;
    if (!kw) return true;
    return [i.name, i.path, i.method, i.remark, ...i.used_by].join(" ").toLowerCase().includes(kw);
  });
});

const covered = computed(() => items.value.filter((i) => i.used_by.length).length);
const assertCount = (d: ApiDefItem) => d.asserts?.length ?? 0;
const sourceLabel = (s: string) => SOURCE_LABEL[s] ?? s;
const jsonText = (v: unknown) => (v == null || v === "" ? "" : typeof v === "string" ? v : JSON.stringify(v, null, 2));

async function confirmBox(title: string, html: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(html, title, {
      type: "warning", dangerouslyUseHTMLString: true,
      confirmButtonText: "继续", cancelButtonText: "返回",
    });
    return true;
  } catch {
    return false;
  }
}

/* ================= 抽屉：新建 / 编辑 ================= */
const drawerVisible = ref(false);
const isEdit = ref(false);
const saving = ref(false);
const origName = ref("");
const origUsed = ref<string[]>([]);
const form = reactive({ name: "", module: "", method: "POST", path: "", remark: "" });
const hdrRows = ref<KvRow[]>([]);
const prmRows = ref<KvRow[]>([]);
const extRows = ref<KvRow[]>([]);
const asrtRows = ref<AssertRow[]>([]);
const bodyText = ref("");

const renameLocked = computed(() => isEdit.value && origUsed.value.length > 0);
const errMsg = (m: string) => { ElMessage.error(m); return null; };

const objToRows = (o: Record<string, unknown> | null | undefined): KvRow[] =>
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

const rulesToRows = (arr: unknown): AssertRow[] =>
  (Array.isArray(arr) ? arr : []).map((a) => {
    const r = Array.isArray(a) ? a : [];
    return { op: String(r[0] ?? ""), path: String(r[1] ?? ""), expected: r[2] == null ? "" : String(r[2]) };
  });

const parseExpected = (s: string): unknown => {
  const t = s.trim();
  if (/^-?\d+(\.\d+)?$/.test(t)) return Number(t);
  if (t === "true") return true;
  if (t === "false") return false;
  return t;
};

function rowsToRules(rows: AssertRow[]): unknown[][] | null {
  if (rows.length > assertMax()) return errMsg(`断言建议最多 ${assertMax()} 条，当前 ${rows.length} 条`);
  const out: unknown[][] = [];
  for (const [i, r] of rows.entries()) {
    if (!r.op) return errMsg(`第 ${i + 1} 条断言缺少操作符`);
    if (!r.path.trim()) return errMsg(`第 ${i + 1} 条断言缺少取值路径`);
    out.push(opNeedExpected(r.op) ? [r.op, r.path.trim(), parseExpected(r.expected)] : [r.op, r.path.trim()]);
  }
  return out;
}

function resetForm() {
  form.name = ""; form.module = ""; form.method = "POST"; form.path = ""; form.remark = "";
  hdrRows.value = []; prmRows.value = []; extRows.value = [];
  asrtRows.value = defaultAsserts(); bodyText.value = "";
}

function openCreate() {
  isEdit.value = false;
  origName.value = ""; origUsed.value = [];
  resetForm();
  drawerVisible.value = true;
}

function openEdit(row: ApiDefItem) {
  isEdit.value = true;
  origName.value = row.name; origUsed.value = row.used_by;
  form.name = row.name; form.module = row.module; form.method = row.method;
  form.path = row.path; form.remark = row.remark;
  hdrRows.value = objToRows(row.headers);
  prmRows.value = objToRows(row.params);
  extRows.value = objToRows(row.extract);
  asrtRows.value = rulesToRows(row.asserts);
  bodyText.value = jsonText(row.body);
  drawerVisible.value = true;
}

/** 复制新建：带过全部要素，只把名称清空——同一接口的第二种写法常从复制开始 */
function copyAsNew(row: ApiDefItem) {
  openEdit(row);
  isEdit.value = false;
  origName.value = ""; origUsed.value = [];
  form.name = "";
}

async function save() {
  const name = form.name.trim();
  if (!name) return errMsg("接口名称不能为空");
  if (name.length > nameMax()) return errMsg(`接口名称最多 ${nameMax()} 字`);
  const badChars = NAME_BAD_CHARS.filter((c) => name.includes(c));
  if (badChars.length) {
    return errMsg(`接口名称不能含 ${badChars.join(" ")}：接口库按名称直接寻址，请用连字符代替`);
  }
  const path = form.path.trim();
  if (!path) return errMsg("请求路径不能为空");
  if (!path.startsWith("/") && !/^https?:\/\//.test(path)) return errMsg("请求路径要以 / 或 http(s):// 开头");
  if (/\s/.test(path)) return errMsg("请求路径不能含空格（参数请写进查询参数区）");

  const headers = rowsToObj(hdrRows.value, "请求头");
  const params = rowsToObj(prmRows.value, "查询参数");
  const extract = rowsToObj(extRows.value, "变量提取");
  const asserts = rowsToRules(asrtRows.value);
  if (headers === null || params === null || extract === null || asserts === null) return;

  let body: unknown = null;
  if (bodyText.value.trim()) {
    try { body = JSON.parse(bodyText.value); }
    catch { return errMsg("请求体示例不是合法 JSON"); }
  }

  const payload: ApiDefCreateReq = {
    name,
    method: form.method,
    path,
    module: form.module.trim() || "未分类",
    remark: form.remark.trim(),
    headers, params, extract,
    asserts: asserts.length ? asserts : null,
    body,
  };
  saving.value = true;
  try {
    if (isEdit.value && renameLocked.value && payload.name !== origName.value) {
      ElMessage.warning("该接口被用例同名引用，请先改用例的接口名称列再重命名");
      return;
    }
    if (isEdit.value) {
      await api.updateApiDef(origName.value, payload);
      ElMessage.success(`接口「${payload.name}」已保存`);
    } else {
      await api.createApiDef(payload);
      ElMessage.success(`接口「${payload.name}」已登记`);
    }
    drawerVisible.value = false;
    loadList();
  } catch { /* 归一化错误提示已在 axios 拦截器里给出（含 409 引用保护） */ }
  finally { saving.value = false; }
}

async function remove(row: ApiDefItem) {
  if (row.used_by.length) {
    ElMessage.warning(`「${row.name}」被 ${row.used_by.length} 条用例同名引用，删除会被后端拒绝`);
    return;
  }
  if (!(await confirmBox("删除接口定义",
    `删除 <b>${row.name}</b>（${row.method} ${row.path}）。<br>只影响接口库的覆盖统计与断言建议，<b>不会动任何用例行</b>。`))) return;
  try {
    await api.deleteApiDef(row.name);
    ElMessage.success("已删除");
    loadList();
  } catch { /* 409/400 的 detail 已由拦截器提示 */ }
}

/* ================= 从存量用例聚合 ================= */
const aggBusy = ref(false);

async function onAggregate(cmd: string) {
  const overwrite = cmd === "overwrite";
  if (!(await confirmBox("从存量用例聚合接口定义",
    `把 Excel 里的用例按「接口名称」归并成接口定义。<br>
     同名接口合并为一条：方法/路径/请求体取出现最多的写法，断言建议取最常见的几条。<br>
     模式：<b>${overwrite ? "覆盖同名定义（会冲掉手工维护的说明与断言建议）" : "跳过同名定义（只补新接口）"}</b>`))) return;
  aggBusy.value = true;
  try {
    const r: AggregateResult = await api.aggregateApiDefs(overwrite);
    loadList();
    const lines = [
      `用例文件：${r.file}`,
      `扫描 ${r.scanned_cases} 条用例 → 归并 ${r.groups} 个接口分组`,
      `新增 ${r.created}、更新 ${r.updated}、跳过 ${r.skipped}`,
    ];
    if (r.conflicts.length) {
      lines.push(`端点已被其他接口名占用，未覆盖：${r.conflicts.slice(0, 8).join("；")}`);
    }
    if (r.rejected.length) {
      lines.push(`接口名称不合规、已跳过 ${r.rejected.length} 个分组：${r.rejected.slice(0, 5).join("；")}`);
    }
    await ElMessageBox.alert(lines.join("<br>"), "聚合完成",
      { dangerouslyUseHTMLString: true, confirmButtonText: "知道了" });
  } catch { /* 拦截器已提示错误；用户关掉结果框也走这里 */ }
  finally { aggBusy.value = false; }
}

/* ================= OpenAPI / Swagger 导入 ================= */
const impVisible = ref(false);
const impText = ref("");
const impOverwrite = ref(false);
const impBusy = ref(false);
const impResult = ref<OpenApiImportResult | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);

function openImport() {
  impResult.value = null;
  impVisible.value = true;
}

function pickFile() { fileInput.value?.click(); }

async function onPickFile(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0];
  if (!file) return;
  impText.value = await file.text();
  (e.target as HTMLInputElement).value = "";
  ElMessage.success(`已读入 ${file.name}（${(file.size / 1024).toFixed(1)} KB），点「解析并导入」提交`);
}

async function doImport() {
  if (!impText.value.trim()) return ElMessage.warning("请先粘贴或选择 OpenAPI/Swagger 文档内容");
  if (impText.value.length > specLimitMb() * 1024 * 1024) {
    return ElMessage.warning(`文档超过 ${specLimitMb()}MB 上限，请先只保留需要的 paths`);
  }
  impBusy.value = true;
  try {
    impResult.value = await api.importOpenApi(impText.value, impOverwrite.value);
    ElMessage.success(`解析 ${impResult.value.parsed} 个接口：新增 ${impResult.value.created}、更新 ${impResult.value.updated}、跳过 ${impResult.value.skipped}`);
    loadList();
  } catch { /* 解析失败的具体原因由后端 detail 给出 */ }
  finally { impBusy.value = false; }
}

/* ================= 生成用例骨架 ================= */
const skVisible = ref(false);
const skDef = ref<ApiDefItem | null>(null);
const skIds = ref<string[]>([""]);
const skDesc = ref("");
const skPriority = ref("P2");
const skRun = ref(true);
const skPreview = ref<SkeletonCase[] | null>(null);
const skWritten = ref<string[]>([]);
const skErrors = ref<string[]>([]);
const skBusy = ref(false);

function suggestCaseId(d: ApiDefItem): string {
  const seg = d.path.split("/").filter(Boolean).pop() ?? "";
  const ascii = seg.replace(/[^A-Za-z0-9]/g, "");
  const fromName = (d.name.match(/[A-Za-z][A-Za-z0-9_]*/) ?? [""])[0];
  const stem = (ascii || fromName || "api_case").toLowerCase().slice(0, 24);
  return `${stem}_001`;
}

function openSkeleton(row: ApiDefItem) {
  skDef.value = row;
  skIds.value = [suggestCaseId(row)];
  skDesc.value = `${row.name} 正常流程`;
  skPriority.value = "P2";
  skRun.value = true;
  skPreview.value = null;
  skWritten.value = [];
  skErrors.value = [];
  skVisible.value = true;
}

function addSkeletonRow() {
  if (skIds.value.length >= SKELETON_MAX_ROWS) return ElMessage.warning(`一次最多生成 ${SKELETON_MAX_ROWS} 条`);
  skIds.value.push("");
}

function payloadCheck(): string[] | null {
  const raw = skIds.value.map((s) => s.trim());
  if (raw.some((s) => !s)) { ElMessage.warning("存在空的用例编号行，请填上或删除该行"); return null; }
  if (new Set(raw).size !== raw.length) { ElMessage.warning("用例编号重复"); return null; }
  const bad = raw.filter((s) => /[\s,，]/.test(s));
  if (bad.length) { ElMessage.warning(`用例编号不能含空格或逗号：${bad.join("、")}`); return null; }
  return raw;
}

async function doPreview() {
  const ids = payloadCheck();
  const d = skDef.value;
  if (!ids || !d) return;
  skBusy.value = true;
  try {
    const r = await api.genSkeleton(d.name, {
      case_ids: ids, case_desc: skDesc.value.trim() || undefined,
      priority: skPriority.value, run: skRun.value, dry_run: true,
    });
    skPreview.value = r.cases;
    skWritten.value = [];
    skErrors.value = [];
  } catch { /* 拦截器已提示 */ }
  finally { skBusy.value = false; }
}

async function doWrite() {
  const ids = payloadCheck();
  const d = skDef.value;
  if (!ids || !d) return;
  if (!(await confirmBox("写入用例骨架到 Excel",
    `将向 <b>${d.name}</b> 追加 <b>${ids.length}</b> 行用例（编号：${ids.join("、")}）。<br>
     骨架带的是接口库里已知的 方法/路径/请求头/推荐断言，<b>业务参数仍需你逐条补全</b>。`))) return;
  skBusy.value = true;
  try {
    const r = await api.genSkeleton(d.name, {
      case_ids: ids, case_desc: skDesc.value.trim() || undefined,
      priority: skPriority.value, run: skRun.value, dry_run: false,
    });
    skWritten.value = r.written;
    skErrors.value = r.errors;
    skPreview.value = r.cases;
    ElMessage.success(r.written.length ? `已写入 ${r.written.length} 条用例，去「用例管理」继续补全参数` : "未写入任何用例");
  } catch { /* 409（编号重复）等的 detail 已提示 */ }
  finally { skBusy.value = false; }
}
</script>

<template>
  <div>
    <div class="page-header">
      <div class="head-tip">
        <b>接口定义库</b>：把同名接口收敛成一份权威定义，新用例选中即带入推荐断言与请求头。
        共 {{ items.length }} 个接口，其中 {{ covered }} 个已有用例覆盖
      </div>
      <div class="header-ops">
        <el-dropdown :disabled="aggBusy" @command="onAggregate">
          <el-button :loading="aggBusy">📥 从存量用例聚合<el-icon class="el-icon--right"><span>▾</span></el-icon></el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="skip">聚合（保留已有定义，只补新接口）</el-dropdown-item>
              <el-dropdown-item command="overwrite">聚合（用最新用例覆盖同名定义）</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button v-perm="'api:manage'" @click="openImport">📄 导入 OpenAPI/Swagger</el-button>
        <el-button v-perm="'api:manage'" type="primary" @click="openCreate">＋ 新建接口</el-button>
      </div>
    </div>

    <div class="filter-bar">
      <el-input v-model="filters.keyword" placeholder="接口名/路径/方法/说明/用例编号" clearable style="width: 260px" />
      <el-select v-model="filters.module" placeholder="模块" clearable filterable style="width: 180px">
        <el-option v-for="m in moduleOptions" :key="m" :value="m" :label="m" />
      </el-select>
      <span class="dim">筛出 {{ filtered.length }} / {{ items.length }}</span>
      <el-button size="small" text @click="loadList">刷新</el-button>
    </div>

    <el-alert type="info" :closable="false" class="rule-alert">
      接口库只存元数据，<b>用例执行不依赖它</b>：删定义不会让任何批次失败。
      但接口名称与用例的「接口名称」列是覆盖关系的锚点，被同名用例引用的定义<b>不可改名、不可删除</b>（后端 409）。
      路径参数写成 <span class="mono">{{ PH_ID }}</span> 形式，与框架的 <span class="mono">{{ PH_VAR }}</span> 占位符一致。
    </el-alert>

    <el-table :data="filtered" size="small" row-key="name" v-loading="loading">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="detail-view">
            <div v-if="row.headers && Object.keys(row.headers).length" class="dv-block">
              <div class="dv-title">推荐请求头</div>
              <div v-for="(v, k) in row.headers" :key="k" class="dv-line mono">{{ k }}: {{ v }}</div>
            </div>
            <div v-if="row.params && Object.keys(row.params).length" class="dv-block">
              <div class="dv-title">推荐查询参数</div>
              <div v-for="(v, k) in row.params" :key="k" class="dv-line mono">{{ k }} = {{ v }}</div>
            </div>
            <div v-if="row.body" class="dv-block">
              <div class="dv-title">请求体示例</div>
              <pre class="dv-json mono">{{ jsonText(row.body) }}</pre>
            </div>
            <div v-if="row.extract && Object.keys(row.extract).length" class="dv-block">
              <div class="dv-title">变量提取建议</div>
              <div v-for="(v, k) in row.extract" :key="k" class="dv-line mono">{{ k }} ← {{ v }}</div>
            </div>
            <div class="dv-block">
              <div class="dv-title">断言建议（{{ assertCount(row) }} 条）</div>
              <div v-for="(a, i) in row.asserts || []" :key="i" class="dv-line mono">
                {{ a[0] }} {{ a[1] }}<span v-if="String(a[2] ?? '') !== ''"> = {{ a[2] }}</span>
                <span class="dim"> {{ OP_DESC[String(a[0])] ?? "" }}</span>
              </div>
              <div v-if="!assertCount(row)" class="dim">无（生成骨架时回退为 status_code = 200）</div>
            </div>
            <div v-if="row.used_by.length" class="dv-block">
              <div class="dv-title">已覆盖该接口的用例（{{ row.used_by.length }} 条）</div>
              <div class="dv-line mono">{{ row.used_by.join(", ") }}</div>
              <el-button size="small" text type="primary" @click="router.push({ path: '/cases', query: { keyword: row.name } })">
                到用例页查看
              </el-button>
            </div>
            <div v-if="!row.remark" class="dim">（无用途说明）</div>
            <div v-else class="dv-remark">{{ row.remark }}</div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="接口名称" min-width="180">
        <template #default="{ row }">
          <span class="mono">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="module" label="模块" width="130" show-overflow-tooltip />
      <el-table-column label="方法" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="METHOD_TYPE[row.method] ?? 'info'" effect="dark">{{ row.method }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="请求路径" min-width="220">
        <template #default="{ row }"><span class="mono">{{ row.path }}</span></template>
      </el-table-column>
      <el-table-column label="断言建议" width="100">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="assertCount(row) ? 'success' : 'info'">
            {{ assertCount(row) }} 条
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="用例覆盖" width="120">
        <template #default="{ row }">
          <el-tag v-if="row.used_by.length" size="small" type="warning">{{ row.used_by.length }} 条</el-tag>
          <el-tag v-else size="small" type="info" effect="plain">未覆盖</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="来源" width="110">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="SOURCE_TYPE[row.source] ?? 'info'">
            {{ sourceLabel(row.source) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="updated_at" label="更新时间" width="160" />
      <el-table-column label="操作" width="230" fixed="right">
        <template #default="{ row }">
          <div class="op-cell">
            <el-button v-perm="'case:write'" size="small" text type="success" @click="openSkeleton(row)">生成骨架</el-button>
            <el-button v-perm="'api:manage'" size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button v-perm="'api:manage'" size="small" text type="primary" @click="copyAsNew(row)">复制</el-button>
            <el-tooltip v-if="row.used_by.length" content="被用例同名引用，改名/删除会被后端拒绝；请先改用例的接口名称列" placement="top">
              <el-button v-perm="'api:manage'" size="small" text type="danger" disabled>删除</el-button>
            </el-tooltip>
            <el-button v-else v-perm="'api:manage'" size="small" text type="danger" @click="remove(row)">删除</el-button>
          </div>
        </template>
      </el-table-column>
      <template #empty>
        <div style="padding: 20px; color: #909399">
          接口库还是空的。点「📥 从存量用例聚合」即可把手工用例里的接口一键沉淀成定义，
          或「📄 导入 OpenAPI/Swagger」从开发侧的接口文档直接拉。
        </div>
      </template>
    </el-table>

    <!-- 接口定义编辑抽屉 -->
    <el-drawer v-model="drawerVisible" size="55%" :title="isEdit ? `编辑接口 ${origName}` : '新建接口定义'">
      <el-form label-width="82px" @submit.prevent>
        <el-form-item label="接口名称">
          <el-input v-model="form.name" class="mono" :disabled="renameLocked"
                    :placeholder="`唯一名称，与用例的「接口名称」列一致（最多 ${nameMax()} 字）`" />
          <div v-if="renameLocked" class="hint">
            <b style="color: #e6a23c">被 {{ origUsed.length }} 条用例同名引用（{{ origUsed.slice(0, 5).join(", ") }}），改名会断掉覆盖关系，已锁定。</b>
          </div>
          <div v-else class="hint">
            用例的「接口名称」列写什么，这里就登记什么，两边靠这个名字建立覆盖关系；
            名称不能含 <span class="mono">{{ NAME_BAD_CHARS.join(" ") }}</span>（接口库按名称寻址）。
          </div>
        </el-form-item>
        <div class="kv-row">
          <span class="fld-label">模块</span>
          <el-select v-model="form.module" filterable allow-create default-first-option placeholder="可新建模块" style="width: 200px">
            <el-option v-for="m in moduleOptions" :key="m" :value="m" :label="m" />
          </el-select>
          <span class="fld-label">请求方式</span>
          <el-select v-model="form.method" style="width: 120px">
            <el-option v-for="m in methods" :key="m" :value="m" :label="m" />
          </el-select>
          <el-input v-model="form.path" class="mono" placeholder="/atlas/list" style="flex: 1" />
        </div>
        <div class="hint path-hint">{{ PATH_PH }}</div>
        <el-form-item label="用途说明">
          <el-input v-model="form.remark" type="textarea" :rows="2" :maxlength="remarkMax()"
                    show-word-limit placeholder="这个接口做什么、有什么坑（如分页游标、鉴权要求）" />
        </el-form-item>

        <el-divider content-position="left">推荐请求头</el-divider>
        <div v-for="(h, hi) in hdrRows" :key="'h' + hi" class="kv-row">
          <el-input v-model="h.key" placeholder="Header 名" class="mono" style="width: 200px" />
          <el-input v-model="h.value" :placeholder="VAR_PH" class="mono" style="flex: 1" />
          <el-button text type="danger" @click="hdrRows.splice(hi, 1)">✕</el-button>
        </div>
        <el-button size="small" text @click="hdrRows.push({ key: '', value: '' })">＋ 请求头</el-button>

        <el-divider content-position="left">推荐查询参数</el-divider>
        <div v-for="(p, pi) in prmRows" :key="'p' + pi" class="kv-row">
          <el-input v-model="p.key" placeholder="参数名" class="mono" style="width: 200px" />
          <el-input v-model="p.value" :placeholder="VAR_PH" class="mono" style="flex: 1" />
          <el-button text type="danger" @click="prmRows.splice(pi, 1)">✕</el-button>
        </div>
        <el-button size="small" text @click="prmRows.push({ key: '', value: '' })">＋ 查询参数</el-button>

        <el-divider content-position="left">请求体示例（JSON，留空 = 无请求体）</el-divider>
        <el-input v-model="bodyText" type="textarea" :rows="5" class="mono" spellcheck="false"
                  placeholder='{"userId": "{{user_id}}", "pageInfo": {"pageSize": 10}}' />

        <el-divider content-position="left">变量提取建议</el-divider>
        <div v-for="(x, xi) in extRows" :key="'x' + xi" class="kv-row">
          <el-input v-model="x.key" placeholder="变量名" class="mono" style="width: 200px" />
          <el-input v-model="x.value" :placeholder="ASSERT_PATH_PH" class="mono" style="flex: 1" />
          <el-button text type="danger" @click="extRows.splice(xi, 1)">✕</el-button>
        </div>
        <el-button size="small" text @click="extRows.push({ key: '', value: '' })">＋ 提取</el-button>
        <div class="hint" style="margin-top: 6px">
          如 <span class="mono">token ← $.data.token</span>：生成骨架时会带进用例的「变量提取」列，
          后续用例即可用 <span class="mono">{{ PH_TOKEN }}</span>。
        </div>

        <el-divider content-position="left">断言建议（最多 {{ assertMax() }} 条）</el-divider>
        <div v-for="(a, ai) in asrtRows" :key="'a' + ai" class="kv-row">
          <el-select v-model="a.op" placeholder="操作符" style="width: 180px">
            <el-option v-for="o in assertOps" :key="o" :value="o" :label="`${o} ${OP_DESC[o] ?? ''}`" />
          </el-select>
          <el-input v-model="a.path" :placeholder="ASSERT_PATH_PH" class="mono" style="flex: 1" />
          <el-input v-if="opNeedExpected(a.op)" v-model="a.expected" placeholder="期望值" class="mono" style="width: 170px" />
          <el-button text type="danger" @click="asrtRows.splice(ai, 1)">✕</el-button>
        </div>
        <el-button size="small" text @click="asrtRows.push({ op: 'eq', path: 'status_code', expected: '200' })">＋ 断言</el-button>
        <div class="hint" style="margin-top: 6px">
          这些建议只用于「生成用例骨架」与用例页的带入，不参与执行；断言真正生效的位置始终是用例行的「断言」列。
        </div>

        <div class="drawer-foot">
          <el-button @click="drawerVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="save">保存接口</el-button>
        </div>
      </el-form>
    </el-drawer>

    <!-- OpenAPI / Swagger 导入 -->
    <el-dialog v-model="impVisible" title="导入 OpenAPI / Swagger 文档" width="60%">
      <div class="hint" style="margin-bottom: 8px">
        支持 OpenAPI 3.x 与 Swagger 2.0 的 <b>JSON 或 YAML 原文</b>（上限 {{ specLimitMb() }}MB）。
        导入只登记接口与推荐断言，<b>不会生成任何用例</b>；生成用例请回列表点「生成骨架」。
      </div>
      <div class="kv-row" style="margin-bottom: 8px">
        <el-button size="small" @click="pickFile">选择 .json / .yaml 文件</el-button>
        <input ref="fileInput" type="file" accept=".json,.yaml,.yml,.txt" hidden @change="onPickFile" />
        <el-switch v-model="impOverwrite" active-text="覆盖同名定义" />
        <span v-if="impText" class="dim">已载入 {{ (impText.length / 1024).toFixed(1) }} KB</span>
      </div>
      <el-input v-model="impText" type="textarea" :rows="12" class="mono" spellcheck="false"
                placeholder='{"openapi":"3.0.1","paths":{"/atlas/list":{"post":{"summary":"相册列表(分页)", …}}}}' />
      <el-alert v-if="impResult" type="success" :closable="false" class="rule-alert" style="margin-top: 10px">
        解析 {{ impResult.parsed }} 个接口：新增 {{ impResult.created }}、更新 {{ impResult.updated }}、跳过 {{ impResult.skipped }}。
        <template v-if="impResult.conflicts.length"><br>端点已被其他接口名占用：{{ impResult.conflicts.slice(0, 3).join("；") }}</template>
        <template v-if="impResult.note_count"><br>另有 {{ impResult.note_count }} 处提示：{{ impResult.skipped_notes.slice(0, 3).join("；") }}</template>
      </el-alert>
      <div class="drawer-foot">
        <el-button @click="impVisible = false">关闭</el-button>
        <el-button type="primary" :loading="impBusy" @click="doImport">解析并导入</el-button>
      </div>
    </el-dialog>

    <!-- 生成用例骨架 -->
    <el-dialog v-model="skVisible" :title="`由接口生成用例骨架：${skDef?.name ?? ''}`" width="72%">
      <div class="hint" style="margin-bottom: 8px">
        骨架带出接口的 方法 / 路径 / 推荐请求头 / 请求体示例 / 断言建议（当前定义：<span class="mono">{{ skDef?.method }} {{ skDef?.path }}</span>）。
        业务参数值仍需逐条补全，所以<b>默认只预览</b>，确认后再写入 Excel。
      </div>
      <div class="sub-label">用例编号（每行一个，编号必须在全库唯一）</div>
      <div v-for="(id, i) in skIds" :key="i" class="kv-row">
        <el-input v-model="skIds[i]" class="mono" placeholder="如 album_list_017" style="width: 300px" />
        <el-button text type="danger" :disabled="skIds.length === 1" @click="skIds.splice(i, 1)">✕</el-button>
      </div>
      <el-button size="small" text @click="addSkeletonRow">＋ 再加一条</el-button>
      <div class="kv-row" style="margin-top: 10px">
        <span class="fld-label">用例描述</span>
        <el-input v-model="skDesc" style="flex: 1" />
        <span class="fld-label">优先级</span>
        <el-select v-model="skPriority" style="width: 100px">
          <el-option v-for="p in ['P0', 'P1', 'P2', 'P3', 'P4', 'P5']" :key="p" :value="p" :label="p" />
        </el-select>
        <span class="fld-label">是否执行</span>
        <el-switch v-model="skRun" active-text="Y" inactive-text="N" />
      </div>

      <template v-if="skPreview">
        <div class="sub-label">预览（{{ skPreview.length }} 行，写回 Excel「用例」表末行）</div>
        <el-table :data="skPreview" size="small" max-height="240">
          <el-table-column prop="case_id" label="编号" width="160" />
          <el-table-column prop="case_desc" label="描述" min-width="140" show-overflow-tooltip />
          <el-table-column prop="method" label="方法" width="80" />
          <el-table-column prop="url" label="请求路径" min-width="160" />
          <el-table-column label="断言" width="70">
            <template #default="{ row }">{{ (row.asserts || []).length }} 条</template>
          </el-table-column>
          <el-table-column label="头/参/体" width="100">
            <template #default="{ row }">
              <span class="mono dim">
                {{ Object.keys(row.headers || {}).length }}/{{ Object.keys(row.params || {}).length }}/{{ row.body ? 1 : 0 }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="priority" label="优先级" width="80" />
        </el-table>
      </template>
      <el-alert v-if="skWritten.length" type="success" :closable="false" class="rule-alert" style="margin-top: 10px">
        已写入 {{ skWritten.length }} 条：{{ skWritten.join(", ") }}
        <el-button size="small" text type="primary" @click="router.push('/cases')">去用例管理补全参数</el-button>
      </el-alert>
      <el-alert v-if="skErrors.length" type="warning" :closable="false" class="rule-alert" style="margin-top: 10px">
        未写入：{{ skErrors.join("；") }}
      </el-alert>

      <div class="drawer-foot">
        <el-button @click="skVisible = false">关闭</el-button>
        <el-button :loading="skBusy" @click="doPreview">预览骨架</el-button>
        <el-button type="primary" :loading="skBusy" @click="doWrite">写入 Excel</el-button>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.head-tip { font-size: 13px; color: #606266; line-height: 1.7; }
.header-ops { display: flex; gap: 8px; flex: 0 0 auto; }
.filter-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.rule-alert { margin-bottom: 12px; line-height: 1.7; font-size: 12px; }
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.hint { color: #909399; font-size: 12px; line-height: 1.6; }
.dim { color: #909399; font-size: 12px; }
.op-cell { display: flex; align-items: center; gap: 2px; white-space: nowrap; }
.op-cell .el-button + .el-button { margin-left: 0; }
.kv-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.fld-label { color: #606266; font-size: 13px; }
.path-hint { margin: 0 0 10px 82px; }
.sub-label { color: #909399; font-size: 12px; margin: 10px 0 6px; }
.drawer-foot { margin-top: 16px; display: flex; justify-content: flex-end; gap: 8px; }
/* 展开详情区 */
.detail-view { padding: 8px 24px 12px; display: flex; flex-wrap: wrap; gap: 18px; }
.dv-block { min-width: 240px; }
.dv-title { font-size: 12px; color: #909399; margin-bottom: 4px; }
.dv-line { font-size: 12px; color: #303133; line-height: 1.8; }
.dv-json { margin: 0; font-size: 12px; line-height: 1.5; max-height: 160px; overflow: auto; background: #fafbfc; padding: 6px 8px; border-radius: 4px; }
.dv-remark { width: 100%; font-size: 12px; color: #606266; line-height: 1.7; }
</style>
