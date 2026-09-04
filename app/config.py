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

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/subscriptions"
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

    stripe_price_plus_monthly: str = ""
    stripe_price_plus_annual: str = ""
    stripe_price_pro_monthly: str = ""
    stripe_price_pro_annual: str = ""

    checkout_success_url: str = "http://localhost:3000/billing/success"
    checkout_cancel_url: str = "http://localhost:3000/billing"
    portal_return_url: str = "http://localhost:3000/billing"

    dunning_grace_days: int = 7
    entitlement_cache_ttl: int = 60

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
