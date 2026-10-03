import { afterEach, expect, test, vi } from "vitest";
import {
  createImagePreset,
  downloadImageOutput,
  requestFromImageOutput,
  retireImagePreset,
  updateImagePreset,
  uploadImageInput,
  uploadImageRecipe,
  type ImageOutput,
} from "./images";

const outputId = "00000000-0000-0000-0000-000000000001";
const path = `/api/v1/image-outputs/${outputId}/content`;

afterEach(() => vi.unstubAllGlobals());

test("uploads bounded private inputs with purpose and actual byte count", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 202,
    json: async () => ({ id: "input-1", state: "processing", purpose: "init" }),
  });
  vi.stubGlobal("fetch", fetchMock);
  const file = new File(["image"], "source.png", { type: "image/png" });
  await uploadImageInput(file, "init");
  const [url, request] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/v1/image-inputs");
  expect(request.method).toBe("POST");
  expect(request.credentials).toBe("same-origin");
  const body = request.body as FormData;
  expect(body.get("purpose")).toBe("init");
  expect(body.get("byte_count")).toBe(String(file.size));
  expect(body.get("file")).toBe(file);
  await expect(
    uploadImageRecipe(new File(["image"], "bad.jpg", { type: "image/jpeg" })),
  ).rejects.toThrow("PNG recipe");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("regeneration restores effective fields once and preserves source input bindings", () => {
  const inputId = "00000000-0000-0000-0000-000000000042";
  const source = {
    recipe: {
      resolved: {
        spec: {
          schema_version: 1,
          model_id: "00000000-0000-0000-0000-000000000002",
          mode: "img2img",
          prompt: "already prefixed, direct subject",
          width: 512,
          height: 512,
          steps: 9,
          guidance: "0.125",
          seed: 7,
          n: 1,
          init_image_id: inputId,
          strength: "0.375",
        },
      },
    },
  } as ImageOutput;
  const unchanged = requestFromImageOutput(source);
  const newSeed = requestFromImageOutput(source, true);
  expect(unchanged).toMatchObject({
    model_id: source.recipe.resolved.spec.model_id,
    prompt: "already prefixed, direct subject",
    preset_id: null,
    preset_revision: null,
    init_image_id: inputId,
    strength: "0.375",
    guidance: "0.125",
    seed: 7,
  });
  expect(newSeed).toMatchObject({ init_image_id: inputId, seed: null });
});

test("redeems a download grant in a header without sending its fragment", async () => {
  const grant = {
    output_id: outputId,
    url: `${path}#grant=private-token`,
    expires_at: "2026-09-30T20:00:00Z",
  };
  const png = new Blob(["png"], { type: "image/png" });
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => grant })
    .mockResolvedValueOnce({ ok: true, status: 200, blob: async () => png });
  vi.stubGlobal("fetch", fetchMock);
  expect(await downloadImageOutput(outputId)).toBe(png);
  expect(fetchMock).toHaveBeenNthCalledWith(2, path, {
    credentials: "same-origin",
    headers: { "X-Coire-Image-Grant": "private-token" },
  });
});

test("rejects a grant that points outside the owner's content path", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ output_id: outputId, url: "https://other.test/image#grant=token" }),
  });
  vi.stubGlobal("fetch", fetchMock);
  await expect(downloadImageOutput(outputId)).rejects.toThrow("Image download grant is invalid");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("refreshes one expired content grant before returning a private image", async () => {
  const grant = (token: string) => ({
    output_id: outputId,
    url: `${path}#grant=${token}`,
  });
  const png = new Blob(["png"], { type: "image/png" });
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => grant("expired") })
    .mockResolvedValueOnce({ ok: false, status: 404 })
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => grant("fresh") })
    .mockResolvedValueOnce({ ok: true, status: 200, blob: async () => png });
  vi.stubGlobal("fetch", fetchMock);
  expect(await downloadImageOutput(outputId)).toBe(png);
  expect(fetchMock).toHaveBeenNthCalledWith(4, path, {
    credentials: "same-origin",
    headers: { "X-Coire-Image-Grant": "fresh" },
  });
});

test("preset mutations use the admin routes and carry the expected revision", async () => {
  const presetId = "00000000-0000-0000-0000-000000000010";
  const created = { id: presetId, revision: 1 };
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce({ ok: true, status: 201, json: async () => created })
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ ...created, revision: 2 }),
    })
    .mockResolvedValueOnce({ ok: true, status: 204 });
  vi.stubGlobal("fetch", fetchMock);
  const body = {
    name: "Portrait",
    prompt_prefix: "",
    defaults: {
      schema_version: 1 as const,
      model_id: "00000000-0000-0000-0000-000000000002",
      prompt: "portrait",
    },
  };
  await createImagePreset(body);
  await updateImagePreset(presetId, { expected_revision: 1, name: "Portrait v2" });
  await retireImagePreset(presetId);
  expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/admin/image-presets", {
    credentials: "same-origin",
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" },
  });
  expect(fetchMock).toHaveBeenNthCalledWith(2, `/api/v1/admin/image-presets/${presetId}`, {
    credentials: "same-origin",
    method: "PATCH",
    body: JSON.stringify({ expected_revision: 1, name: "Portrait v2" }),
    headers: { "Content-Type": "application/json" },
  });
  expect(fetchMock).toHaveBeenNthCalledWith(3, `/api/v1/admin/image-presets/${presetId}`, {
    credentials: "same-origin",
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
  });
});
