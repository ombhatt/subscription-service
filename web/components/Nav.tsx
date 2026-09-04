"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { useUser } from "@/lib/user";

const LINKS = [
  { href: "/", label: "Plans" },
  { href: "/billing", label: "Billing" },
  { href: "/chat", label: "Chat" },
];

export default function Nav() {
  const pathname = usePathname();
  const router = useRouter();
  const { email, ready, signOut } = useUser();

  async function handleSignOut() {
    await signOut();
    router.push("/");
    router.refresh();
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
          {!ready ? (
            <span className="who">…</span>
          ) : email ? (
            <>
              <span className="who">{email}</span>
              <button onClick={handleSignOut}>Sign out</button>
            </>
          ) : (
            <Link className="btn" href="/login">
              Sign in
            </Link>
          )}
        </div>
      </div>
    </nav>
  );
}
