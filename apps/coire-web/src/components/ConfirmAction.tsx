import { useEffect, useState } from "react";

export function ConfirmAction({
  target,
  label,
  onConfirm,
}: {
  target: string;
  label: string;
  onConfirm: () => Promise<void>;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(timer);
  }, [armed]);
  return (
    <button
      className="button danger"
      type="button"
      aria-label={`${label} ${target}`}
      onClick={() => {
        if (!armed) return setArmed(true);
        setArmed(false);
        void onConfirm();
      }}
    >
      {armed ? `Confirm ${label.toLowerCase()} ${target}?` : label}
    </button>
  );
}
