import { createFileRoute } from "@tanstack/react-router";
import { CmsPage } from "../components/screens/CmsPage";
import { meta } from "../lib/meta";
export const Route = createFileRoute("/_workspace/cms")({
  ssr: false,
  head: () =>
    meta(
      "Cms",
      "MOP Reconciliation cms workflow with audited Python engine operations.",
    ),
  component: CmsPage,
});
