import { Fragment, useState } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { Download, Minus, Plus } from "lucide-react";
import { toast } from "sonner";
import { apiQuery, download, display, money, params, type Row, type Summary } from "../../lib/api";
import { useWorkspace } from "../Workspace";
import { Button } from "../ui";
import { Collapsible, Grid, Pager } from "./shared";

const n0 = (v: unknown) => Number(v ?? 0).toLocaleString("en-IN");
// CLAUDE.md 6.9d: a cash receivable is never shown negative (a credit above its deposit is a Defect B data item, listed on the Float screen)
const floor0 = (v: unknown) => Math.max(0, Number(v ?? 0));

type Count = { count: number; value: number };
type Tables = {
  bills: Record<"open" | "auto" | "manual" | "total", Count>;
  bills_ok: boolean;
  bills_difference: number;
  bills_agree: boolean;
  exception_bills_n: number;
  merged_bill_lines: number;
  txns: Record<string, number>;
  txns_ok: boolean;
  txns_difference: number;
  unmapped_agrees: boolean;
  exception_txns_n: number;
  merged_txn_rows: number;
};
type Controls = { controls: { key: string; what: string; holds: boolean; detail: string }[]; notes: string[]; unresolved_pickups: Row[] };

/** The tables the old Summary carried below the matrix: the two cash statements, the bills / transactions two-sided controls, the
 * store-wise sale-and-cash sheet by date (each date opening onto its stores), the matched bills and the controls in full. */
export function SummaryTables({ data }: { data: Summary }) {
  return (
    <>
      <CashStatements cash={data.cash} />
      <MatchStrip />
      <ByDate />
      <Matched />
      <DataNotes data={data} />
    </>
  );
}

/* ---------------------------------------------------------------- the two cash statements */
function Line({ label, value, sign }: { label: string; value: unknown; sign?: string }) {
  return (
    <div className="stmt-line">
      <span>
        {sign ? <b className="stmt-sign">{sign}</b> : null}
        {label}
      </span>
      <b className="numeric">{money(value)}</b>
    </div>
  );
}

function CashStatements({ cash }: { cash: Row }) {
  const c = cash ?? {};
  const income = Number(c["cash_expense_income"] ?? 0);
  const receivable = Number(c["cash_receivable_net"] ?? 0);
  return (
    <section className="section">
      <div className="section-heading">
        <div>
          <h2>Cash position</h2>
          <p>What the stores hold, and what the agency still owes the bank credit for — exact values</p>
        </div>
      </div>
      <div className="stmt-grid">
        <div className="stmt">
          <h3>Cash at store</h3>
          <Line label="Opening balance" value={c["opening_balance"]} />
          <Line label="Cash sale" value={c["cash_sale"]} sign="+" />
          <Line label="Cash deposit" value={c["cash_deposit"]} sign="−" />
          <Line label={income < 0 ? "Net cash expense" : "Net cash income"} value={c["cash_expense_income"]} sign={income < 0 ? "−" : "+"} />
          <div className={`stmt-result ${c["matches"] === false ? "stmt-bad" : ""}`}>
            <span>Closing balance at store</span>
            <b className="numeric">{money(c["closing_at_store"])}</b>
          </div>
        </div>
        <div className="stmt">
          <h3>Cash with agency</h3>
          <Line label="Cash handed over" value={c["cash_deposit"]} />
          <Line label="Cash credited by bank" value={c["cash_received"]} sign="−" />
          <div className={`stmt-result ${c["matches_receivable"] === false ? "stmt-bad" : receivable > 0.005 ? "stmt-warn" : ""}`}>
            <span>Cash receivable</span>
            <b className="numeric">{money(Math.max(0, receivable))}</b>
          </div>
        </div>
      </div>
      {c["matches"] === false ? (
        <div className="alert alert-danger" role="alert">
          <b>MISMATCH</b> — Closing balance at store ({money(c["closing_at_store"])}) does not equal the sum of column M for the last date ({money(c["check_closing"])}).
        </div>
      ) : null}
      {c["matches_receivable"] === false ? (
        <div className="alert alert-danger" role="alert">
          <b>MISMATCH</b> — Cash receivable ({money(receivable)}) does not agree with the store-day aggregate's own deposit − credit ({money(c["check_receivable"])}).
        </div>
      ) : null}
    </section>
  );
}

/* ---------------------------------------------------------------- bills and transactions, two-sided */
function MatchStrip() {
  const { filter } = useWorkspace();
  const { data } = useQuery(apiQuery<Tables>(`/summary/tables?${params(filter)}`));
  if (!data) return <p className="muted">Reading the bills and transactions…</p>;
  const b = data.bills;
  const t = data.txns;
  const billRows: [string, Count][] = [
    ["Open (unmatched)", b.open],
    ["Closed — auto matched", b.auto],
    ["Closed — manually matched", b.manual],
    ["Total", b.total],
  ];
  const txnRows: [string, number, number][] = [
    ["Unmapped (open)", t["unmapped_n"], t["unmapped_v"]],
    ["Mapped to bill — auto", t["auto_n"], t["auto_v"]],
    ["Mapped to bill — manual", t["manual_n"], t["manual_v"]],
    ["Refunded to customer", t["refunded_n"], t["refunded_v"]],
    ["Total", t["total_n"], t["total_v"]],
  ];
  return (
    <section className="section">
      <div className="section-heading">
        <div>
          <h2>POS sale bills and settlement transactions</h2>
          <p>The two sides are checked against each other and against the exception register; a disagreement is shown, never hidden</p>
        </div>
      </div>
      <div className="strip-grid">
        <div>
          <h3 className="muted">POS sale bills</h3>
          <table className="strip">
            <thead>
              <tr>
                <th />
                <th>Count</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              {billRows.map(([l, v], i) => (
                <tr key={l} className={i === billRows.length - 1 ? "strip-total" : ""}>
                  <td>{l}</td>
                  <td className="numeric">{n0(v.count)}</td>
                  <td className="numeric">{money(v.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!data.bills_ok ? (
            <div className="alert alert-danger" role="alert">
              <b>MISMATCH</b> (bills): open + auto closed + manual closed differs from the total by {n0(data.bills_difference)}.
            </div>
          ) : null}
          {!data.bills_agree ? (
            <div className="alert alert-danger" role="alert">
              <b>MISMATCH</b>: {n0(b.open.count)} open bill lines but {n0(data.exception_bills_n)} exception bills + {n0(data.merged_bill_lines)} merged lines.
            </div>
          ) : null}
        </div>
        <div>
          <h3 className="muted">Settlement transactions</h3>
          <table className="strip">
            <thead>
              <tr>
                <th />
                <th>Count</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              {txnRows.map(([l, n, v], i) => (
                <tr key={l} className={i === txnRows.length - 1 ? "strip-total" : ""}>
                  <td>{l}</td>
                  <td className="numeric">{n0(n)}</td>
                  <td className="numeric">{money(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!data.txns_ok ? (
            <div className="alert alert-danger" role="alert">
              <b>MISMATCH</b> (transactions): auto + manual + refunded + unmapped differs from the total by {n0(data.txns_difference)}.
            </div>
          ) : null}
          {!data.unmapped_agrees ? (
            <div className="alert alert-danger" role="alert">
              <b>MISMATCH</b>: {n0(t["unmapped_n"])} unmapped transactions but {n0(data.exception_txns_n)} exception transactions + {n0(data.merged_txn_rows)} merged.
            </div>
          ) : null}
          {t["not_sale_n"] ? <p className="muted">{n0(t["not_sale_n"])} refund / chargeback / failed row(s) are not sales and are outside this count.</p> : null}
        </div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- the store-wise sheet, by business date */
const SHEET_COLUMNS: [string, string][] = [
  ["total_sale", "Total sale"],
  ["digital_sale", "Digital sale"],
  ["digital_received", "Digital received"],
  ["digital_receivable", "Digital receivable"],
  ["opening_balance", "Opening balance"],
  ["cash_sale", "Cash sale"],
  ["cash_deposit", "Cash deposit"],
  ["cash_received", "Cash received"],
  ["cash_receivable", "Cash receivable"],
  ["cash_expense_income", "Cash expense/income"],
  ["closing_balance", "Closing balance"],
];
const cell = (key: string, v: unknown) => money(key === "cash_receivable" ? floor0(v) : v);
// a date-expansion fires one request per date, so "expand all" is bounded
const EXPAND_ALL_LIMIT = 31;

function ByDate() {
  const { filter } = useWorkspace();
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<string[]>([]);
  const { data } = useQuery({ ...apiQuery<{ rows: Row[]; date_check: Row[] }>(`/summary/by-date?${params(filter)}`, open), placeholderData: keepPreviousData });
  const dates = (data?.rows ?? []).filter((r) => r["date"] !== "TOTAL").map((r) => String(r["date"]));
  const toggle = (d: string) => setExpanded((x) => (x.includes(d) ? x.filter((y) => y !== d) : [...x, d]));
  return (
    <Collapsible title="Store-wise sale and cash — by business date" meta={data ? `${n0(dates.length)} business dates` : "Opens onto every store, day by day"} open={open} setOpen={setOpen}>
      {!data ? (
        <p className="muted">Reading…</p>
      ) : (
        <>
          <div className="toolbar-actions">
            <Button
              variant="secondary"
              onClick={() => (dates.length > EXPAND_ALL_LIMIT ? toast.info(`Expand all is limited to ${EXPAND_ALL_LIMIT} dates; narrow the period or open the dates you need.`) : setExpanded(dates))}
            >
              Expand all
            </Button>
            <Button variant="secondary" onClick={() => setExpanded([])}>
              Collapse all
            </Button>
          </div>
          {data.date_check.length ? (
            <div className="alert alert-danger" role="alert">
              <b>MISMATCH</b> — {n0(data.date_check.length)} date row(s) differ from the sum of their own stores; first: {String(data.date_check[0]["date"])} · {String(data.date_check[0]["column"])} (difference {money(data.date_check[0]["difference"])}).
            </div>
          ) : null}
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th />
                  <th>Date</th>
                  <th>Store</th>
                  {SHEET_COLUMNS.map(([k, l]) => (
                    <th key={k}>{l}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r) => {
                  const d = String(r["date"]);
                  const total = d === "TOTAL";
                  const isOpen = expanded.includes(d);
                  return (
                    <Fragment key={d}>
                      <tr className={total ? "strip-total" : ""}>
                        <td>
                          {total ? null : (
                            <Button variant="icon" aria-label={`${isOpen ? "Collapse" : "Expand"} ${display(d)}`} aria-expanded={isOpen} onClick={() => toggle(d)}>
                              {isOpen ? <Minus size={14} /> : <Plus size={14} />}
                            </Button>
                          )}
                        </td>
                        <td className="sticky-cell">{total ? "TOTAL" : display(d)}</td>
                        <td>{String(r["store"])}</td>
                        {SHEET_COLUMNS.map(([k]) => (
                          <td key={k} className="numeric">
                            {cell(k, r[k])}
                          </td>
                        ))}
                      </tr>
                      {isOpen ? <DayRows day={d} /> : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Collapsible>
  );
}

function DayRows({ day }: { day: string }) {
  const { filter } = useWorkspace();
  const { data, isPending } = useQuery(apiQuery<{ rows: Row[] }>(`/summary/by-date/${day}?${params(filter)}`));
  if (isPending || !data)
    return (
      <tr>
        <td colSpan={3 + SHEET_COLUMNS.length} className="muted">
          Reading {display(day)}…
        </td>
      </tr>
    );
  return (
    <>
      {data.rows.map((r) => (
        <tr key={String(r["store"])} className="day-store-row">
          <td />
          <td />
          <td className="store-indent">{String(r["store"])}</td>
          {SHEET_COLUMNS.map(([k]) => (
            <td key={k} className="numeric">
              {cell(k, r[k])}
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

/* ---------------------------------------------------------------- matched bills */
function Matched() {
  const { filter } = useWorkspace();
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const { data } = useQuery({ ...apiQuery<{ rows: Row[]; total: number }>(`/summary/matched?${params(filter, { offset, limit: 50 })}`, open), placeholderData: keepPreviousData });
  return (
    <Collapsible title="Matched bills" meta={data ? `${n0(data.total)} matched bill lines` : "Bills closed by the system or by a person, with the transaction that paid them"} open={open} setOpen={setOpen}>
      <div className="toolbar-actions">
        <Button variant="secondary" onClick={() => download(`/summary/matched/export?${params(filter)}`).catch((e) => toast.error(String(e.message ?? e)))}>
          <Download size={15} />
          Export every matched bill
        </Button>
      </div>
      {data ? (
        <>
          <Grid
            columns={[
              { key: "date" },
              { key: "store" },
              { key: "bill_no", title: "Bill no" },
              { key: "mop", title: "MOP" },
              { key: "amount" },
              { key: "gateway" },
              { key: "transaction_id", title: "Transaction id" },
              { key: "txn_date", title: "Txn date" },
              { key: "lag", title: "Lag (days)", cell: (r) => (Number(r["lag"]) ? `⚠ ${r["lag"]}` : String(r["lag"] ?? 0)) },
              { key: "match_source", title: "Matched by" },
            ]}
            rows={data.rows}
            rowKey={(r, i) => `${r["bill_no"]}-${r["mop"]}-${i}`}
            empty="No matched bills in this selection."
          />
          <Pager offset={offset} total={data.total} setOffset={setOffset} />
        </>
      ) : (
        <p className="muted">Reading…</p>
      )}
    </Collapsible>
  );
}

/* ---------------------------------------------------------------- the controls, in full, and the run's data notes */
function DataNotes({ data }: { data: Summary }) {
  const { filter } = useWorkspace();
  const [open, setOpen] = useState(false);
  const { data: c } = useQuery(apiQuery<Controls>(`/summary/controls?${params(filter)}`, open));
  const matrixControls = (Array.isArray(data.matrix["controls"]) ? data.matrix["controls"] : []) as unknown[][];
  const matrixFailed = matrixControls.filter((x) => Array.isArray(x) && x[1] === false);
  const provisional = Number(data.matrix["provisional_upi_bills"] ?? 0);
  const failing = c?.controls.filter((x) => !x.holds).length ?? 0;
  return (
    <>
      {matrixFailed.map((x, i) => (
        <div key={i} className="alert alert-danger" role="alert">
          <b>Particulars-matrix control does not hold:</b> {String(x[0])} — {String(x[2])}
        </div>
      ))}
      <Collapsible title={failing ? `Data notes — ${failing} control${failing === 1 ? "" : "s"} failing` : "Data notes"} meta="The controls in full, and what the run noted about its own data" open={open} setOpen={setOpen}>
        {!c ? (
          <p className="muted">Reading…</p>
        ) : (
          <>
            <Grid
              columns={[
                { key: "key", title: "" },
                { key: "what", title: "Control" },
                { key: "holds", title: "Result", cell: (r) => (r["holds"] ? "HOLDS" : <b className="stmt-bad">FAILS</b>) },
                { key: "detail", title: "Detail" },
              ]}
              rows={c.controls}
              rowKey={(r) => String(r["key"])}
              tone={(r) => (r["holds"] ? undefined : "danger")}
              empty="This run has no stored controls; re-run to produce them."
            />
            {c.notes.map((n, i) => (
              <p key={i} className="muted">
                {n}
              </p>
            ))}
            {provisional > 0 ? (
              <p className="muted">
                <b>Provisional UPI.</b> {money(data.matrix["provisional_upi_amount"])} of UPI sale ({n0(provisional)} open bills) is provisional: those bills have no matched transaction
                yet and reclassify on match. It is already counted inside UPI, Total digital and Total sale.
              </p>
            ) : null}
            {c.unresolved_pickups.length ? (
              <>
                <h3 className="muted">Pickup codes that do not resolve to a store</h3>
                <Grid columns={[{ key: "PICKUPPOINTCODE", title: "Pickup code" }, { key: "TYPE", title: "Type" }, { key: "rows" }, { key: "value" }]} rows={c.unresolved_pickups} rowKey={(r, i) => `${r["PICKUPPOINTCODE"]}-${i}`} />
              </>
            ) : null}
          </>
        )}
      </Collapsible>
    </>
  );
}
