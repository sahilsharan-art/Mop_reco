import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "../../lib/api";
import { Button, Modal } from "../ui";
export function PasswordDialog({ close }: { close: () => void }) {
  const qc = useQueryClient();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      await api("/auth/password", {
        method: "POST",
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      await qc.invalidateQueries();
      toast.success("Password changed. Other sessions were revoked.");
      close();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Password change failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Change password" onClose={close}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <label>
          Current password
          <input
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            required
          />
        </label>
        <label>
          New password
          <input
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            required
          />
        </label>
        <label>
          Confirm new password
          <input
            type="password"
            autoComplete="new-password"
            value={repeat}
            onChange={(e) => setRepeat(e.target.value)}
            required
          />
        </label>
        {repeat && next !== repeat ? (
          <div className="alert alert-warning">Passwords do not match.</div>
        ) : null}
        <div className="modal-footer">
          <Button variant="secondary" type="button" onClick={close}>
            Cancel
          </Button>
          <Button
            type="submit"
            busy={busy}
            disabled={!current || !next || next !== repeat}
          >
            Change password
          </Button>
        </div>
      </form>
    </Modal>
  );
}
