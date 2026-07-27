type PageHeaderProps = {
  eyebrow: string;
  title: string;
  description: string;
};

export function PageHeader({ eyebrow, title, description }: PageHeaderProps) {
  return (
    <div className="page-header">
      <div>
        <p className="section-kicker">{eyebrow}</p>
        <h1>{title}</h1>
      </div>
      <p className="page-description">{description}</p>
    </div>
  );
}
