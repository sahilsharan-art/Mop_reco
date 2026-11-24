import { useState } from "react";
import { toast } from "sonner";
import { api, display, type Row } from "../lib/api";
import { Button, Modal } from "./ui";
import { label } from "./table-utils";
export function ReviewAction({
  title,
  path,
  details,
  payload = {},
  close,
  done,
}: {
  title: string;
  path: string;
  details: Row;
  payload?: Row;
  close: () => void;
  done: () => Promise<unknown>;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit() {
    setBusy(true);
    try {
      await api(path, {
        method: "POST",
        body: JSON.stringify({ ...payload, reason, confirmed: true }),
      });
      await done();
      toast.success("Action completed and recorded.");
      close();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={title} onClose={close}>
      <dl className="record-details">
        {Object.entries(details).map(([key, v]) => (
          <div key={key}>
            <dt>{label(key)}</dt>
            <dd>{display(v, key)}</dd>
          </div>
        ))}
      </dl>
      <label>
        Reason
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Cancel
        </Button>
        <Button
          variant="danger"
          busy={busy}
          disabled={!reason.trim()}
          onClick={submit}
        >
          Confirm {title.toLowerCase()}
        </Button>
      </div>
    </Modal>
  );
}
