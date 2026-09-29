import { beforeEach, expect, test } from "vitest";
import { loadChatDrafts, saveChatDrafts } from "./chatDrafts";

const ownerA = "00000000-0000-0000-0000-000000000001";
const ownerB = "00000000-0000-0000-0000-000000000002";
const modelId = "00000000-0000-0000-0000-000000000003";
const conversationId = "00000000-0000-0000-0000-000000000004";
const fileId = "00000000-0000-0000-0000-000000000005";

beforeEach(() => sessionStorage.clear());

test("restores bounded same-tab text and model for the same owner", () => {
  const drafts = new Map([["new", { text: "Unsent", modelId }]]);
  loadChatDrafts(ownerA);
  saveChatDrafts(ownerA, drafts);
  expect(loadChatDrafts(ownerA).get("new")).toEqual({ text: "Unsent", modelId });
  expect(sessionStorage.getItem("coire.chat.drafts." + ownerA)).not.toContain("token");
});

test("identity change removes the previous owner's draft store", () => {
  loadChatDrafts(ownerA);
  saveChatDrafts(ownerA, new Map([["new", { text: "Private A", modelId: null }]]));
  expect(loadChatDrafts(ownerB).size).toBe(0);
  expect(sessionStorage.getItem("coire.chat.drafts." + ownerA)).toBeNull();
});

test("ignores oversized UTF-8 text and invalid stored IDs", () => {
  loadChatDrafts(ownerA);
  saveChatDrafts(
    ownerA,
    new Map([
      ["new", { text: "é".repeat(40_000), modelId: null }],
      ["not-a-conversation", { text: "bad", modelId: null }],
    ]),
  );
  expect(loadChatDrafts(ownerA).size).toBe(0);
  sessionStorage.setItem(
    "coire.chat.drafts." + ownerA,
    JSON.stringify([["new", { text: "okay", modelId: "not-a-model" }]]),
  );
  expect(loadChatDrafts(ownerA).size).toBe(0);
});

test("stores only bounded file IDs and page choices for the same owner", () => {
  loadChatDrafts(ownerA);
  saveChatDrafts(
    ownerA,
    new Map([
      [
        conversationId,
        {
          text: "Read this",
          modelId,
          files: [{ file_id: fileId, mode: "visual" as const, pages: [1, 3] }],
          token: "must never persist",
        },
      ],
    ]),
  );
  expect(loadChatDrafts(ownerA).get(conversationId)?.files).toEqual([
    { file_id: fileId, mode: "visual", pages: [1, 3] },
  ]);
  expect(sessionStorage.getItem("coire.chat.drafts." + ownerA)).not.toContain("must never persist");
  expect(loadChatDrafts(ownerB).size).toBe(0);
});

test("rejects forged, duplicate and out-of-range saved file choices", () => {
  loadChatDrafts(ownerA);
  sessionStorage.setItem(
    "coire.chat.drafts." + ownerA,
    JSON.stringify([
      [
        conversationId,
        { text: "bad", modelId, files: [{ file_id: fileId, mode: "text", pages: [1] }] },
      ],
      [
        conversationId,
        { text: "bad", modelId, files: [{ file_id: fileId, mode: "visual", pages: [51] }] },
      ],
      [
        conversationId,
        {
          text: "bad",
          modelId,
          files: [
            { file_id: fileId, mode: "visual", pages: [1] },
            { file_id: fileId, mode: "visual", pages: [2] },
          ],
        },
      ],
    ]),
  );
  expect(loadChatDrafts(ownerA).size).toBe(0);
});
