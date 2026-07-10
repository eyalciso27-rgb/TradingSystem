"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export default function LoginPage() {
  const router = useRouter();
  const [operator, setOperator] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    const response = await fetch("/api/session", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ operator }),
    });
    if (response.ok) {
      router.push("/");
      router.refresh();
    } else {
      setError("operator name required");
    }
  }

  return (
    <main className="login">
      <form onSubmit={submit} className="login-card">
        <h1>Operations Console</h1>
        <p className="env-tag">PAPER observation only — no trading actions</p>
        <label htmlFor="operator">Operator name</label>
        <input
          id="operator"
          name="operator"
          autoComplete="off"
          value={operator}
          onChange={(event) => setOperator(event.target.value)}
        />
        <button type="submit">Enter (read-only)</button>
        {error === null ? null : <p role="alert">{error}</p>}
        <p className="hint">
          Shell auth: session is per-server-process and read-only. Real
          credentials/RBAC arrive with the API wiring — no secrets live here.
        </p>
      </form>
    </main>
  );
}
