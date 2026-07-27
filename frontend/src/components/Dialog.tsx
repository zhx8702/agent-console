import { type ReactNode, type RefObject, useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
  "[contenteditable='true']",
].join(",");

type DialogProps = {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  closeLabel?: string;
  dismissible?: boolean;
  initialFocusRef?: RefObject<HTMLElement | null>;
  className?: string;
};

function focusableElements(container: HTMLElement) {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => !element.hasAttribute("hidden") && element.getAttribute("aria-hidden") !== "true",
  );
}

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  closeLabel = "关闭对话框",
  dismissible = true,
  initialFocusRef,
  className = "",
}: DialogProps) {
  const generatedId = useId();
  const titleId = `${generatedId}-title`;
  const descriptionId = `${generatedId}-description`;
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }

    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusHandle = requestAnimationFrame(() => {
      const preferredTarget = initialFocusRef?.current;
      const firstTarget = dialogRef.current ? focusableElements(dialogRef.current)[0] : null;
      (preferredTarget || firstTarget || dialogRef.current)?.focus();
    });

    return () => {
      cancelAnimationFrame(focusHandle);
      document.body.style.overflow = previousOverflow;
      requestAnimationFrame(() => restoreFocusRef.current?.focus());
    };
  }, [initialFocusRef, open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && dismissible) {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) {
        return;
      }
      const targets = focusableElements(dialogRef.current);
      if (!targets.length) {
        event.preventDefault();
        dialogRef.current.focus();
        return;
      }
      const first = targets[0];
      const last = targets[targets.length - 1];
      if (!dialogRef.current.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [dismissible, onClose, open]);

  if (!open) {
    return null;
  }

  return createPortal(
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (dismissible && event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        ref={dialogRef}
        className={`dialog-surface${className ? ` ${className}` : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
      >
        <header className="dialog-header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description && <p id={descriptionId}>{description}</p>}
          </div>
          {dismissible && (
            <button type="button" className="dialog-close" aria-label={closeLabel} onClick={onClose}>
              <span aria-hidden="true">×</span>
            </button>
          )}
        </header>
        <div className="dialog-body">{children}</div>
        {footer && <footer className="dialog-footer">{footer}</footer>}
      </div>
    </div>,
    document.body,
  );
}
