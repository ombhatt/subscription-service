"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { supabase } from "@/lib/supabase";

type Mode = "sign-in" | "sign-up";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/billing";

  const [mode, setMode] = useState<Mode>("sign-in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [checkYourEmail, setCheckYourEmail] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);

    try {
      if (mode === "sign-up") {
        const { data, error } = await supabase().auth.signUp({ email, password });
        if (error) throw error;
        // With email confirmation on, there is no session yet and the user has
        // to click a link. Saying so beats a silent no-op.
        if (!data.session) {
          setCheckYourEmail(true);
          return;
        }
      } else {
        const { error } = await supabase().auth.signInWithPassword({ email, password });
        if (error) throw error;
      }
      router.push(next);
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (checkYourEmail) {
    return (
      <>
        <h1>Check your email</h1>
        <p className="lede">
          We sent a confirmation link to <strong>{email}</strong>. Open it and you&apos;ll be
          signed in.
        </p>
      </>
    );
  }

  return (
    <>
      <h1>{mode === "sign-in" ? "Sign in" : "Create an account"}</h1>
      <p className="lede">
        {mode === "sign-in"
          ? "Every account starts on the Free plan — no card needed."
          : "You'll start on Free straight away. Upgrade whenever you want."}
      </p>

      {error && (
        <div className="banner error">
          <strong>That didn&apos;t work.</strong> {error}
        </div>
      )}

      <div className="card" style={{ maxWidth: 420 }}>
        <form onSubmit={submit} className="stack" style={{ gap: 12 }}>
          <label className="field">
            <span>Email</span>
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>

          <label className="field">
            <span>Password</span>
            <input
              type="password"
              autoComplete={mode === "sign-in" ? "current-password" : "new-password"}
              required
              minLength={8}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>

          <button className="primary" type="submit" disabled={busy}>
            {busy ? "Working…" : mode === "sign-in" ? "Sign in" : "Create account"}
          </button>
        </form>

        <p className="muted" style={{ marginTop: 14, marginBottom: 0 }}>
          {mode === "sign-in" ? "No account yet? " : "Already have an account? "}
          <button
            style={{ border: "none", background: "none", padding: 0, color: "var(--accent)" }}
            onClick={() => {
              setMode(mode === "sign-in" ? "sign-up" : "sign-in");
              setError(null);
            }}
          >
            {mode === "sign-in" ? "Create one" : "Sign in"}
          </button>
        </p>
      </div>
    </>
  );
}

export default function LoginPage() {
  // useSearchParams needs a Suspense boundary to prerender.
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <LoginForm />
    </Suspense>
  );
}
