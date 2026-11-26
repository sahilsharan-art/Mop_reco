import { createFileRoute, redirect } from "@tanstack/react-router";
import { UploadsPage } from "../components/screens/UploadsPage";
import { meta } from "../lib/meta";
import { meQuery } from "../lib/api";
export const Route = createFileRoute("/_workspace/uploads")({
  ssr: false,
  head: () =>
    meta(
      "Upload",
      "MOP Reconciliation upload workspace with existing Python engine data.",
    ),
  component: UploadsPage,
});
