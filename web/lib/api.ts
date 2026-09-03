import type { ChatReply, Entitlements, Plan } from "./types";

/**
 * All calls go through /api, which next.config.mjs rewrites to the FastAPI
 * service. Same-origin, so no CORS.
 *
 * The X-User-Id header is the development authentication stub -- the backend
 * refuses to accept it when ENVIRONMENT=production. When real auth lands, this
 * is the single place that changes: send a session cookie or bearer token
 * instead, and delete `userId` from every call site's concern.
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

async function request<T>(path: string, userId: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": userId,
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

export function getPlans(userId: string) {
  return request<Plan[]>("/v1/billing/plans", userId);
}

export function getEntitlements(userId: string) {
  return request<Entitlements>("/v1/entitlements", userId);
}

export function startCheckout(
  userId: string,
  tier: string,
  interval: "monthly" | "annual",
) {
  return request<{ checkout_url: string; session_id: string }>("/v1/billing/checkout", userId, {
    method: "POST",
    body: JSON.stringify({ tier, interval }),
  });
}

export function openPortal(userId: string) {
  return request<{ portal_url: string }>("/v1/billing/portal", userId, { method: "POST" });
}

export function sendChat(userId: string, model: string, message: string) {
  return request<ChatReply>("/v1/chat", userId, {
    method: "POST",
    body: JSON.stringify({ model, message }),
  });
}
