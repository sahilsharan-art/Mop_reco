import { useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Search,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { type Row, display } from "../lib/api";
import { Button, Pill } from "./ui";
import { label, toneFor, rowTone, heatOf } from "./table-utils";
export function DataTable({
  rows,
  columns,
  actions,
  searchable = true,
  caption = "records",
  pageSize = 50,
}: {
  rows: Row[];
  columns: string[];
  actions?: (row: Row) => React.ReactNode;
  searchable?: boolean;
  caption?: string;
  pageSize?: number;
}) {
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<{ key: string; desc: boolean } | null>(null);
  const [page, setPage] = useState(0);
  const filtered = useMemo(() => {
    const out = rows.filter((r) =>
      Object.values(r).some((v) =>
        display(v).toLowerCase().includes(search.toLowerCase()),
      ),
    );
    if (sort)
      out.sort((a, b) => {
        const av = a[sort.key],
          bv = b[sort.key];
        const n =
          typeof av === "number" && typeof bv === "number"
            ? av - bv
            : String(av ?? "").localeCompare(String(bv ?? ""));
        return sort.desc ? -n : n;
      });
    return out;
  }, [rows, search, sort]);
  const current = Math.min(
    page,
    Math.max(0, Math.ceil(filtered.length / pageSize) - 1),
  );
  const shown = filtered.slice(current * pageSize, (current + 1) * pageSize);
  return (
    <>
      {searchable ? (
        <div className="table-toolbar">
          <label className="search-field">
            <Search size={16} />
            <input
              aria-label={`Search ${caption}`}
              placeholder={`Search ${caption}`}
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(0);
              }}
            />
          </label>
          <span className="toolbar-count">
            {filtered.length.toLocaleString()} {caption}
          </span>
        </div>
      ) : null}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {columns.map((key, i) => (
                <th
                  key={key}
                  className={i === 0 ? "sticky-cell" : ""}
                  aria-sort={
                    sort?.key === key
                      ? sort.desc
                        ? "descending"
                        : "ascending"
                      : "none"
                  }
                >
                  <Button
                    variant="icon"
                    className="sort-head"
                    onClick={() => {
                      setSort({
                        key,
                        desc: sort?.key === key ? !sort.desc : false,
                      });
                      setPage(0);
                    }}
                  >
                    {label(key)}
                    {sort?.key === key ? (
                      sort.desc ? (
                        <ArrowDown size={13} />
                      ) : (
                        <ArrowUp size={13} />
                      )
                    ) : (
                      <ArrowUpDown size={13} />
                    )}
                  </Button>
                </th>
              ))}
              {actions ? <th>Actions</th> : null}
            </tr>
          </thead>
          <tbody>
            {shown.map((r, index) => (
              <tr
                key={String(r["id"] ?? r["key"] ?? index)}
                data-tone={rowTone(r)}
              >
                {columns.map((key, i) => (
                  <td
                    key={key}
                    className={`${i === 0 ? "sticky-cell " : ""}${typeof r[key] === "number" ? "numeric heat " : ""}${typeof r[key] === "number" && r[key] < 0 ? "heat-neg" : ""}`}
                    style={heatOf(r[key], filtered, key)}
                  >
                    {/status|lifecycle|negative_float/.test(key) ? (
                      <Pill tone={toneFor(key, r[key])}>
                        {display(r[key], key)}
                      </Pill>
                    ) : (
                      display(r[key], key)
                    )}
                  </td>
                ))}
                {actions ? <td>{actions(r)}</td> : null}
              </tr>
            ))}
          </tbody>
        </table>
        {!shown.length ? (
          <div className="empty-state">
            <Search size={24} />
            <h3>No matching {caption}</h3>
          </div>
        ) : null}
      </div>
      {filtered.length > pageSize ? (
        <div className="pagination">
          <span>
            {current * pageSize + 1}–
            {Math.min((current + 1) * pageSize, filtered.length)} of{" "}
            {filtered.length}
          </span>
          <Button
            variant="icon"
            aria-label="Previous page"
            disabled={!current}
            onClick={() => setPage(current - 1)}
          >
            <ChevronLeft size={16} />
          </Button>
          <Button
            variant="icon"
            aria-label="Next page"
            disabled={(current + 1) * pageSize >= filtered.length}
            onClick={() => setPage(current + 1)}
          >
            <ChevronRight size={16} />
          </Button>
        </div>
      ) : null}
    </>
  );
}
