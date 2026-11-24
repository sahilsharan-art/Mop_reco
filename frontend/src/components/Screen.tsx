import type React from "react";
import { useState, useMemo } from "react";
import { useSuspenseQuery, useQueryClient } from "@tanstack/react-query";
import {
  Search,
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
  Download,
  MoreHorizontal,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  AlertTriangle,
  Wallet,
  TrendingUp,
  ShieldCheck,
  Save,
  Plus,
  UploadCloud,
  Sparkles,
} from "lucide-react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import { toast } from "sonner";
import {
  screenQuery,
  params,
  display,
  money,
  api,
  download,
  type TableData,
  type Summary,
  type Row,
  type MatrixColumn,
} from "../lib/api";
import { useWorkspace } from "./Workspace";
import { Button, Modal, Pill } from "./ui";
import {
  titles,
  columns,
  label,
  heatOf,
  toneFor,
  rowTone,
  type ScreenKind,
} from "./table-utils";
export type { ScreenKind } from "./table-utils";
import { ReviewAction } from "./ReviewAction";
import { BulkUpload } from "./screens/BulkUpload";
import { SummaryView } from "./screens/SummaryView";
import { RecordModal } from "./screens/RecordModal";
import { MasterModal } from "./screens/MasterModal";
import { CreateUser } from "./screens/CreateUser";
import { UploadModal } from "./screens/UploadModal";
export function Screen({ kind }: { kind: ScreenKind }) {
  const { filter, bootstrap } = useWorkspace();
  const qc = useQueryClient();
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState("");
  const [master, setMaster] = useState(bootstrap.masters[0]?.key ?? "store");
  const [selected, setSelected] = useState<Row | null>(null);
  const [showUpload, setShowUpload] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [editMaster, setEditMaster] = useState(false);
  const extra = {
    offset: page * 50,
    limit: 50,
    ...(kind === "tasks"
      ? { search, status, sort: sort || "Overdue first, oldest due" }
      : {}),
    ...(kind === "exceptions"
      ? { status, sort: sort || "Amount (high to low)" }
      : {}),
  };
  const endpoint =
    kind === "masters"
      ? `/masters/${master}`
      : `/${kind === "uploads" ? "uploads" : kind}?${params(filter, extra)}`;
  const { data } = useSuspenseQuery(screenQuery(endpoint));
  const table = data as TableData;
  const summary = data as Summary;
  const [withdraw, setWithdraw] = useState<Row | null>(null);
  const [bulkReplies, setBulkReplies] = useState(false);
  const [localSort, setLocalSort] = useState<{
    key: string;
    desc: boolean;
  } | null>(null);
  const rows = useMemo(() => {
    let items = [...(table.rows ?? [])];
    if (search && kind !== "tasks")
      items = items.filter((r) =>
        Object.values(r).some((v) =>
          String(v).toLowerCase().includes(search.toLowerCase()),
        ),
      );
    if (localSort)
      items.sort((a, b) => {
        const av = a[localSort.key],
          bv = b[localSort.key];
        const diff =
          typeof av === "number" && typeof bv === "number"
            ? av - bv
            : String(av ?? "").localeCompare(String(bv ?? ""));
        return localSort.desc ? -diff : diff;
      });
    return items;
  }, [table.rows, search, localSort, kind]);
  const shownColumns =
    kind === "masters" ? (table.fields ?? []) : (columns[kind] ?? []);
  async function exportData() {
    try {
      await download(`/export/${kind}?${params(filter, { status, search })}`);
      toast.success("Export downloaded.");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Export failed.");
    }
  }
  if (kind === "summary") return <SummaryView data={summary} />;
  return (
    <>
      <div className="section-heading">
        <div>
          <h2>{titles[kind]}</h2>
          <p>
            {typeof table.total === "number"
              ? `${table.total.toLocaleString("en-IN")} records`
              : kind === "masters"
                ? `Version ${table.version ?? "—"}`
                : "Account administration"}
          </p>
        </div>
        <div className="toolbar-actions">
          {["tasks", "cash", "float", "exceptions"].includes(kind) ? (
            <Button variant="secondary" onClick={exportData}>
              <Download size={15} />
              Export all filtered records
            </Button>
          ) : null}
          {kind === "tasks" ? (
            <Button variant="secondary" onClick={() => setBulkReplies(true)}>
              <UploadCloud size={15} />
              Upload store replies
            </Button>
          ) : null}
          {kind === "cash" ? (
            <Button
              variant="secondary"
              onClick={() =>
                download(`/store-sheet/export?${params(filter)}`).catch((e) =>
                  toast.error(String(e)),
                )
              }
            >
              <Download size={15} />
              Audited store workbook
            </Button>
          ) : null}
          {kind === "uploads" ? (
            <Button onClick={() => setShowUpload(true)}>
              <UploadCloud size={16} />
              Upload report
            </Button>
          ) : null}
          {kind === "users" ? (
            <Button onClick={() => setShowCreate(true)}>
              <Plus size={16} />
              Add user
            </Button>
          ) : null}
          {kind === "masters" ? (
            <Button
              disabled={!table.in_database}
              onClick={() => setEditMaster(true)}
            >
              <Save size={15} />
              Edit master
            </Button>
          ) : null}
        </div>
      </div>
      {kind === "masters" && !table.in_database ? (
        <div className="alert alert-warning">
          Masters are still sourced from Excel. Editing stays blocked until the
          existing master migration is complete.
        </div>
      ) : null}
      {kind === "tasks" && table.summary ? (
        <div className="metric-strip">
          {["open", "pending", "wip", "closed"].map((key) => {
            const value = table.summary?.[key];
            const tuple = Array.isArray(value) ? value : [];
            return (
              <div
                className={`metric metric-${key === "closed" ? "success" : "warning"}`}
                key={key}
              >
                <span>{key === "wip" ? "Under review" : label(key)}</span>
                <strong>{display(tuple[0])}</strong>
                <small>{money(tuple[1])}</small>
              </div>
            );
          })}
        </div>
      ) : null}
      <div className="table-toolbar">
        <div className="search-field">
          <Search size={16} />
          <input
            aria-label="Search records"
            placeholder={
              kind === "tasks"
                ? "Search store, bill or reply"
                : "Search this page"
            }
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(0);
            }}
          />
        </div>
        {kind === "masters" ? (
          <select
            aria-label="Master selection"
            value={master}
            onChange={(e) => {
              setMaster(e.target.value);
              setLocalSort(null);
            }}
          >
            {bootstrap.masters.map((x) => (
              <option key={x.key} value={x.key}>
                {x.title}
              </option>
            ))}
          </select>
        ) : null}
        {kind === "tasks" || kind === "exceptions" ? (
          <>
            <select
              aria-label="Status filter"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setPage(0);
              }}
            >
              <option value="">All statuses</option>
              {(kind === "tasks"
                ? ["open", "replied", "under_review", "closed"]
                : ["open", "under_investigation", "closed", "auto_resolved"]
              ).map((x) => (
                <option key={x} value={x}>
                  {label(x)}
                </option>
              ))}
            </select>
            <select
              aria-label="Sort records"
              value={sort}
              onChange={(e) => {
                setSort(e.target.value);
                setPage(0);
              }}
            >
              {(kind === "tasks"
                ? ["Overdue first, oldest due", "Largest amount"]
                : [
                    "Amount (high to low)",
                    "Amount (low to high)",
                    "Date (oldest first)",
                    "Date (newest first)",
                    "Store",
                  ]
              ).map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </>
        ) : null}
        <span className="toolbar-count">{rows.length} shown</span>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {shownColumns.map((key) => (
                <th
                  key={key}
                  aria-sort={
                    localSort?.key === key
                      ? localSort.desc
                        ? "descending"
                        : "ascending"
                      : "none"
                  }
                >
                  <Button
                    variant="icon"
                    className="sort-head"
                    onClick={() =>
                      setLocalSort({
                        key,
                        desc: localSort?.key === key ? !localSort.desc : false,
                      })
                    }
                  >
                    {label(key)}
                    {localSort?.key === key ? (
                      localSort.desc ? (
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
              {["tasks", "float", "exceptions", "users", "uploads"].includes(
                kind,
              ) ? (
                <th>Actions</th>
              ) : null}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr
                data-tone={rowTone(row)}
                key={String(
                  row["_id"] ??
                    row["id"] ??
                    row["exception_key"] ??
                    `${row["store"]}-${row["date"] ?? row["business_date"]}-${i}`,
                )}
              >
                {shownColumns.map((key, index) => (
                  <td
                    key={key}
                    style={
                      index === 0 ? undefined : heatOf(row[key], rows, key)
                    }
                    className={`${index === 0 ? "sticky-cell" : ""} ${typeof row[key] === "number" ? `numeric heat ${(row[key] as number) < 0 ? "heat-neg" : ""}` : ""}`}
                  >
                    {/status|role|active|negative_float/.test(
                      key.toLowerCase(),
                    ) ? (
                      <Pill tone={toneFor(key, row[key])}>
                        {display(row[key], key)}
                      </Pill>
                    ) : /^(type|reason|exception_type)$/i.test(key) &&
                      typeof row[key] === "string" ? (
                      <span className="type-chip">
                        {label(String(row[key]))}
                      </span>
                    ) : (
                      display(row[key], key)
                    )}
                  </td>
                ))}
                {["tasks", "float", "exceptions", "users", "uploads"].includes(
                  kind,
                ) ? (
                  <td>
                    <Button
                      variant="icon"
                      title={
                        kind === "uploads"
                          ? "Withdraw and archive upload"
                          : "Open record"
                      }
                      disabled={
                        kind === "uploads" && row["status"] === "deleted"
                      }
                      aria-label={`${kind === "uploads" ? "Withdraw upload" : "Open"} ${display(row["Task id"] ?? row["store"] ?? row["display_name"] ?? row["reference"])}`}
                      onClick={() =>
                        kind === "uploads" ? setWithdraw(row) : setSelected(row)
                      }
                    >
                      <MoreHorizontal size={18} />
                    </Button>
                  </td>
                ) : null}
              </tr>
            ))}
          </tbody>
          {rows.length > 1 &&
          shownColumns.some(
            (k) =>
              typeof rows[0]?.[k] === "number" &&
              !/days|age|id$|threshold|count/i.test(k),
          ) ? (
            <tfoot>
              <tr>
                {shownColumns.map((key, index) => {
                  const nums = rows
                    .map((r) => r[key])
                    .filter((x) => typeof x === "number") as number[];
                  const tot = nums.reduce((a, b) => a + b, 0);
                  return (
                    <td
                      key={key}
                      className={`${index === 0 ? "sticky-cell" : ""} numeric ${tot < 0 ? "heat-neg" : ""}`}
                    >
                      {index === 0
                        ? `Total · ${rows.length}`
                        : nums.length &&
                            !/days|age|id$|threshold|count/i.test(key)
                          ? display(tot, key)
                          : ""}
                    </td>
                  );
                })}
                {["tasks", "float", "exceptions", "users", "uploads"].includes(
                  kind,
                ) ? (
                  <td />
                ) : null}
              </tr>
            </tfoot>
          ) : null}
        </table>
        {rows.length === 0 ? (
          <div className="empty-state">
            <Search size={26} />
            <h3>No matching records</h3>
            <p>Try another store, period or status.</p>
          </div>
        ) : null}
      </div>
      {!["masters", "users", "uploads"].includes(kind) ? (
        <div className="pagination">
          <span>
            Page {page + 1} · {table.total ?? 0} records
          </span>
          <Button
            variant="icon"
            aria-label="Previous page"
            disabled={page === 0}
            onClick={() => setPage(page - 1)}
          >
            <ChevronLeft size={17} />
          </Button>
          <Button
            variant="icon"
            aria-label="Next page"
            disabled={(page + 1) * 50 >= (table.total ?? 0)}
            onClick={() => setPage(page + 1)}
          >
            <ChevronRight size={17} />
          </Button>
        </div>
      ) : null}
      {withdraw ? (
        <ReviewAction
          title="Withdraw upload"
          path={`/uploads/${withdraw["id"]}/withdraw`}
          details={{
            ...withdraw,
            effect:
              "Archive the original file; preserve upload history. Refused while a run is active.",
          }}
          close={() => setWithdraw(null)}
          done={() => qc.invalidateQueries()}
        />
      ) : null}
      {bulkReplies ? (
        <BulkUpload
          kind="replies"
          close={() => setBulkReplies(false)}
          done={() => qc.invalidateQueries()}
        />
      ) : null}
      {selected ? (
        <RecordModal
          kind={kind}
          row={selected}
          close={() => setSelected(null)}
          refresh={() => qc.invalidateQueries({ queryKey: ["screen"] })}
        />
      ) : null}
      {showUpload ? (
        <UploadModal
          close={() => setShowUpload(false)}
          refresh={() => qc.invalidateQueries({ queryKey: ["screen"] })}
        />
      ) : null}
      {showCreate ? (
        <CreateUser
          close={() => setShowCreate(false)}
          refresh={() => qc.invalidateQueries({ queryKey: ["screen"] })}
        />
      ) : null}
      {editMaster ? (
        <MasterModal
          name={master}
          data={table}
          close={() => setEditMaster(false)}
          refresh={() => qc.invalidateQueries({ queryKey: ["screen"] })}
        />
      ) : null}
    </>
  );
}
