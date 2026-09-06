import type { Metadata } from "next";

import Nav from "@/components/Nav";
import "./globals.css";

export const metadata: Metadata = {
  title: "Subscriptions",
  description: "Free, Plus and Pro on Stripe Billing",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/* First thing in the tab order: lets a keyboard user jump the nav
            instead of tabbing through it on every page. Visually hidden until
            focused -- see .skip-link in globals.css. */}
        <a className="skip-link" href="#main">
          Skip to main content
        </a>
        <Nav />
        <main id="main" tabIndex={-1}>
          {children}
        </main>
      </body>
    </html>
  );
}
