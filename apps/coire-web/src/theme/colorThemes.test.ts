import { afterEach, expect, test } from "vitest";
import {
  applyColorTheme,
  clearColorTheme,
  colorTokens,
  colorsError,
  contrast,
  defaultPreferences,
  parsePreferences,
  presets,
  themeError,
  type ColorTheme,
} from "./colorThemes";
const custom: ColorTheme = {
  ...presets[1],
  id: "custom-00000000-0000-4000-8000-000000000001",
  name: "My blue",
};
afterEach(clearColorTheme);

test("restores valid bounded preferences and preserves custom themes when a selection is stale", () => {
  const saved = { version: 1, selectedId: custom.id, customThemes: [custom] };
  expect(parsePreferences(JSON.stringify(saved))).toEqual(saved);
  expect(parsePreferences(JSON.stringify({ ...saved, selectedId: "missing" }))).toEqual({
    ...saved,
    selectedId: "original",
  });
  for (const raw of [
    null,
    "{",
    "null",
    "x".repeat(30001),
    JSON.stringify({ ...saved, version: 2 }),
    JSON.stringify({ ...saved, customThemes: Array(13).fill(custom) }),
    JSON.stringify({ ...saved, customThemes: [custom, custom] }),
  ])
    expect(parsePreferences(raw)).toEqual(defaultPreferences());
});

test("rejects invalid colors, unsafe stored values and unreadable opposing surfaces", () => {
  expect(
    themeError({ ...custom, light: { ...custom.light, accent: "url(https://example.test)" } }),
  ).not.toBeNull();
  expect(
    parsePreferences(
      JSON.stringify({
        version: 1,
        selectedId: custom.id,
        customThemes: [{ ...custom, dark: { ...custom.dark, page: "red" } }],
      }),
    ),
  ).toEqual(defaultPreferences());
  expect(colorsError({ ...custom.light, page: "#ffffff", surface: "#000000" })).toContain(
    "readable",
  );
  expect(themeError({ ...custom, name: " " })).toContain("name");
});

const colors = [
  ...presets.flatMap((t) => [t.light, t.dark]),
  { page: "#777777", surface: "#777777", sidebar: "#777777", accent: "#777777" },
  { page: "#ffffff", surface: "#ffffff", sidebar: "#000000", accent: "#ffff00" },
];
test.each(colors)("derived text remains readable for $page / $accent", (c) => {
  const t = colorTokens(c);
  for (const ink of ["--ink", "--ink-2", "--ink-3", "--ink-4", "--accent-ink"])
    for (const bg of ["--bg", "--surface", "--surface-2", "--surface-3", "--accent-soft"])
      expect(contrast(t[ink], t[bg])).toBeGreaterThanOrEqual(4.5);
  for (const ink of ["--sidebar-ink", "--sidebar-muted", "--sidebar-active"])
    for (const bg of ["--sidebar-bg", "--sidebar-hover"])
      expect(contrast(t[ink], t[bg])).toBeGreaterThanOrEqual(4.5);
  expect(contrast(t["--accent-text"], t["--accent"])).toBeGreaterThanOrEqual(4.5);
  for (const name of ["ok", "warm", "bad", "cold"])
    expect(contrast(t[`--${name}-ink`], t[`--${name}-soft`])).toBeGreaterThanOrEqual(4.5);
});

test("Original clears palette overrides without removing unrelated document styles", () => {
  document.documentElement.style.setProperty("--unrelated-test", "keep");
  applyColorTheme(presets[1], "dark");
  expect(document.documentElement.style.getPropertyValue("--bg")).toBe(presets[1].dark.page);
  applyColorTheme(presets[0], "light");
  expect(document.documentElement.style.getPropertyValue("--bg")).toBe("");
  expect(document.documentElement.style.getPropertyValue("--unrelated-test")).toBe("keep");
  document.documentElement.style.removeProperty("--unrelated-test");
});
