import type { ReactNode } from "react";

import { navigate, usePath } from "./router";

const headerStyle = { borderBottom: "1px solid #ccc", padding: "0 1rem" } as const;
const barStyle = {
  display: "flex",
  flexWrap: "wrap",
  alignItems: "center",
  gap: "0.25rem",
} as const;
const linkStyle = {
  display: "inline-block",
  padding: "0.65rem 0.9rem",
  textDecoration: "none",
  color: "#0b57d0",
} as const;

const links = [
  { href: "/sources", label: "Sources" },
  { href: "/users", label: "User access" },
];

/** Responsive application shell: wrap-around navigation for signed-in pages. */
export default function AppShell({ children }: { children: ReactNode }) {
  const path = usePath();
  return (
    <>
      <header style={headerStyle}>
        <div style={barStyle}>
          <strong style={{ marginRight: "auto", padding: "0.65rem 0" }}>Smart RAG AI</strong>
          <nav aria-label="primary" style={{ display: "flex", flexWrap: "wrap" }}>
            {links.map((link) => (
              <a
                key={link.href}
                href={link.href}
                aria-current={path === link.href ? "page" : undefined}
                style={{
                  ...linkStyle,
                  fontWeight: path === link.href ? 700 : 400,
                }}
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
      {children}
    </>
  );
}
