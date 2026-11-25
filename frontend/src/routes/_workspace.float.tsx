import { createFileRoute, redirect } from "@tanstack/react-router";
import { FloatPage } from "../components/screens/FloatPage";
import { meta } from "../lib/meta";
import { meQuery } from "../lib/api";
export const Route = createFileRoute("/_workspace/float")({
  ssr: false,
  head: () =>
    meta(
      "Float",
      "MOP Reconciliation float workspace with existing Python engine data.",
    ),
  component: FloatPage,
});
