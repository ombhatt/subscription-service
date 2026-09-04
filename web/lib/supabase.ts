"use client";

import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

/**
 * The Supabase browser client.
 *
 * The anon key is public by design — it identifies the project, and every
 * privilege it carries is enforced server-side. The key that must never reach
 * the browser is `service_role`, which bypasses row-level security entirely.
 *
 * `createBrowserClient` from `@supabase/ssr` keeps the session in cookies
 * rather than localStorage, which is what lets middleware see it and redirect
 * before a protected page renders.
 */

let client: SupabaseClient | null = null;

export function supabase(): SupabaseClient {
  if (client) return client;

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  if (!url || !anonKey) {
    throw new Error(
      "NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY must be set in web/.env.local",
    );
  }

  client = createBrowserClient(url, anonKey);
  return client;
}

/**
 * The access token to send to our API, or null when signed out.
 *
 * Read through `getSession` on every call rather than cached: the client
 * refreshes tokens in the background, and a cached copy would keep sending an
 * expired one until the page reloaded.
 */
export async function accessToken(): Promise<string | null> {
  const { data } = await supabase().auth.getSession();
  return data.session?.access_token ?? null;
}
