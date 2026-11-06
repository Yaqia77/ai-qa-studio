/**
 * 错误文案归一：把技术性报错翻译成用户能看懂的话。
 *
 * 原则：面向用户的提示绝不出现端口号、内部服务名、命令行/配置项、Python 异常原文——
 * 普通用户看不懂也解决不了；技术原文折叠保留（错误气泡「详情」），管理员排障仍可用。
 */

// axios 请求级配置扩展：silent=true 的后台探测请求失败不弹全局错误提示
// （模块 augmentation 必须落在顶层声明文件里）
declare module "axios" {
  // eslint-disable-next-line @typescript-eslint/no-empty-interface
  interface AxiosRequestConfig {
    silent?: boolean;
  }
}

/** 命中其一即判定为技术文案（英文报错、内部代号、部署细节），需要翻译。 */
const TECH_PATTERNS: RegExp[] = [
  /[A-Za-z]\w*Error\b/, // Python/JS 异常名：KeyError、ConnectionRefusedError…
  /\bqa-agent\b|\buvicorn\b|\bvLLM\b|\bFastAPI\b/i,
  /\b\d{1,3}(\.\d{1,3}){3}\b|\blocalhost\b|127\.0\.0\.1/i,
  /端口|\d{4}\s*端口|反代|localStorage|config\.yaml|\btraceback\b|堆栈/i,
  /\bHTTP\b\s?\d{3}|\berrno\b|ECONNREFUSED|getaddrinfo/i,
  /^[A-Za-z][A-Za-z0-9 _.-]*$/i, // 纯英文短语（Internal Server Error 之类）
];

/** 后端 detail 是否已面向用户：含中文且不含技术特征才算可读。 */
export function isUserReadable(text: string): boolean {
  if (!text) return false;
  if (!/[\u4e00-\u9fa5]/.test(text)) return false;
  return !TECH_PATTERNS.some((re) => re.test(text));
}

export interface FriendlyError {
  msg: string;
  /** 技术原文：错误气泡「详情」折叠展示；无技术背景时为空，不展示详情入口 */
  tech: string;
}

/** qa-agent error 事件 code（闭集真源在 qa-agent agent/core/errors.py）-> 用户可读文案。 */
const CODE_FRIENDLY: Record<string, string> = {
  platform_error: "平台服务响应异常，请稍后重试；若持续出现请联系管理员",
  provider_unavailable: "AI 模型暂时不可用，请稍后重试或换个模型",
  internal_error: "服务器内部错误，请稍后重试",
  config_error: "服务配置不完整，暂时无法使用，请联系管理员",
  invalid_request: "请求内容有误，请调整后重试",
  tool_not_found: "该能力暂未开放",
  skill_not_available: "该能力暂未开放",
  request_failed: "请求未能完成，请稍后重试",
  unauthorized: "智能助手鉴权失败，请联系管理员检查接入配置",
};

/** 事件 code + 技术 detail -> 用户可读：code 有映射用映射；detail 本就面向用户则沿用；兜底"服务器内部错误"。 */
export function friendlyAgentError(code?: string, detail?: string): FriendlyError {
  const d = (detail || "").trim();
  if (isUserReadable(d)) return { msg: d, tech: code && !CODE_FRIENDLY[code] ? d : "" };
  const friendly = (code && CODE_FRIENDLY[code]) || "服务器内部错误，请稍后重试";
  return { msg: friendly, tech: d };
}
