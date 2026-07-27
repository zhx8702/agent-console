import { useEffect, useId, useState } from "react";

import { Alert, Dialog } from "../../components";
import type {
  ChannelAdapter,
  ChannelConnection,
  ChannelConnectionWrite,
} from "../../lib/channel-connections";
import { VersionConflictError } from "../../lib/api";
import {
  adapterConfigFieldNames,
  adapterConfigDefaults,
  configValuesForAdapter,
  connectionDraftFromValue,
  emptyConnectionDraft,
  validateConnectionDraft,
  type ConnectionEditorErrors,
} from "./model";

type ConnectionEditorDialogProps = {
  open: boolean;
  adapters: ChannelAdapter[];
  connection: ChannelConnection | null;
  connectionAdapterIds: string[];
  initialAdapterId?: string;
  busy: boolean;
  conflict: boolean;
  onClose: () => void;
  onSave: (draft: ChannelConnectionWrite, editingId: string) => Promise<unknown>;
  onReloadAfterConflict: () => Promise<unknown> | void;
};

export function ConnectionEditorDialog({
  open,
  adapters,
  connection,
  connectionAdapterIds,
  initialAdapterId = "",
  busy,
  conflict,
  onClose,
  onSave,
  onReloadAfterConflict,
}: ConnectionEditorDialogProps) {
  const formId = useId();
  const [draft, setDraft] = useState<ChannelConnectionWrite>(() => {
    const adapter = adapters.find((item) => item.id === initialAdapterId);
    return emptyConnectionDraft(initialAdapterId, adapter);
  });
  const [errors, setErrors] = useState<ConnectionEditorErrors>({});
  const [submissionError, setSubmissionError] = useState("");

  useEffect(() => {
    if (!open) return;
    const adapterId = connection?.adapterId || initialAdapterId;
    const adapter = adapters.find((item) => item.id === adapterId);
    setDraft(
      connection
        ? connectionDraftFromValue(connection, adapter)
        : emptyConnectionDraft(initialAdapterId, adapter),
    );
    setErrors({});
    setSubmissionError("");
  }, [connection?.id, initialAdapterId, open]);

  const update = <Key extends keyof ChannelConnectionWrite>(
    key: Key,
    value: ChannelConnectionWrite[Key],
  ) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setErrors((current) => ({ ...current, [key]: undefined }));
    setSubmissionError("");
  };

  const submit = async () => {
    const selectedAdapter = adapters.find((item) => item.id === draft.adapterId);
    const nextErrors = validateConnectionDraft(draft, selectedAdapter);
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length) {
      requestAnimationFrame(() => {
        document.getElementById(formId)
          ?.querySelector<HTMLElement>("[aria-invalid='true']")
          ?.focus();
      });
      return;
    }
    setSubmissionError("");
    try {
      const preparedDraft = selectedAdapter
        ? {
            ...draft,
            configValues: configValuesForAdapter(draft.configValues, selectedAdapter),
            configFieldNames: adapterConfigFieldNames(selectedAdapter),
          }
        : draft;
      await onSave(preparedDraft, connection?.id || "");
      onClose();
    } catch (caught) {
      setSubmissionError(
        caught instanceof VersionConflictError
          ? "服务器上的连接已被其他操作者更新。当前表单仍保留，请重新读取后核对。"
          : caught instanceof Error
            ? caught.message
            : "连接保存失败，请稍后重试",
      );
    }
  };

  const canSubmit = !busy && !connection?.readOnly && !conflict;
  const adapterHasCapacity = (adapter: ChannelAdapter) => (
    adapter.supportsMultipleConnections
    || !connectionAdapterIds.includes(adapter.id)
    || connection?.adapterId === adapter.id
  );
  const availableAdapters = adapters.filter((item) => (
    item.installed && item.enabled && item.available && adapterHasCapacity(item)
  ));
  const selectedAdapter = adapters.find((item) => item.id === draft.adapterId);
  const configFieldNames = selectedAdapter
    ? Array.from(new Set([
        ...selectedAdapter.configOrder,
        ...Object.keys(selectedAdapter.configFields),
      ])).filter((name) => selectedAdapter.configFields[name])
    : [];
  const primarySecret = selectedAdapter?.secretFields[0];
  const dialogDescription = primarySecret
    ? "连接参数可直接填写并保存；平台凭据仍通过部署方的安全密钥引用提供。"
    : "连接参数可直接填写并保存；当前平台不需要额外 Token 或配置文件路径。";

  const updateConfig = (name: string, value: unknown) => {
    setDraft((current) => ({
      ...current,
      configValues: { ...(current.configValues ?? {}), [name]: value },
    }));
    setErrors((current) => ({
      ...current,
      config: current.config
        ? { ...current.config, [name]: "" }
        : undefined,
    }));
    setSubmissionError("");
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={connection ? `编辑连接 · ${connection.displayName}` : "添加消息平台连接"}
      description={dialogDescription}
      dismissible={!busy}
      className="connection-editor-dialog"
      footer={(
        <>
          <button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>
            取消
          </button>
          {conflict && (
            <button
              type="button"
              className="button button-secondary"
              onClick={() => void onReloadAfterConflict()}
              disabled={busy}
            >
              放弃当前表单并重新读取
            </button>
          )}
          <button
            type="submit"
            form={formId}
            className="button button-primary"
            disabled={!canSubmit}
            aria-busy={busy}
          >
            {busy ? "正在保存…" : connection ? "保存连接配置" : "创建连接草稿"}
          </button>
        </>
      )}
    >
      {!availableAdapters.length && !connection && (
        <Alert variant="warning" title="没有可用于新建连接的平台适配器">
          请先在插件市场安装并启用消息平台适配器，再返回这里添加连接。
        </Alert>
      )}
      {connection?.readOnly && (
        <Alert variant="info" title="这是只读连接">
          该连接由部署环境或外部系统托管。控制台只提供安全摘要，不能修改配置或执行连接操作。
        </Alert>
      )}
      {(conflict || submissionError) && (
        <Alert variant="warning" title={conflict ? "连接版本冲突" : "连接未保存"}>
          {submissionError || "服务器已有更新。当前表单仍保留，重新读取前不会覆盖服务器版本。"}
        </Alert>
      )}
      <form
        id={formId}
        className="connection-editor-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
        noValidate
      >
        <div className="form-grid">
          <label className="field span-2">
            <span>消息平台适配器</span>
            <select
              value={draft.adapterId}
              onChange={(event) => {
                const adapterId = event.target.value;
                const adapter = adapters.find((item) => item.id === adapterId);
                setDraft((current) => ({
                  ...current,
                  adapterId,
                  configValues: adapterConfigDefaults(adapter),
                  configFieldNames: adapterConfigFieldNames(adapter),
                  endpointUrl: "",
                  extraConfig: {},
                  secretRef: "",
                }));
                setErrors({});
                setSubmissionError("");
              }}
              disabled={Boolean(connection) || busy || connection?.readOnly}
              required
              aria-invalid={Boolean(errors.adapterId)}
              aria-describedby={errors.adapterId ? `${formId}-adapter-error` : undefined}
            >
              <option value="">选择已安装的平台适配器</option>
              {adapters.map((adapter) => (
                <option
                  key={adapter.id}
                  value={adapter.id}
                  disabled={!adapter.installed || !adapter.enabled || !adapter.available || !adapterHasCapacity(adapter)}
                >
                  {adapter.displayName}{
                    !adapter.available
                      ? "（当前不可用）"
                      : !adapterHasCapacity(adapter)
                        ? "（已存在单实例连接）"
                        : ""
                  }
                </option>
              ))}
            </select>
            {errors.adapterId && <small id={`${formId}-adapter-error`} className="connection-field-error" role="alert">{errors.adapterId}</small>}
          </label>

          <label className="field span-2">
            <span>连接名称</span>
            <input
              value={draft.displayName}
              onChange={(event) => update("displayName", event.target.value)}
              placeholder="例如：生产环境 · 客服连接 A"
              maxLength={128}
              disabled={busy || connection?.readOnly}
              required
              aria-invalid={Boolean(errors.displayName)}
              aria-describedby={errors.displayName ? `${formId}-name-error` : undefined}
            />
            {errors.displayName && <small id={`${formId}-name-error`} className="connection-field-error" role="alert">{errors.displayName}</small>}
          </label>

          {selectedAdapter && !configFieldNames.length && (
            <div className="span-2">
              <Alert variant="info" title="该适配器没有连接参数">
                只需填写连接名称；后续参数由适配器运行时管理。
              </Alert>
            </div>
          )}

          {configFieldNames.map((name) => {
            const field = selectedAdapter?.configFields[name];
            if (!field) return null;
            const value = draft.configValues
              && Object.prototype.hasOwnProperty.call(draft.configValues, name)
              ? draft.configValues[name]
              : undefined;
            const error = errors.config?.[name];
            const inputId = `${formId}-config-${name.replace(/[^A-Za-z0-9_-]/g, "-")}`;
            const errorId = `${inputId}-error`;
            const required = selectedAdapter.configRequired.includes(name);
            const describedBy = [
              field.description ? `${inputId}-help` : "",
              error ? errorId : "",
            ].filter(Boolean).join(" ") || undefined;
            return (
              <label className={`field ${field.type === "boolean" ? "" : "span-2"}`} key={name}>
                <span>{field.title}{required ? " *" : ""}</span>
                {field.enumValues.length ? (
                  <select
                    value={(() => {
                      const index = field.enumValues.findIndex((item) => Object.is(item, value));
                      return index >= 0 ? String(index) : "";
                    })()}
                    onChange={(event) => {
                      if (!event.target.value) {
                        updateConfig(name, undefined);
                        return;
                      }
                      const index = Number(event.target.value);
                      updateConfig(name, field.enumValues[index]);
                    }}
                    disabled={busy || connection?.readOnly}
                    required={required}
                    aria-invalid={Boolean(error)}
                    aria-describedby={describedBy}
                  >
                    <option value="">{required ? "请选择" : "未设置"}</option>
                    {field.enumValues.map((option, index) => (
                      <option value={String(index)} key={`${name}-${index}`}>
                        {String(option)}
                      </option>
                    ))}
                  </select>
                ) : field.type === "boolean" ? (
                  <select
                    value={typeof value === "boolean" ? String(value) : ""}
                    onChange={(event) => updateConfig(
                      name,
                      event.target.value === "" ? undefined : event.target.value === "true",
                    )}
                    disabled={busy || connection?.readOnly}
                    required={required}
                    aria-invalid={Boolean(error)}
                    aria-describedby={describedBy}
                  >
                    <option value="">{required ? "请选择" : "未设置"}</option>
                    <option value="true">开启</option>
                    <option value="false">关闭</option>
                  </select>
                ) : field.type === "null" ? (
                  <select
                    value={value === null ? "null" : ""}
                    onChange={(event) => updateConfig(name, event.target.value === "null" ? null : undefined)}
                    disabled={busy || connection?.readOnly}
                    required={required}
                    aria-invalid={Boolean(error)}
                    aria-describedby={describedBy}
                  >
                    <option value="">{required ? "请选择" : "未设置"}</option>
                    <option value="null">null</option>
                  </select>
                ) : field.type === "number" || field.type === "integer" ? (
                  <input
                    type="number"
                    value={value === undefined ? "" : String(value)}
                    min={field.minimum ?? undefined}
                    max={field.maximum ?? undefined}
                    step={field.type === "integer" ? 1 : "any"}
                    onChange={(event) => updateConfig(
                      name,
                      event.target.value === "" ? undefined : Number(event.target.value),
                    )}
                    disabled={busy || connection?.readOnly}
                    required={required}
                    aria-invalid={Boolean(error)}
                    aria-describedby={describedBy}
                  />
                ) : field.type === "object" || field.type === "array" ? (
                  <textarea
                    value={typeof value === "string" ? value : value === undefined ? "" : JSON.stringify(value, null, 2)}
                    onChange={(event) => {
                      const raw = event.target.value;
                      if (!raw.trim()) {
                        updateConfig(name, undefined);
                        return;
                      }
                      try {
                        const parsed = JSON.parse(raw) as unknown;
                        const typeMatches = field.type === "array"
                          ? Array.isArray(parsed)
                          : Boolean(parsed) && typeof parsed === "object" && !Array.isArray(parsed);
                        updateConfig(name, typeMatches ? parsed : raw);
                      } catch {
                        updateConfig(name, raw);
                      }
                    }}
                    disabled={busy || connection?.readOnly}
                    required={required}
                    rows={5}
                    spellCheck={false}
                    aria-invalid={Boolean(error)}
                    aria-describedby={describedBy}
                  />
                ) : (
                  <input
                    type={field.format === "uri" ? "url" : "text"}
                    value={typeof value === "string" ? value : value === undefined ? "" : JSON.stringify(value)}
                    onChange={(event) => updateConfig(name, event.target.value || undefined)}
                    disabled={busy || connection?.readOnly}
                    required={required}
                    autoComplete="off"
                    aria-invalid={Boolean(error)}
                    minLength={field.minLength ?? undefined}
                    maxLength={field.maxLength ?? undefined}
                    aria-describedby={describedBy}
                  />
                )}
                {field.description && <small id={`${inputId}-help`}>{field.description}</small>}
                {error && <small id={errorId} className="connection-field-error" role="alert">{error}</small>}
              </label>
            );
          })}

          {primarySecret && (
            <label className="field span-2">
              <span>{primarySecret.label}（secret_ref）{primarySecret.required ? " *" : ""}</span>
              <input
                aria-label={`${primarySecret.label}（secret_ref）`}
                value={draft.secretRef}
                onChange={(event) => update("secretRef", event.target.value)}
                placeholder={`env://${primarySecret.environmentVariable || "CONNECTOR_TOKEN"}`}
                autoComplete="off"
                disabled={busy || connection?.readOnly}
                required={primarySecret.required}
                aria-invalid={Boolean(errors.secretRef)}
                aria-describedby={`${formId}-secret-help${errors.secretRef ? ` ${formId}-secret-error` : ""}`}
              />
              <small id={`${formId}-secret-help`}>
                {primarySecret.description || "凭据值只在适配器运行时解析。"} 接受
                {primarySecret.acceptedRefSchemes.map((scheme) => `${scheme}://`).join("、")}
                引用；不要粘贴令牌明文。
              </small>
              {errors.secretRef && <small id={`${formId}-secret-error`} className="connection-field-error" role="alert">{errors.secretRef}</small>}
            </label>
          )}

          {!connection ? (
            <label className="field">
              <span>保存后的期望状态</span>
              <select
                value={draft.desiredState}
                onChange={(event) => update("desiredState", event.target.value)}
                disabled={busy}
              >
                <option value="draft">先保存为草稿</option>
                <option value="disabled">保存为停用</option>
              </select>
            </label>
          ) : (
            <div className="field span-2 connection-editor-lifecycle-note">
              <span>生命周期</span>
              <small>保存配置不会改变连接启停状态；请在连接详情中使用“启用”或“停用”。</small>
            </div>
          )}

        </div>
      </form>
    </Dialog>
  );
}
