# Corpus-wide legal PDF audit — 9 October 2026

This is a read-only audit of every registered legal PDF discovered from `legal_files`, not a fixed filename list. No extraction, revision, review decision, approval, chunk, embedding, or index was changed during this audit. The latest parser is `legal-structure-v11`; only 130.pdf and 131.pdf have persisted v11 drafts. The original 21 latest saved drafts remain v5–v10.

## Corpus summary

| Metric | Result |
|---|---:|
| Total PDFs audited | 23 |
| Total physical pages | 199 |
| PASS / ready for chunking | 0 |
| NEEDS HUMAN REVIEW | 20 |
| FAIL — confirmed persisted structural/source ownership error | 3 |
| Documents improved in a new persisted draft | 2 (130.pdf, 131.pdf) |
| Documents regressed in a read-only v11 preview | 5 (14.pdf, 17.pdf, 94.pdf, 477.pdf, 483.pdf) |
| Newly identified generic failure patterns | 6 (three persisted legacy patterns, three v11 preview patterns) |

All 23 original stored PDFs match their registered SHA-256 and physical page count. All 199 physical pages have saved extraction rows: 182 native-text pages and 17 textless/none pages. A read-only rerun of native block extraction reproduced the saved raw page text and raw blocks on every page. The page-to-block offsets and all 3,835 saved exact provision-to-block slices matched. There were **zero persistence/offset mismatches**. This is not proof that every word matches the visual PDF or that a slice belongs to the correct legal parent.

The latest saved drafts contain 516 section nodes, 682 subsections, 648 paragraphs, 611 source-note marker links, and 25 explicitly printed source-status labels (11 repealed, 9 omitted, 5 deleted). The three reviewed table regions in 112.pdf have no current lossless-table audit error. However, 17 pages have `blank_candidate`, 18 saved page outcomes are `needs_review` (including an ambiguous numbering glyph), 40 unresolved marker warnings occur in current previews, and all 23 latest revisions remain drafts. Local blank-mark events are not legal approval.

The blank candidates are 13.pdf p4 and p2 in each of 26, 37, 94, 97, 112, 127, 130, 131, 221, 329, 362, 393, 453, 483, 2712, and the 1867 PDF. The ambiguous numbering-glyph page is 112.pdf p4. These pages require an accountable reviewer decision even where an earlier local development mark-blank event suppresses an automatic finding.

Automated comparison covered **199/199 pages**. Direct visual inspection in this audit covered **32/199 pages across 11/23 PDFs**: all pages of 130.pdf and 131.pdf plus selected pages of 14, 17, 37, 94, 127, 190, 2712, 477, and 483. The other 167 pages were not freshly checked line by line against rendered images. The prior 21-document report was used as a baseline, not as a substitute for new visual verification.

## Eight-check result for every PDF

Codes: **A** = no issue found by automated checks, not a full visual sign-off; **V** = selected relevant source pages visually compared with the saved structure; **R** = unresolved source-note/marker review remains; **F** = confirmed error in the **persisted** draft; **—** = no relevant division or Schedule in the checked PDF. Columns are: **B** section boundaries; **H** subsection/paragraph hierarchy; **Ftn** footnotes/marker relationships; **Am** current wording and amendment/status separation; **C** Contents versus body; **X** cross-page continuation; **D** Schedules/Parts/Chapters; **S** exact spans, page references, and *ownership*. A result of A never means legal approval.

| PDF and instrument | Pages | Sections/subsections | Latest persisted revision (parser) | B | H | Ftn | Am | C | X | D | S | Remaining findings / review | Embedding readiness |
|---|---:|---:|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|---|---|
| [13.pdf](../uploads/legal/8b/8bfc899eb6fadd2a0ace95fdd1f08059c945d3c001a06434ef5955222f438801.pdf) — The Prisons Act, 1894 | 19 | 62/129 | `a1053c7a-2723-40c5-90be-34b67d7a5d90` (v10) | A | A | R | A | A | A | A | A | blank candidate; unresolved markers 8 | NEEDS_HUMAN_REVIEW |
| [14.pdf](../uploads/legal/c7/c761be3880a9f21e2854f74b1cb3755bf15c64990dc86e8cbcde3e285632eadc.pdf) — The Reformatory Schools Act, 1897 | 10 | 31/28 | `b2bd296a-fac5-4361-85ea-83e77e8f4fb9` (v10) | V | F | R | A | A | A | A | F | §22(2) absent from saved tree; v11 heading regression; unresolved markers 5; possible_number_gap 1; v11 preview regression | BLOCKED_BY_PARSER |
| [17.pdf](../uploads/legal/31/314ddc5eec94d35211c1cbf97bc071ab870101a13de0dec7e16ccba5b84efb9e.pdf) — The Prisoners Act, 1900 | 15 | 35/26 | `54c584a8-fe7a-4bb9-96fb-0444cb931eb2` (v10) | V | V | R | A | A | V | A | V | unresolved markers 8; possible_number_gap 2; v11 preview regression | NEEDS_HUMAN_REVIEW |
| [26.pdf](../uploads/legal/c8/c8c95cbbd44cc91326918e32512e5b711f766a213df3f991367b5fa2703762fb.pdf) — The Restriction of Habitual Offenders (Punjab) Act, 1918 | 6 | 17/11 | `252c9d23-5295-45f1-b3ec-0f34a4edbb3d` (v10) | A | A | A | A | A | A | A | A | blank candidate | NEEDS_HUMAN_REVIEW |
| [37.pdf](../uploads/legal/0c/0c9530e36ee07517e4f3147b400fddad52ddd61c6e952b8029ae0e462a1953d0.pdf) — The Good Conduct Prisoners' Probational Release Act, 1926 | 5 | 8/11 | `5b4c576a-4bf7-49c1-ad6e-d430b07edeff` (v5) | A | A | R | A | A | A | A | A | blank candidate; unresolved markers 2 | NEEDS_HUMAN_REVIEW |
| [38.pdf](../uploads/legal/ee/ee5a56bf0f2b4223816bc805068a5ccaf2848902279854ec064118962b0a82f2.pdf) — The Punjab Borstal Act, 1926 | 13 | 36/60 | `b473958e-a98f-408d-b914-7cd9a8486477` (v10) | A | A | R | A | A | A | A | A | unresolved markers 4 | NEEDS_HUMAN_REVIEW |
| [94.pdf](../uploads/legal/72/72b42aa266749c84b6f80e2c231c60ca029dfcc286ef073bc7dcfff79826504d.pdf) — The Punjab Vagrancy Ordinance, 1958 | 9 | 26/21 | `ff81cbd9-d794-4f57-8c45-47212e76f049` (v7) | V | V | A | A | A | V | A | V | blank candidate; post_list_continuation_in_child 4; v11 preview regression | NEEDS_HUMAN_REVIEW |
| [97.pdf](../uploads/legal/7b/7b665b8046d1ab5fd6db2afe950f9c43fba47f897a6e0e741a9b0da4ed490598.pdf) — The Prevention of Corruption Act (West Pakistan Extension) Ordinance, 1958 | 3 | 4/0 | `79c38d11-c2e1-46ff-be5b-f9c6735c0c22` (v5) | A | A | A | A | A | A | A | A | blank candidate | NEEDS_HUMAN_REVIEW |
| [112.pdf](../uploads/legal/f2/f2d382c7f7daa80515c041870cc876870ed48129726722c1f39550fd84ac1407.pdf) — The Punjab Control of Goondas Ordinance, 1959 | 12 | 34/32 | `dc9fb87b-1029-4dee-8895-d7cdd110891e` (v10) | A | A | R | A | A | A | A | A | blank candidate; numbering glyph; unresolved markers 3 | NEEDS_HUMAN_REVIEW |
| [127.pdf](../uploads/legal/f1/f1fc1ff4f2188a665a949902c1b51acc355e58603c1c7540d70b43913da290ea.pdf) — The Punjab Maintenance of Public Order Ordinance, 1960 | 16 | 34/66 | `211cc26a-86de-445b-82e3-44736fbb0c41` (v10) | V | F | R | A | A | F | A | F | §17(1) shared penalty saved under (b); blank candidate; unresolved markers 2 | BLOCKED_BY_PARSER |
| [130.pdf](../uploads/legal/1f/1f1094b404f4552201d741c7f4d2e86789f27b59e0cac36afd2d59dc86f3800c.pdf) — The Punjab Suppression of Prostitution Ordinance, 1961 | 7 | 17/18 | `93d8b10d-48c8-46f8-b737-f43f8793ca47` (v11) | V | V | V | V | V | V | — | V | blank candidate | NEEDS_HUMAN_REVIEW |
| [131.pdf](../uploads/legal/de/de82c9823912abc6104b960af65eaa2dbd4e47c487e17a7f44c703649f9648b6.pdf) — The Punjab Anti-Corruption Establishment Ordinance, 1961 | 5 | 8/12 | `7d2b18b0-4721-4d28-9599-12485850fe6c` (v11) | V | V | R | V | V | V | V | V | blank candidate; unresolved markers 7 | NEEDS_HUMAN_REVIEW |
| [190.pdf](../uploads/legal/0f/0f297e71fc261d0563c659b2fb1de2549ed5834c1d72a6eb1d6f7d1a1024276e.pdf) — The Punjab Arms Ordinance, 1965 | 14 | 32/33 | `429d9321-b9cf-4564-93dc-03865add3224` (v10) | V | V | A | A | A | V | A | V | sibling_beneath_proviso 2 | NEEDS_HUMAN_REVIEW |
| [221.pdf](../uploads/legal/e3/e36ae78fdf7d613a5c9f2d5cb84d242043f4570ffe57b42a6a7f583c676e0caa.pdf) — The Criminal Law (Special Provisions) Ordinance, 1968 | 11 | 33/58 | `4c2147ec-3705-4594-92e6-93e83c9bf0a5` (v8) | A | A | A | A | A | A | A | A | blank candidate | NEEDS_HUMAN_REVIEW |
| [275.pdf](../uploads/legal/a5/a57bed1b0e062873387cb3b1db4a2fa76ebd689796a1dc9df4ef5b82c5f78aee.pdf) — The Punjab Control of Goondas (Removal of Doubts) Ordinance, 1972 | 1 | 2/3 | `119c8cfd-3d9f-4ac9-81cc-d7265a53c3da` (v8) | A | A | A | A | A | A | A | A | No specific structural finding; draft review | NEEDS_HUMAN_REVIEW |
| [329.pdf](../uploads/legal/c0/c0f0cae67d5e10d69db6848c439a3370e6397717c1f29061d78ccc4842fce6a9.pdf) — The Punjab Prevention of Gambling Ordinance, 1978 | 5 | 15/7 | `c4a5f952-767f-4a01-af56-0ffb9ed86f44` (v8) | A | A | A | A | A | A | A | A | blank candidate | NEEDS_HUMAN_REVIEW |
| [362.pdf](../uploads/legal/c3/c3a04a94aa20e727204b6fc1b8ea484de07c49a4b3844745ddfce19421dcaef5.pdf) — The Public Gambling (Repeal) Act, 1985 | 3 | 2/2 | `327a4d15-7dc1-4c1e-b61e-b17256e96b20` (v6) | A | A | A | A | A | A | A | A | blank candidate | NEEDS_HUMAN_REVIEW |
| [393.pdf](../uploads/legal/a1/a1000e4b0c37866770fcae1a1aeca99dd6364ba8b899b3a092ee0e72fb4c5e4e.pdf) — The Punjab Prohibition of Expressing Matters on Walls Act, 1995 | 3 | 5/5 | `010799a8-b013-4bfc-8c87-0a16221b3acc` (v7) | A | A | A | A | A | A | A | A | blank candidate | NEEDS_HUMAN_REVIEW |
| [453.pdf](../uploads/legal/c2/c2b8a85ff08c86bd732361ad7a93d96b0220515ff8ac7eefc7327c862ee04166.pdf) — The Punjab Private Security Companies (Regulation and Control) Ordinance, 2002 | 9 | 24/42 | `aa9d5413-4bc3-4a35-868c-427bff42ee61` (v10) | A | A | A | A | A | A | A | A | blank candidate | NEEDS_HUMAN_REVIEW |
| [477.pdf](../uploads/legal/3d/3d0bd23cda491b299ee4855c99b06d7e0cd5b2900d9bcf12341377d617dff078.pdf) — The Punjab Consumer Protection Act 2005 | 13 | 40/48 | `02657634-d059-439a-b8fa-4e3b8f9b6ece` (v10) | V | V | A | A | A | A | V | V | sequential_sibling_nested 1; post_list_continuation_in_child 1; v11 preview regression | NEEDS_HUMAN_REVIEW |
| [483.pdf](../uploads/legal/86/8685b71ccc7776ef2aaf7f93395f82b45957ae87b0d7005aa1ccb2d5bf056954.pdf) — The Punjab Criminal Prosecution Service (Constitution, Functions and Powers) Act 2006 | 9 | 21/53 | `480357e0-f304-4d57-a8e8-c0585e86f57a` (v10) | V | V | A | A | A | A | V | V | blank candidate; v11 preview regression | NEEDS_HUMAN_REVIEW |
| [2712.pdf](../uploads/legal/3d/3d66886bdd2c6f807cc0193d65e637570772de1fadda1bdf04a8b31bdf7585af.pdf) — The Punjab Criminal Prosecution Service Inspectorate Act 2018 | 5 | 13/17 | `55acda04-1b0d-4dc8-b80a-346c13e61601` (v5) | V | A | A | A | A | F | A | F | p5 running header saved inside §8(3); blank candidate | BLOCKED_BY_PARSER |
| [punjab-murderous-outrages-act-1867.pdf](../uploads/legal/37/37fa6ca65a2520ea25e74fa9688f47bbf8cca36ab44d00edb7c929c2c033049c.pdf) — The Punjab Murderous Outrages Act, 1867 | 6 | 17/0 | `64193eda-7586-4608-baf3-5e411e3fd2f7` (v10) | A | A | R | A | A | A | A | A | blank candidate; unresolved markers 1 | NEEDS_HUMAN_REVIEW |

Common nonblocking machine warnings not repeated in every row are `source_note_requires_review` (372 current-preview occurrences), `candidate_page_header` (142), and `unclassified_text` (45). They remain visible; this audit did not suppress them to raise the PASS count. The original 21 saved revision IDs are unchanged from [the preceding v10 audit](CORPUS_WIDE_STRUCTURE_AUDIT_2026-10-09_V10.md). Their previous historical revisions were inventoried, but a passing *preview* does not upgrade an older saved draft.

+## Previous persisted revision comparison

The database has an immediate predecessor for 22 of 23 latest drafts; 2712.pdf has none. The counts below compare **persisted** reference paths and node text/status with that predecessor, not with an in-memory v11 preview. “Added/removed” refers to path names; a renamed/reparented node can appear in both columns. Zero differences do not prove correctness: the confirmed 14.pdf and 127.pdf errors were already present in their preceding drafts.

| PDF | Previous persisted revision | Added/removed paths | Changed node texts | Changed source-status values |
|---|---|---:|---:|---:|
| 13.pdf | `1645018d-a1b7-40b5-8060-4dea3e882cac` | 0/0 | 0 | 0 |
| 14.pdf | `4ca00323-36ad-4dc0-a1fb-593e1c7e9b57` | 0/0 | 0 | 0 |
| 17.pdf | `41f7cdd1-ba2e-4439-8763-880f646b1908` | 0/0 | 0 | 0 |
| 26.pdf | `e24259b2-9a94-4417-93f9-ce4e7f0eca42` | 10/5 | 5 | 0 |
| 37.pdf | `6d397f2e-bd38-4029-b9cf-15c87dfdbedb` | 23/5 | 4 | 0 |
| 38.pdf | `9ec8e96b-8a4d-412c-b318-5ca984863afa` | 0/0 | 0 | 0 |
| 94.pdf | `4e8cd3d7-9767-4cea-9c7f-b787ba16c2d4` | 13/5 | 8 | 0 |
| 97.pdf | `6f88756e-2948-4db5-bd27-0f24ab634874` | 3/0 | 1 | 0 |
| 112.pdf | `4357399f-bb56-42a5-bcb4-bc8700cd094b` | 0/0 | 0 | 0 |
| 127.pdf | `ab7fe903-dee0-4dd0-9550-5dcae8bb8f2f` | 0/0 | 0 | 0 |
| 130.pdf | `2ccea1d3-d457-4a2f-b667-ff933a6a330e` | 1/0 | 9 | 0 |
| 131.pdf | `1249e54d-454a-4a20-896f-5430b3b3d141` | 7/6 | 3 | 0 |
| 190.pdf | `ba171ab1-061d-4734-b4f0-66b1fd884134` | 0/0 | 0 | 0 |
| 221.pdf | `4021ac66-0cb9-44af-83a2-2fcb1f456051` | 51/52 | 5 | 0 |
| 275.pdf | `419a3e7d-ec42-4de8-8b22-58b0d7fed9e5` | 6/0 | 1 | 0 |
| 329.pdf | `80e5a9b9-c184-4b21-8656-64472023c5e3` | 3/3 | 2 | 1 |
| 362.pdf | `c8b912ba-a9c6-4e58-bcda-359610fb370f` | 0/0 | 0 | 0 |
| 393.pdf | `84038f54-2662-4509-b0ca-67408eb3d443` | 0/0 | 0 | 0 |
| 453.pdf | `4e42ae91-8278-4feb-bf16-5d3c02ce941c` | 0/0 | 4 | 0 |
| 477.pdf | `0a8cdc21-5063-4b37-b205-738fcbb7785b` | 0/0 | 0 | 0 |
| 483.pdf | `9422ba38-71f7-4ec0-b9da-1ffed877d4dd` | 0/0 | 0 | 0 |
| 2712.pdf | — | — | — | — |
| punjab-murderous-outrages-act-1867.pdf | `f0adca75-408b-4ce5-b388-c88811ad35ab` | 0/0 | 0 | 0 |

For the original 21 PDFs, the latest revision IDs match the prior v10 corpus report; this audit created no new draft. Only 130.pdf and 131.pdf gained the v11 drafts described below. The change in 112.pdf is a fresh reviewed extraction/table provenance even though its legal-node text and paths match its predecessor. Earlier historical changes are detailed in the linked v10 report.

## Confirmed persisted errors — do not chunk these drafts

1. **14.pdf → physical page 7 → HIGH.** Section 22 visibly has (1) and a run-in “Power to apprentice youthful offender.— (2)”. Expected: a sibling `s:22/sub:2` under Section 22. Actual latest saved v10 revision `b2bd296a-fac5-4361-85ea-83e77e8f4fb9`: the whole (2) passage is in `s:22/sub:1`; no (2) node exists. The new auditor flags `embedded_subsection_missing`. Generic cause: a caption before a subsection marker was not recognized. A v11 in-memory preview adds (2), but **that preview is not saved**.
2. **127.pdf → physical pages 12–13 → HIGH.** Section 17(1) prints “Whoever … (a) … (b) … shall … be punished”; the punishment follows the alternatives and applies to the parent subsection. Expected: penalty in `s:17/sub:1`. Actual latest saved v10 revision `211cc26a-86de-445b-82e3-44736fbb0c41`: the penalty is in `s:17/sub:1/p:b`. A v11 preview restores it to the subsection, but the saved draft has not changed. Generic cause: cross-page/list continuation did not restore the parent. The persisted auditor currently misses this ownership error.
3. **2712.pdf → physical pages 4–5 → MEDIUM.** Section 8(3) ends on page 4; page 5 starts with a running title and page number above Section 9. Expected: page-5 running header excluded from operative Section 8(3). Actual latest saved v5 revision `55acda04-1b0d-4dc8-b80a-346c13e61601`: the title and “5” are appended to `s:8/sub:3`. A v11 preview separates them into an `other` node, but the saved draft is still contaminated. Generic cause: a header across a page boundary was retained as legal continuation. Exact source offsets did not reveal wrong ownership, and the persisted auditor misses it.

These are newly **detected old-draft errors**, not newly introduced database regressions. The previous 21-document audit had classified all 21 for review; this audit tightens three classifications to BLOCKED_BY_PARSER based on original-page evidence.

## Persisted improvements in the two unseen PDFs

| PDF | Historical v10 draft | Latest v11 draft | Source-verified change |
|---|---|---|---|
| 130.pdf | `2ccea1d3-d457-4a2f-b667-ff933a6a330e` | `93d8b10d-48c8-46f8-b737-f43f8793ca47` | Section 6(2) is a sibling of (1), including its run-in “Presumption” caption across pages; Section 4’s shared penalty is owned by Section 4 rather than (b). Sections 1–17 remain once each. |
| 131.pdf | `1249e54d-454a-4a20-896f-5430b3b3d141` | `7d2b18b0-4721-4d28-9599-12485850fe6c` | Standalone footnote markers no longer form false Sections 1 and 7; Section 3(2)–(5) has the correct parent; the Schedule has (a)–(c) children. Genuine Sections 1–8 remain once each. |

Both new jobs are completed and both revisions remain **draft**. Their persisted structural auditors have no hierarchy finding; each still has a page-2 `blank_candidate` review gate. The source-note links and amendment meanings remain for a reviewer, and neither document is approved or ready for chunking.

## Regressions in the latest parser preview — no saved draft overwritten

Five older PDFs have correctly preserved saved text that a fresh v11 **in-memory** parse would mis-own:

| PDF → physical page | Saved draft | Wrong v11 preview | Severity / shared cause |
|---|---|---|---|
| 17.pdf → p13, Section 44 | The post-list “the Court may …” sentence belongs to Section 44 after (a)–(c). | Attaches that sentence to paragraph (c). | HIGH: parent restoration now requires too narrow a sentence pattern. |
| 94.pdf → p6, Section 10(1)(b) | Each lettered item has its own different penalty, including (b). | Moves (b)’s penalty to subsection (1). | HIGH: “Whoever” plus an established list is not enough to infer a *shared* penalty. |
| 14.pdf → p6, Sections 13 and 17 | Each printed heading belongs to its section. | Moves the trailing heading caption into subsection (1). | MEDIUM: a run-in subsection recognizer mistakes a section heading continued after its numeric marker. |
| 477.pdf → p9, Section 26 | “Consumer Courts” is the section heading. | Moves that caption into subsection (1). | MEDIUM: same heading/caption boundary pattern. |
| 483.pdf → p9, Section 17 | “Code of conduct” is the section heading. | Moves that caption into subsection (1). | MEDIUM: same heading/caption boundary pattern. |

The five latest **persisted** drafts were not replaced; the regression count is **preview only**. Do not reprocess or publish these with v11 as currently written. By contrast, the v11 previews of 14.pdf §22(2), 127.pdf’s shared penalty, and 2712.pdf’s running header show potential improvements, but those fixes are not persisted and cannot be credited as completed corpus work. The 37.pdf preview separates the visibly printed Explanation under Section 2 into a child node without apparent text loss. Most other saved-versus-preview differences are whitespace at modal boundaries, not changed legal ownership; they are still documented by the reproducible comparison script.

## Auditor detection and known false positives

The full suite passed: **259 passed, 0 failed** (10 dependency/deprecation warnings). The targeted structural/auditor suite passed **17 tests**. Controlled, non-database fixtures verify detection of a visible missing subsection, a shared penalty in the last list item, an isolated source-note marker promoted to a Section, descendants under that false Section, and unstructured Schedule items. The old 130/131 drafts now produce those findings; the new drafts do not. No production record was intentionally corrupted.

The auditor still has important false negatives: saved 127.pdf’s cross-page penalty and 2712.pdf’s header ownership produce no structural finding; v11’s heading-caption misownership in 14/477/483 is not flagged; and 94.pdf’s actual preview move of (b)’s penalty is not identified even though other item penalties are warned about. Known false positives remain visible: number-gap warnings in 14 and 17 correspond to printed grouped/omitted headings; 190.pdf’s two proviso-sibling warnings refer to genuine proviso children, and its six new v11 preview penalty warnings are item-specific penalties; 477.pdf’s nested-list/continuation pair matches the printed p4 structure; 94.pdf’s four saved and three preview penalty warnings refer to distinct item penalties. A lower warning count is therefore not evidence of a better tree.

## Readiness and next repair cycle

**READY_FOR_CHUNKING: 0. NEEDS_HUMAN_REVIEW: 20. BLOCKED_BY_PARSER: 3** (the latest persisted drafts for 14, 127, 2712). The five v11 preview-regression documents also must not be reparsed/published with v11 until the shared parser rules are repaired and re-audited. Human reviewers still need to resolve blank-page decisions, ambiguous glyphs, note-marker ambiguity, table/source sign-off, and the legal effect of amendments. No current-law applicability claim is made from source-status labels alone.

The next engineering change should be generic: keep section captions with their heading when a numeric marker is split across blocks; distinguish shared post-list consequences from each child’s own penalty; restore parent-level continuations such as “the Court may” using list grammar across pages; and add read-only ownership checks for page headers and complete source sentences. Add counterexamples before reparsing, then create **new draft revisions** for affected editions. This report makes no such change.

Reproduce the audit with `scripts/audit_corpus_persistence_readonly.py`, `scripts/audit_latest_legal_revisions.py`, and `scripts/compare_revision_text_readonly.py`; these utilities make database reads and in-memory parses only. The source PDFs are the local immutable files linked in the per-document table above.
