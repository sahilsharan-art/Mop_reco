import { useState } from "react";
import { useSuspenseQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { screenQuery, params, type TableData } from "../../lib/api";
import { Button } from "../ui";
import { Screen } from "../Screen";
import { DataTable } from "../DataTable";
import { useWorkspace } from "../Workspace";
import { ExceptionBills, ExceptionTransactions } from "./ExceptionPairing";
type Tab = "bills" | "transactions" | "register" | "parked";
const TABS: [Tab, string][] = [
  ["bills", "Exception bills"],
  ["transactions", "Exception transactions"],
  ["register", "Full register"],
  ["parked", "Awaiting Axis"],
];
export function ExceptionsPage() {
  const [tab, setTab] = useState<Tab>("bills");
  return (
    <>
      <div className="segmented" role="tablist" aria-label="Exception views">
        {TABS.map(([key, title]) => (
          <Button key={key} role="tab" aria-selected={tab === key} variant={tab === key ? "primary" : "secondary"} onClick={() => setTab(key)}>
            {title}
          </Button>
        ))}
      </div>
      {tab === "bills" ? <ExceptionBills /> : null}
      {tab === "transactions" ? <ExceptionTransactions /> : null}
      {tab === "register" ? <Screen kind="exceptions" /> : null}
      {tab === "parked" ? <ParkedTable /> : null}
    </>
  );
}
function ParkedTable() {
  const { filter } = useWorkspace();
  const [offset, setOffset] = useState(0);
  const { data } = useSuspenseQuery(
    screenQuery(`/parked?${params(filter, { offset })}`),
  );
  const table = data as TableData;
  return (
    <>
      <div className="alert alert-warning">
        Awaiting Axis data — neither matched nor an exception. These bills stay
        outside digital receivable and store tasks until the source arrives and
        reconciliation is rerun.
      </div>
      <DataTable
        rows={table.rows}
        columns={[
          "bill_no",
          "store",
          "business_date",
          "mop",
          "amount",
          "axis_ref",
          "awaiting_source",
          "reason",
        ]}
        caption="parked bills"
      />
      <div className="pagination">
        <span>{table.total ?? 0} parked records</span>
        <Button
          variant="icon"
          aria-label="Previous parked page"
          disabled={!offset}
          onClick={() => setOffset(Math.max(0, offset - 50))}
        >
          <ChevronLeft size={16} />
        </Button>
        <Button
          variant="icon"
          aria-label="Next parked page"
          disabled={offset + 50 >= (table.total ?? 0)}
          onClick={() => setOffset(offset + 50)}
        >
          <ChevronRight size={16} />
        </Button>
      </div>
    </>
  );
}
