import { useEffect, useState } from "react";

type RouteAnnouncerProps = {
  label: string;
  routeKey?: string;
  focusTargetId?: string;
  focusOnChange?: boolean;
  documentTitle?: string;
};

export function RouteAnnouncer({
  label,
  routeKey = label,
  focusTargetId = "main-content",
  focusOnChange = true,
  documentTitle,
}: RouteAnnouncerProps) {
  const [announcement, setAnnouncement] = useState("");

  useEffect(() => {
    setAnnouncement("");
    const handle = requestAnimationFrame(() => {
      setAnnouncement(label);
      if (documentTitle) {
        document.title = documentTitle;
      }
      if (focusOnChange) {
        const target = document.getElementById(focusTargetId);
        if (target instanceof HTMLElement) {
          if (!target.hasAttribute("tabindex")) {
            target.tabIndex = -1;
          }
          target.focus({ preventScroll: true });
        }
      }
    });
    return () => cancelAnimationFrame(handle);
  }, [documentTitle, focusOnChange, focusTargetId, label, routeKey]);

  return (
    <p className="route-announcer" role="status" aria-live="polite" aria-atomic="true">
      {announcement}
    </p>
  );
}
