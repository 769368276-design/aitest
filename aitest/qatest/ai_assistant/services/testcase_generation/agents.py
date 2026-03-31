from __future__ import annotations

from typing import AsyncGenerator, List
import threading

try:
    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.base import TaskResult
    from autogen_agentchat.messages import ModelClientStreamingChunkEvent, MultiModalMessage as AGMultiModalMessage
    from autogen_core import Image as AGImage
except Exception:
    AssistantAgent = None
    TaskResult = None
    ModelClientStreamingChunkEvent = None
    AGMultiModalMessage = None
    AGImage = None


def require_autogen():
    if AssistantAgent is None:
        raise RuntimeError("缺少依赖 autogen-agentchat/autogen-core：请安装后再启用 AI 功能")


def create_agent(selected_model_client, system_message: str, name: str) -> AssistantAgent:
    require_autogen()
    return AssistantAgent(
        name=name,
        model_client=selected_model_client,
        system_message=system_message,
        model_client_stream=True,
    )


async def stream_agent(agent: AssistantAgent, task_message, cancel_event: threading.Event | None = None) -> AsyncGenerator[str, None]:
    if ModelClientStreamingChunkEvent is None or TaskResult is None:
        raise RuntimeError("缺少依赖 autogen-agentchat：请安装后再启用 AI 功能")
    stream = agent.run_stream(task=task_message)
    try:
        async for event in stream:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    await stream.aclose()
                except Exception:
                    pass
                break
            if isinstance(event, ModelClientStreamingChunkEvent):
                yield event.content
            elif isinstance(event, TaskResult):
                break
    finally:
        try:
            await stream.aclose()
        except Exception:
            pass


async def run_agent_to_text(agent: AssistantAgent, task_message, cancel_event: threading.Event | None = None) -> str:
    parts: List[str] = []
    async for chunk in stream_agent(agent, task_message, cancel_event=cancel_event):
        parts.append(chunk)
    return "".join(parts)


__all__ = [
    "AGImage",
    "AGMultiModalMessage",
    "create_agent",
    "require_autogen",
    "run_agent_to_text",
    "stream_agent",
]

