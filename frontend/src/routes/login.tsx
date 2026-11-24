import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ShieldCheck, ArrowRight } from "lucide-react";
import { api, meQuery, type User } from "../lib/api";
import { meta } from "../lib/meta";
import { Button } from "../components/ui";
export const Route = createFileRoute("/login")({
  ssr: false,
  head: () =>
    meta("Sign in", "Secure access to MOP reconciliation operations."),
  component: Login,
});
function Login() {
  const me = useQuery(meQuery);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const change = me.data?.must_change_password;
  async function submit() {
    setBusy(true);
    setError("");
    try {
      const user = change
        ? await api<User>("/auth/password", {
            method: "POST",
            body: JSON.stringify({
              current_password: password,
              new_password: newPassword,
            }),
          })
        : await api<User>("/auth/login", {
            method: "POST",
            body: JSON.stringify({ username, password }),
          });
      qc.setQueryData(["me"], user);
      setPassword("");
      setNewPassword("");
      if (!user.must_change_password) {
        await qc.invalidateQueries({ queryKey: ["bootstrap"] });
        await navigate({ to: "/summary" });
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Sign in failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-page">
      <header className="brand-bar">
        <div className="identity">
          <div className="brand-mark">CK</div>
          <strong>MOP Reconciliation</strong>
        </div>
      </header>
      <form
        className="login-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="login-icon">
          <ShieldCheck size={28} />
        </div>
        <div className="eyebrow">RETAIL FINANCE</div>
        <h1>{change ? "Choose your password" : "Welcome back"}</h1>
        <p>
          {change
            ? "Change your temporary password to continue."
            : "MOP Reconciliation"}
        </p>
        {!change ? (
          <label>
            Username
            <input
              autoComplete="username"
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>
        ) : null}
        <label>
          {change ? "Current password" : "Password"}
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {change ? (
          <label>
            New password
            <input
              type="password"
              autoComplete="new-password"
              required
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
            />
          </label>
        ) : null}
        {error ? (
          <div role="alert" className="alert alert-danger">
            {error}
          </div>
        ) : null}
        <Button type="submit" busy={busy}>
          {change ? "Change password" : "Sign in"}
          <ArrowRight size={16} />
        </Button>
      </form>
    </div>
  );
}
