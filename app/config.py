from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    # --- auth ---
    # Supabase project URL, e.g. https://abcdefgh.supabase.co. Sessions are JWTs
    # signed with the project's asymmetric key and verified against its JWKS.
    supabase_url: str = ""
    # The audience Supabase stamps on an access token for a signed-in user.
    supabase_jwt_audience: str = "authenticated"
    # Supabase caches JWKS at the edge for ten minutes and advises against
    # holding them longer -- a revoked key has to stop working promptly.
    jwks_cache_seconds: int = 600

    # --- timeouts ---
    # Every one of these exists because the default is 'wait indefinitely',
    # and an unbounded wait somewhere is how one slow dependency becomes an
    # outage. The Stripe one matters most: sync holds a row lock across that
    # call, so its timeout is also the ceiling on how long other events for
    # the same customer are blocked.
    stripe_timeout_seconds: float = 10.0
    # How long a *waiter* blocks for that row lock before giving up. Failing
    # fast returns 500 and Stripe retries, which is far better than workers
    # piling up on a lock that a degraded Stripe is holding.
    db_lock_timeout_seconds: float = 5.0
    db_command_timeout_seconds: float = 15.0
    redis_timeout_seconds: float = 2.0

    # --- observability ---
    # JSON by default because that is what a log aggregator needs and the
    # safe default is the production one. Set LOG_JSON=false locally if you
    # would rather read it.
    log_json: bool = True
    log_level: str = "INFO"

    # --- feature flags ---
    # Empty means flags are not configured and every read uses the compiled-in
    # default in app/flags.py. That is the ordinary local and test path, not an
    # error.
    growthbook_client_key: str = ""
    growthbook_api_host: str = "https://cdn.growthbook.io"
    growthbook_timeout_seconds: float = 3.0

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/subscriptions"
    # Migrations need to CREATE and ALTER; the running service does not. Point
    # this at an admin credential and DATABASE_URL at the scoped app role, and
    # a leaked runtime credential -- or a SQL injection that finds one -- cannot
    # drop a table. Unset, migrations fall back to DATABASE_URL, which is the
    # single-credential setup and still what local development uses.
    migration_database_url: str = ""

    @property
    def alembic_url(self) -> str:
        return self.migration_database_url or self.database_url
    redis_url: str | None = "redis://localhost:6379/0"
    admin_api_key: str = "change-me-in-prod"

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    # Leave empty to use the account's default API version. Pin it once you have
    # verified the version you tested against; a wrong value breaks every call.
    stripe_api_version: str = ""
    # Card-required trial on paid checkouts. 0 disables trials entirely.
    trial_period_days: int = 0
    # Charge tax through Stripe Tax. Requires an origin address on the account.
    automatic_tax: bool = True

    # The portal decides for itself which plans a subscriber may switch to, and
    # it only knows what its configuration lists. Unset, the account's default
    # configuration applies -- which is how "Update your subscription" ended up
    # offering the plan the customer was already on and nothing else. Create one
    # from this repo's catalogue with `python -m scripts.configure_portal`.
    stripe_portal_configuration_id: str = ""

    stripe_price_plus_monthly: str = ""
    stripe_price_plus_annual: str = ""
    stripe_price_pro_monthly: str = ""
    stripe_price_pro_annual: str = ""

    checkout_success_url: str = "http://localhost:3000/billing/success"
    checkout_cancel_url: str = "http://localhost:3000/billing"
    portal_return_url: str = "http://localhost:3000/billing"

    # --- abuse control ---
    # POST /v1/billing/contact-sales is the only unauthenticated write, and
    # every accepted request inserts a row. Counted per caller, per window; 0
    # disables a window. Generous on purpose -- a real person fills this form
    # once, and anyone hitting five in an hour is testing or scripting it.
    contact_sales_per_hour: int = 5
    contact_sales_per_day: int = 20
    # Read the caller's address from X-Forwarded-For. Off by default because
    # the header is forgeable when nothing in front rewrites it. Turn it on
    # wherever a proxy or CDN terminates TLS, or every caller shares one budget.
    trust_proxy_headers: bool = False

    # --- operator alerts ---
    # Email to whoever acts on billing problems a job finds but must not fix
    # itself; today, subscriptions still charging a deleted account. Plain SMTP,
    # so any provider works (Resend, Postmark, SES, Gmail). Unset SMTP_HOST,
    # ALERT_EMAIL_TO or ALERT_EMAIL_FROM means not configured: reconcile then
    # exits 1 when it has something to report, rather than dropping it.
    alert_email_to: str = ""  # comma-separated
    alert_email_from: str = ""
    smtp_host: str = ""
    # 587 with STARTTLS is the common case; 465 means TLS from the first byte.
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_timeout_seconds: float = 10.0

    dunning_grace_days: int = 7
    entitlement_cache_ttl: int = 60

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
