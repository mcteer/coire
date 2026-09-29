import { beforeEach, expect, test } from "vitest";
import { loadChatDrafts, saveChatDrafts } from "./chatDrafts";

const ownerA = "00000000-0000-0000-0000-000000000001";
const ownerB = "00000000-0000-0000-0000-000000000002";
const modelId = "00000000-0000-0000-0000-000000000003";

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
