<script setup lang="ts">
/**
 * UI 步骤编辑器：动作下拉（中文标签来自后端 /api/ui-cases/meta 的 actions 字典）
 * + 定位器输入 + 取值输入（可插入 {{变量}} chip）+ 步骤备注。
 * 是否需要定位器/取值由动作字典 need_locator/need_value 驱动，与后端校验同源；
 * 本组件只做编辑与结构化，最终合法性仍由后端落盘那一刻校验兜底。
 */
import { computed, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import type { UiActionMeta, UiStep } from "../types";

const props = defineProps<{
  modelValue: UiStep[];
  actions: UiActionMeta[];
  variables?: string[];
}>();
const emit = defineEmits<{ (e: "update:modelValue", v: UiStep[]): void }>();

/** 编辑态行：value 一律以字符串承载（12 个动作的取值均为文本/路径），保存时透传字符串 */
interface StepRow {
  action: string;
  locator: string;
  value: string;
  desc: string;
}

const metaOf = (action: string) => props.actions.find((a) => a.action === action);
const needLocator = (action: string) => metaOf(action)?.need_locator ?? false;
const needValue = (action: string) => metaOf(action)?.need_value ?? false;
const isExpect = (action: string) => action.startsWith("expect_");

const LOC_PH = "定位器，如 #username / css=.btn / xpath=//div / role=button,name=登录";
const VAL_PH = "取值，支持 {{变量}} 引用";

// 动作自带 hint 优先作为对应输入框占位提示（如 upload 取值=文件路径、
// switch_frame 定位器=iframe 选择器、expect_attr 取值=属性名=期望值）。无 hint 时用通用占位。
// hint 是单字段：仅当该动作「只需定位器不需取值」时判定它描述定位器，否则描述取值。
const locatorPh = (action: string) => {
  const m = metaOf(action);
  return m?.hint && m.need_locator && !m.need_value ? m.hint : LOC_PH;
};
const valuePh = (action: string) => {
  const m = metaOf(action);
  return m?.hint && m.need_value ? m.hint : VAL_PH;
};

const toRow = (s: UiStep): StepRow => ({
  action: s.action ?? "",
  locator: s.locator == null ? "" : String(s.locator),
  value: s.value == null ? "" : String(s.value),
  desc: s.desc ?? "",
});

/** 行 → 提交结构：仅带该动作需要的字段，空的可选项省略（与后端 _validate_step 口径一致） */
const toStep = (r: StepRow): UiStep => {
  const s: UiStep = { action: r.action };
  const loc = r.locator.trim();
  if (loc) s.locator = loc;
  const val = r.value;
  if (needValue(r.action)) s.value = val.trim();
  else if (val.trim()) s.value = val.trim();
  if (r.desc.trim()) s.desc = r.desc.trim();
  return s;
};

const rows = reactive<StepRow[]>([]);

// 外部模型 → 内部行：仅在结构真正变化时同步，避免回填触发写入循环
watch(
  () => props.modelValue,
  (v) => {
    const next = (v ?? []).map(toRow);
    if (JSON.stringify(next) !== JSON.stringify(rows)) rows.splice(0, rows.length, ...next);
  },
  { immediate: true, deep: true }
);

// 内部行 → 外部模型
watch(rows, () => emit("update:modelValue", rows.map(toStep)), { deep: true });

function addStep() {
  rows.push({ action: "goto", locator: "", value: "", desc: "" });
}
function removeStep(i: number) {
  rows.splice(i, 1);
}
function moveStep(i: number, dir: -1 | 1) {
  const j = i + dir;
  if (j < 0 || j >= rows.length) return;
  const t = rows[i];
  rows[i] = rows[j];
  rows[j] = t;
}

// 变量 chip 插入：先聚焦某步取值框，再点变量标签把 {{name}} 追加进去
const focusRow = ref(-1);
function insertVar(name: string) {
  if (focusRow.value < 0 || focusRow.value >= rows.length) {
    ElMessage.warning("请先点击某一步的取值框，再点变量标签插入");
    return;
  }
  rows[focusRow.value].value += `{{${name}}}`;
}

// 变量 chip 展示文本：在脚本里拼接，避免模板 {{ }} 字面量干扰解析
const varChip = (v: string) => "{{" + v + "}}";

// 动作下拉按使用频率分组（常用置顶、高级次级），与后端 meta.actions.group 同源
const groupedActions = computed(() => {
  const common = props.actions.filter((a) => a.group === "常用");
  const advanced = props.actions.filter((a) => a.group !== "常用");
  return [
    { label: "常用", items: common },
    { label: "高级", items: advanced },
  ].filter((g) => g.items.length);
});

// 快捷步骤组合：一键生成骨架，新手只改取值/定位器
interface QuickCombo { key: string; label: string; steps: UiStep[] }
const QUICK_COMBOS: QuickCombo[] = [
  { key: "login", label: "登录流程", steps: [
    { action: "goto", value: "/login", desc: "打开登录页" },
    { action: "fill", locator: "#username", value: "{{username}}" },
    { action: "fill", locator: "#password", value: "{{password}}" },
    { action: "click", locator: "#submit" },
    { action: "expect_text", locator: ".welcome", value: "欢迎", desc: "校验登录成功" },
  ] },
  { key: "search", label: "搜索流程", steps: [
    { action: "goto", value: "/", desc: "打开首页" },
    { action: "fill", locator: "input[name=q]", value: "{{keyword}}" },
    { action: "click", locator: "button[type=submit]" },
    { action: "wait_for", locator: ".result", desc: "等待结果" },
    { action: "expect_visible", locator: ".result" },
  ] },
  { key: "open_assert", label: "打开+断言", steps: [
    { action: "goto", value: "/", desc: "打开页面" },
    { action: "expect_visible", locator: "#title" },
  ] },
];
function insertCombo(c: QuickCombo) {
  rows.push(...c.steps.map((s) => toRow(s)));
}

// 定位器实时提示：根据输入形态提示将按哪种引擎解析
function locatorHint(loc: string): { text: string; type: string } {
  const l = (loc || "").trim();
  if (!l) return { text: "裸写默认按 CSS 解析（如 #id / .class）", type: "info" };
  if (l.startsWith("css=")) return { text: "按 CSS 选择器解析", type: "success" };
  if (l.startsWith("xpath=")) return { text: "按 XPath 解析", type: "success" };
  if (l.startsWith("text=")) return { text: "按可见文本匹配", type: "success" };
  if (l.startsWith("role=")) return { text: "按 ARIA 角色+名称定位（get_by_role）", type: "success" };
  if (l.startsWith("testid=")) return { text: "按 data-testid 稳定标识定位（get_by_test_id）", type: "success" };
  if (l.startsWith("placeholder=")) return { text: "按输入框占位文本定位（get_by_placeholder）", type: "success" };
  if (l.startsWith("alt=")) return { text: "按图片 alt 文本定位（get_by_alt_text）", type: "success" };
  if (l.startsWith("//") || l.startsWith("(")) return { text: "看起来是 XPath，建议加 xpath= 前缀", type: "warning" };
  return { text: "将按 CSS 选择器解析", type: "info" };
}
</script>

<template>
  <div class="ui-step-editor">
    <!-- 顶部工具栏：快捷组合 + 新手三步引导（空态时） -->
    <div class="toolbar">
      <el-dropdown trigger="click" @command="(k: string) => { const c = QUICK_COMBOS.find(x => x.key === k); if (c) insertCombo(c); }">
        <el-button size="small" type="primary" plain>⚡ 插入常用组合<el-icon class="el-icon--right"><span>▾</span></el-icon></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item v-for="c in QUICK_COMBOS" :key="c.key" :command="c.key">{{ c.label }}（{{ c.steps.length }} 步）</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <span class="hint" style="margin:0">新手？先点上方下拉选一个模板骨架，再逐步改定位器与取值。</span>
    </div>

    <el-alert v-if="!rows.length" type="info" :closable="false" show-icon class="guide">
      <template #title>三步快速上手：① 打开页面（goto 填相对路径）→ ② 操作元素（点击/输入，填定位器）→ ③ 断言结果（断言文本/可见）。可点上方「插入常用组合」一键生成骨架。</template>
    </el-alert>

    <div v-for="(r, i) in rows" :key="i" class="step-card">
      <div class="step-head">
        <span class="step-no">{{ i + 1 }}</span>
        <el-select v-model="r.action" placeholder="选择动作" style="width: 200px" size="default">
          <el-option-group v-for="g in groupedActions" :key="g.label" :label="g.label">
            <el-option v-for="a in g.items" :key="a.action" :value="a.action" :label="`${a.desc}（${a.action}）`">
              <span class="opt-key">{{ a.desc }}</span>
              <span class="opt-val mono">{{ a.action }}</span>
            </el-option>
          </el-option-group>
        </el-select>
        <span v-if="isExpect(r.action)" class="expect-tag">断言：期望值不满足即该步失败并中断</span>
        <div class="step-ops">
          <el-button text size="small" :disabled="i === 0" @click="moveStep(i, -1)">↑</el-button>
          <el-button text size="small" :disabled="i === rows.length - 1" @click="moveStep(i, 1)">↓</el-button>
          <el-button text size="small" type="danger" @click="removeStep(i)">✕ 删除</el-button>
        </div>
      </div>

      <div class="step-body">
        <template v-if="needLocator(r.action) || r.locator">
          <el-input v-model="r.locator" :placeholder="locatorPh(r.action)" class="mono" clearable>
            <template #prepend>定位器</template>
          </el-input>
          <div class="loc-hint" :class="'loc-' + locatorHint(r.locator).type">{{ locatorHint(r.locator).text }}</div>
        </template>
        <el-input
          v-if="needValue(r.action) || r.value"
          v-model="r.value"
          :placeholder="valuePh(r.action)"
          class="mono"
          @focus="focusRow = i"
        >
          <template #prepend>取值</template>
        </el-input>
        <el-input v-model="r.desc" placeholder="步骤备注（选填，仅展示用）" clearable>
          <template #prepend>备注</template>
        </el-input>
      </div>
    </div>

    <el-button size="small" type="primary" plain @click="addStep">＋ 添加步骤</el-button>

    <div class="hint" style="margin: 10px 0 0">
      定位器前缀：<span class="mono">css=</span>（默认，可省略）/ <span class="mono">xpath=</span> /
      <span class="mono">text=</span> / <span class="mono">role=button,name=登录</span> /
      <span class="mono">testid=login-btn</span> / <span class="mono">placeholder=请输入用户名</span> / <span class="mono">alt=头像</span>；
      <span class="mono">goto</span> 的取值填相对路径（自动拼环境 base_url）或完整 URL。
    </div>
    <div v-if="variables?.length" class="var-chips">
      <span class="hint" style="margin:0;line-height:22px">点变量插入到聚焦的取值框：</span>
      <el-tag v-for="v in variables" :key="v" size="small" class="var-chip" @click="insertVar(v)">{{ varChip(v) }}</el-tag>
    </div>
  </div>
</template>

<style scoped>
.ui-step-editor { display: flex; flex-direction: column; gap: 10px; }
.step-card { border: 1px solid #e4e7ed; border-radius: 8px; padding: 10px 12px; background: #fafbfc; }
.step-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.step-no { flex: 0 0 auto; width: 22px; height: 22px; line-height: 22px; text-align: center; border-radius: 50%; background: #409eff; color: #fff; font-size: 12px; }
.step-ops { margin-left: auto; display: flex; align-items: center; gap: 2px; white-space: nowrap; }
.expect-tag { color: #e6a23c; font-size: 12px; }
.step-body { display: flex; flex-direction: column; gap: 8px; }
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.hint { color: #909399; font-size: 12px; }
.opt-key { float: left; }
.opt-val { float: right; color: #a0a4a8; margin-left: 16px; }
.var-chips { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.var-chip { cursor: pointer; font-family: Consolas, Monaco, monospace; }
.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 8px 10px; background: #f5f7fa; border: 1px solid #e4e7ed; border-radius: 8px; }
.guide { margin: 0; }
.loc-hint { font-size: 12px; line-height: 18px; padding-left: 2px; }
.loc-info { color: #909399; }
.loc-success { color: #67c23a; }
.loc-warning { color: #e6a23c; }
</style>
