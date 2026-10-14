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
export type UpgradeOperation = components["schemas"]["UpgradeOperation"];
export type UpgradeReleasePage = components["schemas"]["UpgradeReleasePage"];
export type UpgradeReleaseSummary = components["schemas"]["UpgradeReleaseSummary"];
export type UpgradeTargetList = components["schemas"]["UpgradeTargetList"];
export type UpgradeEventPage = components["schemas"]["UpgradeEventPage"];
export type UpgradeStartRequest = components["schemas"]["UpgradeStartRequest"];
export type UpgradeRollbackRequest = components["schemas"]["UpgradeRollbackRequest"];

const upgradesPath = "/api/v1/admin/upgrades";
export const listUpgradeReleases = (cursor?: string) =>
  api<UpgradeReleasePage>(
    `${upgradesPath}/releases${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
export const getUpgradeTargets = () => api<UpgradeTargetList>(`${upgradesPath}/targets`);
export const getUpgradeOperation = (jobId: string) =>
  api<UpgradeOperation>(`${upgradesPath}/${encodeURIComponent(jobId)}`);
export const startStudioUpgrade = (
  node: "coire-edge-a" | "coire-edge-b",
  body: UpgradeStartRequest,
) =>
  api<UpgradeOperation>(`${upgradesPath}/nodes/${node}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const startCoreUpgrade = (body: UpgradeStartRequest) =>
  api<UpgradeOperation>(`${upgradesPath}/control-plane`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const rollbackUpgrade = (jobId: string, body: UpgradeRollbackRequest) =>
  api<UpgradeOperation>(`${upgradesPath}/${encodeURIComponent(jobId)}/rollback`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const cancelUpgrade = (jobId: string, reason: string = "administrator_cancelled") =>
  api<UpgradeOperation>(`${upgradesPath}/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
export const upgradeEventsUrl = (jobId: string) =>
  `${upgradesPath}/${encodeURIComponent(jobId)}/events`;
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
export async function apiError(response: Response): Promise<ApiError> {
  const problem: unknown = await response.json().catch(() => null);
  return new ApiError(response.status, problemMessage(problem, response.status), problem);
}
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    throw await apiError(response);
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
