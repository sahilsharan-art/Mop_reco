import { useState } from "react";
import { toast } from "sonner";
import { api, type Row } from "../../lib/api";
import { useWorkspace } from "../Workspace";
import { Button, Modal } from "../ui";
import { DataTable } from "../DataTable";
export function BulkAccept({
  close,
  done,
}: {
  close: () => void;
  done: () => Promise<unknown>;
}) {
  const { filter } = useWorkspace();
  const [kind, setKind] = useState("exact");
  const [preview, setPreview] = useState<Row | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  async function submit(apply = false) {
    setBusy(true);
    try {
      const result = await api<Row>(
        `/bulk-accept/${apply ? "apply" : "preview"}`,
        {
          method: "POST",
          body: JSON.stringify({
            run_id: Number(filter.run_id),
            kind,
            confirmed: confirm,
            fingerprint: preview?.["fingerprint"],
          }),
        },
      );
      setPreview(result);
      if (apply) {
        await done();
        toast.success(
          `${result["applied"]} applied; ${((result["refused"] as unknown[]) ?? []).length} refused.`,
        );
        setConfirm(false);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Bulk acceptance failed.");
      setPreview(null);
    } finally {
      setBusy(false);
    }
  }
  const rows = (preview?.["rows"] ?? preview?.["refused"] ?? []) as Row[];
  return (
    <Modal title="Review bulk match candidates" onClose={close}>
      <label>
        Candidate type
        <select
          value={kind}
          onChange={(e) => {
            setKind(e.target.value);
            setPreview(null);
            setConfirm(false);
          }}
        >
          <option value="exact">Exact single-transaction matches</option>
          <option value="split">Two-transaction split matches</option>
        </select>
      </label>
      <p className="muted">
        Candidates use the whole open population, not the filtered page.
        Ambiguous or conflicting matches stay open.
      </p>
      {preview ? (
        <>
          <DataTable
            rows={rows}
            columns={Object.keys(rows[0] ?? {})}
            caption="candidate rows"
          />
          <label className="check-label">
            <input
              type="checkbox"
              checked={confirm}
              onChange={(e) => setConfirm(e.target.checked)}
            />
            Confirm these eligible matches. The engine validates every row
            again.
          </label>
        </>
      ) : null}
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Close
        </Button>
        <Button variant="secondary" busy={busy} onClick={() => submit()}>
          Find candidates
        </Button>
        <Button
          busy={busy}
          disabled={!confirm || !rows.length || Boolean(preview?.["applied"])}
          onClick={() => submit(true)}
        >
          Confirm batch
        </Button>
      </div>
    </Modal>
  );
}
