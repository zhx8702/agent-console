import type { ReactNode } from "react";

type PageHeaderProps = {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
};

export function PageHeader({ eyebrow, title, description, actions }: PageHeaderProps) {
  return (
    <div className="page-header">
      <div className="page-header-row">
        <div className="page-header-copy">
          <p className="section-kicker">{eyebrow}</p>
          <h1>{title}</h1>
        </div>
        {actions ? <div className="page-header-actions">{actions}</div> : null}
      </div>
      <p className="page-description">{description}</p>
    </div>
  );
}
