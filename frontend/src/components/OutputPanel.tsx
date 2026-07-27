import { useState } from "react";

type OutputPanelProps = {
  title: string;
  value: string;
  defaultOpen?: boolean;
};

export function OutputPanel({ title, value, defaultOpen = false }: OutputPanelProps) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="panel panel-output panel-scroll">
      <details
        className="output-details"
        open={open}
        onToggle={(event) => setOpen(event.currentTarget.open)}
      >
        <summary>
          <span>
            <span className="section-kicker">技术详情</span>
            <span className="output-details-title">{title}</span>
          </span>
          <span className="output-details-hint" aria-hidden="true">
            {open ? "收起" : "展开"}
          </span>
        </summary>
        <pre className="code-view" aria-label={`${title}完整技术内容`}>{value}</pre>
      </details>
    </section>
  );
}
