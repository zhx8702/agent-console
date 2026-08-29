import { EmptyState } from "./EmptyState";
import { PageHeader } from "./PageHeader";

type GroupScopeEmptyProps = {
  eyebrow: string;
  title: string;
  description: string;
};

export function GroupScopeEmpty({ eyebrow, title, description }: GroupScopeEmptyProps) {
  return (
    <div className="page-grid group-scope-empty">
      <section className="panel panel-hero span-3">
        <PageHeader eyebrow={eyebrow} title={title} description={description} />
        <EmptyState
          title="先选择一个已验证群聊"
          description="本页不接受手填群标识，也不会回退到全局范围。请使用页面上方的群聊选择器。"
        />
      </section>
    </div>
  );
}
