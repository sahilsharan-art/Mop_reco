import { useState } from "react";
import { UploadCloud, Download } from "lucide-react";
import { toast } from "sonner";
import { api, download, type Row } from "../../lib/api";
import { Button, Modal } from "../ui";
import { DataTable } from "../DataTable";
import { useWorkspace } from "../Workspace";
export function BulkUpload({
  kind,
  close,
  done,
}: {
  kind: "matching" | "refunds" | "replies" | "refund_folder";
  close: () => void;
  done: () => Promise<unknown>;
}) {
  const { filter } = useWorkspace();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const [warnings, setWarnings] = useState(false);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  async function submit(apply = false) {
    if (!file) return;
    setBusy(true);
    try {
      const body = new FormData();
      body.set("file", file);
      body.set("run_id", filter.run_id);
      body.set("confirm", String(apply));
      body.set("include_warnings", String(warnings));
      body.set("override_reason", reason);
      const result = await api<Row>(`/bulk/${kind}`, { method: "POST", body });
      setPreview(result);
      if (result["applied"]) {
        await done();
        toast.success(
          `${result["applied_count"]} applied; ${result["failed_count"] ?? 0} failed. ${result["rejected"] ?? 0} rejected rows were not applied.`,
        );
        setConfirmed(false);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Spreadsheet failed.");
      setPreview(null);
    } finally {
      setBusy(false);
    }
  }
  const rows = (preview?.["rows"] ?? []) as Row[];
  return (
    <Modal
      title={
        kind === "refund_folder" ? "Customer refund workbook" : `Bulk ${kind}`
      }
      onClose={close}
    >
      {kind !== "refund_folder" ? (
        <Button
          variant="secondary"
          onClick={() =>
            download(`/bulk/${kind}/template`).catch((e) =>
              toast.error(String(e)),
            )
          }
        >
          <Download size={15} />
          Download template
        </Button>
      ) : null}
      <label className="upload-drop">
        <UploadCloud size={28} />
        <span>{file?.name ?? "Choose XLSX or CSV"}</span>
        <input
          type="file"
          accept=".xlsx,.csv"
          aria-label="Bulk spreadsheet"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setPreview(null);
            setConfirmed(false);
          }}
        />
      </label>
      {preview ? (
        <>
          <div className="metric-strip bulk-metrics">
            {["accepted", "warned", "rejected"].map((k) => (
              <div
                className={`metric metric-${k === "accepted" ? "success" : "warning"}`}
                key={k}
              >
                <span>{k}</span>
                <strong>{String(preview[k] ?? 0)}</strong>
              </div>
            ))}
          </div>
          <DataTable
            rows={rows}
            columns={Object.keys(rows[0] ?? {})}
            caption="preview rows"
          />
          {kind === "matching" ? (
            <>
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={warnings}
                  onChange={(e) => setWarnings(e.target.checked)}
                />
                Include warnings as explained overrides / unverified assertions
              </label>
              {warnings ? (
                <label>
                  Override explanation
                  <textarea
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </label>
              ) : null}
              <p className="muted">
                Matching uploads may partially succeed; every applied row
                belongs to a reversible batch.
              </p>
            </>
          ) : (
            <p className="muted">
              Only accepted rows are applied. Rejected or held rows stay
              unchanged.
            </p>
          )}
          <label className="check-label">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            I reviewed these rows and confirm applying the eligible entries.
          </label>
        </>
      ) : null}
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Close
        </Button>
        <Button
          variant="secondary"
          busy={busy}
          disabled={!file}
          onClick={() => submit()}
        >
          Validate spreadsheet
        </Button>
        <Button
          busy={busy}
          disabled={
            !preview ||
            !confirmed ||
            (warnings && !reason.trim()) ||
            Boolean(preview["applied"])
          }
          onClick={() => submit(true)}
        >
          Confirm and apply
        </Button>
      </div>
    </Modal>
  );
}
