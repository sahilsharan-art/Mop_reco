import { useState } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert, Clock, FileUp, Trash2, UploadCloud } from "lucide-react";
import { toast } from "sonner";
import { api, display, money, type Row } from "../../lib/api";
import { Button, Modal, Pill } from "../ui";
import { Collapsible, Grid, Pager, type Col } from "./shared";

const n0 = (v: unknown) => Number(v ?? 0).toLocaleString("en-IN");
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthLabel = (m: string) => (m ? `${MONTHS[Number(m.slice(5, 7)) - 1]} ${m.slice(0, 4)}` : "—");
const short = (iso: string) => (iso ? `${iso.slice(8, 10)}/${iso.slice(5, 7)}` : "");

type Source = { key: string; title: string; extensions: string[]; month_specific: boolean; note: string };
type Cell = { state: "none" | "present" | "partial"; files?: number; rows?: number; dates_present?: number; days_so_far?: number; from?: string; to?: string; pending?: number; failed?: number; by?: string; at?: string };
type Grid_ = { months: string[]; rows: { source_key: string; report: string; months: Record<string, Cell> }[] };
type Staged = { name: string; size: number; state: "waiting" | "checking" | "checked" | "stored" | "error"; token?: string; ok?: boolean; errors?: string[]; warnings?: string[]; rows?: number; range?: string[] | null; message?: string };

/* ------------------------------------------------------------------------------------------------ the page */
export function UploadsPage() {
  const qc = useQueryClient();
  const [source, setSource] = useState("");
  const [month, setMonth] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["uploads"] });
  const sources = useQuery({ queryKey: ["uploads", "sources"], queryFn: () => api<{ sources: Source[]; from_erp: string[] }>("/uploads/sources"), staleTime: 60000 });
  const grid = useQuery({
    queryKey: ["uploads", "coverage"],
    queryFn: () => api<Grid_>("/uploads/coverage"),
    // while any stored file is still being read, look again every few seconds
    refetchInterval: (q) => (/"pending":[1-9]/.test(JSON.stringify(q.state.data ?? {})) ? 4000 : false),
  });
  return (
    <>
      <section className="section">
        <div className="section-heading">
          <div>
            <h2>Upload data</h2>
            <p>
              Paytm, Axis, Bajaj and CMS files are read only from what is uploaded here, month by month. The numbered folders are no longer read and are left
              untouched. POS bills and petty cash come from the ERP, so nothing is uploaded for them.
            </p>
          </div>
        </div>
        {grid.data && sources.data ? (
          <CoverageGrid grid={grid.data} pick={(s, m) => { setSource(s); setMonth(m); document.getElementById("upload-form")?.scrollIntoView({ behavior: "smooth" }); }} />
        ) : (
          <p className="muted">Reading what has been uploaded…</p>
        )}
      </section>
      {sources.data && grid.data ? <UploadForm sources={sources.data.sources} months={grid.data.months} source={source} setSource={setSource} month={month} setMonth={setMonth} done={refresh} /> : null}
      <History sources={sources.data?.sources ?? []} months={grid.data?.months ?? []} refreshAll={refresh} />
      <Compare />
    </>
  );
}

/* ------------------------------------------------------------------------------------------------ month grid */
function CoverageGrid({ grid, pick }: { grid: Grid_; pick: (source: string, month: string) => void }) {
  return (
    <div className="table-scroll">
      <table className="cover-grid">
        <thead>
          <tr>
            <th>Data</th>
            {grid.months.map((m) => (
              <th key={m}>{monthLabel(m)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {grid.rows.map((r) => (
            <tr key={r.source_key}>
              <td className="sticky-cell">{r.report}</td>
              {grid.months.map((m) => {
                const c = r.months[m];
                return (
                  <td key={m}>
                    <button type="button" className={`cover-cell cover-${c.state}`} title="Upload a file for this month" onClick={() => pick(r.source_key, m)}>
                      {c.state === "none" ? (
                        <span>Not uploaded</span>
                      ) : (
                        <>
                          <b>
                            {c.state === "partial" ? <CircleAlert size={13} /> : <CheckCircle2 size={13} />} {n0(c.files)} file{c.files === 1 ? "" : "s"}
                          </b>
                          <span>
                            {c.dates_present ? `${c.dates_present} of ${c.days_so_far} days` : c.pending ? "reading…" : c.failed ? "not read" : ""}
                          </span>
                          <span>{c.from ? `${short(c.from)} – ${short(c.to ?? "")}` : ""}</span>
                        </>
                      )}
                    </button>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted">
        “N of M days” compares the days the files actually hold with the days the month has had so far. A partial month is not an error until the run needs it.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------ the upload form */
function UploadForm({ sources, months, source, setSource, month, setMonth, done }: {
  sources: Source[]; months: string[]; source: string; setSource: (v: string) => void; month: string; setMonth: (v: string) => void; done: () => Promise<unknown>;
}) {
  const [files, setFiles] = useState<Staged[]>([]);
  const [handles, setHandles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const spec = sources.find((s) => s.key === source);
  const ready = files.filter((f) => f.state === "checked" && f.ok);
  const patch = (i: number, p: Partial<Staged>) => setFiles((all) => all.map((f, j) => (j === i ? { ...f, ...p } : f)));

  async function checkAll() {
    setBusy(true);
    for (let i = 0; i < handles.length; i++) {
      if (files[i]?.state === "checked" || files[i]?.state === "stored") continue;
      patch(i, { state: "checking" });
      try {
        const body = new FormData();
        body.set("file", handles[i]);
        body.set("source_key", source);
        body.set("month", month);
        const r = await api<{ token: string; ok: boolean; errors: string[]; warnings: string[]; row_count: number; date_range: string[] | null }>("/uploads/stage", { method: "POST", body });
        patch(i, { state: "checked", token: r.token, ok: r.ok, errors: r.errors, warnings: r.warnings, rows: r.row_count, range: r.date_range });
      } catch (e) {
        patch(i, { state: "error", message: e instanceof Error ? e.message : "The file could not be checked." });
      }
    }
    setBusy(false);
  }

  async function storeAll() {
    setBusy(true);
    let stored = 0;
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      if (f.state !== "checked" || !f.ok || !f.token) continue;
      try {
        await api("/uploads/confirm", { method: "POST", body: JSON.stringify({ token: f.token }) });
        patch(i, { state: "stored" });
        stored += 1;
      } catch (e) {
        patch(i, { state: "error", message: e instanceof Error ? e.message : "Could not store the file." });
      }
    }
    setBusy(false);
    if (stored) {
      toast.success(`${stored} file${stored === 1 ? "" : "s"} stored. They are being read for the month grid.`);
      await done();
    }
  }

  return (
    <section className="section" id="upload-form">
      <div className="section-heading">
        <div>
          <h2>Add files</h2>
          <p>Choose the data and the month, then one or several files. Each file is checked once (columns, dates inside the chosen month); only checked files are stored.</p>
        </div>
      </div>
      <div className="upload-form-row">
        <label>
          Data
          <select value={source} onChange={(e) => { setSource(e.target.value); setFiles([]); setHandles([]); }}>
            <option value="">Choose…</option>
            {sources.map((s) => (
              <option key={s.key} value={s.key}>
                {s.title}
              </option>
            ))}
          </select>
        </label>
        {spec?.month_specific !== false ? (
          <label>
            Month
            <select value={month} onChange={(e) => { setMonth(e.target.value); setFiles([]); setHandles([]); }}>
              <option value="">Choose…</option>
              {[...months].reverse().map((m) => (
                <option key={m} value={m}>
                  {monthLabel(m)}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <label className="upload-drop">
          <UploadCloud size={22} />
          <span>{handles.length ? `${handles.length} file${handles.length === 1 ? "" : "s"} chosen` : `Choose file(s)${spec ? ` (${spec.extensions.join(", ")})` : ""}`}</span>
          <input
            type="file"
            multiple
            aria-label="Files to upload"
            accept={spec?.extensions.join(",")}
            disabled={!source || (spec?.month_specific !== false && !month)}
            onChange={(e) => {
              const picked = Array.from(e.target.files ?? []);
              setHandles(picked);
              setFiles(picked.map((f) => ({ name: f.name, size: f.size, state: "waiting" as const })));
            }}
          />
        </label>
      </div>
      {spec?.note ? <p className="muted">{spec.note}</p> : null}
      {files.length ? (
        <>
          <Grid
            columns={[
              { key: "name", title: "File" },
              { key: "size", title: "Size", cell: (r) => `${(Number(r["size"]) / 1048576).toFixed(1)} MB` },
              {
                key: "state",
                title: "Check",
                cell: (r) => {
                  const f = r as unknown as Staged;
                  if (f.state === "waiting") return <Pill>Not checked</Pill>;
                  if (f.state === "checking") return <Pill tone="warning"><Clock size={12} /> Checking…</Pill>;
                  if (f.state === "stored") return <Pill tone="success">Stored</Pill>;
                  if (f.state === "error") return <Pill tone="danger">{f.message}</Pill>;
                  return f.ok ? <Pill tone="success">OK · {n0(f.rows)} rows{f.range ? ` · ${display(f.range[0].slice(0, 10))} – ${display(f.range[1].slice(0, 10))}` : ""}</Pill> : <Pill tone="danger">{(f.errors ?? []).join(" ")}</Pill>;
                },
              },
              { key: "warnings", title: "Notes", cell: (r) => ((r as unknown as Staged).warnings ?? []).join(" ") },
            ]}
            rows={files as unknown as Row[]}
            rowKey={(_r, i) => String(i)}
          />
          <div className="modal-footer">
            <Button variant="secondary" busy={busy} disabled={!handles.length} onClick={checkAll}>
              Check the files
            </Button>
            <Button busy={busy} disabled={!ready.length} onClick={storeAll}>
              <FileUp size={15} />
              Store {ready.length} checked file{ready.length === 1 ? "" : "s"}
            </Button>
          </div>
        </>
      ) : null}
    </section>
  );
}

/* ------------------------------------------------------------------------------------------------ history */
function History({ sources, months, refreshAll }: { sources: Source[]; months: string[]; refreshAll: () => Promise<unknown> }) {
  const [source, setSource] = useState("");
  const [month, setMonth] = useState("");
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [withdraw, setWithdraw] = useState<Row | null>(null);
  const qs = new URLSearchParams({ source_key: source, month, status, offset: String(offset), limit: "25" }).toString();
  const { data } = useQuery({
    queryKey: ["uploads", "history", qs],
    queryFn: () => api<{ rows: Row[]; total: number }>(`/uploads/history?${qs}`),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => ((q.state.data?.rows ?? []).some((r) => r["stats"] === "reading") ? 4000 : false),
  });
  const cols: Col[] = [
    { key: "uploaded_at", title: "Uploaded" },
    { key: "data", title: "Data" },
    { key: "month", title: "Month", cell: (r) => monthLabel(String(r["month"] ?? "")) },
    { key: "file", title: "File" },
    { key: "rows" },
    { key: "from", title: "Dates in file", cell: (r) => (r["from"] ? `${display(r["from"])} – ${display(r["to"])}` : "—") },
    { key: "uploaded_by", title: "By" },
    {
      key: "stats",
      title: "Read",
      cell: (r) => <Pill tone={r["stats"] === "read" ? "success" : r["stats"] === "failed" ? "danger" : r["stats"] === "reading" ? "warning" : "neutral"}>{String(r["stats"])}</Pill>,
    },
    { key: "in_use_by_run", title: "Used by run", cell: (r) => (r["in_use_by_run"] ? `#${r["in_use_by_run"]}` : "—") },
    {
      key: "status",
      cell: (r) =>
        r["status"] === "deleted" ? (
          <span>
            <Pill>Withdrawn</Pill> <small className="muted">{String(r["withdrawn_by"] ?? "")}</small>
          </span>
        ) : (
          <Button variant="secondary" aria-label={`Withdraw ${r["file"]}`} onClick={() => setWithdraw(r)}>
            <Trash2 size={14} />
            Withdraw
          </Button>
        ),
    },
  ];
  return (
    <section className="section">
      <div className="section-heading">
        <div>
          <h2>Upload history</h2>
          <p>Every file ever uploaded: what, which month, who, when, and whether a run has used it. A withdrawn file stays in this list.</p>
        </div>
      </div>
      <div className="table-toolbar">
        <select aria-label="Data" value={source} onChange={(e) => { setSource(e.target.value); setOffset(0); }}>
          <option value="">All data</option>
          {sources.map((s) => (
            <option key={s.key} value={s.key}>{s.title}</option>
          ))}
        </select>
        <select aria-label="Month" value={month} onChange={(e) => { setMonth(e.target.value); setOffset(0); }}>
          <option value="">All months</option>
          {[...months].reverse().map((m) => (
            <option key={m} value={m}>{monthLabel(m)}</option>
          ))}
        </select>
        <select aria-label="Status" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
          <option value="">Stored and withdrawn</option>
          <option value="stored">Stored</option>
          <option value="deleted">Withdrawn</option>
        </select>
      </div>
      <Grid columns={cols} rows={data?.rows ?? []} rowKey={(r) => String(r["id"])} empty="Nothing has been uploaded yet." />
      <Pager offset={offset} total={data?.total ?? 0} limit={25} setOffset={setOffset} />
      {withdraw ? <WithdrawModal row={withdraw} close={() => setWithdraw(null)} done={refreshAll} /> : null}
    </section>
  );
}

function WithdrawModal({ row, close, done }: { row: Row; close: () => void; done: () => Promise<unknown> }) {
  const [reason, setReason] = useState("");
  const [sure, setSure] = useState(false);
  const [busy, setBusy] = useState(false);
  async function go() {
    setBusy(true);
    try {
      await api(`/uploads/${row["id"]}/withdraw`, { method: "POST", body: JSON.stringify({ confirmed: true, reason }) });
      toast.success("Withdrawn. The file is kept in the archive and the history row stays.");
      await done();
      close();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not withdraw.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Withdraw this file" onClose={close}>
      <p>
        <b>{String(row["file"])}</b> — {String(row["data"])}, {monthLabel(String(row["month"] ?? ""))}
      </p>
      <p className="muted">
        The file is moved to the archive (never erased) and the next run will not read it, so figures for its dates may change.
        {row["in_use_by_run"] ? ` Run #${row["in_use_by_run"]} already used it; that run's figures do not change.` : ""}
      </p>
      <label>
        Reason (required)
        <input value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      <label className="check-label">
        <input type="checkbox" checked={sure} onChange={(e) => setSure(e.target.checked)} /> I understand
      </label>
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>Cancel</Button>
        <Button variant="danger" busy={busy} disabled={!reason.trim() || !sure} onClick={go}>Withdraw</Button>
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------------------------------------ comparison with the folders */
type Cmp = {
  source_key: string; report: string; folder_dates: number; upload_dates: number; dates_equal: number; dates_differ: number; dates_folder_only: number;
  dates_upload_only: number; folder_rows: number; upload_rows: number; folder_amount: number; upload_amount: number; details: Row[]; details_total: number; note?: string;
};
type CompareResp = {
  quick: { available: boolean; message?: string; sources: (Cmp & { uploaded_files: number; files_read: number })[] };
  exact: { state: "never" | "running" | "done" | "failed"; started_at?: string; finished_at?: string; progress?: string[]; sources?: Cmp[]; error?: string };
};

function Compare() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  const { data } = useQuery({
    queryKey: ["uploads", "compare"],
    queryFn: () => api<CompareResp>("/uploads/compare"),
    enabled: open,
    refetchInterval: (q) => (q.state.data?.exact.state === "running" ? 5000 : false),
  });
  async function start() {
    setBusy(true);
    try {
      await api("/uploads/compare/run", { method: "POST" });
      await qc.invalidateQueries({ queryKey: ["uploads", "compare"] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not start the comparison.");
    } finally {
      setBusy(false);
    }
  }
  const exact = data?.exact;
  const rows = exact?.state === "done" || (exact?.sources?.length ?? 0) > 0 ? (exact?.sources ?? []) : [];
  const sel = rows.find((r) => r.source_key === detail);
  return (
    <Collapsible title="Compare with the folder data" meta="What the uploaded files give, against what the numbered folders gave before they were switched off" open={open} setOpen={setOpen}>
      {data && !data.quick.available ? <p className="alert alert-warning">{data.quick.message}</p> : null}
      {data?.quick.available ? (
        <>
          <div className="toolbar-actions">
            <Button busy={busy || exact?.state === "running"} onClick={start}>
              {exact?.state === "running" ? "Comparing…" : "Run the exact comparison"}
            </Button>
            <span className="muted">
              {exact?.state === "running"
                ? (exact.progress ?? []).slice(-1)[0]
                : exact?.state === "done"
                  ? `Last run finished ${display(exact.finished_at ?? "")}`
                  : exact?.state === "failed"
                    ? `Failed: ${exact.error}`
                    : "Reads every uploaded file the way a run does (duplicates removed) and compares each date to the paisa. Takes a few minutes; nothing is changed."}
            </span>
          </div>
          {rows.length ? (
            <Grid
              columns={[
                { key: "report", title: "Data" },
                { key: "dates_equal", title: "Dates equal" },
                { key: "dates_differ", title: "Differ" },
                { key: "dates_folder_only", title: "Folder only" },
                { key: "dates_upload_only", title: "Upload only" },
                { key: "folder_rows", title: "Folder rows" },
                { key: "upload_rows", title: "Upload rows" },
                { key: "folder_amount", title: "Folder amount", cell: (r) => money(r["folder_amount"]) },
                { key: "upload_amount", title: "Upload amount", cell: (r) => money(r["upload_amount"]) },
                { key: "source_key", title: "", cell: (r) => <Button variant="secondary" onClick={() => setDetail(String(r["source_key"]))}>Dates</Button> },
              ]}
              rows={rows as unknown as Row[]}
              rowKey={(r) => String(r["source_key"])}
              tone={(r) => (Number(r["dates_differ"]) + Number(r["dates_folder_only"]) + Number(r["dates_upload_only"]) > 0 ? "attention" : undefined)}
            />
          ) : null}
          {sel ? (
            <>
              <h3 className="muted">
                {sel.report}: {n0(sel.details_total)} date{sel.details_total === 1 ? "" : "s"} that differ{sel.details_total > sel.details.length ? ` (largest ${sel.details.length} shown)` : ""}
              </h3>
              <Grid
                columns={[
                  { key: "date" },
                  { key: "kind", title: "Kind" },
                  { key: "folder_rows", title: "Folder rows" },
                  { key: "upload_rows", title: "Upload rows" },
                  { key: "folder_amount", title: "Folder amount", cell: (r) => money(r["folder_amount"]) },
                  { key: "upload_amount", title: "Upload amount", cell: (r) => money(r["upload_amount"]) },
                  { key: "amount_difference", title: "Difference", cell: (r) => money(r["amount_difference"]) },
                ]}
                rows={sel.details}
                rowKey={(r, i) => `${r["date"]}-${i}`}
                empty="Every date matches the folder data."
              />
            </>
          ) : null}
          <h3 className="muted">Quick view (no files are read)</h3>
          <p className="muted">
            Each file's own per-date totals added together. Files that overlap (a re-exported month, a late row in the next month's file) are counted twice here, so a date
            where uploads show <b>more</b> than the folders is usually overlap, and a date where they show <b>less</b> is data that did not arrive. The exact comparison above removes
            overlaps the way a run does.
          </p>
          <Grid
            columns={[
              { key: "report", title: "Data" },
              { key: "uploaded_files", title: "Files" },
              { key: "files_read", title: "Read" },
              { key: "dates_equal", title: "Dates equal" },
              { key: "dates_differ", title: "Differ" },
              { key: "dates_folder_only", title: "Folder only" },
              { key: "dates_upload_only", title: "Upload only" },
            ]}
            rows={data.quick.sources as unknown as Row[]}
            rowKey={(r) => String(r["source_key"])}
          />
        </>
      ) : null}
    </Collapsible>
  );
}
