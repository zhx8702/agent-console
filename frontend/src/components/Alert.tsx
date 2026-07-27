import { type ReactNode } from "react";

export type AlertVariant = "info" | "success" | "warning" | "danger";

type AlertProps = {
  children: ReactNode;
  title?: ReactNode;
  variant?: AlertVariant;
  onDismiss?: () => void;
  dismissLabel?: string;
  className?: string;
};

const ICONS: Record<AlertVariant, string> = {
  info: "i",
  success: "✓",
  warning: "!",
  danger: "!",
};

export function Alert({
  children,
  title,
  variant = "info",
  onDismiss,
  dismissLabel = "关闭提示",
  className = "",
}: AlertProps) {
  const urgent = variant === "danger" || variant === "warning";
  return (
    <div
      className={`alert alert-${variant}${className ? ` ${className}` : ""}`}
      role={urgent ? "alert" : "status"}
      aria-live={urgent ? "assertive" : "polite"}
    >
      <span className="alert-icon" aria-hidden="true">
        {ICONS[variant]}
      </span>
      <div className="alert-content">
        {title && <strong>{title}</strong>}
        <div>{children}</div>
      </div>
      {onDismiss && (
        <button type="button" className="alert-dismiss" onClick={onDismiss} aria-label={dismissLabel}>
          <span aria-hidden="true">×</span>
        </button>
      )}
    </div>
  );
}
