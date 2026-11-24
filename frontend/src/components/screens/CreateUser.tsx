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
export function CreateUser({
  close,
  refresh,
}: {
  close: () => void;
  refresh: () => Promise<unknown>;
}) {
  const [form, setForm] = useState({
    username: "",
    display_name: "",
    password: "",
    role: "user",
  });
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      await api("/users", { method: "POST", body: JSON.stringify(form) });
      toast.success("User created; password change required at first sign-in.");
      await refresh();
      close();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Creation failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Add user" onClose={close}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        {["username", "display_name", "password"].map((k) => (
          <label key={k}>
            {label(k)}
            <input
              type={k === "password" ? "password" : "text"}
              autoComplete={k === "password" ? "new-password" : "off"}
              required
              value={form[k as keyof typeof form]}
              onChange={(e) => setForm({ ...form, [k]: e.target.value })}
            />
          </label>
        ))}
        <label>
          Role
          <select
            value={form.role}
            onChange={(e) => setForm({ ...form, role: e.target.value })}
          >
            <option value="user">Operator</option>
            <option value="admin">Administrator</option>
          </select>
        </label>
        <div className="modal-footer">
          <Button variant="secondary" type="button" onClick={close}>
            Cancel
          </Button>
          <Button type="submit" busy={busy}>
            Create user
          </Button>
        </div>
      </form>
    </Modal>
  );
}
