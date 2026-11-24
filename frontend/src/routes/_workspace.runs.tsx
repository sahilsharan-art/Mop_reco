import { createFileRoute } from "@tanstack/react-router";
import { RunsPage } from "../components/screens/RunsPage";
import { meta } from "../lib/meta";
export const Route = createFileRoute("/_workspace/runs")({
  ssr: false,
  head: () =>
    meta(
      "Runs",
      "MOP Reconciliation runs workflow with audited Python engine operations.",
    ),
  component: RunsPage,
});
