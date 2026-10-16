import { useEffect, useState } from "react";

export type Theme = "light" | "dark";
const key = "coire.theme";
function initialTheme(): Theme {
  try {
    return window.localStorage.getItem(key) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(key, theme);
    } catch {
      /* Storage is optional, including at sign-in. */
    }
  }, [theme]);
  return {
    theme,
    toggleTheme: () => setTheme((current) => (current === "light" ? "dark" : "light")),
  };
}
