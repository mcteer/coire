import type { components } from "./schema";
export type ConsoleSnapshot = components["schemas"]["ConsoleSnapshot"];
export type User = components["schemas"]["User"];
export type AuditRecord = components["schemas"]["AuditRecord"];
export type AskResponse = components["schemas"]["AskResponse"];
export type ApiKeyIssued = components["schemas"]["ApiKeyIssued"];
export type ApiKey = components["schemas"]["ApiKey"];
export type ModelVariant = components["schemas"]["ModelVariant"];
export type ActivityItem = components["schemas"]["ActivityItem"];
export type ActivityPage = components["schemas"]["CursorPage_ActivityItem_"];
export type OpsConversation = components["schemas"]["OpsConversation"];
export type OpsProposal = components["schemas"]["OpsProposal"];
export type OpsProposalIssued = components["schemas"]["OpsProposalIssued"];
export type OpsTurnResponse = components["schemas"]["OpsTurnResponse"];
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly problem: unknown = null,
  ) {
    super(message);
  }
}
function problemMessage(problem: unknown, status: number): string {
  if (!problem || typeof problem !== "object") return `Request failed (${status})`;
  const value = problem as { title?: unknown; detail?: unknown };
  if (typeof value.detail === "string") return value.detail;
  if (value.detail && typeof value.detail === "object") {
    const nested = value.detail as { detail?: unknown; code?: unknown };
    if (typeof nested.detail === "string") return nested.detail;
    if (typeof nested.code === "string") return nested.code.replaceAll("_", " ");
  }
  if (typeof value.title === "string") return value.title;
  return `Request failed (${status})`;
}
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const problem: unknown = await response.json().catch(() => null);
    throw new ApiError(response.status, problemMessage(problem, response.status), problem);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const startOpsConversation = () =>
  api<OpsConversation>("/api/v1/admin/ops/conversations", {
    method: "POST",
    body: "{}",
  });

export const sendOpsMessage = (conversationId: string, question: string) =>
  api<OpsTurnResponse>(`/api/v1/admin/ops/conversations/${conversationId}/messages`, {
    method: "POST",
    body: JSON.stringify({ question }),
  });

export const confirmOpsProposal = (issued: OpsProposalIssued) =>
  api<OpsProposal>(`/api/v1/admin/ops/proposals/${issued.proposal.id}/confirm`, {
    method: "POST",
    body: JSON.stringify({
      confirm_token: issued.confirm_token,
      action: issued.proposal.action,
    }),
  });

export const declineOpsProposal = (proposalId: string, reason?: string) =>
  api<OpsProposal>(`/api/v1/admin/ops/proposals/${proposalId}/decline`, {
    method: "POST",
    body: JSON.stringify({ reason: reason || null }),
  });
