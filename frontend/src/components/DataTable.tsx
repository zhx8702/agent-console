import { type CSSProperties, type ReactNode } from "react";

export type DataTableColumn<Row> = {
  id: string;
  header: ReactNode;
  cell: (row: Row, index: number) => ReactNode;
  align?: "left" | "center" | "right";
  width?: CSSProperties["width"];
  className?: string;
};

type DataTableProps<Row> = {
  caption: ReactNode;
  columns: DataTableColumn<Row>[];
  rows: Row[];
  rowKey: (row: Row, index: number) => string | number;
  onRowActivate?: (row: Row, index: number) => void;
  rowLabel?: (row: Row, index: number) => string;
  emptyMessage?: ReactNode;
  className?: string;
};

export function DataTable<Row>({
  caption,
  columns,
  rows,
  rowKey,
  onRowActivate,
  rowLabel,
  emptyMessage = "暂无数据",
  className = "",
}: DataTableProps<Row>) {
  return (
    <div className={`data-table-shell${className ? ` ${className}` : ""}`}>
      <table className="data-table">
        <caption>{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.id}
                scope="col"
                className={column.className}
                style={{ textAlign: column.align, width: column.width }}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!rows.length && (
            <tr>
              <td className="data-table-empty" colSpan={Math.max(columns.length, 1)}>
                {emptyMessage}
              </td>
            </tr>
          )}
          {rows.map((row, index) => (
            <tr key={rowKey(row, index)}>
              {columns.map((column, columnIndex) => (
                <td
                  key={column.id}
                  className={column.className}
                  style={{ textAlign: column.align }}
                >
                  {onRowActivate && columnIndex === 0 ? (
                    <button
                      type="button"
                      className="data-table-cell-action"
                      aria-label={rowLabel?.(row, index) || `打开第 ${index + 1} 行`}
                      onClick={() => onRowActivate(row, index)}
                    >
                      {column.cell(row, index)}
                    </button>
                  ) : (
                    column.cell(row, index)
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
