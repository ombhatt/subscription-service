"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { useUser } from "@/lib/user";

const LINKS = [
  { href: "/", label: "Plans" },
  { href: "/billing", label: "Billing" },
  { href: "/chat", label: "Chat" },
];

export default function Nav() {
  const pathname = usePathname();
  const { userId, changeUser, ready } = useUser();
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    changeUser(draft);
    setEditing(false);
    setDraft("");
  }

  return (
    <nav className="nav">
      <div className="nav-inner">
        <span className="brand">Subscriptions</span>
        {LINKS.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            className={pathname === link.href ? "active" : undefined}
          >
            {link.label}
          </Link>
        ))}

        <div className="nav-right">
          {/* Stands in for a session. Switching users is how you watch two
              customers resolve to different entitlements against one backend. */}
          {editing ? (
            <form onSubmit={submit} className="inline">
              <input
                className="compact"
                autoFocus
                value={draft}
                placeholder="user id"
                onChange={(event) => setDraft(event.target.value)}
                aria-label="Switch to user id"
              />
              <button type="submit">Switch</button>
            </form>
          ) : (
            <>
              <span className="who">{ready ? userId : "…"}</span>
              <button
                onClick={() => {
                  setDraft(userId ?? "");
                  setEditing(true);
                }}
              >
                Switch user
              </button>
            </>
          )}
        </div>
      </div>
    </nav>
  );
}
