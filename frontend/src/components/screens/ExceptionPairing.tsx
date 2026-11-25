import { useDeferredValue, useEffect, useState } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { Download, MessageSquare, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { api, apiQuery, download, display, money, params, type Row } from "../../lib/api";
import { useWorkspace } from "../Workspace";
import { Button, Modal, Pill } from "../ui";
import { RecordModal } from "./RecordModal";
import { BulkAccept } from "./BulkAccept";
import { Collapsible, Grid, Pager, type Col } from "./shared";

const n0 = (v: unknown) => Number(v ?? 0).toLocaleString("en-IN");
const STATUS_LABEL: [string, string][] = [
  ["", "Open and under investigation"],
  ["open", "Open"],
  ["under_investigation", "Under investigation"],
  ["closed", "Closed"],
  ["auto_resolved", "Auto-resolved"],
  ["all", "All, including closed"],
];
const SORTS = ["Amount (high to low)", "Amount (low to high)", "Date (oldest first)", "Date (newest first)", "Store"];

type Pairing = {
  bills: number;
  exact: number;
  exact_value: number;
  split: number;
  split_value: number;
  ambiguous: number;
  ambiguous_value: number;
  none: number;
  none_value: number;
} | null;
type BillsResp = { rows: Row[]; total: number; value: number; pairing: Pairing };
type TxnsResp = { rows: Row[]; total: number; value: number };

function useRunId() {
  const { filter, bootstrap } = useWorkspace();
  return Number(filter.run_id || bootstrap.run_id || 0);
}

/** The error text of a refused match, in full: a validation refusal names every reason (amount, store, status, already used). */
const refused = (e: unknown) => toast.error(e instanceof Error ? e.message : "The match was refused.", { duration: 12000 });

function Controls({ status, setStatus, sort, setSort, count, value, kind, exportPath }: {
  status: string;
  setStatus: (v: string) => void;
  sort: string;
  setSort: (v: string) => void;
  count: number;
  value: number;
  kind: string;
  exportPath: string;
}) {
  return (
    <div className="table-toolbar">
      <select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)}>
        {STATUS_LABEL.map(([k, v]) => (
          <option key={k} value={k}>
            {v}
          </option>
        ))}
      </select>
      <select aria-label="Sort records" value={sort} onChange={(e) => setSort(e.target.value)}>
        {SORTS.map((x) => (
          <option key={x}>{x}</option>
        ))}
      </select>
      <span className="toolbar-count">
        {n0(count)} {kind} · {money(value)}
      </span>
      <Button variant="secondary" onClick={() => download(exportPath).catch((e) => toast.error(String(e.message ?? e)))}>
        <Download size={15} />
        Export all {n0(count)} {kind}
      </Button>
    </div>
  );
}

/* ------------------------------------------------------------------------------------------------ exception bills */
export function ExceptionBills() {
  const { filter } = useWorkspace();
  const runId = useRunId();
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState(SORTS[0]);
  const [offset, setOffset] = useState(0);
  const [edits, setEdits] = useState<Record<string, { t1?: string; t2?: string }>>({});
  const [busyKey, setBusyKey] = useState("");
  const [shared, setShared] = useState<Row | null>(null);
  const [cluster, setCluster] = useState<number | null>(null);
  const [remark, setRemark] = useState<Row | null>(null);
  const [accept, setAccept] = useState(false);
  const [secondOpen, setSecondOpen] = useState(false);
  const filterKey = JSON.stringify(filter);
  useEffect(() => {
    setOffset(0);
    setEdits({});
  }, [filterKey, status, sort]);
  const path = `/exceptions/bills?${params(filter, { status, sort, offset, limit: 50 })}`;
  const deferred = useDeferredValue(path);
  const { data, isPending, error } = useQuery({ ...apiQuery<BillsResp>(deferred), placeholderData: keepPreviousData });
  const second = useQuery(apiQuery<{ rows: Row[] }>(`/exceptions/second-open?${params(filter)}`, secondOpen));
  const refresh = () => qc.invalidateQueries({ queryKey: ["screen"] });
  if (error) throw error;
  if (isPending || !data) return <div className="empty-state"><p>Pairing open bills with unmapped transactions…</p></div>;
  const live = data.pairing !== null;
  const value = (r: Row, n: 1 | 2) => edits[String(r["exception_key"])]?.[n === 1 ? "t1" : "t2"] ?? String(r[`Txn ${n}`] ?? "");

  async function match(r: Row) {
    const key = String(r["exception_key"]);
    setBusyKey(key);
    try {
      const out = await api<{ message: string }>("/exceptions/match", {
        method: "POST",
        body: JSON.stringify({ run_id: runId, bill_no: r["bill_no"], txn1: value(r, 1), txn2: value(r, 2) }),
      });
      toast.success(out.message);
      setEdits((e) => ({ ...e, [key]: {} }));
      await refresh();
    } catch (e) {
      refused(e);
    } finally {
      setBusyKey("");
    }
  }

  const columns: Col[] = [
    { key: "date" },
    { key: "time" },
    { key: "store" },
    { key: "bill_no", title: "Bill no" },
    { key: "mop", title: "MOP" },
    { key: "amount" },
    { key: "created_by", title: "Created by" },
    { key: "status", cell: (r) => <Pill tone={r["status"] === "open" ? "warning" : "neutral"}>{String(r["status"] ?? "").replaceAll("_", " ")}</Pill> },
    ...(live
      ? ([
          {
            key: "Txn 1",
            cell: (r) => (
              <input className="pair-input" aria-label={`Transaction 1 for ${r["bill_no"]}`} value={value(r, 1)} placeholder="paste an id"
                onChange={(e) => setEdits((x) => ({ ...x, [String(r["exception_key"])]: { ...x[String(r["exception_key"])], t1: e.target.value } }))} />
            ),
          },
          { key: "Amount 1", cell: (r) => (r["Amount 1"] == null ? "" : money(r["Amount 1"])) },
          {
            key: "Txn 2",
            cell: (r) => (
              <input className="pair-input" aria-label={`Transaction 2 for ${r["bill_no"]}`} value={value(r, 2)} placeholder="second part only"
                onChange={(e) => setEdits((x) => ({ ...x, [String(r["exception_key"])]: { ...x[String(r["exception_key"])], t2: e.target.value } }))} />
            ),
          },
          { key: "Amount 2", cell: (r) => (r["Amount 2"] == null ? "" : money(r["Amount 2"])) },
          {
            key: "More",
            cell: (r) => (
              <span className="pair-more">
                {String(r["More"] ?? "")}
                {r["cluster"] != null ? (
                  <>
                    {" "}
                    <Button variant="secondary" onClick={() => setCluster(Number(r["cluster"]))}>Resolve</Button>
                  </>
                ) : null}
              </span>
            ),
          },
          {
            key: "Match",
            cell: (r) => (
              <span className="row-actions">
                <Button busy={busyKey === String(r["exception_key"])} disabled={!value(r, 1).trim() && !value(r, 2).trim()} onClick={() => match(r)}>
                  Match
                </Button>
                <Button variant="secondary" title="One transaction that paid this bill and another" onClick={() => setShared(r)}>
                  Shared…
                </Button>
              </span>
            ),
          },
        ] as Col[])
      : []),
    {
      key: "remark",
      cell: (r) => (
        <span>
          {String(r["remark"] ?? "")}
          {r["remark_by"] ? <small className="muted"> — {String(r["remark_by"])}</small> : null}{" "}
          <Button variant="icon" aria-label={`Remark ${r["bill_no"]}`} title="Add a remark" onClick={() => setRemark(r)}>
            <MessageSquare size={14} />
          </Button>
        </span>
      ),
    },
  ];

  return (
    <div>
      <div className="section-heading">
        <div>
          <h2>Exception bills</h2>
          <p>
            Bills raised with no payment found. Each is paired with the one unmapped transaction of the same store and exact amount, offered to one bill
            only. Typing a clean transaction id is the match; nothing is accepted that does not validate.
          </p>
        </div>
        {live ? (
          <div className="toolbar-actions">
            <Button variant="secondary" onClick={() => setAccept(true)}>
              <Sparkles size={15} />
              Accept the system's suggestions…
            </Button>
          </div>
        ) : null}
      </div>
      {data.pairing ? (
        <div className="pair-summary">
          <Pill tone="success">Exact suggestion: {n0(data.pairing.exact)} bills · {money(data.pairing.exact_value)}</Pill>
          <Pill tone="success">Split payment: {n0(data.pairing.split)} · {money(data.pairing.split_value)}</Pill>
          <Pill tone="warning">Ambiguous: {n0(data.pairing.ambiguous)} · {money(data.pairing.ambiguous_value)}</Pill>
          <Pill>No candidate: {n0(data.pairing.none)} · {money(data.pairing.none_value)}</Pill>
        </div>
      ) : (
        <p className="muted">Suggestions are shown for open work only; this view is read-only.</p>
      )}
      <Controls status={status} setStatus={setStatus} sort={sort} setSort={setSort} count={data.total} value={data.value} kind="bills"
        exportPath={`/exceptions/bills/export?${params(filter, { status, sort })}`} />
      <Grid columns={columns} rows={data.rows} rowKey={(r) => String(r["exception_key"])} empty="No exception bills in this view." />
      <Pager offset={offset} total={data.total} setOffset={setOffset} />
      {live ? (
        <Collapsible title="Second transaction still open" meta="A bill a person matched, with another unmapped transaction of the same store and amount nearby" open={secondOpen} setOpen={setSecondOpen}>
          <p className="muted">
            The bill stays closed on the transaction that was matched. A person decides whether the other is a refund (Refunds) or belongs to a second bill.
          </p>
          {second.data ? (
            <Grid
              columns={[{ key: "bill_no", title: "Bill no" }, { key: "store" }, { key: "bill_date", title: "Bill date" }, { key: "amount" }, { key: "matched_txn", title: "Matched transaction" }, { key: "second_gateway", title: "Gateway" }, { key: "second_txn_id", title: "Open transaction" }, { key: "second_date", title: "Date" }, { key: "second_time", title: "Time" }]}
              rows={second.data.rows}
              rowKey={(r, i) => `${r["bill_no"]}-${r["second_txn_id"]}-${i}`}
              empty="No bill has a second open transaction."
            />
          ) : (
            <p className="muted">Reading…</p>
          )}
        </Collapsible>
      ) : null}
      {shared ? <SharedModal bill={shared} runId={runId} close={() => setShared(null)} done={refresh} /> : null}
      {cluster !== null ? <ClusterModal cid={cluster} runId={runId} close={() => setCluster(null)} done={refresh} /> : null}
      {remark ? <RecordModal kind="exceptions" row={remark} close={() => setRemark(null)} refresh={refresh} /> : null}
      {accept ? <BulkAccept close={() => setAccept(false)} done={refresh} /> : null}
    </div>
  );
}

function SharedModal({ bill, runId, close, done }: { bill: Row; runId: number; close: () => void; done: () => Promise<unknown> }) {
  const [other, setOther] = useState("");
  const [txn, setTxn] = useState("");
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      const out = await api<{ message: string }>("/exceptions/match", {
        method: "POST",
        body: JSON.stringify({ run_id: runId, bill_no: bill["bill_no"], txn1: txn, shared_bill_nos: [other.trim()] }),
      });
      toast.success(out.message);
      await done();
      close();
    } catch (e) {
      refused(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="One payment for two bills" onClose={close}>
      <p className="muted">
        When a customer paid two bills with one swipe, both close together — only when both are in the transaction's store and their amounts add up to it
        exactly. Anything else is refused.
      </p>
      <p>
        First bill: <b>{String(bill["bill_no"])}</b> · {money(bill["amount"])}
      </p>
      <label>
        The other bill number
        <input value={other} onChange={(e) => setOther(e.target.value)} />
      </label>
      <label>
        The transaction id (the same one)
        <input value={txn} onChange={(e) => setTxn(e.target.value)} />
      </label>
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>Cancel</Button>
        <Button busy={busy} disabled={!other.trim() || !txn.trim()} onClick={save}>Match both bills</Button>
      </div>
    </Modal>
  );
}

function ClusterModal({ cid, runId, close, done }: { cid: number; runId: number; close: () => void; done: () => Promise<unknown> }) {
  const { data, error } = useQuery(apiQuery<{ bills: Row[]; transactions: Row[]; can_pair: boolean }>(`/exceptions/clusters/${cid}?run_id=${runId}`));
  const [busy, setBusy] = useState(false);
  async function pair() {
    setBusy(true);
    try {
      const out = await api<{ paired: number; of: number; failed: Row[] }>(`/exceptions/clusters/${cid}/pair`, { method: "POST", body: JSON.stringify({ run_id: runId }) });
      toast.success(`Paired ${out.paired} of ${out.of}.`);
      out.failed.forEach((f) => toast.error(`${f["bill_no"]}: ${f["why"]}`, { duration: 12000 }));
      await done();
      close();
    } catch (e) {
      refused(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Ambiguous cluster" onClose={close}>
      {error ? <p className="alert alert-danger">{error instanceof Error ? error.message : "Could not read the cluster."}</p> : null}
      {data ? (
        <>
          <p className="muted">
            {data.bills.length} bills and {data.transactions.length} transactions of one store and one amount cannot be told apart, so nothing is suggested.
            {data.can_pair ? " The counts are equal, so they can be paired in time order; the amounts are identical, so no figure moves." : " The counts differ, so this cannot be paired as a set: match each bill by hand, or refund the extra transactions."}
          </p>
          <h3 className="muted">Bills</h3>
          <Grid columns={[{ key: "bill_no", title: "Bill no" }, { key: "date" }, { key: "time" }, { key: "mop", title: "MOP" }, { key: "amount" }]} rows={data.bills} rowKey={(r, i) => `${r["bill_no"]}-${i}`} />
          <h3 className="muted">Transactions</h3>
          <Grid columns={[{ key: "txn_id", title: "Transaction id" }, { key: "gateway" }, { key: "date" }, { key: "time" }, { key: "amount" }]} rows={data.transactions} rowKey={(r, i) => `${r["txn_id"]}-${i}`} />
        </>
      ) : null}
      <div className="modal-footer">
        <Button variant="secondary" onClick={close}>Close</Button>
        <Button busy={busy} disabled={!data?.can_pair} onClick={pair}>Pair all {data?.bills.length ?? 0} in time order</Button>
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------------------------------------ exception transactions */
export function ExceptionTransactions() {
  const { filter } = useWorkspace();
  const runId = useRunId();
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState(SORTS[0]);
  const [offset, setOffset] = useState(0);
  const [bill, setBill] = useState<Record<string, string>>({});
  const [busyKey, setBusyKey] = useState("");
  const [remark, setRemark] = useState<Row | null>(null);
  const filterKey = JSON.stringify(filter);
  useEffect(() => {
    setOffset(0);
    setBill({});
  }, [filterKey, status, sort]);
  const path = `/exceptions/transactions?${params(filter, { status, sort, offset, limit: 50 })}`;
  const deferred = useDeferredValue(path);
  const { data, isPending, error } = useQuery({ ...apiQuery<TxnsResp>(deferred), placeholderData: keepPreviousData });
  const refresh = () => qc.invalidateQueries({ queryKey: ["screen"] });
  if (error) throw error;
  if (isPending || !data) return <div className="empty-state"><p>Reading the unmapped transactions…</p></div>;
  const live = status === "" || status === "open" || status === "under_investigation";
  async function match(r: Row) {
    const key = String(r["exception_key"]);
    setBusyKey(key);
    try {
      const out = await api<{ message: string }>("/exceptions/txn-match", {
        method: "POST",
        body: JSON.stringify({ run_id: runId, gateway: r["gateway"], transaction_id: r["transaction_id"], bill_no: bill[key] }),
      });
      toast.success(out.message);
      await refresh();
    } catch (e) {
      refused(e);
    } finally {
      setBusyKey("");
    }
  }
  const columns: Col[] = [
    { key: "gateway" },
    { key: "transaction_id", title: "Transaction id" },
    { key: "date" },
    { key: "time" },
    { key: "store" },
    { key: "amount" },
    { key: "payment_mode", title: "Payment mode" },
    { key: "status", cell: (r) => <Pill tone={r["status"] === "open" ? "warning" : "neutral"}>{String(r["status"] ?? "").replaceAll("_", " ")}</Pill> },
    ...(live
      ? ([
          {
            key: "matched_bill",
            title: "Matched bill",
            cell: (r) => (
              <span className="row-actions">
                <input className="pair-input" aria-label={`Bill paid by ${r["transaction_id"]}`} placeholder="type the bill it paid" value={bill[String(r["exception_key"])] ?? ""}
                  onChange={(e) => setBill((x) => ({ ...x, [String(r["exception_key"])]: e.target.value }))} />
                <Button busy={busyKey === String(r["exception_key"])} disabled={!(bill[String(r["exception_key"])] ?? "").trim()} onClick={() => match(r)}>
                  Match
                </Button>
              </span>
            ),
          },
        ] as Col[])
      : []),
    {
      key: "remark",
      cell: (r) => (
        <span>
          {String(r["remark"] ?? "")}
          {r["remark_by"] ? <small className="muted"> — {String(r["remark_by"])}</small> : null}{" "}
          <Button variant="icon" aria-label={`Remark ${r["transaction_id"]}`} title="Add a remark" onClick={() => setRemark(r)}>
            <MessageSquare size={14} />
          </Button>
        </span>
      ),
    },
  ];
  return (
    <div>
      <div className="section-heading">
        <div>
          <h2>Exception transactions</h2>
          <p>
            Money received with no bill behind it. Type the bill a transaction paid to close both together. A transaction that was a customer refund is closed on the
            Refunds screen, never as revenue.
          </p>
        </div>
      </div>
      <Controls status={status} setStatus={setStatus} sort={sort} setSort={setSort} count={data.total} value={data.value} kind="transactions"
        exportPath={`/exceptions/transactions/export?${params(filter, { status, sort })}`} />
      <Grid columns={columns} rows={data.rows} rowKey={(r) => String(r["exception_key"])} empty="No exception transactions in this view." />
      {data.rows.some((r) => r["gateway"] === "axis") ? <p className="muted">Axis does not supply a transaction time.</p> : null}
      <Pager offset={offset} total={data.total} setOffset={setOffset} />
      {remark ? <RecordModal kind="exceptions" row={remark} close={() => setRemark(null)} refresh={refresh} /> : null}
    </div>
  );
}
