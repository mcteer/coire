import type { components as FailoverComponents } from "./failover-schema";
import type { components as CoreComponents } from "./schema";
import { api, ApiError } from "./client";

export type FailoverStatus = FailoverComponents["schemas"]["FailoverStatus"];
export type ResidentModel = FailoverComponents["schemas"]["GatewayModel"];
export type CompletionRequest = CoreComponents["schemas"]["ChatCompletionRequest"];

export const readFailoverStatus = () => api<FailoverStatus>("/failover/tier");
export const readResidentModels = async () =>
  (await api<FailoverComponents["schemas"]["GatewayModelList"]>("/v1/models")).data;

function contentFromEvent(value: unknown): string {
  if (typeof value !== "object" || value === null || !("choices" in value)) return "";
  const choices = value.choices;
  if (!Array.isArray(choices) || choices.length === 0) return "";
  const first: unknown = choices[0];
  if (typeof first !== "object" || first === null || !("delta" in first)) return "";
  const delta: unknown = first.delta;
  if (typeof delta !== "object" || delta === null || !("content" in delta)) return "";
  return typeof delta.content === "string" ? delta.content : "";
}

export async function streamFailoverCompletion(
  model: string,
  messages: CompletionRequest["messages"],
  onContent: (content: string) => void,
): Promise<void> {
  const body: CompletionRequest = {
    model,
    messages,
    stream: true,
    coire_wait_for_model: false,
  };
  const response = await fetch("/v1/chat/completions", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const problem: unknown = await response.json().catch(() => null);
    throw new ApiError(response.status, "Failover inference is unavailable", problem);
  }
  if (!response.body) throw new Error("Inference stream is unavailable");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consume = (frame: string) => {
    const data = frame
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim())
      .join("\n");
    if (!data || data === "[DONE]") return;
    const parsed: unknown = JSON.parse(data);
    onContent(contentFromEvent(parsed));
  };
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = buffer.replaceAll("\r\n", "\n");
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) consume(frame);
  }
  buffer += decoder.decode();
  if (buffer.trim()) consume(buffer);
}
