import { Link } from "@tanstack/react-router";
import { DataTable } from "../DataTable";
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
export function SummaryView({ data }: { data: Summary }) {
  const m = data.matrix;
  const total = m["total_sale"] as MatrixColumn;
  // CLAUDE.md 6.9b: matched by the SYSTEM, matched by a PERSON and asserted-but-unverified are three different figures, never one blended rate.
  const bills = data.bills;
  const systemValue = Number(bills?.auto.value ?? 0);
  const systemRate =
    Number(bills?.total.value ?? 0) > 0 ? (systemValue / Number(bills?.total.value)) * 100 : 0;
  // CLAUDE.md 6.9a: only money NOT received is exposure; money received-but-not-billed is a different, opposite situation
  const notReceivedN = Number(data.risk["not_received_count"] ?? 0);
  const notReceivedV = Number(data.risk["not_received_value"] ?? 0);
  return (
    <>
      <section className="executive-band">
        <div className="hero-intro">
          <div className="eyebrow">COLLECTIONS OVERVIEW</div>
          <h2>MOP Reconciliation</h2>
          <div className="hero-status">
            <ShieldCheck size={16} />
            Cash reconciliation to follow
          </div>
        </div>
        <div className="hero-number">
          <span>Total sale</span>
          <strong>{money(total?.sale)}</strong>
          <small>Selected store and business period</small>
        </div>
        <div className="hero-number secondary">
          <span>Receivable</span>
          <strong>{money(total?.receivable)}</strong>
          <small>Open receivable · never written off</small>
        </div>
      </section>
      <div className="metric-strip">
        <Metric
          icon={Wallet}
          title="Collected"
          value={money(total?.collected)}
          detail="Within selected period"
        />
        <Metric
          icon={CheckCircle2}
          title="Matched by system"
          value={money(systemValue)}
          detail={`${systemRate.toFixed(2)}% of digital value · by a person: ${Number(bills?.manual.count ?? 0).toLocaleString("en-IN")} lines · asserted, unverified: ${Number(data.asserted ?? 0).toLocaleString("en-IN")}`}
          tone="success"
        />
        <Metric
          icon={AlertTriangle}
          title="Amount not received"
          value={money(notReceivedV)}
          detail={`${notReceivedN.toLocaleString("en-IN")} bills raised, no payment found`}
          tone="warning"
        />
        <Metric
          icon={TrendingUp}
          title="Digital sale"
          value={money(data.verdict["digital_value"])}
          detail="Existing reconciliation engine"
        />
      </div>
      <section className="section">
        <div className="section-heading">
          <div>
            <h2>Particulars</h2>
            <p>Sale, collected and receivable</p>
          </div>
          <Pill>₹ · Exact values</Pill>
        </div>
        <div className="table-scroll">
          <table className="matrix">
            <thead>
              <tr>
                <th>Particulars</th>
                {[
                  "total_sale",
                  "upi",
                  "card",
                  "total_digital",
                  "total_cash",
                ].map((k) => (
                  <th key={k}>{label(k)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {["sale", "collected", "receivable"].map((field) => (
                <tr key={field}>
                  <td className="sticky-cell">{label(field)}</td>
                  {[
                    "total_sale",
                    "upi",
                    "card",
                    "total_digital",
                    "total_cash",
                  ].map((k) => {
                    const col = m[k] as MatrixColumn;
                    const value = col?.[field as keyof MatrixColumn];
                    return (
                      <td
                        className={`numeric ${field === "receivable" && value ? "attention-cell" : ""}`}
                        key={k}
                      >
                        {money(value)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <details className="adjustments">
          <summary>Adjustments and data controls</summary>
          <dl>
            {[
              "gift_voucher_redeemed",
              "credit_note_issued",
              "credit_note_received",
              "provisional_upi_amount",
              "provisional_upi_bills",
            ].map((k) => (
              <div key={k}>
                <dt>{label(k)}</dt>
                <dd>{display(m[k], k)}</dd>
              </div>
            ))}
          </dl>
          {Array.isArray(m["controls"])
            ? m["controls"].map((v, i) => <p key={i}>{display(v)}</p>)
            : null}
        </details>
      </section>
      <div className="workspace-shortcuts">
        <Link to="/matching">Resolve payment matches →</Link>
        <Link to="/tasks">Review store replies →</Link>
        <Link to="/runs">Run history →</Link>
        <Link to="/exceptions">Open exception register →</Link>
      </div>
      <DashPanels data={data} total={total} />
      <div className="analytics-grid">
        <section className="section">
          <div className="section-heading">
            <div>
              <h2>Digital collection trajectory</h2>
              <p>Actual sale and unmatched value by business date</p>
            </div>
          </div>
          {data.trend.length ? (
            <div className="chart">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={data.trend}>
                  <CartesianGrid vertical={false} stroke="var(--border)" />
                  <XAxis
                    dataKey="business_date"
                    tick={{ fontSize: 12, fill: "var(--muted-foreground)" }}
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    tickFormatter={(v) => `${Number(v) / 100000}L`}
                    tick={{ fontSize: 12, fill: "var(--muted-foreground)" }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <Tooltip formatter={(v) => money(Number(v))} />
                  <Area
                    type="monotone"
                    dataKey="digital_value"
                    stroke="var(--primary)"
                    fill="var(--primary-tint)"
                    strokeWidth={2}
                  />
                  <Area
                    type="monotone"
                    dataKey="unmatched_value"
                    stroke="var(--warning)"
                    fill="var(--warning-tint)"
                    strokeWidth={2}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="empty-state chart-empty">
              <TrendingUp size={26} />
              <p>No bill-level trend for this period yet.</p>
            </div>
          )}
        </section>
        <section className="section">
          <div className="section-heading">
            <div>
              <h2>Exception focus</h2>
              <p>Open items by type</p>
            </div>
            <AlertTriangle size={17} />
          </div>
          {data.exceptions.length ? (
            data.exceptions.slice(0, 6).map((row, i) => (
              <div
                className="exception-line"
                key={i}
                style={
                  {
                    "--share": `${Math.round((Number(row["value"] ?? 0) / Math.max(1, ...data.exceptions.map((r) => Number(r["value"] ?? 0)))) * 100)}%`,
                  } as React.CSSProperties
                }
              >
                <span className="exception-icon">
                  <AlertTriangle size={15} />
                </span>
                <div>
                  <strong>{label(String(row["type"]))}</strong>
                  <small>{String(row["count"])} records</small>
                </div>
                <b>{money(row["value"])}</b>
              </div>
            ))
          ) : (
            <div className="empty-state">
              <CheckCircle2 size={26} />
              <p>No open exceptions.</p>
            </div>
          )}
        </section>
      </div>
      <section className="section">
        <div className="section-heading">
          <div>
            <h2>Store exposure</h2>
            <p>Selected period · exact values</p>
          </div>
          <Link to="/cash">Open Cash Position →</Link>
        </div>
        <DataTable
          rows={(data as Summary & { stores?: Row[] }).stores ?? []}
          columns={[
            "store",
            "total_sale",
            "digital_sale",
            "digital_received",
            "digital_receivable",
            "cash_receivable",
            "closing_balance",
          ]}
          caption="stores"
        />
      </section>
    </>
  );
}
function Metric({
  icon: Icon,
  title,
  value,
  detail,
  tone = "primary",
}: {
  icon: typeof Wallet;
  title: string;
  value: string;
  detail: string;
  tone?: string;
}) {
  return (
    <div className={`metric metric-${tone}`}>
      <span>
        {title}
        <Icon size={16} />
      </span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function DashPanels({
  data,
  total,
}: {
  data: Summary;
  total: MatrixColumn | undefined;
}) {
  const m = data.matrix;
  const r = data.risk ?? {};
  const sale = Number(total?.sale ?? 0),
    col = Number(total?.collected ?? 0),
    rec = Number(total?.receivable ?? 0);
  const coll = sale ? Math.min(100, (col / sale) * 100) : 0;
  const mix = [
    ["UPI", "upi", "mix-a"],
    ["Card", "card", "mix-b"],
    ["Cash", "total_cash", "mix-c"],
  ].map(([l, k, c]) => ({
    l,
    c,
    v: Number((m[k] as MatrixColumn)?.sale ?? 0),
  }));
  const mixT = mix.reduce((a, b) => a + b.v, 0) || 1;
  const risk = [
    {
      l: "Not received",
      d: "Bill exists, money missing",
      n: r["not_received_count"],
      v: Number(r["not_received_value"] ?? 0),
      t: "danger",
    },
    {
      l: "Received, not billed",
      d: "Money arrived without a bill",
      n: r["received_not_billed_count"],
      v: Number(r["received_not_billed_value"] ?? 0),
      t: "warning",
    },
    {
      l: "Other exceptions",
      d: "Duplicates, store mapping, files",
      n: r["other_count"],
      v: Number(r["other_value"] ?? 0),
      t: "neutral",
    },
  ];
  const riskT = risk.reduce((a, b) => a + Math.abs(b.v), 0) || 1;
  const top = data.exceptions[0];
  const insight = `${coll.toFixed(1)}% of sale is collected; ${money(rec)} still receivable.${top ? ` Largest exception bucket: ${label(String(top["type"]))} at ${money(top["value"])}.` : ""}`;
  return (
    <>
      <div className="insight-ribbon">
        <Sparkles size={16} />
        <b>Executive Briefing</b>
        <span>{insight}</span>
      </div>
      <div className="dash-grid">
        <section className="dash-card">
          <div className="section-heading">
            <div>
              <h2>Collection health</h2>
              <p>Collected vs total sale</p>
            </div>
          </div>
          <div className="ring-wrap">
            <div
              className="ring"
              style={{ "--pct": `${coll}%` } as React.CSSProperties}
            >
              <strong>{coll.toFixed(1)}%</strong>
              <small>collected</small>
            </div>
            <dl className="ring-legend">
              <div>
                <dt>Sale</dt>
                <dd>{money(sale)}</dd>
              </div>
              <div>
                <dt>Collected</dt>
                <dd className="ok">{money(col)}</dd>
              </div>
              <div>
                <dt>Receivable</dt>
                <dd className="bad">{money(rec)}</dd>
              </div>
            </dl>
          </div>
        </section>
        <section className="dash-card">
          <div className="section-heading">
            <div>
              <h2>Risk distribution</h2>
              <p>Opposite risks kept separate</p>
            </div>
          </div>
          <div className="risk-bar">
            {risk.map((x) => (
              <span
                key={x.l}
                className={`rb-${x.t}`}
                style={{ width: `${(Math.abs(x.v) / riskT) * 100}%` }}
              />
            ))}
          </div>
          {risk.map((x) => (
            <div key={x.l} className={`risk-row rr-${x.t}`}>
              <div>
                <strong>{x.l}</strong>
                <small>{x.d}</small>
              </div>
              <span className="pill">{String(x.n ?? 0)}</span>
              <b>{money(x.v)}</b>
            </div>
          ))}
        </section>
        <section className="dash-card">
          <div className="section-heading">
            <div>
              <h2>Payment mix</h2>
              <p>Share of sale by mode</p>
            </div>
          </div>
          <div className="mix-bar">
            {mix.map((x) => (
              <span
                key={x.l}
                className={x.c}
                style={{ width: `${(x.v / mixT) * 100}%` }}
              />
            ))}
          </div>
          {mix.map((x) => (
            <div key={x.l} className="mix-row">
              <i className={x.c} />
              <span>{x.l}</span>
              <em>{((x.v / mixT) * 100).toFixed(1)}%</em>
              <b>{money(x.v)}</b>
            </div>
          ))}
        </section>
      </div>
    </>
  );
}
