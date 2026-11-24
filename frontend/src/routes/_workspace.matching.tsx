import { createFileRoute } from "@tanstack/react-router";
import { MatchingPage } from "../components/screens/MatchingPage";
import { meta } from "../lib/meta";
export const Route = createFileRoute("/_workspace/matching")({
  ssr: false,
  head: () =>
    meta(
      "Matching",
      "MOP Reconciliation matching workflow with audited Python engine operations.",
    ),
  component: MatchingPage,
});
