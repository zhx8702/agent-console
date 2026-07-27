type StatusTileProps = {
  label: string;
  value: string;
};

function deriveStatus(value: string): string | undefined {
  const v = value.toLowerCase();
  if (v === "ok" || v === "healthy") return "ok";
  if (v === "enabled" || v === "已启用") return "enabled";
  if (v === "error" || v === "fail" || v === "failed") return "error";
  return undefined;
}

export function StatusTile({ label, value }: StatusTileProps) {
  const status = deriveStatus(value);
  return (
    <article className="status-tile" data-status={status}>
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}
