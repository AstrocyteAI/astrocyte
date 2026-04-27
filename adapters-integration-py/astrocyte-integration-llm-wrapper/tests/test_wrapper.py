from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from astrocyte.types import MemoryHit, RecallResult, RetainResult

from astrocyte_integration_llm_wrapper import AstrocyteMemoryWrapper, MemoryWrapperConfig


class FakeCompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Calvin prefers dark mode.",
                    }
                }
            ]
        }


@dataclass
class FakeBrain:
    retained: list[dict[str, Any]]

    async def recall(self, query: str, **kwargs: Any) -> RecallResult:
        assert query == "What does Calvin prefer?"
        return RecallResult(
            hits=[MemoryHit(text="Calvin prefers dark mode.", score=0.99)],
            total_available=1,
            truncated=False,
        )

    async def retain(self, content: str, **kwargs: Any) -> RetainResult:
        self.retained.append({"content": content, **kwargs})
        return RetainResult(stored=True, memory_id="m1")


@pytest.mark.asyncio
async def test_wrapper_injects_recall_and_retains_exchange() -> None:
    completions = FakeCompletions()
    brain = FakeBrain(retained=[])
    wrapper = AstrocyteMemoryWrapper(
        completions,
        brain=brain,  # type: ignore[arg-type]
        config=MemoryWrapperConfig(bank_id="user-calvin"),
    )

    response = await wrapper.create(
        model="gpt-test",
        messages=[{"role": "user", "content": "What does Calvin prefer?"}],
    )

    assert response["choices"][0]["message"]["content"] == "Calvin prefers dark mode."
    assert completions.kwargs is not None
    messages = completions.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert "Calvin prefers dark mode." in messages[0]["content"]
    assert brain.retained == [
        {
            "content": "User: What does Calvin prefer?\nAssistant: Calvin prefers dark mode.",
            "bank_id": "user-calvin",
            "context": None,
            "content_type": "conversation",
            "source": "llm-wrapper",
            "tags": ["llm-wrapper"],
        }
    ]


@pytest.mark.asyncio
async def test_wrapper_handles_object_style_response() -> None:
    class ObjectCompletions(FakeCompletions):
        async def create(self, **kwargs: Any) -> Any:
            self.kwargs = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="Object-style answer."),
                    )
                ]
            )

    brain = FakeBrain(retained=[])
    wrapper = AstrocyteMemoryWrapper(
        ObjectCompletions(),
        brain=brain,  # type: ignore[arg-type]
        config=MemoryWrapperConfig(bank_id="user-calvin"),
    )

    await wrapper.create(messages=[{"role": "user", "content": "What does Calvin prefer?"}])

    assert brain.retained[0]["content"].endswith("Assistant: Object-style answer.")
