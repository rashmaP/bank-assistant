from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"))
    bank_base_url: str = field(default_factory=lambda: os.environ.get("BANK_BASE_URL", "http://localhost:8001"))
    bank_service_key: str = field(default_factory=lambda: os.environ.get("BANK_SERVICE_KEY", "dev-service-key"))
    auth_secret: str = field(default_factory=lambda: os.environ.get("AUTH_SECRET", "dev-only-secret"))
    demo_auth_enabled: bool = field(default_factory=lambda: os.environ.get("DEMO_AUTH_ENABLED", "true").lower() == "true")
    token_ttl_seconds: int = 3600
    max_message_chars: int = 1000
    max_agent_steps: int = 6
    max_history_turns: int = 12
    rate_limit_per_minute: int = 20
