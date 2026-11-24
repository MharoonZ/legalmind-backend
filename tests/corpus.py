"""A small in-memory publication for tests.

The wording is abridged from public Punjab statutes so that retrieval, context
and citation tests exercise realistic legal text. Vectors come from a
deterministic bag-of-words hash, so no model weights are needed.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import numpy as np

from app.embeddings.profiles import EmbeddingProfile
from app.retrieval.snapshot import Chunk, Instrument, ProvisionNode, PublicationSnapshot, Segment
from app.retrieval.text import STOPWORDS, stem, words

DIMENSION = 96
PROFILE = EmbeddingProfile(name="test-hash-v1", model_id="test/hash", dimension=DIMENSION,
                           max_tokens=512, input_recipe="title_label_heading_body")

GAMBLING = "The Punjab Prevention of Gambling Ordinance, 1978"
REPEAL = "The Public Gambling (Repeal) Act, 1985"
PRISONS = "The Prisons Act, 1894"
PRISONERS = "The Prisoners Act, 1900"
CONSUMER = "The Punjab Consumer Protection Act 2005"

# (kind, label, heading, body)
LAWS: dict[str, list[tuple[str, str | None, str, str]]] = {
    GAMBLING: [
        ("preamble", None, "Preamble",
         "WHEREAS it is expedient to provide for the prevention of gambling in the Province of the Punjab."),
        ("section", "1", "Short title, extent and commencement.",
         "1. Short title, extent and commencement.– (1) This Ordinance may be called the Punjab "
         "Prevention of Gambling Ordinance, 1978.\n(2) It extends to the whole of the Punjab."),
        ("section", "2", "Definitions.",
         "2. Definitions.– In this Ordinance, unless there is anything repugnant in the subject or context–\n"
         "(a) “common gaming house” means any house, room, tent, enclosure, vehicle, vessel or other place "
         "whatsoever in which any instruments of gaming are kept or used for gaming purposes;\n"
         "(b) “gaming” includes wagering or betting;\n"
         "(c) “instruments of gaming” includes any article used or intended to be used as a means of gaming."),
        ("section", "3", "Penalty for owning or keeping or having charge of a common gaming house.",
         "3. Penalty for owning or keeping or having charge of a common gaming house.– \n\n(1) \nWhoever– \n \n"
         "(a) being the owner or occupier of any house, room, tent or other place keeps or uses the same as a "
         "common gaming-house; or \n\n(b) has the care or management of any common gaming-house,\n\n"
         "shall be punishable with imprisonment for a term which shall not be less than one month nor \n"
         "more than one year, or with fine which shall not be less than one hundred rupees nor more than \n"
         "one thousand rupees, or with both. \n \n(2) In a prosecution under sub-section (1), it shall not be "
         "necessary to prove that the person \nfound playing was playing for any money, wager or stake."),
        ("section", "4", "Penalty for being found in common gaming-house.",
         "4. Penalty for being found in common gaming-house.– Whoever is found in any common gaming-house "
         "gaming or present for the purpose of gaming shall be punishable with imprisonment for a term which "
         "may extend to one year, or with fine which may extend to five hundred rupees, or with both."),
        ("section", "5", "Penalty for gaming in a public place.",
         "5. Penalty for gaming in a public place.– Whoever is found gaming in any public place, \n"
         "street or thoroughfare, \nshall be punishable with imprisonment for a term which may extend to \n\n"
         "1[three years], or with fine which may extend to 2[five thousand rupees], or with both."),
        ("section", "5-A", "Powers of police officers.",
         "5-A. Powers of police officers.– A police officer not below the rank of Sub-Inspector may arrest "
         "without warrant any person committing an offence under section 5."),
        ("section", "8", "Power to enter and search.",
         "8. Power to enter and search.– If a Magistrate has reason to believe that any place is used as a "
         "common gaming-house, he may authorise a police officer to enter and search that place."),
    ],
    REPEAL: [
        ("section", "1", "Short title and commencement.",
         "1. Short title and commencement.– (1) This Act may be called the Public Gambling (Repeal) Act, 1985."),
        ("section", "2", "Repeal.",
         "2. Repeal.– The Public Gambling Act, 1867 is hereby repealed in its application to the Punjab."),
    ],
    PRISONS: [
        ("section", "3", "Definitions.",
         "3. Definitions.– In this Act– (1) “prison” means any jail or place used permanently or temporarily "
         "for the detention of prisoners; (2) “criminal prisoner” means any prisoner duly committed to custody."),
        ("section", "9", "Officers not to have business dealings with prisoners.",
         "9. Officers not to have business dealings with prisoners.– No officer of a prison shall sell or let, "
         "nor shall any person in trust for or employed by him sell or let, or derive any benefit from selling "
         "or letting, any article to any prisoner."),
        ("section", "31", "Maintenance of certain prisoners from private sources.",
         "31. Maintenance of certain prisoners from private sources.– A civil prisoner or an unconvicted "
         "criminal prisoner shall be permitted to maintain himself, and to purchase food from private sources."),
    ],
    PRISONERS: [
        ("section", "3", "Officers in charge of prisons to detain persons duly committed to their custody.",
         "3. Officers in charge of prisons to detain persons duly committed to their custody.– The officer "
         "in charge of a prison shall receive and detain all persons duly committed to his custody."),
    ],
    CONSUMER: [
        ("section", "2", "Definitions.",
         "2. Definitions.– In this Act, unless there is anything repugnant in the subject or context–\n"
         "(c) “consumer” means a person or entity who buys or obtains on lease any product for a consideration;\n"
         "(j) “manufacturer” includes a person or entity who is in the business of manufacturing a product."),
        ("section", "4", "Liability for defective products.",
         "4. Liability for defective products.– (1) The manufacturer of a product shall be liable to a consumer "
         "for damages proximately caused by a characteristic of the product that renders the product defective "
         "when such damage arose from a reasonably anticipated use of the product by a consumer."),
        ("section", "19", "Receipt to be issued to the purchaser.",
         "19. Receipt to be issued to the purchaser.– Every manufacturer or trader who sells any goods shall "
         "issue to the purchaser a receipt showing the date of sale, description of goods sold, the quantity "
         "and price of the goods and the name and address of the seller."),
        ("section", "25", "Filing of Claims.",
         "25. Filing of Claims.– A claim for damages arising out of contravention of any provisions of this Act "
         "shall be filed before a Consumer Court, as provided in section 4."),
        ("section", "32", "Penalties.",
         "32. Penalties.– (1) Where a manufacturer fails to perform or in any way infringes the liabilities "
         "provided in sections 4 to 8, 11, 13, 14, 16, 18 to 22, he shall be punished with imprisonment which "
         "may extend to two years or with fine which may extend to hundred thousand rupees or with both."),
    ],
}


def identity(*parts: object) -> UUID:
    return uuid5(NAMESPACE_URL, "legalmind-test/" + "/".join(map(str, parts)))


def embed(text: str) -> np.ndarray:
    """Deterministic bag-of-stems vector; similar wording gives similar vectors."""
    vector = np.zeros(DIMENSION, dtype=np.float32)
    for word in words(text):
        if word in STOPWORDS:
            continue
        digest = hashlib.sha256(stem(word).encode()).digest()
        vector[int.from_bytes(digest[:4], "big") % DIMENSION] += 1.0
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        vector[0] = 1.0
        return vector
    return vector / norm


def fake_encode(text: str, profile: EmbeddingProfile) -> np.ndarray:
    return embed(text)


def make_snapshot(*, jurisdiction: str = "PK-PB") -> PublicationSnapshot:
    instruments, chunks, nodes, vectors = [], [], [], []
    for title, provisions in LAWS.items():
        law_id = identity("law", title)
        instruments.append(Instrument(
            id=law_id, title=title, citation_key=title.lower().replace(" ", "-")[:60], aliases=(),
            instrument_type="ordinance" if "Ordinance" in title else "act", jurisdiction_code=jurisdiction,
            legal_version_id=identity("version", title), edition_key="test-edition",
            applicability_status="unknown", revision_id=identity("revision", title),
            chunk_set_id=identity("chunk-set", title)))
        for ordinal, (kind, label, heading, body) in enumerate(provisions, 1):
            root_id = identity("provision", title, kind, label)
            path = f"f:test/{kind[0]}:{label or ordinal}"
            nodes.append(ProvisionNode(root_id, law_id, None, kind, label, heading, path, ordinal * 10,
                                       body, None))
            named = "Preamble" if kind == "preamble" else f"{kind.capitalize()} {label}"
            prefix = f"{title} — {named}" + ("" if kind == "preamble" else f" — {heading}")
            text = f"{prefix}\n\n{body}"
            start = len(prefix) + 2
            middle = start + len(body) // 2
            # Two source spans per provision, the second on the following page.
            segments = tuple(Segment(
                start=low, end=high, provision_id=root_id, kind=kind, label=label, reference_path=path,
                source_span_id=identity("span", title, label, low), file_id=identity("file", title),
                file_sha256=hashlib.sha256(title.encode()).hexdigest(), filename="source.pdf",
                source_url=None, page_index=page) for low, high, page in (
                    (start, middle, ordinal), (middle, len(text), ordinal + 1)))
            chunks.append(Chunk(
                index=len(chunks), id=identity("chunk", title, kind, label), instrument_id=law_id,
                root_provision_id=root_id, ordinal=ordinal, kind=kind, label=label, heading=heading,
                reference_path=path, sequence_in_root=1, root_total_chunks=1, prefix=prefix, text=text,
                segments=segments))
            vectors.append(embed(f"{title} {heading} {body}"))
    # Sub-provisions of Gambling section 3, so nested references can be resolved.
    law_id = identity("law", GAMBLING)
    root_id = identity("provision", GAMBLING, "section", "3")
    sub1 = identity("provision", GAMBLING, "s3", "sub1")
    nodes.extend([
        ProvisionNode(sub1, law_id, root_id, "subsection", "1", None, "f:test/s:3/sub:1", 41, "(1) Whoever–", None),
        ProvisionNode(identity("provision", GAMBLING, "s3", "sub1a"), law_id, sub1, "paragraph", "a", None,
                      "f:test/s:3/sub:1/p:a", 42, "(a) being the owner", None),
        ProvisionNode(identity("provision", GAMBLING, "s3", "sub2"), law_id, root_id, "subsection", "2", None,
                      "f:test/s:3/sub:2", 43, "(2) In a prosecution", None),
    ])
    return PublicationSnapshot(
        publication_id=identity("publication"), name="test-publication", manifest_sha256="a" * 64,
        profile=PROFILE, instruments=instruments, chunks=chunks, vectors=np.vstack(vectors), nodes=nodes)


def law_id(title: str) -> UUID:
    return identity("law", title)


def settings(**overrides):
    """Only the fields the retrieval and answer services read."""
    values = dict(
        legal_search_candidate_pool=50, legal_search_min_similarity=0.05, legal_search_rrf_k=60,
        legal_search_embedding_timeout_seconds=3,
        qa_rerank_mode="off", qa_rerank_model_name="test/reranker", qa_rerank_budget_ms=1500,
        qa_rerank_candidates=8, qa_rerank_max_pair_tokens=256, qa_rerank_batch_size=8,
        qa_rerank_device="cpu", qa_rerank_use_fp16=False, qa_rerank_top_k=4,
        qa_context_max_units=12, qa_context_max_chars=24000, qa_context_max_reference_sections=2,
        qa_context_max_referring_sections=2,
        qa_context_max_definition_sections=1, qa_history_max_turns=6, qa_rewrite_enabled=True,
        qa_rewrite_below_similarity=0.2, qa_rewrite_timeout_seconds=6, qa_request_timeout_seconds=60,
        qa_repair_attempts=1)
    values.update(overrides)
    return SimpleNamespace(**values)
