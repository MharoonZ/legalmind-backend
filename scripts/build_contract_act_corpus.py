"""Rebuild the draft corpus JSON from the pinned source PDF.

Run from the backend root:
    python scripts/build_contract_act_corpus.py

The extraction is deterministic.  It never claims legal review or edits an
existing database.  Inspect the generated workbook against the PDF before
promoting this corpus for user-facing retrieval.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# A direct script invocation places scripts/ on sys.path, not the backend root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.corpus.extractor import ContractActExtractor  # noqa: E402


def main() -> None:
    pdf = ROOT / "data" / "sources" / "contract_act_1872_pakistan_code.pdf"
    output = ROOT / "data" / "contract_act_1872_sections_1_30.json"
    corpus = ContractActExtractor().extract(pdf)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(corpus.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(corpus.sections)} draft sections to {output}")
    print(f"PDF SHA-256: {corpus.pdf_sha256}")
    print(f"Corpus SHA-256: {corpus.corpus_sha256}")


if __name__ == "__main__":
    main()
