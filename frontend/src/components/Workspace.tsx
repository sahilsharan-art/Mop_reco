import { createContext, useContext, useState } from "react";
import {
  Link,
  Outlet,
  useRouterState,
  useNavigate,
} from "@tanstack/react-router";
import { useSuspenseQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  LayoutDashboard,
  ListChecks,
  Wallet,
  Landmark,
  TriangleAlert,
  Upload,
  Database,
  Users,
  LogOut,
  RefreshCw,
  CalendarDays,
  Store,
  ArrowUpRight,
  Link2,
  RotateCcw,
  GitCompareArrows,
  Play,
  KeyRound,
} from "lucide-react";
import { bootstrapQuery, noticesQuery, api, type Filter, type Bootstrap, type Notice } from "../lib/api";
import { PasswordDialog } from "./screens/PasswordDialog";
import { Button, Pill } from "./ui";
import { toast } from "sonner";
const nav = [
  { to: "/summary", label: "Summary", icon: LayoutDashboard },
  { to: "/tasks", label: "Store Task", icon: ListChecks },
  { to: "/cash", label: "Cash Position", icon: Wallet },
  { to: "/float", label: "Float", icon: Landmark },
  { to: "/exceptions", label: "Exceptions", icon: TriangleAlert },
  { to: "/uploads", label: "Upload", icon: Upload },
  { to: "/masters", label: "Masters", icon: Database },
  { to: "/matching", label: "Matching", icon: Link2 },
  { to: "/refunds", label: "Refunds", icon: RotateCcw },
  { to: "/cms", label: "CMS Review", icon: GitCompareArrows },
  { to: "/runs", label: "Runs", icon: Play },
  { to: "/users", label: "Users", icon: Users },
] as const;
const Context = createContext<{ filter: Filter; bootstrap: Bootstrap } | null>(
  null,
);
export function useWorkspace() {
  const x = useContext(Context);
  if (!x) throw new Error("Workspace context unavailable");
  return x;
}
/** The warnings the old dashboard showed on every page: a cut-off POS file, parked Axis, a period the run does not cover, a failed control,
 * a bills / transactions MISMATCH. Loud when something is wrong; nothing at all when everything holds. Loaded beside the screen, never blocking it. */
function Notices({ filter, latestRun }: { filter: Filter; latestRun: number | null }) {
  const { data, isError } = useQuery(noticesQuery(filter));
  const items: Notice[] = [...(data?.notices ?? [])];
  if (latestRun && filter.run_id && Number(filter.run_id) !== latestRun)
    items.unshift({
      key: "not_latest_run",
      level: "warning",
      title: `Run #${filter.run_id} is not the latest completed run`,
      text: `The latest completed run is #${latestRun}. These figures are as of the older run, and changes cannot be saved against it.`,
    });
  if (isError)
    items.push({
      key: "notices_failed",
      level: "info",
      title: "Warnings could not be loaded",
      text: "Do not assume the data is complete. Refresh the page; if this stays, tell your administrator.",
    });
  if (!items.length) return null;
  return (
    <div className="notices">
      {items.map((n) => (
        <div key={n.key} className={`notice notice-${n.level}`} role={n.level === "error" ? "alert" : "status"}>
          <strong>{n.title}</strong>
          <span>{n.text}</span>
        </div>
      ))}
    </div>
  );
}
export function Workspace() {
  const { data: bootstrap } = useSuspenseQuery(bootstrapQuery);
  const [filter, setFilter] = useState<Filter>({
    run_id: String(bootstrap.run_id ?? ""),
    date_from: bootstrap.date_from,
    date_to: bootstrap.date_to,
    store: "",
  });
  const [draft, setDraft] = useState(filter);
  const [password, setPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const path = useRouterState({ select: (s) => s.location.pathname });
  const active = nav.find((x) => x.to === path);
  async function signOut() {
    setBusy(true);
    try {
      await api("/auth/logout", { method: "POST" });
      qc.clear();
      await navigate({ to: "/login" });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Sign out failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Context.Provider value={{ filter, bootstrap }}>
      <div className="app">
        <header className="brand-bar">
          <div className="identity">
            <div className="brand-mark">CK</div>
            <div>
              <strong>MOP Reconciliation</strong>
              <span>Retail finance operations</span>
            </div>
          </div>
          <div className="account">
            <span className="account-name">{bootstrap.user.display_name}</span>
            <Pill>
              {bootstrap.user.role === "admin" ? "Administrator" : "Operator"}
            </Pill>
            <Button
              variant="icon"
              aria-label="Change password"
              title="Change password"
              onClick={() => setPassword(true)}
            >
              <KeyRound size={17} />
            </Button>
            <Button
              variant="icon"
              aria-label="Sign out"
              title="Sign out"
              busy={busy}
              onClick={signOut}
            >
              <LogOut size={17} />
            </Button>
          </div>
        </header>
        <nav className="main-nav" aria-label="Main navigation">
          {nav
            .filter((x) => x.to !== "/users" || bootstrap.user.role === "admin")
            .map((x) => (
              <Link
                key={x.to}
                to={x.to}
                className={path === x.to ? "nav-link active" : "nav-link"}
              >
                <x.icon size={16} />
                {x.label}
              </Link>
            ))}
        </nav>
        <main className="page">
          <div className="page-top">
            <div>
              <div className="eyebrow">FINANCE WORKSPACE</div>
              <h1>{active?.label ?? "MOP Reconciliation"}</h1>
            </div>
            <div className="run-stamp">
              <span className="status-dot" />
              {bootstrap.run_id ? `Run #${filter.run_id}` : "No completed run"}
              <span className="dot-separator">·</span>
              <span>{filter.store || "All stores"}</span>
            </div>
          </div>
          <form
            className="filter-band"
            onSubmit={(e) => {
              e.preventDefault();
              if (draft.date_from > draft.date_to) {
                toast.error("Start date must precede end date.");
                return;
              }
              setFilter(draft);
            }}
          >
            <label>
              <span>
                <Store size={13} /> Store
              </span>
              <select
                aria-label="Store filter"
                value={draft.store}
                onChange={(e) => setDraft({ ...draft, store: e.target.value })}
              >
                <option value="">All stores</option>
                {bootstrap.stores.map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
            </label>
            <label>
              <span>
                <CalendarDays size={13} /> From date
              </span>
              <input
                type="date"
                aria-label="From date"
                value={draft.date_from}
                onChange={(e) =>
                  setDraft({ ...draft, date_from: e.target.value })
                }
              />
            </label>
            <label>
              <span>To date</span>
              <input
                type="date"
                aria-label="To date"
                value={draft.date_to}
                onChange={(e) =>
                  setDraft({ ...draft, date_to: e.target.value })
                }
              />
            </label>
            <label>
              <span>Reconciliation run</span>
              <select
                aria-label="Reconciliation run"
                value={draft.run_id}
                onChange={(e) =>
                  // another run covers other dates: empty dates mean that run's own whole range, never the previous run's dates
                  setDraft({ ...draft, run_id: e.target.value, date_from: "", date_to: "" })
                }
              >
                {bootstrap.runs
                  .filter((r) => r["status"] === "completed")
                  .map((r) => (
                    <option key={String(r["id"])} value={String(r["id"])}>
                      Run #{String(r["id"])}
                    </option>
                  ))}
              </select>
            </label>
            <Button type="submit">
              Apply <ArrowUpRight size={15} />
            </Button>
            <Button
              variant="icon"
              type="button"
              aria-label="Refresh data"
              title="Refresh data"
              onClick={() => qc.invalidateQueries()}
            >
              <RefreshCw size={16} />
            </Button>
          </form>
          <Notices filter={filter} latestRun={bootstrap.run_id} />
          <Outlet />
          {password ? (
            <PasswordDialog close={() => setPassword(false)} />
          ) : null}
        </main>
      </div>
    </Context.Provider>
  );
}
