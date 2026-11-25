import { useState } from "react";
import { useSuspenseQuery, useQueryClient } from "@tanstack/react-query";
import { Play, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { api, screenQuery, type Row, type TableData } from "../../lib/api";
import { useWorkspace } from "../Workspace";
import { Button } from "../ui";
import { ReviewAction } from "../ReviewAction";
import { DataTable } from "../DataTable";
export function RunsPage() {
  const { filter } = useWorkspace();
  const qc = useQueryClient();
  const { data } = useSuspenseQuery(screenQuery("/runs", 10000));
  const table = data as TableData & { starting: boolean };
  const [lo, setLo] = useState(filter.date_from);
  const [hi, setHi] = useState(filter.date_to);
  const [check, setCheck] = useState<Row | null>(null);
  const [override, setOverride] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [stored, setStored] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(start = false) {
    setBusy(true);
    try {
      const result = await api<Row>(`/runs/${start ? "start" : "preflight"}`, {
        method: "POST",
        body: JSON.stringify({
          date_from: lo,
          date_to: hi,
          confirmed,
          allow_incomplete_range: override,
          use_stored_masters: stored,
        }),
      });
      if (start) {
        toast.success(String(result["message"]));
        setCheck(null);
        setConfirmed(false);
        await qc.invalidateQueries();
      } else setCheck(result);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Run request failed.");
    } finally {
      setBusy(false);
    }
  }
  const warnings = (check?.["warnings"] ?? []) as string[];
  const masters = (check?.["masters"] ?? []) as Row[];
  const running =
    table.starting ||
    table.rows.some((r) =>
      ["running", "stalled"].includes(String(r["status"])),
    );
  return (
    <>
      <div className="section-heading">
        <div>
          <h2>Run reconciliation</h2>
          <p>
            Every business date remains rerunnable. Previous completed runs stay
            available.
          </p>
        </div>
        <Button
          variant="icon"
          aria-label="Refresh run progress"
          onClick={() => qc.invalidateQueries()}
        >
          <RefreshCw size={18} />
        </Button>
      </div>
      {running ? (
        <div className="alert alert-warning">
          A run is starting, running or stalled. Another run cannot be queued.
        </div>
      ) : null}
      <div className="run-form">
        <label>
          From business date
          <input
            type="date"
            value={lo}
            onChange={(e) => {
              setLo(e.target.value);
              setCheck(null);
              setConfirmed(false);
            }}
          />
        </label>
        <label>
          To business date
          <input
            type="date"
            value={hi}
            onChange={(e) => {
              setHi(e.target.value);
              setCheck(null);
              setConfirmed(false);
            }}
          />
        </label>
        <Button
          variant="secondary"
          busy={busy}
          disabled={!lo || !hi || lo > hi || running}
          onClick={() => submit()}
        >
          Check source coverage
        </Button>
      </div>
      {check ? (
        <section className="section">
          {warnings.map((w) => (
            <div className="alert alert-warning" key={w}>
              {w}
            </div>
          ))}
          {check["axis_note"] ? (
            <div className="alert">{String(check["axis_note"])}</div>
          ) : null}
          {warnings.length ? (
            <label className="check-label">
              <input
                type="checkbox"
                checked={override}
                onChange={(e) => setOverride(e.target.checked)}
              />
              I understand the incomplete coverage and explicitly authorize
              running this range.
            </label>
          ) : null}
          {masters.map((m) => (
            <div className="alert alert-warning" key={String(m["master"])}>
              <strong>{String(m["master"])}</strong>
              <p>{((m["problems"] as string[]) ?? []).join("; ")}</p>
              {m["stored_available"] ? (
                <label>
                  Written authorization to use this stored master for this run
                  only
                  <input
                    value={stored[String(m["master"])] ?? ""}
                    onChange={(e) =>
                      setStored({
                        ...stored,
                        [String(m["master"])]: e.target.value,
                      })
                    }
                  />
                </label>
              ) : (
                <p>
                  No stored version is available. Fix the source master first.
                </p>
              )}
            </div>
          ))}
          <label className="check-label">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            Confirm reconciliation from {lo} to {hi}. Source data is never
            modified.
          </label>
          <Button
            busy={busy}
            disabled={
              running ||
              !confirmed ||
              (warnings.length > 0 && !override) ||
              masters.some(
                (m) =>
                  !m["stored_available"] ||
                  !stored[String(m["master"])]?.trim(),
              )
            }
            onClick={() => submit(true)}
          >
            <Play size={16} />
            Start reconciliation
          </Button>
        </section>
      ) : null}
      <DataTable
        rows={table.rows}
        columns={[
          "run",
          "range",
          "started",
          "status",
          "duration_s",
          "current_stage",
          "progress_detail",
          "heartbeat_age_s",
          "bill_lines",
          "matched",
          "by",
          "failure_reason",
        ]}
        caption="runs"
        actions={(r) =>
          r["status"] === "stalled" ? (
            <Button variant="danger" onClick={() => setFailed(r)}>
              Mark as failed
            </Button>
          ) : null
        }
      />
      {failed ? (
        <ReviewAction
          title="Mark stalled run as failed"
          path={`/runs/${failed["run"]}/mark-failed`}
          details={{
            ...failed,
            warning:
              "Confirm the worker is really stopped. A stale heartbeat alone does not prove the run is dead.",
          }}
          close={() => setFailed(null)}
          done={() => qc.invalidateQueries()}
        />
      ) : null}
      <p className="muted run-note">
        Run failures may leave intermediate stages saved; inspect the recorded
        failure before retrying. Do not stop the API while a run is active.
      </p>
    </>
  );
}
