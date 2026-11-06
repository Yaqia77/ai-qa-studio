/**
 * SSE 流式读取：POST + JSON body + ReadableStream 手工分帧。
 *
 * 为什么不用 axios / EventSource：
 * - axios 拿不到渐进式 body（响应在 resolve 前是整包缓冲），流式渲染必须走 fetch；
 * - 原生 EventSource 只支持 GET 且不能带自定义头（X-Agent-Key），对话端点是 POST JSON；
 * - 分帧逻辑就十几行，不值得为此引一个依赖。
 */
import type { ChatEvent, ChatReq } from "../types";
import { friendlyAgentError, isUserReadable } from "./errors";

export interface SseHandle {
  /** 服务端 done 事件里的 session_id（回退用响应头 X-Session-Id） */
  sessionId: string;
}

export async function postChatStream(
  body: ChatReq,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<SseHandle> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  // 网关 Key 与 axios 通道同源（localStorage），本地 require_key=false 时留空即可
  const key = localStorage.getItem("agent_key");
  if (key) headers["X-Agent-Key"] = key;

  const resp = await fetch("/agent/v1/chat/stream", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    signal,
  }).catch((e: unknown) => {
    // 手动停止不是故障：原样上抛，由调用方按 AbortError 静默处理
    if ((e as Error)?.name === "AbortError") throw e;
    // 网络层失败（服务未起/断网）：不暴露端口与部署细节，技术定位留给控制台与留痕
    throw new Error("智能助手服务暂时不可达，请稍后重试或联系管理员");
  });
  if (!resp.ok) {
    // 流开始前的错误仍是统一 JSON 形状 {detail, code, trace_id}：
    // detail 面向用户才直出，技术性文案（异常原文/端口/内部服务名）按 code 翻译成可读提示
    let msg = friendlyAgentError(resp.status >= 500 ? "internal_error" : undefined, "").msg;
    let code = "";
    let isJson = false;
    try {
      const j = await resp.json();
      isJson = true;
      code = String(j?.code || "");
      const d = j?.detail ? String(j.detail) : "";
      msg = isUserReadable(d) ? d : friendlyAgentError(code || undefined, d).msg;
      if (code === "unauthorized") msg = "智能助手鉴权失败，请联系管理员检查接入配置";
    } catch { /* 非 JSON 错误体 */ }
    // 5xx 且非统一 JSON（如开发现代理解层报的错体）：几乎总是 Agent 服务本身掉了，
    // “服务不可达”比“服务器内部错误”更贴近事实与用户可行动作
    if (!isJson && resp.status >= 500) msg = "智能助手服务暂时不可达，请稍后重试或联系管理员";
    const err = new Error(msg) as Error & { tech?: string };
    err.tech = code;   // 随错误对象携带 code：错误气泡的详情区仍可展示
    throw err;
  }
  const headerSession = resp.headers.get("x-session-id") || "";
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let sessionId = headerSession;

  const handle = (block: string) => {
    let type = "";
    let data = "";
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event: ")) type = line.slice(7).trim();
      else if (line.startsWith("data: ")) data += line.slice(6).trim();
    }
    if (!type) return; // 注释/心跳帧
    let payload: unknown = {};
    try { payload = data ? JSON.parse(data) : {}; } catch { return; }
    if (type === "done") sessionId = String((payload as { session_id?: string }).session_id || sessionId);
    onEvent({ type, payload } as ChatEvent);
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      handle(block);
    }
  }
  if (buf.trim()) handle(buf); // 服务端最后一帧后无空行时兜底
  return { sessionId };
}
