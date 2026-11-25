import { useRef, useState } from "react";
import { toast } from "sonner";
import { api, type Row } from "../../lib/api";
import { useWorkspace } from "../Workspace";
import { Button, Modal, Pill } from "../ui";
import { Grid } from "./shared";

type Result = {
  kind: string;
  file: string;
  rows_in_file: number;
  accepted: number;
  rejected: number;
  ignored: number;
  summary: string;
  actions: { match: number; refund: number; remark: number };
  can_apply: boolean;
  applied: boolean;
  counts?: { matched: number; refunded: number; remarks: number; batch_id: number };
  rows: Row[];
};

const n0 = (v: number) => v.toLocaleString("en-IN");

/** Upload the table's own export, filled in. Checked first (nothing is written); applied only on confirmation, all or nothing. */
export function TableUpload({ kind, close, done }: { kind: "bills" | "transactions"; close: () => void; done: () => Promise<unknown> }) {
  const { filter, bootstrap } = useWorkspace();
  const runId = Number(filter.run_id || bootstrap.run_id || 0);
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function send(apply: boolean) {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const body = new FormData();
      body.set("file", file);
      body.set("run_id", String(runId));
      body.set("confirm", apply ? "true" : "false");
      if (apply && result) body.set("expect_accepted", String(result.accepted));
      const out = await api<Result>(`/exceptions/${kind}/upload`, { method: "POST", body });
      setResult(out);
      if (apply) {
        const c = out.counts;
        toast.success(`Applied: ${c?.matched ?? 0} matched, ${c?.refunded ?? 0} refunded, ${c?.remarks ?? 0} remarks (batch ${c?.batch_id}).`);
        setConfirm(false);
        await done();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "The file could not be checked.");
      if (!apply) setResult(null);
    } finally {
      setBusy(false);
    }
  }

  const matches = result?.actions.match ?? 0;
  return (
    <Modal title={kind === "bills" ? "Upload the filled bills table" : "Upload the filled transactions table"} onClose={close}>
      <p className="muted">
        {kind === "bills"
          ? "Download the Exception bills table, type the transaction id in Txn 1 (or a remark), and upload that same file. "
          : "Download the Exception transactions table, type the bill number in matched_bill, or the customer name, Zoho number and refund date for a refund (not both), and upload that same file. "}
        Every row is checked exactly as a typed match is. Nothing is written until you confirm, and one rejected row applies nothing.
      </p>
      <div className="upload-pick">
        <input
          ref={input}
          type="file"
          accept=".xlsx,.csv"
          aria-label="Filled table file"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setResult(null);
            setConfirm(false);
            setError("");
          }}
        />
        <Button busy={busy && !result?.can_apply} disabled={!file} onClick={() => send(false)}>
          Check the file
        </Button>
      </div>
      {error ? <p className="alert alert-danger">{error}</p> : null}
      {result ? (
        <>
          <div className="pair-summary">
            <Pill tone="success">Acceptable: {n0(result.accepted)}</Pill>
            <Pill tone={result.rejected ? "danger" : "neutral"}>Rejected: {n0(result.rejected)}</Pill>
            <Pill>Skipped, unchanged: {n0(result.ignored)}</Pill>
          </div>
          <p className="muted">{result.summary}</p>
          {result.accepted > 0 ? (
            <p>
              Applying will record {n0(result.actions.match)} match{result.actions.match === 1 ? "" : "es"}
              {kind === "transactions" ? `, ${n0(result.actions.refund)} refund${result.actions.refund === 1 ? "" : "s"}` : ""} and {n0(result.actions.remark)} remark
              {result.actions.remark === 1 ? "" : "s"}.
            </p>
          ) : null}
          {kind === "bills" && matches > 100 ? (
            <p className="alert alert-warning">
              {n0(matches)} matches are in this file. An export carries the system's own suggestions in Txn 1, so uploading it untouched accepts every one of them.
              Confirm only if you reviewed them.
            </p>
          ) : null}
          {result.rows.length ? (
            <Grid
              columns={[{ key: "row" }, { key: "action" }, { key: "id", title: "Id" }, { key: "against" }, { key: "verdict" }, { key: "why", title: "Reason" }]}
              rows={result.rows}
              rowKey={(r, i) => `${r["row"]}-${i}`}
              empty="Nothing in this file acts on anything."
            />
          ) : null}
          {result.applied ? (
            <p className="alert alert-success">Applied in one step as batch {result.counts?.batch_id}. The tables above have been refreshed.</p>
          ) : result.can_apply ? (
            <label className="check-label">
              <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
              Apply these {n0(result.accepted)} rows. The file is checked again as it is applied.
            </label>
          ) : result.rejected ? (
            <p className="alert alert-danger">Nothing can be applied while any row is rejected. Correct the file and check it again.</p>
          ) : null}
        </>
      ) : null}
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Close
        </Button>
        <Button busy={busy && Boolean(result?.can_apply)} disabled={!result?.can_apply || !confirm || result.applied} onClick={() => send(true)}>
          {result?.can_apply ? `Apply ${n0(result.accepted)} row${result.accepted === 1 ? "" : "s"}` : "Apply"}
        </Button>
      </div>
    </Modal>
  );
}
