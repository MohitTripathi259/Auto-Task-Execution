"""
Raw Anthropic Messages API wrapper — used by the control plane only.
(Planner, risk scorer, policy generator, reporter)

The executor inside ECS uses Claude Code SDK separately.
"""

import base64
from typing import Any

import anthropic

from src.common.config import settings
from src.common.logging import get_logger

logger = get_logger(__name__)

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def invoke_text(
    system: str,
    user: str,
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> str:
    model = model or settings.CLAUDE_PLANNER_MODEL
    logger.debug("llm.invoke_text", model=model)

    response = get_client().messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )

    logger.debug(
        "llm.invoke_text.done",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

    content = response.content[0]
    if content.type != "text":
        raise ValueError(f"Unexpected content type: {content.type}")
    return content.text


def invoke_with_tools(
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str | None = None,
    max_tokens: int = 4096,
) -> anthropic.types.Message:
    model = model or settings.CLAUDE_PLANNER_MODEL
    logger.debug("llm.invoke_with_tools", model=model, tools=[t["name"] for t in tools])

    response = get_client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        tools=tools,  # type: ignore[arg-type]
    )

    logger.debug(
        "llm.invoke_with_tools.done",
        stop_reason=response.stop_reason,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    return response


def invoke_vision(
    system: str,
    user: str,
    image_paths: list[str],
    model: str | None = None,
    max_tokens: int = 4096,
) -> str:
    model = model or settings.CLAUDE_PLANNER_MODEL

    content: list[dict[str, Any]] = []
    for path in image_paths:
        with open(path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()
        ext = path.rsplit(".", 1)[-1].lower()
        media_type = f"image/{ext}" if ext in ("png", "jpg", "jpeg", "gif", "webp") else "image/png"
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        })
    content.append({"type": "text", "text": user})

    response = get_client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text  # type: ignore[union-attr]


def count_tokens(system: str, user: str, model: str | None = None) -> int:
    """Preflight token estimation to stay within budget."""
    model = model or settings.CLAUDE_PLANNER_MODEL
    response = get_client().messages.count_tokens(
        model=model,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.input_tokens
