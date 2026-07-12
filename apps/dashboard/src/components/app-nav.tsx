/**
 * Shared top navigation for the Operations Console. Plain anchor links (no
 * client interactivity, no actions) with the current section highlighted so
 * an operator always knows where they are and how to move between screens.
 */

export type ConsoleSection =
  | "dashboard"
  | "sources"
  | "candidates"
  | "approvals"
  | "positions"
  | "emergency"
  | "research";

const SECTIONS: readonly { readonly id: ConsoleSection; readonly label: string; readonly href: string }[] = [
  { id: "dashboard", label: "Dashboard", href: "/" },
  { id: "sources", label: "Sources", href: "/sources" },
  { id: "candidates", label: "Candidates", href: "/candidates" },
  { id: "approvals", label: "Approvals", href: "/approvals" },
  { id: "positions", label: "Positions", href: "/positions" },
  { id: "emergency", label: "Emergency", href: "/emergency" },
  { id: "research", label: "Research", href: "/research" },
];

export function AppNav({
  current,
  operator,
  role,
}: {
  current: ConsoleSection;
  operator: string;
  role: string;
}) {
  return (
    <nav className="appnav" aria-label="Console sections">
      <span className="appnav-brand">Trading Ops</span>
      <ul className="appnav-links">
        {SECTIONS.map((section) => (
          <li key={section.id}>
            <a
              className={`appnav-link${section.id === current ? " active" : ""}`}
              href={section.href}
              aria-current={section.id === current ? "page" : undefined}
            >
              {section.label}
            </a>
          </li>
        ))}
      </ul>
      <span className="appnav-right">
        <span className="env-pill" title="Paper observation only — no live trading">
          PAPER
        </span>
        <span className="operator">
          {operator} · {role}
        </span>
      </span>
    </nav>
  );
}
