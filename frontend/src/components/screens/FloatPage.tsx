import { useDeferredValue, useEffect, useState } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { AlertTriangle, Download, UploadCloud, Pencil } from "lucide-react";
import { toast } from "sonner";
import { api, apiQuery, download, display, money, params, type Row } from "../../lib/api";
import { useWorkspace } from "../Workspace";
import { Button, Modal, Pill } from "../ui";
import { Collapsible, Grid, Pager, type Col } from "./shared";

type Tiles = {
  as_of?: string;
  stores_above?: number;
  float_above?: number;
  high_no_remark?: number;
  longest?: { days: number; store: string; through: string } | null;
};
type FloatResp = {
  rows: Row[];
  total: number;
  tiles: Tiles;
  quality: { negative_n: number; negative_value: number; defect_b_n: number; defect_b_value: number };
};
type Quality = { negative: Row[]; defect_b: Row[] };

const VIEWS: [string, string][] = [
  ["unremarked", "Unremarked only (work queue)"],
  ["high", "High float only"],
  ["weekend", "Weekend-explained only"],
  ["all", "All"],
];
const n0 = (v: unknown) => Number(v ?? 0).toLocaleString("en-IN");
const when = (v: unknown) => (typeof v === "string" && v ? display(v) : "—");

/** Float = opening balance less the cash deposited that day (CLAUDE.md 6.15). NOT the closing balance. Read from the store-day sheet's own
 * float column; this screen adds the threshold, the high flag, the streak, the negative-float check and the weekend explanation. */
export function FloatPage() {
  const { filter } = useWorkspace();
  const qc = useQueryClient();
  const [view, setView] = useState("unremarked");
  const [offset, setOffset] = useState(0);
  const [picked, setPicked] = useState<Row | null>(null);
  const [upload, setUpload] = useState(false);
  const [negOpen, setNegOpen] = useState(false);
  const [dqOpen, setDqOpen] = useState(false);
  const filterKey = JSON.stringify(filter);
  useEffect(() => setOffset(0), [filterKey, view]);
  const path = `/float?${params(filter, { view, offset, limit: 50 })}`;
  const deferred = useDeferredValue(path);
  const { data, isPending, error } = useQuery({ ...apiQuery<FloatResp>(deferred), placeholderData: keepPreviousData });
  const quality = useQuery(apiQuery<Quality>(`/float/data-quality?${params(filter)}`, negOpen || dqOpen));
  const refresh = () => qc.invalidateQueries({ queryKey: ["screen"] });
  if (error) throw error;
  if (isPending || !data) return <div className="empty-state"><p>Building the float register…</p></div>;
  const t = data.tiles ?? {};
  const q = data.quality;
  const asOf = t.as_of ? display(t.as_of) : "";
  const columns: Col[] = [
    { key: "store" },
    { key: "business_date", title: "Business date" },
    { key: "day_name", title: "Day" },
    { key: "opening_balance", title: "Opening" },
    { key: "cash_sale", title: "Cash sale" },
    { key: "cash_deposit", title: "Deposit" },
    {
      key: "float_amount",
      title: "Float",
      cell: (r) => (
        <>
          {r["negative_float"] ? "⚠ " : ""}
          {money(r["float_amount"])}
        </>
      ),
    },
    { key: "float_threshold", title: "Float threshold" },
    { key: "high_float", title: "High float?", cell: (r) => (r["high_float"] ? <Pill tone="danger">Yes</Pill> : "") },
    { key: "days_held_high", title: "Days held high", numeric: true },
    { key: "cash_received", title: "Cash received" },
    { key: "closing_balance", title: "Closing balance" },
    {
      key: "remark",
      cell: (r) => (r["remark"] ? <span title={r["is_auto"] ? "Explained automatically by the system" : undefined}>{String(r["remark"])}{r["is_auto"] ? " (auto)" : ""}</span> : "—"),
    },
    { key: "remark_by", title: "Remark by" },
    { key: "remark_at", title: "Remark at", cell: (r) => when(r["remark_at"]) },
    {
      key: "_open",
      title: "",
      cell: (r) => (
        <Button variant="icon" title="Remark this store-day" aria-label={`Remark ${r["store"]} ${r["business_date"]}`} onClick={() => setPicked(r)}>
          <Pencil size={15} />
        </Button>
      ),
    },
  ];
  return (
    <div>
      <div className="section-heading">
        <div>
          <h2>Float register</h2>
          <p>
            Float is the opening cash balance less the cash deposited that day — what the store held back instead of banking. It is not the closing
            balance.
          </p>
        </div>
        <div className="toolbar-actions">
          <Button variant="secondary" onClick={() => download(`/float/export?${params(filter, { view })}`).catch((e) => toast.error(String(e.message ?? e)))}>
            <Download size={15} />
            Export this view
          </Button>
          <Button variant="secondary" onClick={() => setUpload(true)}>
            <UploadCloud size={15} />
            Bulk upload remarks
          </Button>
        </div>
      </div>
      <div className="metric-strip">
        <div className={`metric ${t.stores_above ? "metric-warning" : "metric-success"}`}>
          <span>Stores above threshold</span>
          <strong>{n0(t.stores_above)}</strong>
          <small>{asOf ? `as of ${asOf}` : "—"}</small>
        </div>
        <div className={`metric ${t.float_above ? "metric-warning" : "metric-success"}`}>
          <span>Total float above threshold</span>
          <strong>{money(t.float_above ?? 0)}</strong>
          <small>{asOf ? `as of ${asOf}` : "—"}</small>
        </div>
        <div className={`metric ${t.high_no_remark ? "metric-warning" : "metric-success"}`}>
          <span>High float, no remark</span>
          <strong>{n0(t.high_no_remark)}</strong>
          <small>the work queue — store-days with no explanation, auto or manual</small>
        </div>
        <div className="metric metric-warning">
          <span>Longest high-float run</span>
          <strong>{t.longest ? `${n0(t.longest.days)} day(s)` : "0 days"}</strong>
          <small>{t.longest ? `${t.longest.store}, to ${display(t.longest.through)}` : "none"}</small>
        </div>
      </div>

      <h3 className="muted">Data quality</h3>
      {q.negative_n ? (
        <div className="alert alert-warning">
          <AlertTriangle size={15} /> <b>{n0(q.negative_n)} store-day(s) show a deposit larger than the opening balance</b> (catch-up or misdated deposit) —
          combined float {money(q.negative_value)}.
        </div>
      ) : null}
      {q.defect_b_n ? (
        <div className="alert alert-warning">
          <AlertTriangle size={15} /> <b>{n0(q.defect_b_n)} store-day(s) show credits exceeding deposits</b> (Defect B) — combined {money(q.defect_b_value)}.
        </div>
      ) : null}
      {!q.negative_n && !q.defect_b_n ? <p className="muted">No data-quality issues in this range.</p> : null}
      {q.negative_n ? (
        <Collapsible title="Negative float" meta={`${n0(q.negative_n)} store-day(s)`} open={negOpen} setOpen={setNegOpen}>
          {quality.data ? (
            <Grid
              columns={[
                { key: "store" },
                { key: "business_date", title: "Business date" },
                { key: "opening_balance", title: "Opening" },
                { key: "cash_deposit", title: "Deposit" },
                { key: "float_amount", title: "Float" },
              ]}
              rows={quality.data.negative}
              rowKey={(r, i) => `${r["store"]}-${r["business_date"]}-${i}`}
            />
          ) : (
            <p className="muted">Reading…</p>
          )}
        </Collapsible>
      ) : null}
      {q.defect_b_n ? (
        <Collapsible title="Credits exceeding deposits (Defect B)" meta={`${n0(q.defect_b_n)} store-day(s)`} open={dqOpen} setOpen={setDqOpen}>
          <p className="muted">
            These rows indicate a credit posted against the wrong date or a shared pickup code, not money owed to the agency. Open item: Defect B (CMS
            date attribution).
          </p>
          {quality.data ? (
            <Grid
              columns={[
                { key: "store" },
                { key: "business_date", title: "Date" },
                { key: "I", title: "I (deposit)", cell: (r) => money(r["I"]) },
                { key: "J", title: "J (credit)", cell: (r) => money(r["J"]) },
                { key: "J_minus_I", title: "J − I", cell: (r) => money(r["J_minus_I"]) },
                { key: "pickup_code", title: "Pickup code" },
                { key: "slip_no", title: "Slip no" },
              ]}
              rows={quality.data.defect_b}
              rowKey={(r, i) => `${r["store"]}-${r["business_date"]}-${i}`}
            />
          ) : (
            <p className="muted">Reading…</p>
          )}
        </Collapsible>
      ) : null}

      <div className="table-toolbar">
        <select aria-label="Show" value={view} onChange={(e) => setView(e.target.value)}>
          {VIEWS.map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
        <span className="toolbar-count">{n0(data.total)} row(s) in this view</span>
      </div>
      <Grid columns={columns} rows={data.rows} rowKey={(r, i) => `${r["store"]}-${r["business_date"]}-${i}`} tone={(r) => (r["negative_float"] ? "danger" : r["high_float"] ? "warning" : undefined)} empty="No store-day in this view." />
      <Pager offset={offset} total={data.total} setOffset={setOffset} />
      {picked ? <RemarkModal row={picked} close={() => setPicked(null)} done={refresh} /> : null}
      {upload ? <UploadModal close={() => setUpload(false)} done={refresh} /> : null}
    </div>
  );
}

function RemarkModal({ row, close, done }: { row: Row; close: () => void; done: () => Promise<unknown> }) {
  const [text, setText] = useState(row["is_auto"] ? "" : String(row["remark"] ?? ""));
  const [busy, setBusy] = useState(false);
  const history = useQuery(apiQuery<Row[]>(`/float/history?store=${encodeURIComponent(String(row["store"]))}&business_date=${row["business_date"]}`));
  async function save() {
    setBusy(true);
    try {
      await api("/float/remark", { method: "POST", body: JSON.stringify({ store: row["store"], business_date: row["business_date"], text }) });
      toast.success("Remark saved.");
      await done();
      close();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not save the remark.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={`${row["store"]} · ${display(row["business_date"])} (${row["day_name"]})`} onClose={close}>
      <p>
        Float {money(row["float_amount"])} against a threshold of {money(row["float_threshold"])}; closing balance {money(row["closing_balance"])}.
      </p>
      {row["is_auto"] ? (
        <p className="muted">
          Explained automatically as <b>{String(row["remark"])}</b>. You can replace it with your own text; once you do it becomes yours and the system never
          overwrites it on a later run.
        </p>
      ) : row["remark"] ? (
        <p className="muted">
          Remarked by <b>{String(row["remark_by"])}</b> {when(row["remark_at"])}.
        </p>
      ) : (
        <p className="muted">No remark on record yet.</p>
      )}
      <label>
        Remark
        <textarea value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      {history.data && history.data.length ? (
        <details>
          <summary>Remark history — {history.data.length} entr{history.data.length === 1 ? "y" : "ies"}</summary>
          <div className="history">
            {history.data.map((h, i) => (
              <p key={i}>
                {when(h["remark_at"])} · {String(h["remark_by"])} · {String(h["remark"])}
              </p>
            ))}
          </div>
        </details>
      ) : null}
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Cancel
        </Button>
        <Button busy={busy} disabled={!text.trim()} onClick={save}>
          Save remark
        </Button>
      </div>
    </Modal>
  );
}

type UploadPreview = { rows: Row[]; accepted: number; rejected: number; applied: number; failed: Row[] };

function UploadModal({ close, done }: { close: () => void; done: () => Promise<unknown> }) {
  const { filter } = useWorkspace();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<UploadPreview | null>(null);
  const [busy, setBusy] = useState(false);
  async function send(confirm: boolean) {
    if (!file) return;
    setBusy(true);
    try {
      const body = new FormData();
      body.append("file", file);
      const result = await api<UploadPreview>(`/float/upload?${params(filter, { confirm: String(confirm) })}`, { method: "POST", body });
      setPreview(result);
      if (confirm) {
        toast.success(`${result.applied} remark(s) applied${result.failed.length ? `, ${result.failed.length} failed` : ""}.`);
        await done();
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "The upload failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Bulk upload float remarks" onClose={close}>
      <p className="muted">
        Explain many high-float store-days at once. Every row is checked first and nothing is applied until you confirm. A row is rejected when its store
        or date is outside the loaded range, or the store-date is not flagged high float.
      </p>
      <div className="toolbar-actions">
        <Button variant="secondary" onClick={() => download("/float/template").catch((e) => toast.error(String(e.message ?? e)))}>
          <Download size={15} />
          Download the template
        </Button>
      </div>
      <label>
        The completed template (.xlsx or .csv)
        <input
          type="file"
          accept=".xlsx,.csv"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setPreview(null);
          }}
        />
      </label>
      {preview ? (
        <>
          <p>
            <b>{preview.accepted}</b> would be accepted · <b>{preview.rejected}</b> rejected{preview.applied ? ` · ${preview.applied} applied` : ""}
          </p>
          <Grid columns={[{ key: "row" }, { key: "store" }, { key: "business_date", title: "Business date" }, { key: "remark" }, { key: "verdict" }, { key: "why", title: "Why" }]} rows={preview.rows} rowKey={(r, i) => `${r["row"]}-${i}`} tone={(r) => (r["verdict"] === "reject" ? "danger" : undefined)} />
          {preview.failed.map((f, i) => (
            <p key={i} className="alert alert-danger">
              Row {String(f["row"])} ({String(f["store"])}/{String(f["business_date"])}): {String(f["why"])}
            </p>
          ))}
        </>
      ) : null}
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Close
        </Button>
        <Button variant="secondary" busy={busy} disabled={!file} onClick={() => send(false)}>
          Check the file
        </Button>
        <Button busy={busy} disabled={!file || !preview || !preview.accepted || preview.applied > 0} onClick={() => send(true)}>
          Apply {preview?.accepted ?? 0} accepted row(s)
        </Button>
      </div>
    </Modal>
  );
}
