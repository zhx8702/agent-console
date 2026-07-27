import { SearchableSelect, type SearchableSelectOption } from "../../components/SearchableSelect";
import { DangerAction } from "../../components/DangerAction";
import { formatJson } from "../../lib/api";
import { isGroupSession, type MemoryEvent, type RuntimeProfile, type WxbotSession } from "./model";

type BackfillPanelProps = {
  connectionId: string;
  pickerSessionId: string;
  pickerOptions: SearchableSelectOption[];
  daysLimit: number;
  maxMessagesPerSession: number;
  limit: number;
  runtimeProfile: RuntimeProfile | null;
  userId: string;
  selectedSessions: WxbotSession[];
  events: MemoryEvent[];
  onPickerSessionIdChange: (value: string) => void;
  onSessionIdsTextChange: (value: string) => void;
  onDaysLimitChange: (value: number) => void;
  onMaxMessagesPerSessionChange: (value: number) => void;
  onLimitChange: (value: number) => void;
  onRuntimeOutputChange: (value: string) => void;
  onAddSessions: (sessionIds: string[]) => void;
  onRemoveSession: (sessionId: string) => void;
  onRunBackfill: () => void | Promise<void>;
  onLoadRuntimeProfile: () => void | Promise<void>;
  onLoadEvents: () => void | Promise<void>;
};

export function BackfillPanel({
  connectionId,
  pickerSessionId,
  pickerOptions,
  daysLimit,
  maxMessagesPerSession,
  limit,
  runtimeProfile,
  userId,
  selectedSessions,
  events,
  onPickerSessionIdChange,
  onSessionIdsTextChange,
  onDaysLimitChange,
  onMaxMessagesPerSessionChange,
  onLimitChange,
  onRuntimeOutputChange,
  onAddSessions,
  onRemoveSession,
  onRunBackfill,
  onLoadRuntimeProfile,
  onLoadEvents,
}: BackfillPanelProps) {
  return (
    <section className="panel memory-backfill-panel">
      <div className="panel-header">
        <div>
          <p className="section-kicker">历史回填</p>
          <h3>SDK 历史回填与运行态</h3>
        </div>
      </div>
      <div className="admin-notice admin-notice-warning">
        技术运维视图：回填和事件列表用于排障；响应面板只保留计数、范围和 ID，不提供聊天正文。
      </div>
      <div className="form-grid">
        <label className="field span-2">
          <span>历史连接</span>
          <input value={connectionId} readOnly aria-readonly="true" aria-label="历史连接" />
          <small>原始 SDK 历史只允许默认租户的 legacy 微信连接。</small>
        </label>
        <label className="field span-2">
          <span>批量选择会话</span>
          <SearchableSelect
            value={pickerSessionId}
            onChange={onPickerSessionIdChange}
            options={pickerOptions}
            placeholder="搜索并选择要加入回填的会话"
            searchPlaceholder="搜索群名、私聊名或会话 ID"
            emptyText="暂无可加入的会话"
            noResultsText="没有匹配的会话"
          />
        </label>
        <div className="field span-2">
          <span>回填范围</span>
          <strong>{selectedSessions[0]?.session_name || "尚未加入当前群"}</strong>
          <small>只允许页面上方已验证的当前群；不接受手工输入会话 ID 或私聊范围。</small>
        </div>
        <label className="field">
          <span>回溯天数</span>
          <input type="number" min={0} max={3650} value={daysLimit} onChange={(event) => onDaysLimitChange(Number(event.target.value) || 0)} />
        </label>
        <label className="field">
          <span>单会话最多消息</span>
          <input
            type="number"
            min={1}
            max={500}
            value={maxMessagesPerSession}
            onChange={(event) => onMaxMessagesPerSessionChange(Number(event.target.value) || 200)}
          />
        </label>
        <label className="field">
          <span>查询条数</span>
          <input type="number" min={1} max={500} value={limit} onChange={(event) => onLimitChange(Number(event.target.value) || 50)} />
        </label>
      </div>
      <div className="action-row">
        <button
          className="button button-secondary"
          onClick={() => {
            if (!pickerSessionId) {
              onRuntimeOutputChange(formatJson({ error: "请先选择要加入的会话" }));
              return;
            }
            onAddSessions([pickerSessionId]);
            onPickerSessionIdChange("");
          }}
        >
          加入选中会话
        </button>
        <DangerAction
          label="执行历史回填"
          title="确认回填当前群成员历史"
          confirmLabel="确认回填"
          pendingLabel="正在回填…"
          disabled={!selectedSessions.length || !userId}
          impact={(
            <ul>
              <li>群聊：{selectedSessions[0]?.session_name || "未选择"}</li>
              <li>成员：<code>{userId || "未选择"}</code></li>
              <li>连接：<code>{connectionId}</code></li>
              <li>回溯 {daysLimit} 天，单群最多 {maxMessagesPerSession} 条。</li>
              <li>只读取已授权群范围；请求使用稳定幂等键。</li>
            </ul>
          )}
          onConfirm={onRunBackfill}
        />
        <button className="button button-secondary" onClick={() => void onLoadRuntimeProfile()}>
          读取运行时合并视图
        </button>
        <button className="button button-secondary" onClick={() => void onLoadEvents()}>
          列出互动事件
        </button>
        <button
          className="button button-secondary"
          onClick={() => {
            onSessionIdsTextChange("");
            onRuntimeOutputChange(formatJson({ cleared: true }));
          }}
        >
          清空回填范围
        </button>
      </div>
      <div className="persona-scope-note">
        <strong>{runtimeProfile?.user_id || userId || "未选择用户"}</strong>
        <span>
          当前运行时会把全局身份记忆和当前会话记忆合并后注入模型。当前会话消息数：
          <span className="mono"> {runtimeProfile?.session_message_count ?? 0}</span>
          ，全局累计消息数：
          <span className="mono"> {runtimeProfile?.identity_message_count ?? 0}</span>
          。
        </span>
      </div>
      <div
        className="table-scroll compact-table-scroll"
        role="region"
        aria-label="历史回填会话范围表格"
        tabIndex={0}
      >
        <table>
          <caption className="sr-only">历史回填会话范围</caption>
          <thead>
            <tr>
              <th scope="col">已选回填会话</th>
              <th scope="col">类型</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            {selectedSessions.map((item) => (
              <tr key={item.session_id}>
                <th scope="row">
                  {item.session_name}
                  <div className="mono">{item.session_id}</div>
                </th>
                <td>{isGroupSession(item) ? "群聊" : "私聊"}</td>
                <td>
                  <button className="button-danger-sm" onClick={() => onRemoveSession(item.session_id)}>
                    移除
                  </button>
                </td>
              </tr>
            ))}
            {!selectedSessions.length && (
              <tr>
                <td colSpan={3}>当前还没有选中任何回填会话</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div
        className="table-scroll compact-table-scroll"
        role="region"
        aria-label="当前群成员互动事件技术元数据表格"
        tabIndex={0}
      >
        <table>
          <caption className="sr-only">当前群成员互动事件技术元数据</caption>
          <thead>
            <tr>
              <th scope="col">ID</th>
              <th scope="col">时间</th>
              <th scope="col">会话</th>
              <th scope="col">用户消息</th>
              <th scope="col">系统回复</th>
              <th scope="col">追踪 ID</th>
            </tr>
          </thead>
          <tbody>
            {events.map((item) => (
              <tr key={item.id}>
                <th scope="row" className="mono">{item.id}</th>
                <td>{item.created_at || "-"}</td>
                <td className="mono">{item.session_id || "-"}</td>
                <td>{item.user_text ? "已隐藏（不展示正文）" : "-"}</td>
                <td>{item.assistant_text ? "已隐藏（不展示正文）" : "-"}</td>
                <td className="mono">{item.trace_id || "-"}</td>
              </tr>
            ))}
            {!events.length && (
              <tr>
                <td colSpan={6}>暂无互动事件</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
