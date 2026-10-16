import { useEffect, useState, type ReactNode } from "react";

export function ConfirmAction({
  target,
  label,
  onConfirm,
  ariaLabel,
  icon,
}: {
  target: string;
  label: string;
  onConfirm: () => Promise<void>;
  ariaLabel?: string;
  icon?: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(timer);
  }, [armed]);
  return (
    <>
      <button
        className="button danger"
        type="button"
        disabled={busy}
        title={armed ? `Confirm ${label.toLowerCase()} ${target}?` : label}
        aria-label={ariaLabel ?? `${label} ${target}`}
        onClick={() => {
          setFailure(null);
          if (!armed) return setArmed(true);
          setArmed(false);
          setBusy(true);
          void onConfirm()
            .catch((cause: unknown) => setFailure(String(cause)))
            .finally(() => setBusy(false));
        }}
      >
        {armed ? `Confirm ${label.toLowerCase()} ${target}?` : (icon ?? label)}
      </button>
      {failure && <span role="alert">{failure}</span>}
    </>
  );
}
