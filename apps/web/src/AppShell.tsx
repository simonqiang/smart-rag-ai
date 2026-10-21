import type { ReactNode } from "react";

import { navigate, usePath } from "./router";
import "./styles/tokens.css";

const links = [
  { href: "/ask", label: "Ask" },
  { href: "/sources", label: "Sources" },
  { href: "/activity", label: "Activity" },
  { href: "/analytics", label: "Analytics" },
  { href: "/users", label: "User access" },
  { href: "/settings/retention", label: "Retention" },
  { href: "/settings/ai-provider", label: "AI provider" },
  { href: "/settings/backup", label: "Backup" },
];

/** Responsive application shell: wrap-around navigation for signed-in pages. */
export default function AppShell({ children }: { children: ReactNode }) {
  const path = usePath();
  const needsMainLandmark = path === "/activity" || path === "/analytics";
  return (
    <>
      <header className="app-shell-header">
        <div className="app-shell-bar">
          <strong className="app-shell-brand">Smart RAG AI</strong>
          <nav aria-label="primary" className="app-shell-nav">
            {links.map((link) => (
              <a
                key={link.href}
                href={link.href}
                className="app-shell-link"
                aria-current={path === link.href ? "page" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  navigate(link.href);
                }}
              >
                {link.label}
              </a>
            ))}
          </nav>
        </div>
      </header>
      {needsMainLandmark ? <main className="app-shell-content">{children}</main> : children}
    </>
  );
}
