import type React from "react";
import { useState, useMemo } from "react";
import { useSuspenseQuery, useQueryClient } from "@tanstack/react-query";
import {
  Search,
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
  Download,
  MoreHorizontal,
  CheckCircle2,
  AlertTriangle,
  Wallet,
  TrendingUp,
  ShieldCheck,
  Save,
  Plus,
  UploadCloud,
  Sparkles,
} from "lucide-react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import { toast } from "sonner";
import {
  screenQuery,
  params,
  display,
  money,
  api,
  download,
  type TableData,
  type Summary,
  type Row,
  type MatrixColumn,
} from "../../lib/api";
import { useWorkspace } from "../Workspace";
import { Button, Modal, Pill } from "../ui";
import { label, type ScreenKind } from "../table-utils";
export function UploadModal({
  close,
  refresh,
}: {
  close: () => void;
  refresh: () => Promise<unknown>;
}) {
  const { bootstrap } = useWorkspace();
  const [key, setKey] = useState(bootstrap.sources[0]?.key ?? "");
  const [month, setMonth] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  const spec = bootstrap.sources.find((s) => s.key === key);
  async function submit(confirm = false) {
    if (!file) return;
    setBusy(true);
    try {
      const body = new FormData();
      body.set("file", file);
      body.set("source_key", key);
      body.set("month", month);
      body.set("confirm", String(confirm));
      const result = await api<Row>("/uploads", { method: "POST", body });
      setPreview(result);
      if (result["stored"]) {
        toast.success(`Stored ${String(result["filename"])}`);
        await refresh();
        close();
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Upload failed.");
      setPreview(null);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Upload report" onClose={close}>
      <label>
        Report type
        <select
          value={key}
          onChange={(e) => {
            setKey(e.target.value);
            setPreview(null);
            setFile(null);
          }}
        >
          {bootstrap.sources.map((s) => (
            <option key={s.key} value={s.key}>
              {s.title}
            </option>
          ))}
        </select>
      </label>
      {spec?.month_specific ? (
        <label>
          Report month
          <input
            type="month"
            value={month}
            onChange={(e) => {
              setMonth(e.target.value);
              setPreview(null);
            }}
          />
        </label>
      ) : null}
      <label className="upload-drop">
        <UploadCloud size={30} />
        <span>{file?.name ?? "Choose a report file"}</span>
        <input
          aria-label="Report file"
          type="file"
          accept={spec?.extensions.join(",")}
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setPreview(null);
          }}
        />
      </label>
      {spec?.note ? <p className="muted">{spec.note}</p> : null}
      {preview ? (
        <div className={`alert alert-${preview["ok"] ? "success" : "danger"}`}>
          <strong>
            {preview["ok"] ? "Validation passed" : "Validation failed"}
          </strong>
          <p>
            {display(preview["row_count"])} rows ·{" "}
            {display(preview["date_range"])}
          </p>
          {Array.isArray(preview["errors"])
            ? preview["errors"].map((e, i) => <p key={i}>{String(e)}</p>)
            : null}
          {Array.isArray(preview["warnings"])
            ? preview["warnings"].map((e, i) => <p key={i}>{String(e)}</p>)
            : null}
        </div>
      ) : null}
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Cancel
        </Button>
        <Button
          variant="secondary"
          busy={busy}
          disabled={!file || Boolean(spec?.month_specific && !month)}
          onClick={() => submit(false)}
        >
          Validate report
        </Button>
        <Button
          busy={busy}
          disabled={!preview?.["ok"]}
          onClick={() => submit(true)}
        >
          Confirm upload
        </Button>
      </div>
    </Modal>
  );
}
