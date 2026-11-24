"""Download the embedding and reranker weights the API loads at start-up.

    python -m scripts.pipeline.download_models
    python -m scripts.pipeline.download_models --skip-reranker

The API itself only ever reads models from the local Hugging Face cache, so a
server never starts a multi-gigabyte download in the middle of a request. Run
this once per machine (or bake it into the image) before starting the API.
"""
from __future__ import annotations

import argparse
import os

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.embeddings.profiles import ACTIVE_PROFILE

_WEIGHTS = ["*.json", "*.txt", "*.model", "model.safetensors", "pytorch_model.bin", "1_Pooling/*",
            "tokenizer*", "sentencepiece*"]


def _reranker_name() -> str:
    """The configured reranker, or the default when no settings exist yet (an image build)."""
    try:
        return get_settings().qa_rerank_model_name
    except ValidationError:
        return os.environ.get("QA_RERANK_MODEL_NAME") or Settings.model_fields["qa_rerank_model_name"].default


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-reranker", action="store_true",
                        help="Only fetch the embedding model (QA_RERANK_MODE=off needs nothing else)")
    args = parser.parse_args()
    from huggingface_hub import snapshot_download

    models = [ACTIVE_PROFILE.model_id]
    if not args.skip_reranker:
        models.append(_reranker_name())
    for name in models:
        path = snapshot_download(name, allow_patterns=_WEIGHTS)
        print(f"{name} -> {path}")


if __name__ == "__main__":
    main()
