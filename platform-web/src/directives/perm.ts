/**
 * v-perm 按钮级权限指令（设计文档：菜单级 → 按钮级 → 数据级）。
 *
 * 用法：
 *   v-perm="'report:delete'"            单个权限码
 *   v-perm="['case:delete','user:manage']"  任一命中即显示
 * 无权限时直接移除节点；真正的安全边界始终在后端接口二次校验。
 */
import type { Directive } from "vue";
import { hasPerm } from "../stores/auth";

export const perm: Directive<HTMLElement, string | string[]> = {
  mounted(el, binding) {
    const codes = Array.isArray(binding.value) ? binding.value : [binding.value];
    if (codes.length > 0 && !codes.some(hasPerm)) {
      el.parentNode?.removeChild(el);
    }
  },
};
