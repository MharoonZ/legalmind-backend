"""Encoding safety, immutable profile identity, and unchanged historical inputs."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from app.embeddings import encoder
from app.embeddings.profiles import ACTIVE_PROFILE, assert_profile_matches


@pytest.mark.parametrize("field,value", [
    ("name", "another-profile"), ("model_id", "another-model"), ("dimension", 768),
    ("max_tokens", 512), ("input_recipe", "another-recipe"), ("normalize", False)])
def test_all_profile_parameters_are_checked(field, value):
    with pytest.raises(ValueError, match=field):
        assert_profile_matches(replace(ACTIVE_PROFILE, **{field: value}), ACTIVE_PROFILE)


def test_input_recipe_preserves_existing_complete_inputs():
    value = encoder.build_embedding_input("Act", "19", "Receipt", "Complete source", ACTIVE_PROFILE)
    assert value == "Act — 19 — Receipt\n\nComplete source"
    assert encoder.input_sha256(value) == encoder.input_sha256("Act — 19 — Receipt\n\nComplete source")


def test_old_character_truncation_is_rejected():
    with pytest.raises(encoder.EmbeddingInputError, match="Re-chunk"):
        encoder.build_embedding_input("Act", None, None, "x" * 25000, ACTIVE_PROFILE)


def test_token_budget_includes_special_tokens():
    class Tokenizer:
        def encode(self, value, **kwargs):
            assert kwargs == {"add_special_tokens": True, "truncation": False}
            return list(range(len(value) + 2))
    assert encoder.check_token_lengths(["abc"], Tokenizer(), 5) == [5]
    with pytest.raises(encoder.EmbeddingInputError, match="6 tokens"):
        encoder.check_token_lengths(["abcd"], Tokenizer(), 5)


@pytest.mark.parametrize("vector", [[0.0] * 1024, [1.0], [float("nan")] * 1024,
                                   [float("inf")] * 1024, [1.0] * 1024])
def test_invalid_vector_never_reaches_persistence(vector):
    with pytest.raises(ValueError):
        encoder.validate_vector(vector, ACTIVE_PROFILE)


def test_encoding_checks_lengths_before_model_forward(monkeypatch):
    class Tokenizer:
        def encode(self, *args, **kwargs):
            return [1] * 8193
    model = SimpleNamespace(tokenizer=Tokenizer(), encode=lambda *a, **kw: pytest.fail("must not encode truncated input"))
    monkeypatch.setattr(encoder, "get_encoder", lambda p: model)
    with pytest.raises(encoder.EmbeddingInputError):
        encoder.encode_texts(["short characters can tokenize differently"], ACTIVE_PROFILE)


def test_encoder_validates_output_count_and_dimension(monkeypatch):
    tokenizer = SimpleNamespace(encode=lambda *a, **k: [1, 2])
    model = SimpleNamespace(tokenizer=tokenizer, encode=lambda *a, **k: np.ones((1, 768)))
    monkeypatch.setattr(encoder, "get_encoder", lambda p: model)
    with pytest.raises(ValueError, match="1024"):
        encoder.encode_texts(["query"], ACTIVE_PROFILE)


def test_shared_model_forward_calls_are_serialized(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from time import sleep

    barrier, guard = Barrier(2), Lock()
    active = peak = 0

    def run_model(*args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        sleep(.02)
        with guard:
            active -= 1
        return np.eye(1, 1024)

    model = SimpleNamespace(tokenizer=SimpleNamespace(encode=lambda *a, **k: [1]), encode=run_model)
    monkeypatch.setattr(encoder, "get_encoder", lambda profile: model)

    def encode_query(query):
        barrier.wait(timeout=2)
        return encoder.encode_texts([query], ACTIVE_PROFILE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(encode_query, ("first", "second")))
    assert peak == 1 and outputs[0] == outputs[1]


def test_model_cache_distinguishes_configuration_and_disallows_remote_code(monkeypatch):
    import sys
    calls = []
    class Model:
        max_seq_length = 8192
        def __init__(self, model_id, **kwargs):
            calls.append((model_id, kwargs))
        def get_sentence_embedding_dimension(self):
            return 1024
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Model))
    monkeypatch.setattr(encoder, "_models", {})
    one = encoder.get_encoder(ACTIVE_PROFILE)
    assert encoder.get_encoder(ACTIVE_PROFILE) is one
    two = encoder.get_encoder(replace(ACTIVE_PROFILE, max_tokens=4096))
    assert two is not one and two.max_seq_length == 4096
    assert calls == [(ACTIVE_PROFILE.model_id, {"trust_remote_code": False, "local_files_only": True})] * 2


def test_valid_existing_embedding_retry_never_attempts_a_database_write():
    import asyncio
    from contextlib import asynccontextmanager
    from dataclasses import asdict
    from uuid import uuid4
    from app.embeddings.repository import EmbeddingRepository
    profile_id, chunk_id, revision_id = uuid4(), uuid4(), uuid4()
    existing = SimpleNamespace(chunk_id=chunk_id, revision_id=revision_id,
        input_sha256="a"*64, norm=1.0, dimension=1024)
    class Session:
        async def get(self, *args): return SimpleNamespace(id=profile_id, **asdict(ACTIVE_PROFILE))
        @asynccontextmanager
        async def begin(self): yield
        async def execute(self, statement):
            assert statement.is_select, "A healthy existing input must not reach INSERT triggers"
            return SimpleNamespace(all=lambda: [existing])
    class DB:
        @asynccontextmanager
        async def session(self): yield Session()
    result = asyncio.run(EmbeddingRepository(DB()).upsert_embeddings(profile_id,[{
        "chunk_id":chunk_id,"revision_id":revision_id,"input_sha256":"a"*64,
        "embedding":[1.0]+[0.0]*1023}]))
    assert result == 0


def test_internal_vector_diagnostic_uses_a_numeric_cosine_score():
    import asyncio
    from contextlib import asynccontextmanager
    from uuid import uuid4
    from sqlalchemy import Float
    from pgvector.sqlalchemy import Vector
    from app.embeddings.repository import EmbeddingRepository
    class Session:
        async def execute(self, statement):
            assert isinstance(statement.selected_columns["vector_score"].type, Float)
            for binding in statement.compile().binds.values():
                if isinstance(binding.type, Vector):
                    assert isinstance(binding.value, list), "A numeric score constant cannot use the vector binder"
            return SimpleNamespace(all=lambda: [])
    class DB:
        @asynccontextmanager
        async def session(self): yield Session()
    assert asyncio.run(EmbeddingRepository(DB()).vector_search([1.0]+[0.0]*1023,uuid4())) == []
