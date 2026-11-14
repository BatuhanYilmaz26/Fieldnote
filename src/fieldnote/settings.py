"""Typed, fail-closed runtime configuration.

Dotenv loading is explicit: Settings(_env_file='.env'). This avoids silently
reading an unrelated working-directory file in workers or tests.
"""

from __future__ import annotations

import ipaddress
import re
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="", case_sensitive=False, extra="forbid",
        hide_input_in_errors=True, validate_default=True,
    )

    app_env: Literal["development", "test", "staging", "production"] = "development"
    development_mode: bool = True
    bind_host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8000
    auth_mode: Literal["local", "oidc"] = "local"
    local_owner_id: str = "local-developer"
    database_url: SecretStr | None = Field(default=None, repr=False)
    migration_database_url: SecretStr | None = Field(default=None, repr=False)
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    session_secret: SecretStr | None = Field(default=None, repr=False)
    allowed_origins: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1", "testserver")

    model_provider: Literal["fixture", "openrouter"] = "fixture"
    research_provider: Literal["fixture", "treg", "tavily", "tinyfish"] = "fixture"
    enable_external_calls: bool = False
    topic_discovery: Literal["fixture", "public_feeds"] = "fixture"
    openrouter_qualified: bool = False
    treg_qualified: bool = False
    tinyfish_free_qualified: bool = False
    tinyfish_api_key: SecretStr | None = Field(default=None, repr=False)
    tavily_qualified: bool = False
    openrouter_api_key: SecretStr | None = Field(default=None, repr=False)
    openrouter_model: str = "openrouter/free"
    openrouter_base_url: Literal["https://openrouter.ai/api/v1"] = "https://openrouter.ai/api/v1"
    model_policy: Literal["free_only", "budgeted"] = "free_only"
    model_allowlist: tuple[str, ...] = ("openrouter/free",)
    openrouter_provider_allowlist: tuple[str, ...] = ()
    provider_data_collection: Literal["deny", "allow"] = "deny"
    provider_require_parameters: bool = True
    treg_token: SecretStr | None = Field(default=None, repr=False)
    treg_org: str | None = None
    treg_base_url: Literal["https://treg.to"] = "https://treg.to"
    treg_routing_policy: Literal["free_only", "budgeted"] = "free_only"
    treg_max_cost: str | None = None
    tavily_api_key: SecretStr | None = Field(default=None, repr=False)

    max_run_seconds: Annotated[int, Field(ge=1, le=3600)] = 600
    max_model_calls: Annotated[int, Field(ge=1, le=128)] = 32
    max_review_rounds: Annotated[int, Field(ge=0, le=2)] = 2
    model_max_concurrency: Annotated[int, Field(ge=1, le=16)] = 3
    provider_rpm: Annotated[int, Field(ge=1)] = 1
    provider_tpm: Annotated[int, Field(ge=1)] = 16_000
    user_daily_calls: Annotated[int, Field(ge=1, le=10_000)] = 100
    provider_circuit_failures: Annotated[int, Field(ge=1, le=20)] = 3
    provider_circuit_seconds: Annotated[int, Field(ge=1, le=600)] = 30
    graph_version: str = "v1"
    max_user_nonterminal_runs: Annotated[int, Field(ge=1, le=20)] = 2
    max_global_nonterminal_runs: Annotated[int, Field(ge=1, le=10_000)] = 100
    max_request_bytes: Annotated[int, Field(ge=1024, le=32_768)] = 32_768
    max_research_queries: Annotated[int, Field(ge=1, le=4)] = 4
    max_results_per_query: Annotated[int, Field(ge=1, le=5)] = 5
    max_extracted_pages: Annotated[int, Field(ge=1, le=8)] = 8
    max_evidence_tokens: Annotated[int, Field(ge=1024, le=12_000)] = 12_000
    max_research_calls: Annotated[int, Field(ge=1, le=100)] = 12
    research_provider_rpm: Annotated[int, Field(ge=1, le=1000)] = 12
    research_provider_daily_requests: Annotated[int, Field(ge=1, le=100_000)] = 100
    research_provider_daily_urls: Annotated[int, Field(ge=1, le=100_000)] = 100
    research_user_daily_calls: Annotated[int, Field(ge=1, le=100_000)] = 100
    run_budget_usd: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=6)] = Decimal("0")
    user_daily_budget_usd: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=6)] = Decimal("0")
    job_lease_seconds: Annotated[int, Field(ge=3, le=600)] = 90
    job_heartbeat_seconds: Annotated[int, Field(ge=1, le=120)] = 20
    max_job_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    worker_poll_seconds: Annotated[float, Field(gt=0, le=60)] = 1.0
    worker_stale_seconds: Annotated[int, Field(ge=5, le=600)] = 60
    database_pool_min_size: Annotated[int, Field(ge=0, le=20)] = 1
    database_pool_max_size: Annotated[int, Field(ge=1, le=50)] = 5
    database_pool_timeout: Annotated[float, Field(gt=0, le=60)] = 5.0
    connect_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 5.0
    research_timeout_seconds: Annotated[float, Field(gt=0, le=90)] = 30.0
    model_timeout_seconds: Annotated[float, Field(gt=0, le=180)] = 90.0
    event_retention_days: Annotated[int, Field(ge=1, le=365)] = 7
    run_retention_days: Annotated[int, Field(ge=1, le=365)] = 30
    source_retention_days: Annotated[int, Field(ge=1, le=365)] = 7
    deleted_run_grace_hours: Annotated[int, Field(ge=1, le=720)] = 24
    retention_batch_size: Annotated[int, Field(ge=1, le=1000)] = 100
    langgraph_strict_msgpack: bool = True

    worker_concurrency: Annotated[int, Field(ge=1, le=8)] = 1
    worker_drain_seconds: Annotated[int, Field(ge=1, le=120)] = 20
    scheduler_poll_seconds: Annotated[float, Field(gt=0, le=60)] = 2.0
    session_ttl_hours: Annotated[int, Field(ge=1, le=168)] = 8
    sse_heartbeat_seconds: Annotated[int, Field(ge=1, le=60)] = 15
    sse_poll_seconds: Annotated[float, Field(ge=0.1, le=5)] = 0.5
    max_observers_per_user: Annotated[int, Field(ge=1, le=20)] = 4
    metrics_token: SecretStr | None = Field(default=None, repr=False)
    log_format: Literal["json", "text"] = "text"

    model_max_input_tokens: Annotated[int, Field(ge=1024, le=128_000)] = 6_000
    model_max_output_tokens: Annotated[int, Field(ge=256, le=8192)] = 8192
    openrouter_input_price_per_million: Annotated[
        Decimal, Field(ge=0, max_digits=12, decimal_places=6)
    ] = Decimal("0")
    openrouter_output_price_per_million: Annotated[
        Decimal, Field(ge=0, max_digits=12, decimal_places=6)
    ] = Decimal("0")
    openrouter_schema_repairs: Annotated[int, Field(ge=0, le=1)] = 1
    treg_max_cost_micro: Annotated[int, Field(ge=0, le=1_000_000)] = 0
    research_max_chars_per_page: Annotated[int, Field(ge=1000, le=20_000)] = 20_000

    @field_validator("bind_host")
    @classmethod
    def nonblank_host(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char in value for char in "/\\?#@\r\n"):
            raise ValueError("BIND_HOST must be a host/IP, not a URL")
        return value

    @field_validator("graph_version")
    @classmethod
    def safe_graph_version(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
            raise ValueError("GRAPH_VERSION must be a short safe identifier")
        return value

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def postgres_dsn(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            parsed = urlsplit(value.get_secret_value())
            if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path.strip("/"):
                raise ValueError("A PostgreSQL connection URL with database name is required")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def explicit_origins(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                origin == "*" or parsed.scheme not in {"http", "https"}
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            ):
                raise ValueError("ALLOWED_ORIGINS requires exact HTTP(S) origins, never wildcards")
        return origins

    @field_validator("allowed_hosts")
    @classmethod
    def explicit_hosts(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        if not hosts or any("*" in host or not host.strip() for host in hosts):
            raise ValueError("ALLOWED_HOSTS requires explicit host names")
        return hosts

    @model_validator(mode="after")
    def valid_profile(self) -> Settings:
        if self.topic_discovery == "public_feeds" and not self.enable_external_calls:
            raise ValueError("Public topic feeds require ENABLE_EXTERNAL_CALLS=true")
        hosted = self.app_env in {"staging", "production"}
        if self.job_heartbeat_seconds * 2 >= self.job_lease_seconds:
            raise ValueError("JOB_HEARTBEAT_SECONDS must be less than half JOB_LEASE_SECONDS")
        if self.database_pool_min_size > self.database_pool_max_size:
            raise ValueError("Database pool minimum exceeds maximum")
        if self.database_pool_max_size < self.worker_concurrency + 1:
            raise ValueError("Database pool must reserve a connection for each worker plus the API")
        if self.model_max_output_tokens > self.provider_tpm:
            raise ValueError("Model output limit cannot exceed the shared per-minute token budget")
        if self.max_user_nonterminal_runs > self.max_global_nonterminal_runs:
            raise ValueError("Per-user admission cannot exceed global admission")
        if self.source_retention_days > self.run_retention_days or self.event_retention_days > self.run_retention_days:
            raise ValueError("Source/event retention cannot exceed run retention")
        if not self.langgraph_strict_msgpack:
            raise ValueError("Restricted checkpoint deserialization must remain enabled")

        if self.auth_mode == "local":
            try:
                loopback = ipaddress.ip_address(self.bind_host).is_loopback
            except ValueError:
                # Do not trust arbitrary DNS names to continue resolving to
                # loopback. The documented localhost name is the sole alias.
                loopback = self.bind_host.lower() == "localhost"
            if hosted or not self.development_mode or not loopback:
                raise ValueError("Local authentication requires development/test mode and loopback binding")
        if hosted:
            if self.development_mode or self.auth_mode != "oidc":
                raise ValueError("Hosted profiles forbid development mode and local authentication")
            if self.database_url is None:
                raise ValueError("DATABASE_URL is required for hosted profiles")
            if "fixture" in {self.model_provider, self.research_provider}:
                raise ValueError("Hosted profiles cannot use fixture providers")
            if "testserver" in self.allowed_hosts:
                raise ValueError("Hosted ALLOWED_HOSTS must exclude the test host")
        if self.auth_mode == "oidc":
            if not self.oidc_issuer or not self.oidc_client_id or self.session_secret is None:
                raise ValueError("OIDC issuer/client and SESSION_SECRET are required")
            parsed = urlsplit(self.oidc_issuer)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("OIDC_ISSUER requires a trusted HTTPS issuer URL")
            if len(self.session_secret.get_secret_value()) < 32:
                raise ValueError("SESSION_SECRET must contain at least 32 characters")
        if "fixture" in {self.model_provider, self.research_provider} and (
            hosted or not self.development_mode
        ):
            raise ValueError("Fixture providers require explicit development/test mode")

        if self.model_provider == "openrouter":
            if not self.enable_external_calls or not self.openrouter_qualified or self.openrouter_api_key is None:
                raise ValueError("OpenRouter requires ENABLE_EXTERNAL_CALLS, qualification and OPENROUTER_API_KEY")
            if not self.openrouter_api_key.get_secret_value().strip():
                raise ValueError("OPENROUTER_API_KEY must not be blank")
        if self.openrouter_model not in self.model_allowlist:
            raise ValueError("OPENROUTER_MODEL must occur in MODEL_ALLOWLIST")
        if self.model_policy == "free_only" and self.openrouter_model != "openrouter/free" and not self.openrouter_model.endswith(":free"):
            raise ValueError("Free-only policy forbids paid model IDs")
        if self.model_policy == "free_only" and (
            self.openrouter_input_price_per_million != 0
            or self.openrouter_output_price_per_million != 0
        ):
            raise ValueError("Free-only policy cannot carry a paid price snapshot")
        if self.model_policy == "budgeted" and (
            self.run_budget_usd <= 0 or self.user_daily_budget_usd <= 0
        ):
            raise ValueError("Budgeted model policy needs positive run and daily budgets")
        if self.model_policy == "budgeted" and (
            self.openrouter_input_price_per_million <= 0
            and self.openrouter_output_price_per_million <= 0
        ):
            raise ValueError("Budgeted model policy needs an explicit qualified price snapshot")
        if self.model_policy == "budgeted" and not self.openrouter_provider_allowlist:
            raise ValueError("Budgeted model policy needs an explicit provider allowlist")

        if self.research_provider == "tinyfish" and (
            not self.enable_external_calls or not self.tinyfish_free_qualified
            or self.tinyfish_api_key is None or not self.tinyfish_api_key.get_secret_value().strip()
        ):
            raise ValueError("TinyFish requires ENABLE_EXTERNAL_CALLS, a qualified free Search/Fetch contract and TINYFISH_API_KEY")
        if self.research_provider == "treg":
            if not self.enable_external_calls or not self.treg_qualified or self.treg_token is None:
                raise ValueError("Treg requires ENABLE_EXTERNAL_CALLS, qualification and TREG_TOKEN")
            if self.treg_routing_policy == "budgeted" and (
                self.treg_max_cost_micro <= 0 or self.run_budget_usd <= 0 or self.user_daily_budget_usd <= 0
            ):
                raise ValueError("Paid Treg routing needs a positive maximum cost and budgets")
            if self.treg_routing_policy == "free_only" and self.treg_max_cost_micro != 0:
                raise ValueError("Free-only Treg routing requires a zero route cost cap")
        if self.research_provider == "tavily" and (
            not self.enable_external_calls or not self.tavily_qualified or self.tavily_api_key is None
        ):
            raise ValueError("Tavily requires ENABLE_EXTERNAL_CALLS, qualification and TAVILY_API_KEY")
        return self

    def sanitized_summary(self) -> dict[str, object]:
        """An allowlisted operational summary, never a complete settings dump."""
        return {
            "app_env": self.app_env,
            "bind_host": self.bind_host,
            "port": self.port,
            "auth_mode": self.auth_mode,
            "database_configured": self.database_url is not None,
            "model_provider": self.model_provider,
            "research_provider": self.research_provider,
            "model_policy": self.model_policy,
            "external_calls_enabled": self.enable_external_calls,
            "max_run_seconds": self.max_run_seconds,
            "max_model_calls": self.max_model_calls,
            "max_review_rounds": self.max_review_rounds,
            "worker_concurrency": self.worker_concurrency,
            "worker_drain_seconds": self.worker_drain_seconds,
            "session_ttl_hours": self.session_ttl_hours,
            "log_format": self.log_format,
            "restricted_checkpoint_types": self.langgraph_strict_msgpack,
        }
