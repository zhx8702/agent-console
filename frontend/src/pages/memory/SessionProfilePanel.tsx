import type { SessionProfile } from "./model";

type SessionProfilePanelProps = {
  shortTermMemory: string;
  manualNotes: string;
  profiles: SessionProfile[];
  canSave: boolean;
  onShortTermMemoryChange: (value: string) => void;
  onManualNotesChange: (value: string) => void;
  onSelectProfile: (profile: SessionProfile) => void;
  onLoad: () => void | Promise<void>;
  onSave: () => void | Promise<void>;
  onList: () => void | Promise<void>;
};

export function SessionProfilePanel({
  shortTermMemory,
  manualNotes,
  profiles,
  canSave,
  onShortTermMemoryChange,
  onManualNotesChange,
  onSelectProfile,
  onLoad,
  onSave,
  onList,
}: SessionProfilePanelProps) {
  return (
    <section className="panel memory-profile-panel">
      <div className="panel-header">
        <div>
          <p className="section-kicker">会话记忆</p>
          <h3>会话覆盖记忆</h3>
        </div>
      </div>
      <div className="form-grid">
        <label className="field span-2">
          <span>短期记忆</span>
          <textarea rows={7} value={shortTermMemory} onChange={(event) => onShortTermMemoryChange(event.target.value)} />
        </label>
        <label className="field span-2">
          <span>当前会话备注</span>
          <textarea rows={7} value={manualNotes} onChange={(event) => onManualNotesChange(event.target.value)} />
        </label>
      </div>
      <div className="action-row">
        <button className="button button-secondary" onClick={() => void onLoad()}>
          读取会话记忆
        </button>
        <button className="button button-primary" onClick={() => void onSave()} disabled={!canSave}>
          保存会话记忆
        </button>
        <button className="button button-secondary" onClick={() => void onList()}>
          列出会话档案
        </button>
      </div>
      <div className="table-scroll compact-table-scroll">
        <table>
          <caption className="sr-only">当前群成员的会话记忆档案</caption>
          <thead>
            <tr>
              <th scope="col">会话 ID</th>
              <th scope="col">WXID</th>
              <th scope="col">消息数</th>
              <th scope="col">导入数</th>
            </tr>
          </thead>
          <tbody>
            {profiles.map((item) => (
              <tr key={`${item.session_id}-${item.user_id}`}>
                <th scope="row" className="mono">
                  <button type="button" className="table-cell-action mono" onClick={() => onSelectProfile(item)}>
                    {item.session_id}
                  </button>
                </th>
                <td className="mono">{item.user_id}</td>
                <td>{item.message_count ?? 0}</td>
                <td>{item.imported_message_count ?? 0}</td>
              </tr>
            ))}
            {!profiles.length && (
              <tr>
                <td colSpan={4}>暂无会话记忆</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
