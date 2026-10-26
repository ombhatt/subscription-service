/**
 * Where to send someone after they sign in, from the `?next=` they arrived with.
 *
 * `next` is whatever the link said, and anyone can write the link. Handed to
 * `router.push` unchecked, `javascript:...` runs in this origin the moment the
 * victim signs in -- with the Supabase session cookie readable from script --
 * and `https://elsewhere` is a phishing page one sign-in away from ours. So only
 * a path on this origin is followed; anything else falls back.
 *
 * The check resolves against a placeholder origin rather than inspecting the
 * string, because the URL parser is what the browser will use: it drops tabs
 * and newlines and reads `\` as `/`, so `/\evil.com` and `/\t/evil.com` are both
 * `//evil.com` by the time they navigate.
 */

const PLACEHOLDER = "https://placeholder.invalid";

export function safeNext(next: string | null | undefined, fallback = "/billing"): string {
  if (!next || !next.startsWith("/")) return fallback;
  let url: URL;
  try {
    url = new URL(next, PLACEHOLDER);
  } catch {
    return fallback;
  }
  if (url.origin !== PLACEHOLDER) return fallback;
  // Check what is returned as well as what came in: dot segments can leave a
  // path that starts with `//` ("/.//evil.com" resolves to "//evil.com"), which
  // the browser then reads as another host.
  const path = `${url.pathname}${url.search}${url.hash}`;
  return new URL(path, PLACEHOLDER).origin === PLACEHOLDER ? path : fallback;
}
