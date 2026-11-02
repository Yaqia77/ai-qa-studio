/**
 * SSE 流式读取：POST + JSON body + ReadableStream 手工分帧。
 *
 * 为什么不用 axios / EventSource：
 * - axios 拿不到渐进式 body（响应在 resolve 前是整包缓冲），流式渲染必须走 fetch；
 * - 原生 EventSource 只支持 GET 且不能带自定义头（X-Agent-Key），对话端点是 POST JSON；
 * - 分帧逻辑就十几行，不值得为此引一个依赖。
 */
import type { ChatEvent, ChatReq } from "../types";

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
  });
  if (!resp.ok) {
    // 流开始前的错误仍是统一 JSON 形状 {detail, code, trace_id}：取 detail 给用户看
    let msg = `Agent 调用失败（HTTP ${resp.status}）`;
    try {
      const j = await resp.json();
      if (j?.detail) msg = String(j.detail);
      if (j?.code === "unauthorized") msg = "Agent 网关鉴权失败：请在浏览器 localStorage 设置 agent_key 后重试";
    } catch { /* 非 JSON 错误体，保留状态码文案 */ }
    throw new Error(msg);
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
