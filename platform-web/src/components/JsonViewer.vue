<script setup lang="ts">
/** JSON 代码块查看器：嵌套 JSON 字符串自动解包 + 语法着色（键/字符串/数字/布尔/null）。
 *
 * 动机：接口响应的 body 常是一段被转义的 JSON 字符串（"{\"success\":true,...}"），
 * 直接 JSON.stringify 会显示成一堆反斜杠，可读性极差；这里先把「字符串里其实是 JSON」
 * 的值递归解析成结构，再着色。无第三方依赖，风格与整页浅色统一。 */
import { computed } from "vue";

const props = defineProps<{ data: unknown; maxHeight?: number }>();

/** 深度解包：任何「去空格后像 JSON」的字符串都尝试解析并继续下钻，解析失败则原样保留。 */
function unwrap(v: unknown): unknown {
  if (typeof v === "string") {
    const s = v.trim();
    if ((s.startsWith("{") && s.endsWith("}")) || (s.startsWith("[") && s.endsWith("]"))) {
      try {
        return unwrap(JSON.parse(s));
      } catch {
        return v; // 像 JSON 但解析失败：当作普通文本
      }
    }
    return v;
  }
  if (Array.isArray(v)) return v.map(unwrap);
  if (v && typeof v === "object") {
    const o: Record<string, unknown> = {};
    for (const k of Object.keys(v as Record<string, unknown>)) {
      o[k] = unwrap((v as Record<string, unknown>)[k]);
    }
    return o;
  }
  return v;
}

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** 先按 2 空格缩进序列化，再单遍正则分色：整段引号串（含其后冒号）优先成 token，
 *  避免字符串内部的数字/冒号被二次误染。 */
function highlight(obj: unknown): string {
  const json = JSON.stringify(obj, null, 2);
  if (json === undefined) return esc(String(obj));
  return esc(json).replace(
    /("(?:\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\b(?:true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (m) => {
      let cls = "jv-num";
      if (m[0] === '"') cls = /:\s*$/.test(m) ? "jv-key" : "jv-str";
      else if (m === "true" || m === "false") cls = "jv-bool";
      else if (m === "null") cls = "jv-null";
      // key 分支保留其后的冒号，仅给引号部分上色会打断对齐，这里整段（含冒号）同色即可
      return `<span class="${cls}">${m}</span>`;
    },
  );
}

const html = computed(() => highlight(unwrap(props.data)));
const isEmpty = computed(() => props.data == null);
</script>

<template>
  <pre
    v-if="!isEmpty"
    class="jv"
    :style="maxHeight ? { maxHeight: maxHeight + 'px' } : undefined"
    v-html="html"
  ></pre>
  <span v-else class="jv-empty">—</span>
</template>

<style scoped>
.jv {
  background: #f8f9fb;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 10px 12px;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 460px;
  overflow: auto;
  margin: 0;
  color: #303133;
}
.jv :deep(.jv-key) { color: #a71d5d; }   /* 键：玫红 */
.jv :deep(.jv-str) { color: #0a7d34; }   /* 字符串：绿 */
.jv :deep(.jv-num) { color: #1673b1; }   /* 数字：蓝 */
.jv :deep(.jv-bool) { color: #cf581b; }  /* 布尔：橙 */
.jv :deep(.jv-null) { color: #8a8f99; font-style: italic; }  /* null：灰斜体 */
.jv-empty { color: #909399; }
</style>
