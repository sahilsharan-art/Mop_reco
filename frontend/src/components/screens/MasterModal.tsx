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
export function MasterModal({
  name,
  data,
  close,
  refresh,
}: {
  name: string;
  data: TableData;
  close: () => void;
  refresh: () => Promise<unknown>;
}) {
  const [rows, setRows] = useState(data.rows.map((r) => ({ ...r })));
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      await api(`/masters/${name}`, {
        method: "POST",
        body: JSON.stringify({
          rows,
          note,
          expected_version: data.version ?? null,
        }),
      });
      toast.success("Master saved with audit history.");
      await refresh();
      close();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Edit master" onClose={close}>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {data.fields?.map((k) => (
                <th key={k}>{label(k)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {data.fields?.map((k) => (
                  <td key={k}>
                    <input
                      aria-label={`${label(k)} row ${i + 1}`}
                      value={String(row[k] ?? "")}
                      onChange={(e) =>
                        setRows(
                          rows.map((r, index) =>
                            index === i ? { ...r, [k]: e.target.value } : r,
                          ),
                        )
                      }
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Button
        variant="secondary"
        onClick={() =>
          setRows([
            ...rows,
            Object.fromEntries((data.fields ?? []).map((k) => [k, ""])),
          ])
        }
      >
        <Plus size={15} />
        Add row
      </Button>
      <label>
        Reason for change
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          required
        />
      </label>
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>
          Cancel
        </Button>
        <Button busy={busy} disabled={!note.trim()} onClick={save}>
          Confirm and save version
        </Button>
      </div>
    </Modal>
  );
}
