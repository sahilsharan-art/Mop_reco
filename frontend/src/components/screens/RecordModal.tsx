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
export function RecordModal({
  kind,
  row,
  close,
  refresh,
}: {
  kind: ScreenKind;
  row: Row;
  close: () => void;
  refresh: () => Promise<unknown>;
}) {
  const [text, setText] = useState("");
  const [action, setAction] = useState(kind === "users" ? "unlock" : "remark");
  const [role, setRole] = useState("user");
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<Row[] | null>(null);
  async function save() {
    setBusy(true);
    try {
      if (kind === "tasks")
        await api(`/tasks/${row["_id"]}`, {
          method: "POST",
          body: JSON.stringify({
            action,
            text: action === "review" ? "Under review" : text,
          }),
        });
      else if (kind === "float")
        await api("/float/remark", {
          method: "POST",
          body: JSON.stringify({
            store: row["store"],
            business_date: row["business_date"],
            text,
          }),
        });
      else if (kind === "exceptions")
        await api("/exceptions/remark", {
          method: "POST",
          body: JSON.stringify({ exception_key: row["exception_key"], text }),
        });
      else if (kind === "users")
        await api(`/users/${row["id"]}`, {
          method: "POST",
          body: JSON.stringify({ action, role, password: text }),
        });
      toast.success("Saved.");
      await refresh();
      close();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={kind === "users" ? "Manage user" : "Record detail"}
      onClose={close}
    >
      <dl className="record-details">
        {Object.entries(row)
          .filter(([k]) => !k.startsWith("_") && k !== "exception_key")
          .map(([k, v]) => (
            <div key={k}>
              <dt>{label(k)}</dt>
              <dd>{display(v, k)}</dd>
            </div>
          ))}
      </dl>
      {kind === "tasks" || kind === "users" ? (
        <label>
          Action
          <select value={action} onChange={(e) => setAction(e.target.value)}>
            {(kind === "tasks"
              ? ["remark", "reply", "review", "close"]
              : ["unlock", "activate", "deactivate", "role", "reset_password"]
            ).map((x) => (
              <option key={x} value={x}>
                {label(x)}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {kind === "users" && action === "role" ? (
        <label>
          Role
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="user">Operator</option>
            <option value="admin">Administrator</option>
          </select>
        </label>
      ) : null}
      {kind !== "users" && action !== "review" ? (
        <label>
          {action === "close" ? "Reason for closing" : "Remark / reply"}
          <textarea
            required
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
      ) : kind === "users" && action === "reset_password" ? (
        <label>
          Temporary password
          <input
            type="password"
            autoComplete="new-password"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
      ) : null}
      {kind === "tasks" ? (
        <Button
          variant="secondary"
          onClick={async () => {
            try {
              setHistory(await api<Row[]>(`/tasks/${row["_id"]}/history`));
            } catch (e) {
              toast.error(String(e));
            }
          }}
        >
          View history
        </Button>
      ) : null}
      {history ? (
        <div className="history">
          {history.map((h, i) => (
            <p key={i}>
              {Object.values(h)
                .map((v) => display(v))
                .join(" · ")}
            </p>
          ))}
        </div>
      ) : null}
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Cancel
        </Button>
        <Button
          variant={
            action === "close" || action === "deactivate" ? "danger" : "primary"
          }
          busy={busy}
          disabled={
            (kind !== "users" && action !== "review" && !text.trim()) ||
            (kind === "users" && action === "reset_password" && !text)
          }
          onClick={save}
        >
          {action === "close"
            ? "Confirm close"
            : action === "deactivate"
              ? "Confirm deactivate"
              : "Save"}
        </Button>
      </div>
    </Modal>
  );
}
