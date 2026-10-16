"""Read-only embedding health audit. Exit 2 on integrity issues; never writes profiles/vectors."""
import argparse
import asyncio
import json
from pathlib import Path
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import Database
from app.services.embedding_audit import audit_embeddings
from app.embeddings.encoder import build_embedding_input, encode_texts_async, get_encoder
from app.embeddings.profiles import ACTIVE_PROFILE


async def run(check_tokens=False, output=None, verify_model_sample=0):
    db = Database(get_settings())
    try:
        async with db.session() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            report = await audit_embeddings(session, check_tokens=check_tokens)
            samples = (await session.execute(text("""
                SELECT i.canonical_title,c.label,c.heading,c.chunk_text,e.embedding::text AS vector
                FROM legal_chunks c JOIN chunk_sets cs ON cs.id=c.chunk_set_id
                JOIN corpus_revisions r ON r.id=c.revision_id
                JOIN legal_versions v ON v.id=r.legal_version_id
                JOIN legal_instruments i ON i.id=v.instrument_id
                JOIN legal_chunk_embeddings e ON e.chunk_id=c.id
                JOIN embedding_profiles ep ON ep.id=e.profile_id AND ep.name=:profile
                WHERE r.review_state='approved' AND cs.chunker_version=:chunker
                ORDER BY md5(c.id::text) LIMIT :limit
            """), {"profile": ACTIVE_PROFILE.name,
                "chunker": __import__('app.services.legal_chunking', fromlist=['ChunkConfig']).ChunkConfig().chunker_version,
                "limit": verify_model_sample})).mappings().all() if verify_model_sample else []
        if samples:
            # An in-memory calibration probe. Existing vectors are never updated.
            try:
                inputs = [build_embedding_input(r["canonical_title"],r["label"],r["heading"],r["chunk_text"],ACTIVE_PROFILE) for r in samples]
                vectors = await encode_texts_async(inputs,ACTIVE_PROFILE,batch_size=1)
                similarities = []
                for row, vector in zip(samples,vectors,strict=True):
                    stored = json.loads(row["vector"])
                    cosine = sum(a*b for a,b in zip(stored,vector,strict=True)) / (
                        sum(a*a for a in stored)**.5 * sum(b*b for b in vector)**.5)
                    similarities.append(cosine)
                model = get_encoder(ACTIVE_PROFILE)
                report["model_probe"] = {"samples":len(samples),"cosine_similarities":similarities,
                    "loaded_dimension":model.get_sentence_embedding_dimension(),
                    "loaded_max_seq_length":model.max_seq_length,"device":str(model.device),
                    "writes":0,"ok":min(similarities)>=.9999}
                if not report["model_probe"]["ok"]:
                    report["issues"]["model_probe_mismatch"] = 1
                    report["ok"] = False
            except Exception as exc:
                report["model_probe"] = {"error":type(exc).__name__,"writes":0,"ok":False}
                report["ok"] = False
        content = json.dumps(report, indent=2, default=str)
        if output:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_text(content, encoding="utf-8")
        print(content)
        return report["ok"]
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-tokens", action="store_true", help="Check all inputs with the actual tokenizer; weights are not loaded")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-model-sample", type=int, default=0,
        help="Compare up to 10 existing vectors to in-memory model probes; never saves or replaces vectors")
    args = parser.parse_args()
    if not 0 <= args.verify_model_sample <= 10:
        parser.error("--verify-model-sample must be between 0 and 10")
    raise SystemExit(0 if asyncio.run(run(args.check_tokens, args.output, args.verify_model_sample)) else 2)
