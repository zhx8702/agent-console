import { useCallback, useEffect, useState } from "react";

import { Dialog } from "./Dialog";

type UnsavedChangesGuardProps = {
  when: boolean;
  message?: string;
  onDiscard?: () => void;
  confirmDiscard?: (message: string) => boolean;
};

const DEFAULT_MESSAGE = "当前页面有尚未保存的修改，确定要离开吗？";

function isModifiedClick(event: MouseEvent) {
  return event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0;
}

export function useUnsavedChangesGuard({
  when,
  message = DEFAULT_MESSAGE,
  onDiscard,
  confirmDiscard = () => false,
}: UnsavedChangesGuardProps) {
  const confirmNavigation = useCallback(() => {
    if (!when) {
      return true;
    }
    const confirmed = confirmDiscard(message);
    if (confirmed) {
      onDiscard?.();
    }
    return confirmed;
  }, [confirmDiscard, message, onDiscard, when]);

  useEffect(() => {
    if (!when) {
      return;
    }

    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = message;
      return message;
    };

    const linkClick = (event: MouseEvent) => {
      if (event.defaultPrevented || isModifiedClick(event)) {
        return;
      }
      const target = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>("a[href]") : null;
      if (
        !target ||
        target.target === "_blank" ||
        target.hasAttribute("download") ||
        target.dataset.bypassUnsavedGuard === "true"
      ) {
        return;
      }
      const destination = new URL(target.href, window.location.href);
      if (destination.origin !== window.location.origin || destination.href === window.location.href) {
        return;
      }
      if (!confirmNavigation()) {
        event.preventDefault();
        event.stopPropagation();
      }
    };

    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", linkClick, true);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("click", linkClick, true);
    };
  }, [confirmNavigation, message, when]);

  return confirmNavigation;
}

export function UnsavedChangesGuard(props: UnsavedChangesGuardProps) {
  const {
    when,
    message = DEFAULT_MESSAGE,
    onDiscard,
    confirmDiscard,
  } = props;
  const [pendingTarget, setPendingTarget] = useState<HTMLAnchorElement | null>(null);

  useEffect(() => {
    if (!when) {
      setPendingTarget(null);
      return;
    }

    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = message;
      return message;
    };

    const linkClick = (event: MouseEvent) => {
      if (event.defaultPrevented || isModifiedClick(event)) {
        return;
      }
      const target = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>("a[href]") : null;
      if (
        !target ||
        target.target === "_blank" ||
        target.hasAttribute("download") ||
        target.dataset.bypassUnsavedGuard === "true"
      ) {
        return;
      }
      const destination = new URL(target.href, window.location.href);
      if (destination.origin !== window.location.origin || destination.href === window.location.href) {
        return;
      }

      if (confirmDiscard) {
        if (confirmDiscard(message)) {
          onDiscard?.();
          return;
        }
      } else {
        setPendingTarget(target);
      }
      event.preventDefault();
      event.stopPropagation();
    };

    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", linkClick, true);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("click", linkClick, true);
    };
  }, [confirmDiscard, message, onDiscard, when]);

  const discardAndContinue = () => {
    const target = pendingTarget;
    setPendingTarget(null);
    onDiscard?.();
    if (!target) {
      return;
    }
    target.dataset.bypassUnsavedGuard = "true";
    target.click();
    queueMicrotask(() => delete target.dataset.bypassUnsavedGuard);
  };

  return (
    <Dialog
      open={pendingTarget !== null}
      onClose={() => setPendingTarget(null)}
      title="放弃未保存的修改？"
      description={message}
      footer={
        <>
          <button type="button" className="button button-secondary" onClick={() => setPendingTarget(null)}>
            继续编辑
          </button>
          <button type="button" className="button button-danger" onClick={discardAndContinue}>
            放弃修改并离开
          </button>
        </>
      }
    >
      <p>离开后，本页尚未保存的草稿无法恢复。</p>
    </Dialog>
  );
}
