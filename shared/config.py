"""Settings loaded from the environment. One place, per ADR-0005."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gowa_base_url: str = "http://localhost:3000"
    gowa_basic_auth: str | None = None
    gowa_device_id: str | None = None
    gowa_webhook_secret: str = "dev-secret"
    # Comma-separated previous secret(s), still accepted during a
    # rotation window so gowa's config can be updated without a moment
    # of dropped webhooks (Plan Phase 6: "webhook secret rotation").
    gowa_webhook_secret_previous: str = ""
    wa_send_min_gap_s: float = 4.0

    database_url: str = "sqlite+aiosqlite:///./watcher.db"
    vault_root: str = "./vault"

    cms_base_url: str | None = None
    cms_token: str | None = None
    cms_seed_path: str = "~/projects/watcher-seed/members.json"

    proposal_expiry_hours: float = 24.0

    jev_base_url: str | None = None
    jev_api_key: str | None = None

    models_base_url: str = "https://openrouter.ai/api/v1"
    models_api_key: str | None = None
    worker_model_name: str = "z-ai/glm-5.3-flash"
    mentor_model_name: str = "anthropic/claude-sonnet-5"
    decision_confidence_threshold: float = 0.6

    batch_n: int = 40
    batch_t_minutes: float = 180.0
    batch_quiet_minutes: float = 5.0
    message_concat_window_minutes: float = 2.0
    thread_context_max_chars: int = 400
    # Structured ingestion (ADR-0012): one assignment call per chunk of
    # messages; a chunk is cut when its prompt would exceed this many
    # estimated tokens (shared.models.tokens) or this many messages.
    ingest_max_prompt_tokens: int = 12000
    ingest_max_messages_per_call: int = 40
    # Model for the assignment + thread-update calls; None = the Worker.
    ingest_model_name: str | None = None
    thread_stale_days: float = 3.0
    thread_silent_days: float = 7.0
    lapis_base_url: str = "https://lapis.dvenom.in"
    lapis_vault_id: str = "aries"
    lapis_token: str | None = None
    bot_mention_name: str = "watcher"

    # Observability (ADR-0013). Metrics always go to SQLite; these tune
    # what's reported to WhatsApp and whether traces are exported.
    #
    # An ingest run that processed fewer than this many messages posts
    # no report - the tick fires every 2 minutes and most ticks do
    # nothing, so 0 here means ~720 messages/day in the logs channel.
    obs_report_min_messages: int = 1
    # Jev bills per decision and reports no tokens, so its cost is this
    # configured estimate (ADR-0006 cites ~$0.0004), flagged
    # cost_source="estimated" so a dashboard can tell it from a measured
    # OpenRouter cost.
    jev_cost_per_call: float = 0.0004
    otel_enabled: bool = True
    # OTLP/HTTP traces endpoint; empty disables export even when
    # otel_enabled. Phoenix listens on :6006 (HTTP) in docker-compose.
    otel_endpoint: str = "http://phoenix:6006/v1/traces"
    otel_service_name: str = "watcher"
    # Spans carry prompts and message text. True is the point of tracing
    # and matches ADR-0012's keep-the-PII stance for this internal
    # deployment - but it does put WhatsApp content in the trace store.
    otel_include_content: bool = True


settings = Settings()
