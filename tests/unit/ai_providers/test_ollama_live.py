"""Optional live Ollama smoke test (Task 10).

Skips cleanly when Ollama is down or the profile models are not pulled; the
blocking suite in test_contracts.py needs none of it.
"""

import json
import urllib.request

import pytest

from ai_providers.contracts import GenerationRequest
from ai_providers.ollama import OllamaEmbeddingProvider, OllamaGenerationProvider
from foundation.config import Settings


def _profile_models() -> tuple[str, str] | None:
    settings = Settings.load()
    try:
        with urllib.request.urlopen(f"{settings.ollama_host.rstrip('/')}/api/tags", timeout=3) as r:
            pulled = {m["name"] for m in json.load(r).get("models", [])}
    except OSError:
        return None
    profile = settings.active_profile
    if not any(name == profile.chat_model or name.startswith(profile.chat_model + ":") for name in pulled):
        return None
    if not any(
        name == profile.embedding_model or name.startswith(profile.embedding_model + ":")
        for name in pulled
    ):
        return None
    return profile.chat_model, profile.embedding_model


def test_live_generation_and_embedding_smoke() -> None:
    models = _profile_models()
    if models is None:
        pytest.skip("Ollama unreachable or profile models not pulled")
    chat_model, embedding_model = models
    settings = Settings.load()
    host = settings.ollama_host

    generation = OllamaGenerationProvider(host, model=chat_model).generate(
        GenerationRequest(prompt="Reply with exactly: OK")
    )
    assert generation.model == chat_model
    assert generation.text.strip()

    embeddings = OllamaEmbeddingProvider(
        host, model=embedding_model, expected_dimensions=settings.active_profile.embedding_dimensions
    ).embed(["hello", "world"])
    assert embeddings.count == 2
    assert embeddings.dimensions == settings.active_profile.embedding_dimensions
