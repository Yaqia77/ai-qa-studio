<script setup lang="ts">
/**
 * 批量导入导出弹窗壳：把「UI 用例批量导入」的布局模板统一到各用例类型（接口/UI）。
 *
 * 布局（自上而下）：通道切换（radio 按钮组）+ 模板下载/导出等文字动作 + 右侧选择文件上传
 * → 提示条 → 主体（默认插槽，由宿主页面渲染各自的预览表/粘贴框）→ 结果（#result 插槽）
 * → 底部（关闭 + #footer 插槽的操作按钮，插槽作用域带当前通道）。
 * 本组件只管布局与事件透传，不含任何解析/落盘业务：预览→确认→导入的链路由宿主页面实现。
 */
interface IoChannel { key: string; label: string }
interface IoAction { key: string; label: string }

withDefaults(defineProps<{
  visible: boolean;
  title: string;
  /** 导入通道（如 excel/json、smart/direct），至少一个 */
  channels: IoChannel[];
  /** 当前通道（配合 @update:mode 做 v-model:mode） */
  mode: string;
  /** 顶部提示条文案 */
  alertText: string;
  /** 文字动作按钮（下载模板/导出等），点击 emit("action", key) */
  actions?: IoAction[];
  /** 文件选择的 accept */
  accept?: string;
  /** 上传按钮 loading（解析中） */
  busy?: boolean;
  /** 是否有写权限：false 时隐藏上传入口（只保留模板下载/导出） */
  writable?: boolean;
  width?: string;
}>(), {
  actions: () => [],
  accept: ".xlsx,.xlsm",
  busy: false,
  writable: true,
  width: "780px",
});

const emit = defineEmits<{
  (e: "update:visible", v: boolean): void;
  (e: "update:mode", v: string): void;
  (e: "action", key: string): void;
  (e: "file", raw: File): void;
}>();

// el-upload 关闭自动上传，仅取原始 File 交给宿主页面分流处理
function onPick(uploadFile: { raw?: File }) {
  if (uploadFile?.raw) emit("file", uploadFile.raw);
}
</script>

<template>
  <el-dialog :model-value="visible" :title="title" :width="width"
             @update:model-value="emit('update:visible', !!$event)">
    <div class="io-bar">
      <el-radio-group :model-value="mode" size="small"
                      @update:model-value="emit('update:mode', String($event))">
        <el-radio-button v-for="c in channels" :key="c.key" :value="c.key">{{ c.label }}</el-radio-button>
      </el-radio-group>
      <el-button v-for="a in actions" :key="a.key" size="small" text type="primary"
                 @click="emit('action', a.key)">{{ a.label }}</el-button>
      <el-upload v-if="writable" :auto-upload="false" :show-file-list="false" :accept="accept"
                 style="margin-left: auto" :on-change="onPick">
        <el-button size="small" type="primary" plain :loading="busy">选择文件上传</el-button>
      </el-upload>
    </div>

    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
      <template #title>{{ alertText }}</template>
    </el-alert>

    <slot :mode="mode" />
    <slot name="result" />

    <template #footer>
      <span v-if="$slots['footer-info']" class="io-foot-info"><slot name="footer-info" /></span>
      <el-button @click="emit('update:visible', false)">关闭</el-button>
      <slot name="footer" :mode="mode" />
    </template>
  </el-dialog>
</template>

<style scoped>
.io-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.io-foot-info { float: left; color: #909399; font-size: 12px; line-height: 32px; }
</style>
