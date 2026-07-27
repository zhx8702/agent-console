import { Link } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";

export function NotFoundPage() {
  return (
    <section className="not-found-panel" aria-label="页面不存在">
      <div className="not-found-code" aria-hidden="true">404</div>
      <div>
        <PageHeader
          eyebrow="页面不可用"
          title="页面不存在"
          description="这个地址没有对应的控制台页面，可能是链接已过期，或插件入口已经调整。"
        />
        <div className="action-row">
          <Link className="button button-primary" to="/">返回控制台概览</Link>
        </div>
        <p className="not-found-hint">
          请检查地址，或从主导航重新进入目标功能。
        </p>
      </div>
    </section>
  );
}
