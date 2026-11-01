"""The kit critic's Claude client: structured output with vision, through OpenRouter or the Anthropic API.

The same client AnyScene's planner uses (github.com/apekshik/anyscene endpoint/planner.py `_client`, `strict_schema`),
cut down to what the critic needs.
"""

import os

import anthropic

MODEL = "claude-opus-5-5"  # on the Anthropic API
OPENROUTER_MODEL = "anthropic/claude-opus-5.5"


def strict_schema(model) -> dict:
    """Pydantic's JSON schema in the strict form structured outputs need: every object closed and fully required."""
    schema = model.model_json_schema()

    def walk(node):
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("title", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


def client() -> tuple[anthropic.Anthropic, str, dict]:
    """Anthropic directly when ANTHROPIC_API_KEY is set; otherwise OpenRouter's Anthropic-compatible endpoint with
    OPENROUTER_API_KEY. base_url is always explicit, because a stray ANTHROPIC_BASE_URL in the environment (Claude Code
    sets one) would otherwise redirect the SDK."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        extra = {"betas": ["server-side-fallback-2026-07-01", "fast-mode-2026-02-01"], "fallbacks": "default",
                 "speed": "fast"}
        return anthropic.Anthropic(base_url="https://api.anthropic.com"), MODEL, extra
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("set ANTHROPIC_API_KEY or OPENROUTER_API_KEY")
    # Fast mode passes through OpenRouter; server-side fallbacks are an Anthropic API feature.
    return (anthropic.Anthropic(base_url="https://openrouter.ai/api", auth_token=key, api_key=None),
            os.environ.get("OPENROUTER_MODEL", OPENROUTER_MODEL), {"betas": ["fast-mode-2026-02-01"], "speed": "fast"})
