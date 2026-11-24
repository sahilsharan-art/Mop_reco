import { useState } from "react";
import { useSuspenseQuery, useQueryClient } from "@tanstack/react-query";
import { Upload, Plus, Undo2 } from "lucide-react";
import { toast } from "sonner";
import {
  api,
  params,
  screenQuery,
  type Row,
  type TableData,
  display,
} from "../../lib/api";
import { Button, Modal } from "../ui";
import { DataTable } from "../DataTable";
import { ReviewAction } from "../ReviewAction";
import { useWorkspace } from "../Workspace";
import { BulkUpload } from "./BulkUpload";
export function RefundsPage() {
  const { filter } = useWorkspace();
  const qc = useQueryClient();
  const done = () => qc.invalidateQueries();
  const { data } = useSuspenseQuery(screenQuery(`/refunds?${params(filter)}`));
  const table = data as TableData & { history: Row[] };
  const [tab, setTab] = useState("refunds");
  const [bulk, setBulk] = useState<"refunds" | "refund_folder" | null>(null);
  const [create, setCreate] = useState(false);
  const [reverse, setReverse] = useState<Row | null>(null);
  return (
    <>
      <div className="section-heading">
        <div>
          <h2>Customer refunds</h2>
          <p>Refunds remain distinct from revenue and ordinary bill matches.</p>
        </div>
        <div className="toolbar-actions">
          <Button variant="secondary" onClick={() => setBulk("refund_folder")}>
            <Upload size={15} />
            Customer workbook
          </Button>
          <Button variant="secondary" onClick={() => setBulk("refunds")}>
            <Upload size={15} />
            Refund template
          </Button>
          <Button onClick={() => setCreate(true)}>
            <Plus size={15} />
            Record refund
          </Button>
        </div>
      </div>
      <div className="segmented">
        {["refunds", "audit history"].map((t) => (
          <Button
            key={t}
            variant={tab === t ? "primary" : "secondary"}
            onClick={() => setTab(t)}
          >
            {t}
          </Button>
        ))}
      </div>
      <DataTable
        rows={tab === "refunds" ? table.rows : (table.history ?? [])}
        columns={
          tab === "refunds"
            ? [
                "id",
                "store",
                "business_date",
                "gateway",
                "transaction_id",
                "amount",
                "zoho_no",
                "customer_name",
                "refund_date",
                "status",
                "uploaded_by",
              ]
            : [
                "at",
                "action",
                "by",
                "gateway",
                "transaction_id",
                "store",
                "amount",
                "zoho_no",
                "customer_name",
                "reason",
              ]
        }
        caption={tab}
        actions={
          tab === "refunds"
            ? (r) => (
                <Button
                  variant="icon"
                  aria-label={`Reverse refund ${r["id"]}`}
                  onClick={() => setReverse(r)}
                >
                  <Undo2 size={16} />
                </Button>
              )
            : undefined
        }
      />
      {bulk ? (
        <BulkUpload kind={bulk} close={() => setBulk(null)} done={done} />
      ) : null}
      {create ? (
        <RefundDialog close={() => setCreate(false)} done={done} />
      ) : null}
      {reverse ? (
        <ReviewAction
          title="Reverse refund"
          path={`/refunds/${reverse["id"]}/reverse`}
          details={reverse}
          close={() => setReverse(null)}
          done={done}
        />
      ) : null}
    </>
  );
}
function RefundDialog({
  close,
  done,
}: {
  close: () => void;
  done: () => Promise<unknown>;
}) {
  const [form, setForm] = useState({
    transaction_ref: "",
    customer_name: "",
    zoho_no: "",
    refund_date: "",
    bill_no: "",
    refund_amount: "",
  });
  const [preview, setPreview] = useState<Row | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  async function submit(apply = false) {
    setBusy(true);
    try {
      const result = await api<Row>(`/refunds/${apply ? "apply" : "preview"}`, {
        method: "POST",
        body: JSON.stringify({
          ...form,
          refund_amount: form.refund_amount ? Number(form.refund_amount) : null,
          confirmed,
        }),
      });
      if (apply) {
        await done();
        toast.success("Refund recorded with audit history.");
        close();
      } else setPreview(result);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Refund failed.");
      setPreview(null);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Record customer refund" onClose={close}>
      {Object.keys(form).map((k) => (
        <label key={k}>
          {k.replaceAll("_", " ")}
          {k === "bill_no"
            ? " · optional overpayment bill"
            : k === "refund_amount"
              ? " · only for bill + refund split"
              : ""}
          <input
            type={
              k === "refund_date"
                ? "date"
                : k === "refund_amount"
                  ? "number"
                  : "text"
            }
            step="0.01"
            value={form[k as keyof typeof form]}
            onChange={(e) => {
              setForm({ ...form, [k]: e.target.value });
              setPreview(null);
              setConfirmed(false);
            }}
          />
        </label>
      ))}
      {preview ? (
        <>
          <dl className="record-details">
            {Object.entries((preview["transaction"] as Row) ?? {}).map(
              ([k, v]) => (
                <div key={k}>
                  <dt>{k}</dt>
                  <dd>{display(v, k)}</dd>
                </div>
              ),
            )}
          </dl>
          <label className="check-label">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            I confirm this refund and any bill / overpayment split.
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
          disabled={
            !form.transaction_ref ||
            !form.customer_name ||
            !form.zoho_no ||
            !form.refund_date
          }
          onClick={() => submit()}
        >
          Validate refund
        </Button>
        <Button
          busy={busy}
          disabled={!preview || !confirmed}
          onClick={() => submit(true)}
        >
          Confirm refund
        </Button>
      </div>
    </Modal>
  );
}
