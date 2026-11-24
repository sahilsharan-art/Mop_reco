import { createFileRoute, redirect } from "@tanstack/react-router";
import { ExceptionsPage } from "../components/screens/ExceptionsPage";
import { meta } from "../lib/meta";
import { meQuery } from "../lib/api";
export const Route = createFileRoute("/_workspace/exceptions")({
  ssr: false,
  head: () =>
    meta(
      "Exceptions",
      "MOP Reconciliation exceptions workspace with existing Python engine data.",
    ),
  component: ExceptionsPage,
});
