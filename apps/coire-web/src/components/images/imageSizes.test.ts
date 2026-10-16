import { expect, test } from "vitest";
import {
  aspectOf,
  aspectRatios,
  fitImageSize,
  validImageSize,
  type SizeLimits,
} from "./imageSizes";
const limits = {
  min_width: 512,
  min_height: 512,
  max_width: 1980,
  max_height: 1980,
  max_pixels: 1980 ** 2,
} as SizeLimits;
test("keeps exact whole-pixel aspect ratios within the 1980 edge", () => {
  expect(fitImageSize(1980, aspectRatios[0], limits)).toEqual({ width: 1980, height: 1980 });
  expect(fitImageSize(1980, aspectRatios[3], limits)).toEqual({ width: 1968, height: 1107 });
  expect(fitImageSize(1980, aspectRatios[6], limits)).toEqual({ width: 1107, height: 1968 });
  expect(aspectOf(1968, 1107)).toBe("16:9");
  expect(aspectOf(1980, 1107)).toBe("custom");
});
test("refuses unqualified small axes, oversized pixels and invalid custom dimensions", () => {
  expect(fitImageSize(512, aspectRatios[3], limits)).toBeNull();
  expect(validImageSize(1980, 1980, limits)).toBe(true);
  expect(validImageSize(1981, 1980, limits)).toBe(false);
  expect(validImageSize(1980.5, 1107, limits)).toBe(false);
  expect(fitImageSize(1980, aspectRatios[0], { ...limits, max_pixels: 1024 ** 2 })).toEqual({
    width: 1024,
    height: 1024,
  });
});
