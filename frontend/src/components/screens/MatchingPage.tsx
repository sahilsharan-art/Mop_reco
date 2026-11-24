import { useState } from "react";
import { useSuspenseQuery, useQueryClient } from "@tanstack/react-query";
import {
  Upload,
  Link2,
  Undo2,
  History,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  Search,
} from "lucide-react";
import { toast } from "sonner";
import {
  api,
  params,
  screenQuery,
  type Row,
  type TableData,
  display,
} from "../../lib/api";
import { useWorkspace } from "../Workspace";
import { Button, Modal, Pill } from "../ui";
import { DataTable } from "../DataTable";
import { ReviewAction } from "../ReviewAction";
import { BulkUpload } from "./BulkUpload";
import { BulkAccept } from "./BulkAccept";
export function MatchingPage() {
  const { filter } = useWorkspace();
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries();
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [tab, setTab] = useState("bills");
  const { data } = useSuspenseQuery(
    screenQuery(
      `/matching?${params(filter)}&register=${tab === "bills" ? "bills" : "manual"}&search=${encodeURIComponent(search)}&offset=${offset}`,
    ),
  );
  const table = data as TableData & { matches: Row[] };
  const [selected, setSelected] = useState<Row | null>(null);
  const [reverse, setReverse] = useState<Row | null>(null);
  const [bulk, setBulk] = useState(false);
  const [accept, setAccept] = useState(false);
  const [history, setHistory] = useState<Row[] | null>(null);
  return (
    <>
      <div className="section-heading">
        <div>
          <h2>Bill ↔ transaction matching</h2>
          <p>
            System matches, manual matches and unverified assertions remain
            separate.
          </p>
        </div>
        <div className="toolbar-actions">
          <Button variant="secondary" onClick={() => setAccept(true)}>
            <CheckCheck size={16} />
            Review bulk candidates
          </Button>
          <Button onClick={() => setBulk(true)}>
            <Upload size={16} />
            Upload matches
          </Button>
        </div>
      </div>
      <div className="segmented" role="tablist" aria-label="Matching registers">
        {["bills", "manual register"].map((t) => (
          <Button
            role="tab"
            aria-selected={tab === t}
            key={t}
            variant={tab === t ? "primary" : "secondary"}
            onClick={() => {
              setTab(t);
              setOffset(0);
            }}
          >
            {t}
          </Button>
        ))}
      </div>
      <div className="table-toolbar">
        <label className="search-field">
          <Search size={16} />
          <input
            aria-label="Search all matching records"
            placeholder="Search bill or store across all records"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setOffset(0);
            }}
          />
        </label>
        <span>{table.total ?? 0} records</span>
      </div>
      {tab === "bills" ? (
        <DataTable
          searchable={false}
          rows={table.rows}
          columns={[
            "bill_no",
            "store",
            "business_date",
            "mop",
            "amount",
            "match_source",
            "effective_matched",
            "matched_txn_ref",
          ]}
          caption="bills"
          actions={(r) => (
            <Button
              variant="secondary"
              disabled={
                Boolean(r["effective_matched"]) ||
                r["manual_status"] === "asserted_unverified"
              }
              onClick={() => setSelected(r)}
            >
              <Link2 size={14} />
              Match
            </Button>
          )}
        />
      ) : (
        <DataTable
          searchable={false}
          rows={table.matches ?? []}
          columns={[
            "id",
            "bill_no",
            "store",
            "business_date",
            "amount",
            "gateway",
            "transaction_ref",
            "status",
            "lifecycle",
            "operator",
            "batch_id",
          ]}
          caption="manual matches"
          actions={(r) => (
            <div className="row-actions">
              <Button
                variant="icon"
                aria-label={`History match ${r["id"]}`}
                onClick={() =>
                  api<Row[]>(`/matching/${r["id"]}/history`)
                    .then(setHistory)
                    .catch((e) => toast.error(String(e)))
                }
              >
                <History size={16} />
              </Button>
              <Button
                variant="icon"
                aria-label={`Reverse match ${r["id"]}`}
                disabled={r["lifecycle"] !== "active"}
                onClick={() => setReverse(r)}
              >
                <Undo2 size={16} />
              </Button>
            </div>
          )}
        />
      )}{" "}
      <div className="pagination">
        <span>
          {(table.total ?? 0) > 0 ? offset + 1 : 0}–
          {Math.min(offset + 50, table.total ?? 0)} of {table.total ?? 0}
        </span>
        <Button
          variant="icon"
          aria-label="Previous matching page"
          disabled={!offset}
          onClick={() => setOffset(Math.max(0, offset - 50))}
        >
          <ChevronLeft size={16} />
        </Button>
        <Button
          variant="icon"
          aria-label="Next matching page"
          disabled={offset + 50 >= (table.total ?? 0)}
          onClick={() => setOffset(offset + 50)}
        >
          <ChevronRight size={16} />
        </Button>
      </div>
      {selected ? (
        <MatchDialog
          row={selected}
          close={() => setSelected(null)}
          done={refresh}
        />
      ) : null}
      {reverse ? (
        <ReviewAction
          title="Reverse match"
          path={`/matching/${reverse["id"]}/reverse`}
          details={reverse}
          close={() => setReverse(null)}
          done={refresh}
        />
      ) : null}
      {history ? (
        <Modal title="Match audit history" onClose={() => setHistory(null)}>
          <DataTable
            rows={history}
            columns={["event", "at", "operator", "detail"]}
            caption="events"
          />
        </Modal>
      ) : null}
      {bulk ? (
        <BulkUpload
          kind="matching"
          close={() => setBulk(false)}
          done={refresh}
        />
      ) : null}
      {accept ? (
        <BulkAccept close={() => setAccept(false)} done={refresh} />
      ) : null}
    </>
  );
}
function MatchDialog({
  row,
  close,
  done,
}: {
  row: Row;
  close: () => void;
  done: () => Promise<unknown>;
}) {
  const { filter } = useWorkspace();
  const [mode, setMode] = useState("single");
  const [ref, setRef] = useState("");
  const [second, setSecond] = useState("");
  const [shared, setShared] = useState("");
  const [reason, setReason] = useState("");
  const [override, setOverride] = useState("");
  const [preview, setPreview] = useState<Row | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const input = {
    run_id: Number(filter.run_id),
    bill_no: row["bill_no"],
    transaction_ref: ref,
    second_transaction_ref: mode === "split" ? second : "",
    shared_bill_nos:
      mode === "shared"
        ? shared
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean)
        : [],
    reason,
    reason_code: "operator_review",
    override_reason: override,
    confirmed: confirm,
  };
  async function submit(apply = false) {
    setBusy(true);
    try {
      const result = await api<Row>(
        `/matching/${apply ? "apply" : "preview"}`,
        { method: "POST", body: JSON.stringify(input) },
      );
      if (apply) {
        await done();
        toast.success(
          `Recorded ${display(result["status"] ?? "shared match")}.`,
        );
        close();
      } else {
        setPreview(result);
        setConfirm(false);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Matching failed.");
      setPreview(null);
    } finally {
      setBusy(false);
    }
  }
  const validation = preview?.["validation"] as Row | undefined;
  const unavailable = validation?.["case"] === "c";
  const discrepant = validation?.["case"] === "b";
  function change() {
    setPreview(null);
    setConfirm(false);
  }
  return (
    <Modal title={`Match bill ${row["bill_no"]}`} onClose={close}>
      <dl className="record-details">
        {["store", "business_date", "mop", "amount"].map((k) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{display(row[k], k)}</dd>
          </div>
        ))}
      </dl>
      <label>
        Payment structure
        <select
          value={mode}
          onChange={(e) => {
            setMode(e.target.value);
            change();
          }}
        >
          <option value="single">One transaction</option>
          <option value="split">Split · two transactions</option>
          <option value="shared">Shared · one transaction across bills</option>
        </select>
      </label>
      <label>
        Transaction reference
        <input
          value={ref}
          onChange={(e) => {
            setRef(e.target.value);
            change();
          }}
        />
      </label>
      {mode === "split" ? (
        <label>
          Second transaction reference
          <input
            value={second}
            onChange={(e) => {
              setSecond(e.target.value);
              change();
            }}
          />
        </label>
      ) : null}
      {mode === "shared" ? (
        <label>
          Other bill numbers · comma separated
          <input
            value={shared}
            onChange={(e) => {
              setShared(e.target.value);
              change();
            }}
          />
        </label>
      ) : null}
      <label>
        Reason / evidence
        <textarea
          value={reason}
          onChange={(e) => {
            setReason(e.target.value);
            change();
          }}
        />
      </label>
      {preview ? (
        <>
          <div
            className={`alert alert-${validation?.["ok"] ? "success" : "warning"}`}
          >
            <Pill tone={validation?.["ok"] ? "success" : "warning"}>
              {unavailable
                ? "Unverified assertion"
                : discrepant
                  ? "Discrepancy needs explanation"
                  : "Validated match"}
            </Pill>
            {((validation?.["problems"] as string[]) ?? []).map((p) => (
              <p key={p}>{p}</p>
            ))}
            {validation?.["date_lag_note"] ? (
              <p>{String(validation["date_lag_note"])}</p>
            ) : null}
          </div>
          {discrepant ? (
            <label>
              Written discrepancy explanation
              <textarea
                value={override}
                onChange={(e) => setOverride(e.target.value)}
              />
            </label>
          ) : null}
          <label className="check-label">
            <input
              type="checkbox"
              checked={confirm}
              onChange={(e) => setConfirm(e.target.checked)}
            />
            {unavailable
              ? "Save as asserted / unverified, not as matched."
              : "I confirm the displayed match and any explained discrepancy."}
          </label>
        </>
      ) : null}
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Cancel
        </Button>
        <Button
          variant="secondary"
          busy={busy}
          disabled={!ref || !reason.trim()}
          onClick={() => submit()}
        >
          Validate
        </Button>
        <Button
          busy={busy}
          disabled={!preview || !confirm || (discrepant && !override.trim())}
          onClick={() => submit(true)}
        >
          {unavailable ? "Record unverified assertion" : "Confirm match"}
        </Button>
      </div>
    </Modal>
  );
}
