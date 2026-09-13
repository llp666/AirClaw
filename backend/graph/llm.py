"""内网 LLM 接入。

PRD 要求模型服务兼容 OpenAI API 格式，故主路径是 `ChatOpenAI`。

本地原型使用的 agnes-2.5-flash 是**推理模型**，响应里把思考过程放在独立的
`reasoning_content` 字段（与 `content` 并列）。`langchain_openai` 默认不识别该字段，
会直接丢弃。`ReasoningChatOpenAI` 覆写两处转换函数把它捞出来，存入
`additional_kwargs["reasoning_content"]`，供前端「思考链」展示。

    非流式：_create_chat_result      —— 从 ChatCompletion 对象的 message 上取
    流式：  _convert_delta_to_message_chunk —— 从 StreamChunk 的 delta 上取

不使用推理模型的部署（如内网普通对话模型）不受影响：取不到该字段时行为与
原生 ChatOpenAI 完全一致。
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessageChunk

from config import get_settings

REASONING_KEY = "reasoning_content"


def _extract_reasoning(obj: Any, *attrs: str) -> str | None:
    """从对象或 dict 上按候选属性名取 reasoning_content。"""
    for attr in attrs:
        value = getattr(obj, attr, None)
        if value:
            return value
        if isinstance(obj, dict) and obj.get(attr):
            return obj[attr]
    return None


class ReasoningChatOpenAI(ChatOpenAI):
    """保留 reasoning_content 的 ChatOpenAI。"""

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)

        raw_choices = (
            response.get("choices", [])
            if isinstance(response, dict)
            else getattr(response, "choices", []) or []
        )
        for i, choice in enumerate(raw_choices):
            message = (
                choice.get("message")
                if isinstance(choice, dict)
                else getattr(choice, "message", None)
            )
            if message is None:
                continue
            reasoning = _extract_reasoning(message, REASONING_KEY, "reasoning")
            if reasoning and i < len(result.generations):
                result.generations[i].message.additional_kwargs[REASONING_KEY] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(
        self, chunk, default_chunk_class, base_generation_info
    ):
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation_chunk is None:
            return None

        choices = (
            chunk.get("choices", [])
            if isinstance(chunk, dict)
            else getattr(chunk, "choices", []) or []
        )
        if (
            choices
            and isinstance(generation_chunk.message, AIMessageChunk)
            and (
                reasoning := _extract_reasoning(
                    choices[0].get("delta", {})
                    if isinstance(choices[0], dict)
                    else getattr(choices[0], "delta", None),
                    REASONING_KEY,
                    "reasoning",
                )
            )
        ):
            generation_chunk.message.additional_kwargs[REASONING_KEY] = reasoning
        return generation_chunk


def build_llm(*, thinking: bool | None = None) -> ChatOpenAI:
    """按当前配置构造 LLM 实例。每次调用都新建，确保读到最新的 .env。

    thinking 显式传入时覆盖配置值。工具性调用（生成标题、压缩摘要）应传 False：
    推理型模型会把 max_tokens 大量消耗在 reasoning_content 上，导致正文为空。
    """
    settings = get_settings()
    enable_thinking = settings.llm_enable_thinking if thinking is None else thinking

    # chat_template_kwargs 是 vLLM / SGLang 系推理服务的通用开关。
    # 内网若用不识别该参数的模型，服务端会忽略它，不产生副作用。
    extra_body: dict[str, Any] = {
        "chat_template_kwargs": {
            "enable_thinking": enable_thinking,
        }
    }

    return ReasoningChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=settings.llm_request_timeout,
        streaming=True,
        extra_body=extra_body,
    )
