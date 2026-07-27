import { Alert, Dialog } from "../../components";

export type DiscardChangesPrompt = {
  title: string;
  description: string;
  confirmLabel: string;
};

type DiscardChangesDialogProps = {
  prompt: DiscardChangesPrompt | null;
  onCancel: () => void;
  onConfirm: () => void;
};

export function DiscardChangesDialog({
  prompt,
  onCancel,
  onConfirm,
}: DiscardChangesDialogProps) {
  return (
    <Dialog
      open={Boolean(prompt)}
      onClose={onCancel}
      title={prompt?.title || "放弃未保存修改"}
      description={prompt?.description}
      footer={
        <>
          <button className="button button-secondary" type="button" onClick={onCancel}>
            继续编辑
          </button>
          <button className="button button-danger" type="button" onClick={onConfirm}>
            {prompt?.confirmLabel || "放弃修改"}
          </button>
        </>
      }
    >
      <Alert variant="warning" title="未保存内容将丢失">
        此操作只放弃浏览器中的当前草稿，不会撤销已经保存到服务器的版本。
      </Alert>
    </Dialog>
  );
}
