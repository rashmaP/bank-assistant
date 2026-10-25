from __future__ import annotations

from .config import Settings
from .llm import LLM, AnthropicLLM


def build_llm(cfg: Settings) -> LLM:
    """Pick the model adapter from config (LLM_PROVIDER=anthropic|openai)."""
    if cfg.llm_provider == "openai":
        from .llm_openai import OpenAICompatLLM
        return OpenAICompatLLM(cfg.openai_api_key, cfg.openai_model, cfg.openai_base_url)
    return AnthropicLLM(cfg.anthropic_api_key, cfg.anthropic_model)
