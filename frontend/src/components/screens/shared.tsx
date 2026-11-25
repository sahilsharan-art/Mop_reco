import type { ReactNode } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { display, type Row } from "../../lib/api";
import { label } from "../table-utils";
import { Button } from "../ui";

/** A server-paged footer: "Page 2 of 7 · 340 records". */
export function Pager({
  offset,
  total,
  limit = 50,
  setOffset,
}: {
  offset: number;
  total: number;
  limit?: number;
  setOffset: (n: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="pagination">
      <span>
        Page {Math.floor(offset / limit) + 1} of {pages} · {total.toLocaleString("en-IN")} records
      </span>
      <Button variant="icon" aria-label="Previous page" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
        <ChevronLeft size={17} />
      </Button>
      <Button variant="icon" aria-label="Next page" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>
        <ChevronRight size={17} />
      </Button>
    </div>
  );
}

export type Col = {
  key: string;
  title?: string;
  /** a custom cell; the default formats the value by its column name (rupees, DD/MM/YYYY, counts) */
  cell?: (row: Row, index: number) => ReactNode;
  numeric?: boolean;
};

/** A plain read-only grid. Money and dates are formatted by column name in one place (`display`). */
export function Grid({
  columns,
  rows,
  rowKey,
  empty = "Nothing here for this selection.",
  tone,
}: {
  columns: Col[];
  rows: Row[];
  rowKey: (row: Row, index: number) => string;
  empty?: string;
  tone?: (row: Row) => string | undefined;
}) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key}>{c.title ?? label(c.key)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={rowKey(row, i)} data-tone={tone?.(row)}>
              {columns.map((c, ci) => (
                <td key={c.key} className={`${ci === 0 ? "sticky-cell" : ""} ${c.numeric || typeof row[c.key] === "number" ? "numeric" : ""}`}>
                  {c.cell ? c.cell(row, i) : display(row[c.key], c.key)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 ? (
        <div className="empty-state">
          <p>{empty}</p>
        </div>
      ) : null}
    </div>
  );
}

/** A collapsible section whose header always shows its count, and whose body is only built (and fetched) once opened. */
export function Collapsible({
  title,
  meta,
  open,
  setOpen,
  children,
}: {
  title: string;
  meta?: string;
  open: boolean;
  setOpen: (v: boolean) => void;
  children: ReactNode;
}) {
  return (
    <section className="section">
      <div className="section-heading">
        <div>
          <h2>{title}</h2>
          {meta ? <p>{meta}</p> : null}
        </div>
        <Button variant="secondary" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Show"}
        </Button>
      </div>
      {open ? children : null}
    </section>
  );
}
