<script setup lang="ts">
/**
 * AI 生成（Agent 接入一期 M4）：意图输入 → 目标选择 → 生成 → 预览对照 → 确认落库。
 *
 * 链路两段、各司其职（设计上不可混线）：
 * - 生成：直连 qa-agent /v1/generations（独立服务，产出只是草稿，dry_run 恒 true）；
 * - 落库：走平台 /api/agent/cases/preview|commit —— 权威字段校验与写 Excel 都在平台侧，
 *   Agent 产出必须过平台闸门 + 人工点击确认才入库（case:write 权限）。
 * 降级语义必须显性：fake/离线产出标 degraded、authoritative=false 时明示"仅供建议"。
 */
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import type {
  ApiDefItem, CaseItem, GenerationOut, Project,
  AgentPreviewResp, GenerationValidationItem,
} from "../types";

/* ---------------- 表单与素材 ---------------- */
const projects = ref<Project[]>([]);
const defs = ref<ApiDefItem[]>([]);
const cases = ref<CaseItem[]>([]);
const project = ref<Project | null>(null);
const mode = ref<"generate" | "complete" | "expand">("generate");
const target = ref("");
const intent = ref("");
const count = ref(3);
const agentOnline = ref<boolean | null>(null);

const MODES = [
  { value: "generate", label: "生成整套", tip: "按接口定义产出正例/边界/异常用例集" },
  { value: "complete", label: "补全单条", tip: "给已有用例补缺失的断言/参数/提取" },
  { value: "expand", label: "同风格扩展", tip: "基于已有用例扩展参数等价类变体" },
] as const;

const filteredCases = computed(() =>
  cases.value.filter((c) => !target.value || c.case_id.toLowerCase().includes(target.value.toLowerCase()))
);

async function loadProjects() {
  projects.value = (await api.listProjects()).filter((p) => p.kind === "api");
  project.value = projects.value[0] ?? null;
}

async function loadTargetPool() {
  // generate 选接口名；complete/expand 选已有用例编号（都来自平台真实数据，不手填臆测）
  defs.value = await api.listApiDefs();
  if (project.value) {
    const page = await api.listCases({ file: project.value.file });
    cases.value = page.items;
  }
}

function onModeChange() {
  target.value = "";
}

async function switchProject(p: Project) {
  project.value = p;
  target.value = "";
  await loadTargetPool();
}

async function checkAgent() {
  try {
    const h = await api.agentHealth();
    agentOnline.value = h.skills?.includes("case_generate") ?? false;
  } catch {
    agentOnline.value = false;   // 拦截器已提示原因，这里只落状态
  }
}

onMounted(async () => {
  await loadProjects();
  await loadTargetPool();
  void checkAgent();
});

/* ---------------- 生成 ---------------- */
const generating = ref(false);
const result = ref<GenerationOut | null>(null);
const previewResp = ref<AgentPreviewResp | null>(null);
const committing = ref(false);

async function doGenerate() {
  if (!target.value) {
    ElMessage.warning(mode.value === "generate" ? "请选择目标接口" : "请选择要补全/扩展的用例");
    return;
  }
  generating.value = true;
  result.value = null;
  previewResp.value = null;
  try {
    result.value = await api.agentGenerate({
      mode: mode.value,
      target: target.value,
      intent: intent.value,
      count: count.value,
      platform: "interface-auto",
      project_id: project.value?.id ?? "",
      dry_run: true,
    });
    if (!result.value.cases.length) {
      ElMessage.warning("未产出可用草稿（看下方错误与校验明细）");
    }
  } finally {
    generating.value = false;
  }
}

/* ---------------- 平台权威校验 + 落库 ---------------- */
const previewing = ref(false);

async function doPreview() {
  if (!draftCases.value.length) return;
  previewing.value = true;
  try {
    previewResp.value = await api.agentPreviewCases({
      cases: draftCases.value,
      project_id: project.value?.id ?? "",
    });
    if (previewResp.value.failed > 0) {
      ElMessage.warning(`平台校验未全过：${previewResp.value.failed} 条有问题，落库会被整批拒绝`);
    } else {
      ElMessage.success(`平台校验通过 ${previewResp.value.passed} 条，可确认落库`);
    }
  } finally {
    previewing.value = false;
  }
}

async function doCommit() {
  if (!draftCases.value.length) return;
  await ElMessageBox.confirm(
    `确认将 ${draftCases.value.length} 条草稿写入项目「${project.value?.name}」的用例文件？\n` +
    "落库前平台会再跑一次权威校验，任一条非法即整批拒绝。",
    "确认落库", { type: "warning", confirmButtonText: "确认写入", cancelButtonText: "再看看" }
  );
  committing.value = true;
  try {
    const resp = await api.agentCommitCases({
      cases: draftCases.value,
      project_id: project.value?.id ?? "",
    });
    ElMessage.success(`已落库 ${resp.committed} 条（跳过已存在 ${resp.skipped}）→ ${resp.file}`);
    result.value = null;
    previewResp.value = null;
    await loadTargetPool();
  } finally {
    committing.value = false;
  }
}

/* ---------------- 展示派生 ---------------- */
const draftCases = computed(() => result.value?.cases ?? []);

/** 逐条状态合并：Agent 预检 + 平台权威校验（后者存在则以其为准） */
function itemState(caseId: string): { passed: boolean; exists: boolean; problems: string[]; from: string } {
  const plat = previewResp.value?.items.find((i) => i.case_id === caseId);
  if (plat) return { passed: plat.passed, exists: plat.exists, problems: plat.problems, from: "平台权威校验" };
  const agent: GenerationValidationItem | undefined = result.value?.validation.items?.find(
    (i) => i.case_id === caseId);
  if (agent) return { passed: agent.passed, exists: false, problems: agent.problems, from: "Agent 预检" };
  return { passed: false, exists: false, problems: ["（无校验记录）"], from: "" };
}

function pretty(v: unknown): string {
  if (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)
      || (typeof v === "object" && !Array.isArray(v) && !Object.keys(v as object).length)) return "—";
  return typeof v === "string" ? v : JSON.stringify(v, null, 2);
}

/** complete/expand 的逐字段新旧对照（GenerationOut.diffs，设计 5.1/5.2⑤）；generate 无基准则为空 */
function caseDiff(caseId: string): { field: string; from: unknown; to: unknown }[] {
  return (result.value?.diffs ?? []).find((d) => d.case_id === caseId)?.changed ?? [];
}

const allPlatformPassed = computed(() =>
  !!previewResp.value && previewResp.value.failed === 0 && previewResp.value.passed > 0);
</script>

<template>
  <div style="padding: 16px">
    <!-- 服务状态 + 生成表单 -->
    <el-card shadow="never" style="margin-bottom: 14px">
      <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 12px">
        <b style="font-size: 15px">用例生成（qa-agent · case_generate）</b>
        <el-tag v-if="agentOnline === true" type="success" size="small">Agent 在线</el-tag>
        <el-tag v-else-if="agentOnline === false" type="danger" size="small">Agent 不可达</el-tag>
        <el-tag v-else size="small">检测中…</el-tag>
        <el-link style="margin-left: auto" :underline="false" @click="checkAgent">重新检测</el-link>
      </div>

      <el-form label-width="84px">
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="目标项目">
              <el-select :model-value="project?.id" style="width: 100%" @change="(id: string) => switchProject(projects.find(p => p.id === id)!)"
                         placeholder="落库到哪个项目的用例文件">
                <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="生成模式">
              <el-radio-group v-model="mode" @change="onModeChange">
                <el-radio-button v-for="m in MODES" :key="m.value" :value="m.value">
                  <el-tooltip :content="m.tip" placement="top">{{ m.label }}</el-tooltip>
                </el-radio-button>
              </el-radio-group>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="期望条数">
              <el-input-number v-model="count" :min="1" :max="20" :disabled="mode === 'complete'" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item :label="mode === 'generate' ? '目标接口' : '参考用例'">
          <el-select v-if="mode === 'generate'" v-model="target" filterable style="width: 420px"
                     placeholder="接口定义库中选择（存在性由平台判定）">
            <el-option v-for="d in defs" :key="d.name" :label="`${d.name}（${d.method} ${d.path}）`" :value="d.name">
              <span>{{ d.name }}</span>
              <span style="float: right; color: #909399; font-size: 12px">{{ d.method }} {{ d.path }}</span>
            </el-option>
          </el-select>
          <el-select v-else v-model="target" filterable style="width: 420px"
                     placeholder="已有用例编号（补全/扩展的基准）">
            <el-option v-for="c in filteredCases" :key="c.case_id"
                       :label="`${c.case_id} ${c.case_desc}`" :value="c.case_id" />
          </el-select>
        </el-form-item>
        <el-form-item label="意图补充">
          <el-input v-model="intent" type="textarea" :rows="2" maxlength="1000" show-word-limit
                    placeholder="可选：覆盖哪些场景、参数范围、断言侧重等（模型只能参考已入库知识，越具体越好）" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="generating" :disabled="agentOnline === false" @click="doGenerate">
            {{ generating ? "生成中（检索+生成+校验闸门）…" : "生成草稿" }}
          </el-button>
          <span style="margin-left: 12px; color: #909399; font-size: 12px">
            产出仅草稿预览，人工确认后才写入用例文件（dry_run 硬约束）
          </span>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 降级与错误必须显性，禁止把降级结果当正常结论 -->
    <el-alert v-if="result?.degraded" type="warning" show-icon :closable="false" style="margin-bottom: 12px"
              title="产出为降级结果（fake/离线模型）：结构可参考，内容不可当真实结论直接使用" />
    <el-alert v-else-if="result && !result.validation.authoritative" type="info" show-icon :closable="false"
              style="margin-bottom: 12px"
              title="平台权威校验未启用（Agent 读不到平台 preview）：以下仅为结构预检，落库前务必点『平台校验预览』" />
    <el-alert v-if="result?.errors?.length" type="error" show-icon :closable="false" style="margin-bottom: 12px"
              :title="`生成链路提示（${result.errors.length}）`" :description="result.errors.join('；')" />

    <!-- 草稿对照表 -->
    <el-card v-if="result" shadow="never" style="margin-bottom: 14px">
      <template #header>
        <div style="display: flex; align-items: center; gap: 12px">
          <b>草稿预览（{{ draftCases.length }} 条）</b>
          <el-tag size="small">闸门：{{ result.validation.passed }}/{{ result.validation.checked }} 过检</el-tag>
          <el-tag v-if="previewResp" :type="previewResp.failed ? 'danger' : 'success'" size="small">
            平台校验：{{ previewResp.passed }} 通过 / {{ previewResp.failed }} 问题
          </el-tag>
          <div style="margin-left: auto; display: flex; gap: 8px">
            <el-button :loading="previewing" :disabled="!draftCases.length" @click="doPreview">平台校验预览</el-button>
            <el-button v-perm="'case:write'" type="primary" :loading="committing"
                       :disabled="!draftCases.length || !allPlatformPassed" @click="doCommit">
              确认落库
            </el-button>
          </div>
        </div>
      </template>

      <el-empty v-if="!draftCases.length" description="无草稿产出，看上方错误明细" />
      <el-collapse v-else accordion>
        <el-collapse-item v-for="(c, idx) in draftCases" :key="idx" :name="idx">
          <template #title>
            <div style="display: flex; align-items: center; gap: 8px; width: 100%; padding-right: 8px">
              <b>{{ c.case_id || '(缺编号)' }}</b>
              <el-tag size="small" type="info">{{ c.method }}</el-tag>
              <span style="color: #606266; font-size: 12px">{{ c.url }}</span>
              <span style="color: #909399; font-size: 12px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">
                {{ c.case_desc }}
              </span>
              <el-tag v-if="itemState(String(c.case_id)).exists" size="small" type="warning">编号已存在</el-tag>
              <el-tag :type="itemState(String(c.case_id)).passed ? 'success' : 'danger'" size="small">
                {{ itemState(String(c.case_id)).from || '未校验' }}
              </el-tag>
            </div>
          </template>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="接口">{{ c.api_name }}</el-descriptions-item>
            <el-descriptions-item label="模块">{{ c.module }}</el-descriptions-item>
            <el-descriptions-item label="优先级">{{ c.priority }}</el-descriptions-item>
            <el-descriptions-item label="启用">{{ c.run ? "是" : "否" }}</el-descriptions-item>
            <el-descriptions-item label="请求头"><pre style="margin: 0">{{ pretty(c.headers) }}</pre></el-descriptions-item>
            <el-descriptions-item label="参数"><pre style="margin: 0">{{ pretty(c.params) }}</pre></el-descriptions-item>
            <el-descriptions-item label="请求体"><pre style="margin: 0">{{ pretty(c.body) }}</pre></el-descriptions-item>
            <el-descriptions-item label="变量提取"><pre style="margin: 0">{{ pretty(c.extract) }}</pre></el-descriptions-item>
            <el-descriptions-item label="断言" :span="2">
              <pre style="margin: 0">{{ pretty(c.asserts) }}</pre>
            </el-descriptions-item>
          </el-descriptions>
          <!-- 补全/扩展逐字段对照：模型真正改了什么一目了然，未提及字段沿用原值不刷红 -->
          <template v-if="caseDiff(String(c.case_id)).length">
            <el-divider content-position="left" style="margin: 10px 0 6px">
              <el-text size="small" type="info">与原用例对照（基准：{{ target }}，改 {{ caseDiff(String(c.case_id)).length }} 处）</el-text>
            </el-divider>
            <el-table :data="caseDiff(String(c.case_id))" size="small" border>
              <el-table-column prop="field" label="字段" width="110" />
              <el-table-column label="原值" min-width="200">
                <template #default="{ row }"><pre style="margin: 0; white-space: pre-wrap">{{ pretty(row.from) }}</pre></template>
              </el-table-column>
              <el-table-column label="新值（草稿）" min-width="200">
                <template #default="{ row }"><pre style="margin: 0; white-space: pre-wrap; color: #e6a23c">{{ pretty(row.to) }}</pre></template>
              </el-table-column>
            </el-table>
          </template>
          <div v-if="itemState(String(c.case_id)).problems.length" style="margin-top: 8px">
            <el-text type="danger" size="small">
              问题：{{ itemState(String(c.case_id)).problems.join('；') }}
            </el-text>
          </div>
          <div style="margin-top: 8px">
            <el-text size="small" type="info">知识引用：</el-text>
            <el-tag v-for="k in (c.citations as string[]) || []" :key="k" size="small" style="margin: 0 4px">
              {{ k }}
            </el-tag>
          </div>
        </el-collapse-item>
      </el-collapse>

      <!-- 本次生成的全部知识引用（按引用可回跳原文） -->
      <el-divider content-position="left">知识溯源（{{ result.citations.length }}）</el-divider>
      <div v-for="ct in result.citations" :key="ct.kb_id" style="font-size: 12px; color: #606266; line-height: 22px">
        <el-tag size="small" style="margin-right: 6px">{{ ct.domain || "kb" }}</el-tag>
        <a :href="ct.source_uri" target="_blank" style="color: #409eff; text-decoration: none">{{ ct.kb_id }}</a>
        <span v-if="ct.title"> · {{ ct.title }}</span>
        <span v-if="ct.score" style="color: #909399"> · 相关度 {{ (ct.score * 100).toFixed(0) }}%</span>
      </div>
      <el-empty v-if="!result.citations.length" :image-size="42" description="无知识引用——产出不可溯源，不应被采纳" />
    </el-card>
  </div>
</template>
