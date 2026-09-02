import { afterEach, expect, test, vi } from "vitest";
import { api } from "./client";

afterEach(() => vi.restoreAllMocks());

test("maps RFC 9457 detail into a typed API error", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ title: "Conflict", detail: "A newer version exists" }), {
        status: 409,
        headers: { "content-type": "application/problem+json" },
      }),
    ),
  );
  await expect(api("/resource")).rejects.toMatchObject({
    status: 409,
    message: "A newer version exists",
  });
});

test("maps nested Coire problem details without rendering object Object", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ detail: { code: "edit_conflict" } }), { status: 409 }),
      ),
  );
  await expect(api("/resource")).rejects.toThrow("edit conflict");
});
