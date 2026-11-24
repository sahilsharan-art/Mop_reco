import { useState } from "react";
import { useSuspenseQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Undo2, History } from "lucide-react";
import { toast } from "sonner";
import {
  api,
  params,
  screenQuery,
  type Row,
  type TableData,
} from "../../lib/api";
import { Button, Modal } from "../ui";
import { DataTable } from "../DataTable";
import { ReviewAction } from "../ReviewAction";
import { useWorkspace } from "../Workspace";
export function CmsPage() {
  const { filter } = useWorkspace();
  const qc = useQueryClient();
  const done = () => qc.invalidateQueries();
  const { data } = useSuspenseQuery(screenQuery(`/cms?${params(filter)}`));
  const table = data as TableData & { overrides: Row[]; controls: Row[] };
  const [tab, setTab] = useState("deposit chain");
  const [create, setCreate] = useState(false);
  const [reverse, setReverse] = useState<Row | null>(null);
  const rows =
    tab === "deposit chain"
      ? table.rows
      : tab === "override history"
        ? table.overrides
        : table.controls;
  return (
    <>
      <div className="section-heading">
        <div>
          <h2>CMS deposit chain</h2>
          <p>
            Computed bank figures are retained beside overrides. Carry-forward
            is rebuilt across the full chain.
          </p>
        </div>
        <Button onClick={() => setCreate(true)}>
          <Plus size={16} />
          Enter override
        </Button>
      </div>
      <div className="segmented">
        {["deposit chain", "override history", "controls", "remarks"].map(
          (t) => (
            <Button
              key={t}
              variant={tab === t ? "primary" : "secondary"}
              onClick={() => setTab(t)}
            >
              {t}
            </Button>
          ),
        )}
      </div>
      {tab === "remarks" ? (
        <CmsRemarks />
      ) : (
        <DataTable
          rows={rows ?? []}
          columns={Object.keys(rows?.[0] ?? {})}
          caption={tab}
          actions={
            tab === "override history"
              ? (r) => (
                  <Button
                    variant="icon"
                    disabled={r["status"] !== "active"}
                    aria-label={`Revert override ${r["id"]}`}
                    onClick={() => setReverse(r)}
                  >
                    <Undo2 size={16} />
                  </Button>
                )
              : undefined
          }
        />
      )}
      {create ? <CmsDialog close={() => setCreate(false)} done={done} /> : null}
      {reverse ? (
        <ReviewAction
          title="Revert CMS override"
          path={`/cms/overrides/${reverse["id"]}/revert`}
          details={{
            ...reverse,
            result: "Restore computed bank figure; retain override history.",
          }}
          close={() => setReverse(null)}
          done={done}
        />
      ) : null}
    </>
  );
}
function CmsDialog({
  close,
  done,
}: {
  close: () => void;
  done: () => Promise<unknown>;
}) {
  const { filter, bootstrap } = useWorkspace();
  const [form, setForm] = useState({
    store: filter.store,
    deposit_date: filter.date_from,
    column: "same_day",
    amount: "",
    reason: "",
  });
  const [preview, setPreview] = useState<Row | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const input = {
    run_id: Number(filter.run_id),
    date_from: filter.date_from,
    date_to: filter.date_to,
    rows: [{ ...form, amount: form.amount ? Number(form.amount) : null }],
    confirmed,
  };
  function update(k: string, v: string) {
    setForm({ ...form, [k]: v });
    setPreview(null);
    setConfirmed(false);
  }
  async function submit(apply = false) {
    setBusy(true);
    try {
      const result = await api<Row>(
        `/cms/overrides/${apply ? "apply" : "preview"}`,
        { method: "POST", body: JSON.stringify(input) },
      );
      if (apply) {
        await done();
        toast.success("Override recorded; computed amount retained.");
        close();
      } else setPreview(result);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Override failed.");
      setPreview(null);
    } finally {
      setBusy(false);
    }
  }
  const rejected = (preview?.["rejected"] ?? []) as Row[];
  const accepted = (preview?.["accepted"] ?? []) as Row[];
  return (
    <Modal title="Review CMS override" onClose={close}>
      <label>
        Store
        <select
          value={form.store}
          onChange={(e) => update("store", e.target.value)}
        >
          <option value="">Select store</option>
          {bootstrap.stores.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
      </label>
      <label>
        Deposit date
        <input
          type="date"
          value={form.deposit_date}
          onChange={(e) => update("deposit_date", e.target.value)}
        />
      </label>
      <label>
        Bank credit column
        <select
          value={form.column}
          onChange={(e) => update("column", e.target.value)}
        >
          <option value="same_day">Same day</option>
          <option value="next_day">Next day</option>
        </select>
      </label>
      <label>
        Override amount
        <input
          type="number"
          step="0.01"
          value={form.amount}
          onChange={(e) => update("amount", e.target.value)}
        />
      </label>
      <label>
        Reason
        <textarea
          value={form.reason}
          onChange={(e) => update("reason", e.target.value)}
        />
      </label>
      {preview ? (
        <>
          <DataTable
            rows={[...accepted, ...rejected]}
            columns={Object.keys(accepted[0] ?? rejected[0] ?? {})}
            caption="override preview"
          />
          {rejected.length ? (
            <div className="alert alert-danger">
              Resolve all rejected rows; no override can be applied.
            </div>
          ) : (
            <label className="check-label">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              Replace the effective value and recompute the full carry-forward
              chain.
            </label>
          )}
        </>
      ) : null}
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Cancel
        </Button>
        <Button
          variant="secondary"
          busy={busy}
          disabled={!form.store || !form.reason.trim() || !form.amount}
          onClick={() => submit()}
        >
          Preview override
        </Button>
        <Button
          busy={busy}
          disabled={!confirmed || !!rejected.length || !accepted.length}
          onClick={() => submit(true)}
        >
          Confirm override
        </Button>
      </div>
    </Modal>
  );
}

function CmsRemarks() {
  const { filter } = useWorkspace();
  const qc = useQueryClient();
  const { data } = useSuspenseQuery(
    screenQuery(`/cms/remarks?${params(filter)}`),
  );
  const [create, setCreate] = useState(false);
  const [history, setHistory] = useState<Row[] | null>(null);
  const rows = (data as TableData).rows;
  return (
    <>
      <div className="section-heading">
        <h2>Period remarks</h2>
        <Button onClick={() => setCreate(true)}>
          <Plus size={16} />
          Add remark
        </Button>
      </div>
      <DataTable
        rows={rows}
        columns={["store", "remark", "remark_by", "remark_at"]}
        caption="CMS remarks"
        actions={(r) => (
          <Button
            variant="icon"
            aria-label={`CMS remark history ${r["store"]}`}
            onClick={() =>
              api<Row[]>(
                `/cms/remarks/history?${params(filter)}&store=${encodeURIComponent(String(r["store"]))}`,
              )
                .then(setHistory)
                .catch((e) => toast.error(String(e)))
            }
          >
            <History size={16} />
          </Button>
        )}
      />
      {create ? (
        <CmsRemarkDialog
          close={() => setCreate(false)}
          done={() => qc.invalidateQueries()}
        />
      ) : null}
      {history ? (
        <Modal title="CMS remark history" onClose={() => setHistory(null)}>
          <DataTable
            rows={history}
            columns={["When", "By", "Remark"]}
            caption="remark history"
          />
        </Modal>
      ) : null}
    </>
  );
}
function CmsRemarkDialog({
  close,
  done,
}: {
  close: () => void;
  done: () => Promise<unknown>;
}) {
  const { filter, bootstrap } = useWorkspace();
  const [store, setStore] = useState(filter.store || bootstrap.stores[0] || "");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      await api(`/cms/remarks?${params(filter)}`, {
        method: "POST",
        body: JSON.stringify({ store, reason }),
      });
      await done();
      toast.success("CMS remark saved with history.");
      close();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "CMS remark failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Add CMS period remark" onClose={close}>
      <label>
        Store
        <select value={store} onChange={(e) => setStore(e.target.value)}>
          {bootstrap.stores.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>
      <p className="muted">
        {filter.date_from} – {filter.date_to}
      </p>
      <label>
        Remark
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Cancel
        </Button>
        <Button busy={busy} disabled={!store || !reason.trim()} onClick={save}>
          Save remark
        </Button>
      </div>
    </Modal>
  );
}
