"use client";

/**
 * Source-connections screen (IMP-1102): one row per source with CAPABILITY
 * and RUNTIME as two separate columns (DoD), plus a re-auth request button
 * where a re-auth flow exists. The button only REQUESTS re-auth (the owner
 * completes it out of band); the console never accepts credentials and never
 * shows a full account id.
 */

import { useState } from "react";

import { AppNav } from "./app-nav";
import { SOURCE_REAUTH_PATH } from "../lib/routes";
import { SOURCE_CONNECTIONS, type RuntimeState, type SourceKind } from "../lib/sources";

function runtimeChipClass(runtime: RuntimeState): string {
  // Map the source runtime vocabulary onto the shared chip tones.
  if (runtime === "CONNECTED") {
    return "chip chip-ok";
  }
  if (runtime === "BLOCKED_NO_POI") {
    return "chip chip-danger";
  }
  if (runtime === "NOT_CONFIGURED") {
    return "chip chip-muted";
  }
  return "chip chip-warn"; // DISCONNECTED / NEEDS_REAUTH
}

export function SourcesScreen({ operator, role }: { operator: string; role: string }) {
  const [messages, setMessages] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  async function requestReauth(kind: SourceKind): Promise<void> {
    setBusy(kind);
    setMessages((prev) => ({ ...prev, [kind]: "requesting…" }));
    try {
      const response = await fetch(SOURCE_REAUTH_PATH, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ kind }),
      });
      const body: unknown = await response.json();
      const instruction =
        typeof body === "object" && body !== null && "instruction" in body
          ? String((body as { instruction: unknown }).instruction)
          : "re-auth requested";
      setMessages((prev) => ({
        ...prev,
        [kind]: response.status === 202 ? instruction : `refused (${response.status})`,
      }));
    } catch {
      setMessages((prev) => ({ ...prev, [kind]: "request failed" }));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <AppNav current="sources" operator={operator} role={role} />
      <main className="console">
        <header className="console-header">
          <h1>Source Connections</h1>
        </header>
        <div className="callout callout-info" role="status">
          <span className="callout-body">
            <strong>Capability vs. Runtime</strong>
            <small>
              Capability = what a source is permitted to do; Runtime = its live state. Real
              activation is gated on each source&apos;s PoI, so a permitted source can still be
              blocked.
            </small>
          </span>
        </div>

        <section className="panel panel-wide">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Transport</th>
                  <th>Capability</th>
                  <th>Runtime</th>
                  <th>Account</th>
                  <th>Evidence</th>
                  <th>Re-auth</th>
                </tr>
              </thead>
              <tbody>
                {SOURCE_CONNECTIONS.map((source) => (
                  <tr key={source.kind}>
                    <td>{source.displayName}</td>
                    <td>{source.transport}</td>
                    <td>
                      <span className="chip chip-muted" title={source.capabilityNote}>
                        {source.capability}
                      </span>
                    </td>
                    <td>
                      <span className={runtimeChipClass(source.runtime)} title={source.runtimeDetail}>
                        {source.runtime}
                      </span>
                    </td>
                    <td>{source.accountLabelMasked}</td>
                    <td className="muted">{source.evidenceRef}</td>
                    <td>
                      {source.reauthAvailable ? (
                        <button
                          type="button"
                          disabled={busy === source.kind}
                          onClick={() => void requestReauth(source.kind)}
                        >
                          Request re-auth
                        </button>
                      ) : (
                        <span className="action-reason">key-configured</span>
                      )}
                      {messages[source.kind] === undefined ? null : (
                        <p className="reauth-msg">{messages[source.kind]}</p>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </main>
    </>
  );
}
