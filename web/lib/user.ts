"use client";

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

/**
 * The development stand-in for a session.
 *
 * A real app reads this from an auth provider; here it is a name in
 * localStorage so you can switch between customers and watch their
 * entitlements differ. Everything downstream treats it as an opaque user id,
 * which is what it will be once auth is real.
 *
 * This is a *shared* store rather than per-component state: with useState in
 * each caller, switching user in the nav re-rendered only the nav, and every
 * page kept querying the previous user.
 */

const STORAGE_KEY = "demo-user-id";
const DEFAULT_USER = "alice";

let current: string | null = null;
const listeners = new Set<() => void>();

function readStored(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || DEFAULT_USER;
  } catch {
    // Private browsing, or storage disabled.
    return DEFAULT_USER;
  }
}

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(onChange: () => void) {
  listeners.add(onChange);

  // Another tab switched user; keep this one in step.
  const onStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) {
      current = readStored();
      emit();
    }
  };
  window.addEventListener("storage", onStorage);

  return () => {
    listeners.delete(onChange);
    window.removeEventListener("storage", onStorage);
  };
}

function getSnapshot(): string {
  if (current === null) current = readStored();
  return current;
}

/** During SSR and hydration there is no localStorage; `ready` gates on this. */
function getServerSnapshot(): string {
  return DEFAULT_USER;
}

export function setUser(next: string) {
  const trimmed = next.trim();
  if (!trimmed || trimmed === current) return;
  current = trimmed;
  try {
    window.localStorage.setItem(STORAGE_KEY, trimmed);
  } catch {
    // Not fatal -- the switch just will not survive a reload.
  }
  emit();
}

export function useUser() {
  const userId = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  // False until mounted, so pages do not fetch for the server's default user
  // and then immediately refetch for the real one.
  const [ready, setReady] = useState(false);
  useEffect(() => setReady(true), []);

  const changeUser = useCallback((next: string) => setUser(next), []);

  return { userId, changeUser, ready };
}
