export type IconName =
  | "chat"
  | "images"
  | "training"
  | "admin"
  | "overview"
  | "models"
  | "instances"
  | "activity"
  | "identity"
  | "audit"
  | "upgrades"
  | "sun"
  | "moon"
  | "menu"
  | "close"
  | "arrow"
  | "plus"
  | "logout"
  | "palette"
  | "chevron";
const paths: Record<IconName, string> = {
  chat: "M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z",
  images:
    "M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm-1 13 5-5 5 5 4-4 4 4M8 7h.01",
  training: "m3 9 9-5 9 5-9 5-9-5Zm3 2v6c4 3 8 3 12 0v-6M21 9v8",
  admin: "M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Zm-3 9 2 2 4-4",
  overview: "M3 3h7v7H3V3Zm11 0h7v7h-7V3ZM3 14h7v7H3v-7Zm11 0h7v7h-7v-7Z",
  models: "m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5",
  instances: "M4 3h16v7H4V3Zm0 11h16v7H4v-7M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  identity:
    "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M17 3a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3.9M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  audit: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6M8 13h8M8 17h5",
  upgrades: "M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5",
  sun: "M12 2v2M12 20v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  moon: "M21 12.8A9 9 0 0 1 11.2 3 9 9 0 1 0 21 12.8Z",
  menu: "M4 6h16M4 12h16M4 18h16",
  close: "m6 6 12 12M6 18 18 6",
  arrow: "M12 19V5m-6 6 6-6 6 6",
  plus: "M12 5v14M5 12h14",
  logout: "M9 21H4V3h5m5 5 4 4-4 4M8 12h13",
  palette:
    "M12 3a9 9 0 1 0 0 18h1.5a2 2 0 0 0 1.5-3.3 1.6 1.6 0 0 1 1.2-2.7H18a3 3 0 0 0 3-3c0-5-4-9-9-9ZM7 10h.01M10 6.5h.01M15 6.5h.01M18 10h.01",
  chevron: "m9 5 7 7-7 7",
};
export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
export function CoireMark({ size = 30 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 40 40" fill="none" aria-hidden="true">
      <path
        d="M30.5 10A14 14 0 1 0 30.5 30"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path
        d="M25.5 15A7 7 0 1 0 25.5 25"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <circle cx="31" cy="20" r="2.5" fill="currentColor" />
    </svg>
  );
}
