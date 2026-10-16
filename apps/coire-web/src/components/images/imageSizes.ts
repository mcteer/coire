import type { ImageModelList } from "../../api/images";
export type SizeLimits = NonNullable<ImageModelList["items"][number]["capability"]>;
export const aspectRatios = [
  { id: "1:1", label: "Square · 1:1", width: 1, height: 1 },
  { id: "4:3", label: "Landscape · 4:3", width: 4, height: 3 },
  { id: "3:2", label: "Landscape · 3:2", width: 3, height: 2 },
  { id: "16:9", label: "Widescreen · 16:9", width: 16, height: 9 },
  { id: "3:4", label: "Portrait · 3:4", width: 3, height: 4 },
  { id: "2:3", label: "Portrait · 2:3", width: 2, height: 3 },
  { id: "9:16", label: "Tall portrait · 9:16", width: 9, height: 16 },
] as const;
export function aspectOf(width: number, height: number): string {
  return aspectRatios.find((r) => width * r.height === height * r.width)?.id ?? "custom";
}
export function fitImageSize(
  edge: number,
  ratio: { width: number; height: number },
  limits: SizeLimits,
): { width: number; height: number } | null {
  const scale = Math.floor(
    Math.min(
      edge / Math.max(ratio.width, ratio.height),
      limits.max_width / ratio.width,
      limits.max_height / ratio.height,
      Math.sqrt(limits.max_pixels / (ratio.width * ratio.height)),
    ),
  );
  const width = ratio.width * scale,
    height = ratio.height * scale;
  return width >= limits.min_width && height >= limits.min_height ? { width, height } : null;
}
export function validImageSize(width: number, height: number, limits: SizeLimits): boolean {
  return (
    Number.isInteger(width) &&
    Number.isInteger(height) &&
    width >= limits.min_width &&
    height >= limits.min_height &&
    width <= limits.max_width &&
    height <= limits.max_height &&
    width * height <= limits.max_pixels &&
    Math.ceil(width / 16) * 16 * Math.ceil(height / 16) * 16 <= 16000000
  );
}
