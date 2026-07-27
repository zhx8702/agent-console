import type { IdentityProfile } from "./model";

type IdentityProfilePanelProps = {
  longTermMemory: string;
  manualNotes: string;
  profiles: IdentityProfile[];
  canSave: boolean;
  emptyText: string;
  onLongTermMemoryChange: (value: string) => void;
  onManualNotesChange: (value: string) => void;
  onSelectProfile: (profile: IdentityProfile) => void;
  onLoad: () => void | Promise<void>;
  onSave: () => void | Promise<void>;
  onList: () => void | Promise<void>;
};

export function IdentityProfilePanel({
  longTermMemory,
  manualNotes,
  profiles,
  canSave,
  emptyText,
  onLongTermMemoryChange,
  onManualNotesChange,
  onSelectProfile,
  onLoad,
  onSave,
  onList,
}: IdentityProfilePanelProps) {
  return (
    <section className="panel memory-profile-panel">
      <div className="panel-header">
        <div>
          <p className="section-kicker">身份记忆</p>
          <h3>全局身份记忆</h3>
        </div>
      </div>
      <div className="form-grid">
        <label className="field span-2">
          <span>长期记忆</span>
          <textarea
            rows={9}
            value={longTermMemory}
            onChange={(event) => onLongTermMemoryChange(event.target.value)}
            placeholder="兼容渲染缓存：建议优先在“单条记忆”中管理长期记忆。"
          />
        </label>
        <p className="muted-copy span-2">
          长期记忆文本是兼容渲染缓存；建议使用“单条记忆”管理可审计、可筛选的长期记忆。
        </p>
        <label className="field span-2">
          <span>全局人工备注</span>
          <textarea rows={7} value={manualNotes} onChange={(event) => onManualNotesChange(event.target.value)} />
        </label>
      </div>
      <div className="action-row">
        <button className="button button-secondary" onClick={() => void onLoad()}>
          读取全局记忆
        </button>
        <button className="button button-primary" onClick={() => void onSave()} disabled={!canSave}>
          保存全局记忆
        </button>
        <button className="button button-secondary" onClick={() => void onList()}>
          列出全局档案
        </button>
      </div>
      <div className="action-row"><span className="pill pill-muted">列表范围：当前已验证群成员</span></div>
      <p className="muted-copy">
        “读取/保存全局记忆”只使用当前群名册中已验证成员。点击下方档案行可重新载入该成员的长期记忆。
      </p>
      <div
        className="table-scroll compact-table-scroll"
        role="region"
        aria-label="当前已验证成员的全局身份记忆表格"
        tabIndex={0}
      >
        <table>
          <caption className="sr-only">当前已验证成员的全局身份记忆</caption>
          <thead>
            <tr>
              <th scope="col">WXID</th>
              <th scope="col">消息数</th>
              <th scope="col">导入数</th>
              <th scope="col">最近会话</th>
            </tr>
          </thead>
          <tbody>
            {profiles.map((item) => (
              <tr key={`${item.channel}-${item.source_key}-${item.user_id}`}>
                <th scope="row" className="mono">
                  <button type="button" className="table-cell-action mono" onClick={() => onSelectProfile(item)}>
                    {item.user_id}
                  </button>
                </th>
                <td>{item.message_count ?? 0}</td>
                <td>{item.imported_message_count ?? 0}</td>
                <td className="mono">{item.last_session_id || "-"}</td>
              </tr>
            ))}
            {!profiles.length && (
              <tr>
                <td colSpan={4}>{emptyText}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
