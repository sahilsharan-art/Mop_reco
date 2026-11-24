import { createFileRoute, redirect } from "@tanstack/react-router";
import { meQuery, bootstrapQuery, ApiError } from "../lib/api";
import { Workspace } from "../components/Workspace";
export const Route = createFileRoute("/_workspace")({
  ssr: false,
  beforeLoad: async ({ context }) => {
    try {
      const user = await context.queryClient.ensureQueryData(meQuery);
      if (user.must_change_password) throw redirect({ to: "/login" });
    } catch (e) {
      if (e instanceof ApiError && e.status === 401)
        throw redirect({ to: "/login" });
      throw e;
    }
  },
  loader: ({ context }) => context.queryClient.ensureQueryData(bootstrapQuery),
  component: Workspace,
  errorComponent: ({ error }) => (
    <main className="empty-state">
      <h1>Connection unavailable</h1>
      <p>
        {error instanceof Error
          ? error.message
          : "Cannot connect to MOP Reconciliation."}
      </p>
      <a href="/login">Sign in again</a>
    </main>
  ),
  notFoundComponent: () => <p>Page not found.</p>,
});
