import { createFileRoute, redirect } from "@tanstack/react-router";
import { MastersPage } from "../components/screens/MastersPage";
import { meta } from "../lib/meta";
import { meQuery } from "../lib/api";
export const Route = createFileRoute("/_workspace/masters")({
  ssr: false,
  head: () =>
    meta(
      "Masters",
      "MOP Reconciliation masters workspace with existing Python engine data.",
    ),
  component: MastersPage,
});
