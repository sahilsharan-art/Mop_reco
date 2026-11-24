import { createFileRoute } from "@tanstack/react-router";
import { RefundsPage } from "../components/screens/RefundsPage";
import { meta } from "../lib/meta";
export const Route = createFileRoute("/_workspace/refunds")({
  ssr: false,
  head: () =>
    meta(
      "Refunds",
      "MOP Reconciliation refunds workflow with audited Python engine operations.",
    ),
  component: RefundsPage,
});
