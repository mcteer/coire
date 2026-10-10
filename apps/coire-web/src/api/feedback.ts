/** Private feedback transport; every wire shape comes from generated contracts. */
import { api } from "./client";
import type { components } from "./schema";

export type FeedbackPreference = components["schemas"]["FeedbackPreference"];
export type FeedbackPreferenceUpdate = components["schemas"]["FeedbackPreferenceUpdate"];
export type ThumbUpdate = components["schemas"]["ThumbUpdate"];
export type FeedbackReceipt = components["schemas"]["FeedbackReceipt"];
export type ConversationFeedbackPage = components["schemas"]["ConversationFeedbackPage"];
export type MessageFeedback = components["schemas"]["MessageFeedback"];
export type ComparisonCreate = components["schemas"]["ComparisonCreate"];
export type ComparisonDetail = components["schemas"]["ComparisonDetail"];
export type ComparisonReceipt = components["schemas"]["ComparisonReceipt"];
export type ComparisonSelect = components["schemas"]["ComparisonSelect"];
export type ComparisonDismiss = components["schemas"]["ComparisonDismiss"];
export type ComparisonSelectionReceipt = components["schemas"]["ComparisonSelectionReceipt"];
export type ComparisonEvent = components["schemas"]["ComparisonEvent"];
export type PreferenceExportCreate = components["schemas"]["PreferenceExportCreate"];
export type PreferenceExportDetail = components["schemas"]["PreferenceExportDetail"];
export type PreferenceExportReceipt = components["schemas"]["PreferenceExportReceipt"];
export type PreferenceExportPage = components["schemas"]["PreferenceExportPage"];

const chat = "/api/v1/chat";
const exportsPath = "/api/v1/admin/feedback/exports";
const conversationPath = (id: string) => `${chat}/conversations/${encodeURIComponent(id)}`;
const comparisonPath = (conversation: string, id: string) =>
  `${conversationPath(conversation)}/comparisons/${encodeURIComponent(id)}`;
const pageQuery = (cursor?: string | null) =>
  `?limit=25${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;

export const getFeedbackPreference = () => api<FeedbackPreference>(`${chat}/feedback-preference`);
export const updateFeedbackPreference = (body: FeedbackPreferenceUpdate) =>
  api<FeedbackPreference>(`${chat}/feedback-preference`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
export const getConversationFeedback = (id: string, cursor?: string | null) =>
  api<ConversationFeedbackPage>(`${conversationPath(id)}/feedback${pageQuery(cursor)}`);
export const updateThumb = (conversation: string, message: string, body: ThumbUpdate) =>
  api<FeedbackReceipt>(
    `${conversationPath(conversation)}/messages/${encodeURIComponent(message)}/feedback`,
    {
      method: "PUT",
      body: JSON.stringify(body),
    },
  );
export const createComparison = (conversation: string, body: ComparisonCreate) =>
  api<ComparisonReceipt>(`${conversationPath(conversation)}/comparisons`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const getComparison = (conversation: string, id: string) =>
  api<ComparisonDetail>(comparisonPath(conversation, id));
export const selectComparison = (conversation: string, id: string, body: ComparisonSelect) =>
  api<ComparisonSelectionReceipt>(`${comparisonPath(conversation, id)}/selection`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const dismissComparison = (conversation: string, id: string, body: ComparisonDismiss) =>
  api<ComparisonReceipt>(`${comparisonPath(conversation, id)}/dismiss`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const submitFeedbackExport = (body: PreferenceExportCreate, key: string) =>
  api<PreferenceExportReceipt>(exportsPath, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Idempotency-Key": key },
  });
export const listFeedbackExports = (cursor?: string | null) =>
  api<PreferenceExportPage>(`${exportsPath}${pageQuery(cursor)}`);
export const getFeedbackExport = (id: string) =>
  api<PreferenceExportDetail>(`${exportsPath}/${encodeURIComponent(id)}`);
export const cancelFeedbackExport = (id: string, expectedVersion: number, key: string) =>
  api<PreferenceExportReceipt>(`${exportsPath}/${encodeURIComponent(id)}/cancel`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion }),
    headers: { "Idempotency-Key": key },
  });
export type FeedbackReviewDetail = components["schemas"]["FeedbackReviewDetail"];
export type ReviewState = "unreviewed" | "reviewed" | "skipped";
const reviewPath = "/api/v1/admin/feedback/comparisons";
export const listFeedbackReview = (state: ReviewState, cursor?: string | null) =>
  api<components["schemas"]["FeedbackReviewPage"]>(
    `${reviewPath}?state=${state}&limit=25${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
  );
export const getFeedbackReview = (id: string) =>
  api<FeedbackReviewDetail>(`${reviewPath}/${encodeURIComponent(id)}`);
export const judgeFeedbackPair = (
  id: string,
  body: components["schemas"]["AdminPairJudgement"],
  key: string = crypto.randomUUID(),
) =>
  api<components["schemas"]["FeedbackReviewReceipt"]>(
    `${reviewPath}/${encodeURIComponent(id)}/judgement`,
    { method: "PUT", body: JSON.stringify(body), headers: { "Idempotency-Key": key } },
  );
