import type React from "react";
export type ScreenKind =
  | "summary"
  | "tasks"
  | "cash"
  | "float"
  | "exceptions"
  | "uploads"
  | "masters"
  | "users";
export const titles: Record<ScreenKind, string> = {
  summary: "Summary",
  tasks: "Store task register",
  cash: "Store-day cash position",
  float: "Float register",
  exceptions: "Exception register",
  uploads: "Upload history",
  masters: "Master data",
  users: "Users",
};
export const columns: Partial<Record<ScreenKind, string[]>> = {
  tasks: [
    "Task id",
    "Store",
    "Business date",
    "Type",
    "Amount",
    "Due by",
    "Status",
    "Store reply",
    "HO remark",
  ],
  cash: [
    "store",
    "date",
    "total_sale",
    "digital_sale",
    "cash_sale",
    "digital_received",
    "digital_receivable",
    "opening_balance",
    "cash_deposit",
    "cash_received",
    "cash_receivable",
    "cash_expense_income",
    "closing_balance",
    "float_amount",
  ],
  float: [
    "store",
    "business_date",
    "day_name",
    "float_amount",
    "float_threshold",
    "days_held_high",
    "negative_float",
    "remark",
  ],
  exceptions: [
    "store",
    "date",
    "type",
    "reference",
    "amount",
    "age_days",
    "status",
    "first_seen",
    "last_seen",
  ],
  uploads: [
    "original_file_name",
    "report_name",
    "month",
    "row_count",
    "size_bytes",
    "uploaded_by",
    "uploaded_at",
    "status",
  ],
  users: [
    "display_name",
    "username",
    "role",
    "active",
    "must_change_password",
    "last_login_at",
  ],
};
export const label = (key: string) =>
  key.replaceAll("_", " ").replace(/\b\w/g, (x) => x.toUpperCase());
export function heatOf(
  v: unknown,
  rows: Record<string, unknown>[],
  key: string,
) {
  if (typeof v !== "number" || !v || /days|age|id$|count/i.test(key))
    return undefined;
  const vals = rows
    .map((r) => r[key])
    .filter((x) => typeof x === "number") as number[];
  if (new Set(vals).size < 2) return undefined;
  const max = Math.max(
    1,
    ...rows.map((r) =>
      typeof r[key] === "number" ? Math.abs(r[key] as number) : 0,
    ),
  );
  const pct = Math.round(Math.min(1, Math.abs(v) / max) * 100);
  return { "--heat": `${pct}%` } as React.CSSProperties;
}
export function toneFor(k: string, v: unknown) {
  if (/negative/.test(k.toLowerCase()))
    return /yes|true/i.test(String(v)) ? "danger" : "success";
  return tone(v);
}
export function rowTone(row: Record<string, unknown>) {
  if (row["lifecycle"] === "reversed") return "neutral";
  for (const k of Object.keys(row)) {
    if (/status|negative_float|severity/.test(k.toLowerCase())) {
      const t = toneFor(k, row[k]);
      if (t !== "neutral") return t;
    }
  }
  return undefined;
}
export function tone(v: unknown) {
  const s = String(v).toLowerCase().trim().replaceAll(" ", "_");
  if (
    [
      "closed",
      "matched",
      "matched_manual",
      "stored",
      "completed",
      "auto_resolved",
      "active",
      "true",
      "yes",
    ].includes(s)
  )
    return "success";
  if (
    [
      "failed",
      "negative",
      "blocked",
      "discrepant",
      "not_found",
      "asserted",
      "asserted_unverified",
      "unmatched",
      "negative_float",
    ].includes(s)
  )
    return "danger";
  if (
    [
      "open",
      "pending",
      "under_review",
      "under_investigation",
      "replied",
      "running",
    ].includes(s)
  )
    return "warning";
  return "neutral";
}
