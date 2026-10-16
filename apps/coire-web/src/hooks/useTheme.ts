import { useEffect, useState } from "react";
import {
  applyColorTheme,
  clearColorTheme,
  colorPreferencesKey,
  defaultPreferences,
  maxCustomThemes,
  parsePreferences,
  presets,
  themeError,
  type Appearance,
  type ColorTheme,
} from "../theme/colorThemes";

export type Theme = Appearance;
const key = "coire.theme";
function initialTheme(): Theme {
  try {
    return window.localStorage.getItem(key) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}
function initialColors() {
  try {
    return parsePreferences(window.localStorage.getItem(colorPreferencesKey));
  } catch {
    return defaultPreferences();
  }
}
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [colorPreferences, setColorPreferences] = useState(initialColors);
  const [preview, previewPalette] = useState<ColorTheme | null>(null);
  const [preferencesSaved, setPreferencesSaved] = useState(true);
  const colorThemes = [...presets, ...colorPreferences.customThemes];
  const selectedPalette =
    colorThemes.find((t) => t.id === colorPreferences.selectedId) ?? presets[0];
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    applyColorTheme(preview ?? selectedPalette, theme);
    try {
      window.localStorage.setItem(key, theme);
    } catch {
      /* Appearance remains usable. */
    }
  }, [theme, preview, selectedPalette]);
  useEffect(() => {
    try {
      window.localStorage.setItem(colorPreferencesKey, JSON.stringify(colorPreferences));
      setPreferencesSaved(true);
    } catch {
      setPreferencesSaved(false);
    }
  }, [colorPreferences]);
  useEffect(() => () => clearColorTheme(), []);
  const selectPalette = (id: string) => {
    if (!colorThemes.some((t) => t.id === id)) return;
    previewPalette(null);
    setColorPreferences((p) => ({ ...p, selectedId: id }));
  };
  const saveCustomTheme = (draft: ColorTheme): string | null => {
    const error = themeError(draft);
    if (error) return error;
    const existing = colorPreferences.customThemes.some((t) => t.id === draft.id);
    if (!existing && colorPreferences.customThemes.length >= maxCustomThemes)
      return "You can save up to 12 custom themes. Delete one before adding another.";
    const name = draft.name.trim();
    if (colorThemes.some((t) => t.id !== draft.id && t.name.toLowerCase() === name.toLowerCase()))
      return "Choose a different theme name.";
    setColorPreferences((p) => ({
      version: 1,
      selectedId: draft.id,
      customThemes: [...p.customThemes.filter((t) => t.id !== draft.id), { ...draft, name }],
    }));
    previewPalette(null);
    return null;
  };
  const deleteCustomTheme = (id: string) => {
    previewPalette(null);
    setColorPreferences((p) => ({
      ...p,
      selectedId: p.selectedId === id ? "original" : p.selectedId,
      customThemes: p.customThemes.filter((t) => t.id !== id),
    }));
  };
  return {
    theme,
    toggleTheme: () => setTheme((current) => (current === "light" ? "dark" : "light")),
    colorThemes,
    selectedPalette,
    preferencesSaved,
    previewPalette,
    selectPalette,
    saveCustomTheme,
    deleteCustomTheme,
  };
}
