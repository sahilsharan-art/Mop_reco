import { useState } from "react";
import { useSuspenseQuery, useQueryClient } from "@tanstack/react-query";
import { Upload, Save, History, Undo2 } from "lucide-react";
import { toast } from "sonner";
import {
  api,
  download,
  screenQuery,
  type Row,
  type TableData,
} from "../../lib/api";
import { useWorkspace } from "../Workspace";
import { Button, Modal } from "../ui";
import { DataTable } from "../DataTable";
import { ReviewAction } from "../ReviewAction";
import { MasterModal } from "./MasterModal";
export function MastersPage() {
  const { bootstrap } = useWorkspace();
  const qc = useQueryClient();
  const done = () => qc.invalidateQueries();
  const [name, setName] = useState(bootstrap.masters[0]?.key ?? "store");
  const { data } = useSuspenseQuery(screenQuery(`/masters/${name}`));
  const table = data as TableData;
  const [edit, setEdit] = useState(false);
  const [history, setHistory] = useState(false);
  const [restore, setRestore] = useState<Row | null>(null);
  const [importing, setImporting] = useState(false);
  return (
    <>
      <div className="section-heading">
        <div>
          <h2>Master data</h2>
          <p>
            Current version #{table.version ?? "—"} · original versions and
            effective dates retained.
          </p>
        </div>
        <div className="toolbar-actions">
          <Button
            variant="secondary"
            onClick={() =>
              download("/masters-import/snapshot").catch((e) =>
                toast.error(String(e)),
              )
            }
          >
            Migration snapshot
          </Button>
          <Button variant="secondary" onClick={() => setImporting(true)}>
            <Upload size={15} />
            Import snapshot
          </Button>
          <Button variant="secondary" onClick={() => setHistory(true)}>
            <History size={15} />
            Version history
          </Button>
          <Button disabled={!table.in_database} onClick={() => setEdit(true)}>
            <Save size={15} />
            Edit master
          </Button>
        </div>
      </div>
      <label className="master-select">
        Master
        <select value={name} onChange={(e) => setName(e.target.value)}>
          {bootstrap.masters.map((m) => (
            <option key={m.key} value={m.key}>
              {m.title}
            </option>
          ))}
        </select>
      </label>
      {!table.in_database ? (
        <div className="alert alert-warning">
          Masters are sourced from Excel. Editing and restore require a
          validated migration snapshot.
        </div>
      ) : null}
      <DataTable
        rows={table.rows}
        columns={table.fields ?? []}
        caption="master rows"
      />
      {edit ? (
        <MasterModal
          name={name}
          data={table}
          close={() => setEdit(false)}
          refresh={done}
        />
      ) : null}
      {history ? (
        <Modal title="Master version history" onClose={() => setHistory(false)}>
          <DataTable
            rows={table.history ?? []}
            columns={Object.keys(table.history?.[0] ?? {})}
            caption="versions"
            actions={(r) => (
              <Button
                variant="secondary"
                disabled={!table.in_database || r["id"] === table.version}
                onClick={() => {
                  setHistory(false);
                  setRestore(r);
                }}
              >
                <Undo2 size={15} />
                Restore
              </Button>
            )}
          />
        </Modal>
      ) : null}
      {restore ? (
        <ReviewAction
          title="Restore master version"
          path={`/masters/${name}/restore`}
          payload={{
            version_id: restore["id"],
            expected_version: table.version,
          }}
          details={{
            master: name,
            current_version: table.version,
            restore_version: restore["id"],
            effect:
              "Create a new current version; historical versions remain unchanged.",
          }}
          close={() => setRestore(null)}
          done={done}
        />
      ) : null}
      {importing ? (
        <ImportSnapshot close={() => setImporting(false)} done={done} />
      ) : null}
    </>
  );
}
function ImportSnapshot({
  close,
  done,
}: {
  close: () => void;
  done: () => Promise<unknown>;
}) {
  const [snapshot, setSnapshot] = useState<Row | null>(null);
  const [preview, setPreview] = useState<Row | null>(null);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  async function submit(apply = false) {
    setBusy(true);
    try {
      const result = await api<Row>(
        `/masters-import/${apply ? "apply" : "preview"}`,
        {
          method: "POST",
          body: JSON.stringify({
            snapshot,
            reason,
            confirmed,
            expected_versions: preview?.["current_versions"] ?? {},
          }),
        },
      );
      if (apply) {
        await done();
        toast.success("Master snapshot imported and verified.");
        close();
      } else setPreview(result);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Snapshot failed.");
      setPreview(null);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Import master snapshot" onClose={close}>
      <div className="alert alert-warning">
        The snapshot must contain all six masters with original dates and intact
        fingerprints. Imports save versions progressively, not atomically;
        inspect any reported failure before retrying.
      </div>
      <label>
        Snapshot JSON
        <input
          type="file"
          accept=".json"
          aria-label="Master snapshot JSON"
          onChange={async (e) => {
            setPreview(null);
            setConfirmed(false);
            const file = e.target.files?.[0];
            if (!file) return;
            try {
              setSnapshot(JSON.parse(await file.text()));
            } catch {
              setSnapshot(null);
              toast.error("The selected file is not valid JSON.");
            }
          }}
        />
      </label>
      <label>
        Import reason
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      {preview ? (
        <>
          <DataTable
            rows={preview["rows"] as Row[]}
            columns={["master", "versions", "rows"]}
            caption="snapshot masters"
          />
          <label className="check-label">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            Import these versions, verify readback, and switch master sourcing
            to the database.
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
          disabled={!snapshot || !reason.trim()}
          onClick={() => submit()}
        >
          Validate snapshot
        </Button>
        <Button
          busy={busy}
          disabled={!preview || !confirmed || !reason.trim()}
          onClick={() => submit(true)}
        >
          Confirm import
        </Button>
      </div>
    </Modal>
  );
}
