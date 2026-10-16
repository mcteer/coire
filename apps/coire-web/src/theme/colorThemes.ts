/** Browser-local color preferences. No credentials, application data or wire contracts. */
export type Appearance = "light" | "dark";
export type ThemeColors = { page: string; surface: string; sidebar: string; accent: string };
export type ColorTheme = { id: string; name: string; light: ThemeColors; dark: ThemeColors };
export type ColorPreferences = { version: 1; selectedId: string; customThemes: ColorTheme[] };
export const colorPreferencesKey = "coire.color-themes.v1";
export const maxCustomThemes = 12;
export const presets: readonly ColorTheme[] = [
  {
    id: "original",
    name: "Original",
    light: { page: "#faf8f5", surface: "#f2eee8", sidebar: "#f2eee8", accent: "#8a6830" },
    dark: { page: "#081027", surface: "#111d3b", sidebar: "#081027", accent: "#c9a05a" },
  },
  {
    id: "ocean",
    name: "Ocean",
    light: { page: "#f5f8fc", surface: "#ffffff", sidebar: "#eaf0f8", accent: "#2457a7" },
    dark: { page: "#101827", surface: "#182337", sidebar: "#0b1220", accent: "#79a7fa" },
  },
  {
    id: "forest",
    name: "Forest",
    light: { page: "#f6f8f2", surface: "#fefffc", sidebar: "#eaf0e5", accent: "#35654d" },
    dark: { page: "#111c18", surface: "#1b2b24", sidebar: "#0b1410", accent: "#8ac8a6" },
  },
  {
    id: "plum",
    name: "Plum",
    light: { page: "#faf6fa", surface: "#fffaff", sidebar: "#f1e8f2", accent: "#764774" },
    dark: { page: "#1d1521", surface: "#2a2030", sidebar: "#150f19", accent: "#d0a5d8" },
  },
];
export function validHex(value: unknown): value is string {
  return typeof value === "string" && /^#[\da-f]{6}$/i.test(value);
}
const rgb = (hex: string) => [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16));
export function mix(a: string, b: string, amount: number): string {
  const x = rgb(a),
    y = rgb(b);
  return (
    "#" +
    x
      .map((v, i) =>
        Math.round(v * (1 - amount) + y[i] * amount)
          .toString(16)
          .padStart(2, "0"),
      )
      .join("")
  );
}
function luminance(hex: string): number {
  return rgb(hex).reduce((sum, v, i) => {
    const c = v / 255;
    return (
      sum + [0.2126, 0.7152, 0.0722][i] * (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
    );
  }, 0);
}
export function contrast(a: string, b: string): number {
  const values = [luminance(a), luminance(b)].sort((x, y) => x - y);
  return (values[1] + 0.05) / (values[0] + 0.05);
}
const minimumContrast = (ink: string, surfaces: string[]) =>
  Math.min(...surfaces.map((bg) => contrast(ink, bg)));
function foreground(surfaces: string[]): string {
  return minimumContrast("#000000", surfaces) >= minimumContrast("#ffffff", surfaces)
    ? "#000000"
    : "#ffffff";
}
function surfaces(colors: ThemeColors): string[] {
  return [colors.page, colors.surface, mix(colors.surface, colors.page, 0.55)];
}
export function colorsError(value: unknown): string | null {
  if (!value || typeof value !== "object") return "Choose valid colors.";
  const c = value as ThemeColors;
  if (Object.keys(c).length !== 4 || ![c.page, c.surface, c.sidebar, c.accent].every(validHex))
    return "Use six-digit hex colors, such as #2457a7.";
  const backgrounds = surfaces(c);
  if (minimumContrast(foreground(backgrounds), backgrounds) < 4.5)
    return "Keep page and surface colors both light or both dark so text stays readable.";
  return null;
}
export function themeError(value: unknown): string | null {
  if (!value || typeof value !== "object") return "Choose a valid theme.";
  const t = value as ColorTheme;
  if (
    Object.keys(t).length !== 4 ||
    typeof t.id !== "string" ||
    !/^custom-[\da-f-]{36}$/.test(t.id) ||
    typeof t.name !== "string" ||
    !t.name.trim() ||
    t.name.trim().length > 40
  )
    return "Give your theme a name of 1–40 characters.";
  return colorsError(t.light) || colorsError(t.dark);
}
export function defaultPreferences(): ColorPreferences {
  return { version: 1, selectedId: "original", customThemes: [] };
}
export function parsePreferences(raw: string | null): ColorPreferences {
  if (!raw || raw.length > 30000) return defaultPreferences();
  try {
    const p = JSON.parse(raw) as ColorPreferences;
    if (
      !p ||
      p.version !== 1 ||
      typeof p.selectedId !== "string" ||
      !Array.isArray(p.customThemes) ||
      p.customThemes.length > maxCustomThemes ||
      p.customThemes.some((t) => themeError(t)) ||
      new Set(p.customThemes.map((t) => t.id)).size !== p.customThemes.length
    )
      return defaultPreferences();
    if (![...presets, ...p.customThemes].some((t) => t.id === p.selectedId))
      return { version: 1, selectedId: "original", customThemes: p.customThemes };
    return { version: 1, selectedId: p.selectedId, customThemes: p.customThemes };
  } catch {
    return defaultPreferences();
  }
}
function readableTone(target: string, backgrounds: string[], minimum = 4.5): string {
  const ink = foreground(backgrounds);
  for (let i = 0; i <= 100; i++) {
    const color = mix(target, ink, i / 100);
    if (minimumContrast(color, backgrounds) >= minimum) return color;
  }
  return ink;
}
export function colorTokens(colors: ThemeColors): Record<string, string> {
  const error = colorsError(colors);
  if (error) throw new Error(error);
  const backgrounds = surfaces(colors),
    ink = foreground(backgrounds),
    sidebarInk = foreground([colors.sidebar]);
  const accentSoftCandidate = mix(colors.surface, colors.accent, 0.1);
  const accentSoft =
    contrast(ink, accentSoftCandidate) >= 4.5 ? accentSoftCandidate : colors.surface;
  backgrounds.push(accentSoft);
  const accentInk = readableTone(colors.accent, backgrounds);
  const sidebarHover = mix(colors.sidebar, sidebarInk === "#000000" ? "#ffffff" : "#000000", 0.08);
  const values: Record<string, string> = {
    "--bg": colors.page,
    "--bg-tint-a": colors.page,
    "--bg-tint-b": colors.page,
    "--surface": colors.surface,
    "--surface-2": backgrounds[2],
    "--surface-3": colors.page,
    "--border": mix(colors.surface, ink, 0.22),
    "--divider": mix(colors.surface, ink, 0.14),
    "--ink": readableTone(mix(ink, colors.surface, 0.08), backgrounds),
    "--ink-2": readableTone(mix(ink, colors.surface, 0.22), backgrounds),
    "--ink-3": readableTone(mix(ink, colors.surface, 0.42), backgrounds),
    "--ink-4": readableTone(mix(ink, colors.surface, 0.42), backgrounds),
    "--accent": colors.accent,
    "--accent-2": colors.accent,
    "--accent-ink": accentInk,
    "--accent-text": foreground([colors.accent]),
    "--accent-soft": accentSoft,
    "--accent-gradient": `linear-gradient(135deg, ${colors.accent}, ${colors.accent})`,
    "--accent-gradient-h": `linear-gradient(90deg, ${colors.accent}, ${colors.accent})`,
    "--sidebar-bg": colors.sidebar,
    "--sidebar-ink": readableTone(mix(sidebarInk, colors.sidebar, 0.18), [colors.sidebar]),
    "--sidebar-muted": readableTone(mix(sidebarInk, colors.sidebar, 0.4), [colors.sidebar]),
    "--sidebar-active": readableTone(colors.accent, [colors.sidebar, sidebarHover]),
    "--sidebar-border": mix(colors.sidebar, sidebarInk, 0.2),
    "--sidebar-hover": sidebarHover,
  };
  for (const [name, color] of [
    ["ok", "#337f62"],
    ["warm", "#9c702a"],
    ["bad", "#ba4844"],
    ["cold", "#606a78"],
  ]) {
    const candidate = mix(colors.surface, color, 0.1);
    const soft = contrast(ink, candidate) >= 4.5 ? candidate : colors.surface;
    values[`--${name}-soft`] = soft;
    values[`--${name}-ink`] = readableTone(color, [soft, ...backgrounds]);
  }
  return values;
}
const ownedTokens = Object.keys(colorTokens(presets[1].light));
export function clearColorTheme(): void {
  for (const key of ownedTokens) document.documentElement.style.removeProperty(key);
  delete document.documentElement.dataset.colorTheme;
}
export function applyColorTheme(theme: ColorTheme, appearance: Appearance): void {
  if (theme.id === "original") {
    clearColorTheme();
    return;
  }
  for (const [key, value] of Object.entries(colorTokens(theme[appearance])))
    document.documentElement.style.setProperty(key, value);
  document.documentElement.dataset.colorTheme = theme.id;
}
