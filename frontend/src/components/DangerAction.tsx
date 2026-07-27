import { type ReactNode, useId, useState } from "react";

import { Alert } from "./Alert";
import { Dialog } from "./Dialog";

type DangerActionProps = {
  label: string;
  title: string;
  impact: ReactNode;
  onConfirm: () => void | Promise<void>;
  confirmLabel?: string;
  cancelLabel?: string;
  pendingLabel?: string;
  disabled?: boolean;
  className?: string;
};

export function DangerAction({
  label,
  title,
  impact,
  onConfirm,
  confirmLabel = "确认执行",
  cancelLabel = "取消",
  pendingLabel = "正在执行…",
  disabled = false,
  className = "",
}: DangerActionProps) {
  const generatedId = useId();
  const impactHeadingId = `${generatedId}-impact-heading`;
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  const close = () => {
    if (!pending) {
      setOpen(false);
      setError("");
    }
  };

  const execute = async () => {
    if (pending) {
      return;
    }
    setPending(true);
    setError("");
    try {
      await onConfirm();
      setOpen(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请稍后重试");
    } finally {
      setPending(false);
    }
  };

  return (
    <>
      <button
        type="button"
        className={`button button-danger${className ? ` ${className}` : ""}`}
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        {label}
      </button>
      <Dialog
        open={open}
        onClose={close}
        title={title}
        description="这是高风险操作。请先确认影响范围，提交期间不要重复操作。"
        dismissible={!pending}
        className="danger-action-dialog"
        footer={
          <>
            <button type="button" className="button button-secondary" onClick={close} disabled={pending}>
              {cancelLabel}
            </button>
            <button
              type="button"
              className="button button-danger"
              onClick={() => void execute()}
              disabled={pending}
              aria-busy={pending}
            >
              {pending ? pendingLabel : confirmLabel}
            </button>
          </>
        }
      >
        <section className="danger-action-impact" aria-labelledby={impactHeadingId}>
          <h3 id={impactHeadingId}>影响范围</h3>
          <div>{impact}</div>
        </section>
        {error && (
          <Alert variant="danger" title="操作未完成">
            {error}
          </Alert>
        )}
      </Dialog>
    </>
  );
}
