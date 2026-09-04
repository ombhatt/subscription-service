"use client";

import type { User } from "@supabase/supabase-js";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { supabase } from "./supabase";

/**
 * The signed-in user, kept in step with Supabase's auth state.
 *
 * This replaced a `localStorage` name that stood in for a session. The shape it
 * exposes is deliberately the same — an id, and a `ready` flag — because
 * everything downstream only ever treated it as an opaque user id, which is
 * exactly what it still is. That id is now the Supabase user's UUID, and it is
 * what the subscriptions table keys on.
 */

export interface SessionState {
  user: User | null;
  userId: string | null;
  email: string | null;
  ready: boolean;
  signOut: () => Promise<void>;
}

export function useUser(): SessionState {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let active = true;

    supabase()
      .auth.getSession()
      .then(({ data }) => {
        if (!active) return;
        setUser(data.session?.user ?? null);
        setReady(true);
      })
      .catch(() => {
        if (active) setReady(true);
      });

    // Fires on sign-in, sign-out, and every silent token refresh, so a session
    // that expires in another tab does not leave this one looking signed in.
    const { data: listener } = supabase().auth.onAuthStateChange((_event, session) => {
      if (!active) return;
      setUser(session?.user ?? null);
      setReady(true);
    });

    return () => {
      active = false;
      listener.subscription.unsubscribe();
    };
  }, []);

  const signOut = useCallback(async () => {
    await supabase().auth.signOut();
  }, []);

  return {
    user,
    userId: user?.id ?? null,
    email: user?.email ?? null,
    ready,
    signOut,
  };
}

/**
 * Send signed-out visitors to the sign-in page.
 *
 * Deliberately client-side rather than Next middleware. Middleware would have
 * to call Supabase's `getUser()` on every navigation -- a network round trip
 * per page load -- and it was never the security boundary anyway: the API
 * verifies every token itself, so someone who skipped this would reach a page
 * whose every request 401s. This buys the redirect without the latency.
 */
export function useRequireSession(): SessionState {
  const state = useUser();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (state.ready && !state.userId) {
      router.replace(`/login?next=${encodeURIComponent(pathname)}`);
    }
  }, [state.ready, state.userId, router, pathname]);

  return state;
}
