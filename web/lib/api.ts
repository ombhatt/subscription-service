import { accessToken } from "./supabase";
import type { ChatReply, Entitlements, Plan, SubscriptionSummary } from "./types";

/**
 * All calls go through /api, which next.config.mjs rewrites to the FastAPI
 * service. Same-origin, so no CORS.
 *
 * Authentication is the Supabase access token as a bearer credential. The
 * backend verifies its signature against the project's JWKS on every request,
 * so a token is proof of identity on its own — but the *tier* is always
 * resolved server-side from the subscription, never from a claim in the token,
 * because tokens are minted before a downgrade and stay valid after it.
 */

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

/** Thrown when there is no session at all, so callers can send the user to sign in. */
export class NotSignedIn extends ApiError {
  constructor() {
    super(401, null, "not signed in");
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await accessToken();
  if (!token) throw new NotSignedIn();

  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(init.headers ?? {}),
    },
    cache: "no-store",
  });

  const text = await response.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }

  if (!response.ok) {
    const message =
      (body as { message?: string; detail?: string })?.message ??
      (body as { detail?: string })?.detail ??
      `Request failed with ${response.status}`;
    throw new ApiError(response.status, body, message);
  }
  return body as T;
}

/** The pricing catalogue is public — it has to render before anyone signs up. */
export async function getPlans(): Promise<Plan[]> {
  const response = await fetch("/api/v1/billing/plans", { cache: "no-store" });
  if (!response.ok) throw new ApiError(response.status, null, "could not load plans");
  return response.json();
}

export function getEntitlements() {
  return request<Entitlements>("/v1/entitlements");
}

export function startCheckout(tier: string, interval: "monthly" | "annual") {
  return request<{ checkout_url: string; session_id: string }>("/v1/billing/checkout", {
    method: "POST",
    body: JSON.stringify({ tier, interval }),
  });
}

export function getSubscription() {
  return request<SubscriptionSummary>("/v1/billing/subscription");
}

export function openPortal() {
  return request<{ portal_url: string }>("/v1/billing/portal", { method: "POST" });
}

export function sendChat(model: string, message: string) {
  return request<ChatReply>("/v1/chat", {
    method: "POST",
    body: JSON.stringify({ model, message }),
  });
}
