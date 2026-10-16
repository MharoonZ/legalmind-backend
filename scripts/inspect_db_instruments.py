import asyncio
from sqlalchemy import text
from app.core.config import get_settings
from app.db.session import Database

async def inspect():
    db = Database(get_settings())
    try:
        async with db.session() as session:
            # 1. Total overview of files & instruments & categories
            result = await session.execute(text("""
                SELECT 
                    f.original_filename, 
                    i.canonical_title,
                    i.citation_key,
                    j.code as jurisdiction_code,
                    COALESCE(string_agg(DISTINCT cat.name, ', '), 'None') as categories,
                    COUNT(DISTINCT c.id) as chunk_count
                FROM legal_files f
                JOIN legal_file_assignments lfa ON lfa.file_id = f.id
                JOIN legal_versions lv ON lv.id = lfa.legal_version_id
                JOIN legal_instruments i ON i.id = lv.instrument_id
                JOIN jurisdictions j ON j.id = i.jurisdiction_id
                LEFT JOIN instrument_categories ic ON ic.instrument_id = i.id
                LEFT JOIN categories cat ON cat.id = ic.category_id
                LEFT JOIN corpus_revisions r ON r.legal_version_id = lv.id
                LEFT JOIN chunk_sets cs ON cs.revision_id = r.id
                LEFT JOIN legal_chunks c ON c.chunk_set_id = cs.id
                GROUP BY f.original_filename, i.canonical_title, i.citation_key, j.code
                ORDER BY f.original_filename;
            """))
            rows = result.mappings().all()
            print(f"\n================ ALL UPLOADED FILES & INSTRUMENTS IN DATABASE ({len(rows)} TOTAL) ================")
            print(f"{'Filename':<12} | {'Chunks':<6} | {'Juris':<6} | {'Categories':<22} | {'Instrument Title'}")
            print("-" * 105)
            for r in rows:
                print(f"{r['original_filename']:<12} | {r['chunk_count']:<6} | {r['jurisdiction_code']:<6} | {r['categories'][:20]:<22} | {r['canonical_title']}")

            # 2. Specifically look up 477.pdf and Section 19
            print("\n================ EXACT DETAILS FOR 477.pdf (Section 19) ================")
            sec19_res = await session.execute(text("""
                SELECT 
                    f.original_filename,
                    i.canonical_title,
                    c.heading,
                    c.reference_path,
                    c.char_count,
                    c.chunk_text
                FROM legal_chunks c
                JOIN chunk_sets cs ON cs.id = c.chunk_set_id
                JOIN corpus_revisions r ON r.id = cs.revision_id
                JOIN legal_versions lv ON lv.id = r.legal_version_id
                JOIN legal_instruments i ON i.id = lv.instrument_id
                JOIN legal_file_assignments lfa ON lfa.legal_version_id = lv.id
                JOIN legal_files f ON f.id = lfa.file_id
                WHERE f.original_filename = '477.pdf' AND (c.heading ILIKE '%Section 19%' OR c.reference_path ILIKE '%19%')
                LIMIT 2;
            """))
            sec19_rows = sec19_res.mappings().all()
            for row in sec19_rows:
                print(f"File: {row['original_filename']}")
                print(f"Act Title: {row['canonical_title']}")
                print(f"Heading: {row['heading']}")
                print(f"Reference Path: {row['reference_path']}")
                print(f"Characters: {row['char_count']}")
                print(f"Content excerpt:\n{row['chunk_text'][:350]}...\n")
    finally:
        await db.dispose()

if __name__ == "__main__":
    asyncio.run(inspect())
