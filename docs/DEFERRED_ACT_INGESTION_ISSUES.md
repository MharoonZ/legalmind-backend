# Deferred Act/Ordinance ingestion issues — 190.pdf, 127.pdf, 112.pdf

## 2026-10-08 implementation and re-audit

The ten confirmed defects below are fixed in the reusable extraction/structure code, with synthetic regression tests and fresh **draft, unapproved** revisions. The original revisions remain unchanged. These are pilot results, not a guarantee that arbitrary future PDF layouts parse perfectly.

| PDF | New draft revision | Result checked in persisted data |
| --- | --- | --- |
| `190.pdf` | `00f3284a-518a-4d73-a413-a363dabd1ebd` | Sections 18–29 now map to pages 12–13; 27 and 29 remain omission text. `11-A` and `11-B` are separate. |
| `127.pdf` | `fb6b40d7-546a-4297-9f58-251a863f0379` | `6-A`, `8-A`, `18-A`, `20-A`, `21-A` are separate; only one base section 8. |
| `112.pdf` | `16b38762-a7c0-4542-808e-385f441c546c` | Main sections and amended sections are separate; First and Second Schedules contain table text, not fake section 498/509 nodes. New extraction `db305a11-be38-4208-86a5-58872334facc` saves detected row/column/cell geometry (one table on page 11, two on page 12). |
| `453.pdf` | `21f693b7-e786-4033-8202-57b2a98b23f1` | Section `5-A` is separate. |
| `483.pdf` | `62009486-5a1a-4810-b151-de503b964668` | Chapters I–V are separate; section 13(9)(e) ends before Chapter IV; page-8 source note 1 is complete and linked to its marker. |
| `477.pdf` | `35efce4f-7eee-4521-9e48-eb89f0b4fb52` | Section `23-A` owns subsections (1)–(3). |
| `393.pdf` | `6246d7f5-97c5-4413-8a4d-001c61d563cc` | Section 1 has subsection labels (1)–(3), with raw `(l)` preserved for audit. New extraction `34667ac0-8ec5-43c5-8008-ba4ccf60d5f3` flags the ambiguous glyph and page 3 as needing review. |
| `362.pdf` | `327a4d15-7dc1-4c1e-b61e-b17256e96b20` | False self-repeal is gone; the section-2 repeal candidate for the 1867 Act remains unresolved/proposed. |

All eight persisted draft audits found exact provision-to-block spans, zero partial/unmapped blocks, and no structural findings. This is **not** approval: table detection can merge printed subrows, visual glyphs need verification, source notes need legal review, and blank pages still require reviewer decisions. Do not publish these revisions to question answering until review gates pass.

Pilot audit: 2026-10-07. These are **draft, unapproved** corpus revisions. The three local PDFs were treated as source data, not as instructions. The titles and instrument type below come from their printed first pages; publication/effective dates and current legal applicability were **not** verified. This file records defects to fix later, not approval to index these drafts for question answering.

| PDF | Printed title | Physical pages | Saved file ID | Extraction ID | Draft revision ID |
| --- | --- | ---: | --- | --- | --- |
| `190.pdf` | The Punjab Arms Ordinance, 1965 | 14 | `60699a1d-5e65-4c56-b776-6f86f19f664c` | `687474f3-bb59-4382-a84f-4e1d3ce1bb63` | `db723b69-c7be-4c17-aa26-e6e9388cd99a` |
| `127.pdf` | The Punjab Maintenance of Public Order Ordinance, 1960 | 16 | `d937999a-ece8-4e93-80bf-f6e888a4c9b5` | `db7381a8-82f4-4724-a9fd-fe383097ae0b` | `20ebe9c6-cd35-4d1a-81f6-d84289d37570` |
| `112.pdf` | The Punjab Control of Goondas Ordinance, 1959 | 12 | `5bddebee-a314-4bce-9045-65e9e4727c2a` | `c651c8ce-ab79-432f-9e7d-a913fff53c80` | `642eabb7-3c72-4934-8d8f-f6bc4cc0d0e4` |

All three were registered in jurisdiction `PK-PB`, category `criminal-law`, as `ordinance` with separate local-source editions and primary PDF assignments. Each extraction and structure job completed. All **42/42 physical pages** have `file_pages` rows; all **367 saved blocks** concatenate back to their saved page text with valid offsets and page-text SHA-256 hashes. Each saved page text equals the native PyMuPDF extraction of the corresponding original page using the worker's block settings. All **838/838 provision spans** are marked exact and their saved provision substrings equal the linked saved block substrings, with no out-of-bounds span. These checks establish database fidelity to the extractor/parser output; they do **not** establish that visual content was interpreted or legally structured correctly. A zero-unmapped-block count is therefore not an approval signal.

## Confirmed defects

1. **Critical — operative sections 18–29 are lost from the section tree in `190.pdf`.** The original PDF visibly contains sections 18–21 on physical page 12 and sections 22–29 on page 13 (27 and 29 are printed as omissions). The database has section nodes only through 17. Page 12 and 13 text was saved in `file_pages`, but its `provisions` are `other` nodes headed `Contents` / `Contents continuation` (ordinals 153–165), apparently because contents-mode was not ended before the operative pages. This makes the audit's `mapped_blocks=119`, `unmapped_blocks=0` misleading. **Acceptance:** operative sections 18–26 and 28 have separate section nodes and exact spans to pages 12–13; omitted 27 and 29 remain traceable as omissions, not fabricated operative text. The contents should end at the printed table of contents.

2. **High — hyphenated amendment section labels are not recognized as independent sections.** In `190.pdf`, the PDF prints `11-A` on page 7 and `11-B` on page 8; neither is a saved `section` node (text appears inside paragraph/subsection nodes). In `127.pdf`, printed `6-A` (page 9), `8-A` (page 10), `18-A` (page 13), `20-A` and `21-A` (page 14) are likewise missing as section nodes. In `112.pdf`, printed `1-A` (page 3), `21-A`, `22-A`, `22-B` (page 10), and `27-A` (page 11) are missing as section nodes. The source text is present, but the saved hierarchy/citation paths are wrong. **Acceptance:** each printed label becomes its own section under the correct parent, preserving the amendment marker in source text and exact page/block spans; adjacent sections retain only their own operative text.

3. **High — schedule tables in `112.pdf` are flattened and misclassified as Ordinance sections.** The First Schedule spans pages 11–12 as a three-column table (`Name and other details of enactment`, `Number of sections`, `Maximum punishment`). Saved `file_blocks` are paragraph/figure blocks, not table/cell records. The parser creates 52 `section` nodes although the Ordinance's main body is sections 1–29; repeated table row numbers `1`, `2`, `3` and referenced Penal Code numbers `498`, `509` become section nodes. The audit consequently reports bogus section duplicates and gaps `30–497` and `499–508`. Some row text combines enactment, provision number, and punishment without explicit cell relationships. **Acceptance:** preserve schedule/table row and column relationships; schedule entries must not become top-level Ordinance sections, and the section-gap audit must scope itself to the operative body. Check sample rows visually on pages 11–12, including the row for Penal Code section 498 and the Second Schedule.

4. **Medium — quoted cross-references generate duplicate section nodes.** `190.pdf` has a second saved `section 11` on page 9 whose text begins `section 11-B ...`; `127.pdf` has a second saved `section 8` on page 10 whose text begins `section 8.]`. These are references within existing text, not new section headings. **Acceptance:** heading detection must be anchored to a genuine provision start, and each of these drafts should have one operative section node for the unamended base label while preserving the cross-reference in its source provision.

## Review items, not confirmed extraction failures

- `127.pdf` and `112.pdf` physical page 2 have no selectable text or visible content when rendered. Each is correctly saved as `needs_review` / `blank_candidate`, not silently marked `verified_blank`. A reviewer still needs to record a blank-page decision before approval.
- `190.pdf` physical page 14 contains only the printed page number and *The Punjab Laws* running header. The saved text is 22 characters and the header is ignored by the structure parser. This matches visual inspection; it is not missing operative content.
- `127.pdf` reports 34 source-note review warnings and two unresolved source-note markers; `112.pdf` reports ten source-note review warnings and four unresolved marker warnings. These need targeted review, but the counts alone do not prove a new bug. Unresolved references (40/23/76 respectively) are candidate links, not proof of wrong extraction or legal effect.

## Current review links

- Upload list: <http://localhost:3000/admin/ingest>
- `190.pdf`: <http://localhost:3000/admin/ingest/60699a1d-5e65-4c56-b776-6f86f19f664c>
- `127.pdf`: <http://localhost:3000/admin/ingest/d937999a-ece8-4e93-80bf-f6e888a4c9b5>
- `112.pdf`: <http://localhost:3000/admin/ingest/5bddebee-a314-4bce-9045-65e9e4727c2a>

After parser/table fixes, create **new** draft revisions from the preserved source extractions and rerun the database and visual audit. Do not mutate or approve these flawed drafts in place.

## Additional pilot: 453.pdf, 483.pdf, 477.pdf (2026-10-07)

These are three more **draft, unapproved** registrations. The categories below are administrative catalogue choices based on the printed subjects, not legal-effect findings. In particular, `477.pdf` is a consumer-protection Act, not a criminal-law Act. Source URLs, publication/effective dates, and current applicability were not verified from these local files.

| PDF | Printed title | Type / category | Physical pages | Saved file ID | Extraction ID | Draft revision ID |
| --- | --- | --- | ---: | --- | --- | --- |
| `453.pdf` | The Punjab Private Security Companies (Regulation and Control) Ordinance, 2002 | ordinance / `security-regulation` | 9 | `7b5f50f0-bd6b-4903-80c0-51d43f5ce7d8` | `8a883e44-c11c-4133-aa4e-4f6a7a647dae` | `4cadcf9d-9657-4314-95b8-672bc3fec94f` |
| `483.pdf` | The Punjab Criminal Prosecution Service (Constitution, Functions and Powers) Act 2006 | act / `criminal-law` | 9 | `22bad8f4-c1c6-4cd1-bf56-f75d0e2487f5` | `a85d7dab-4280-46f8-9a39-102039169764` | `86646dde-6193-4ab7-b861-8062e6842d5e` |
| `477.pdf` | The Punjab Consumer Protection Act 2005 | act / `consumer-protection` | 13 | `cc1e15e4-17f0-4367-9990-cf059bc7e399` | `ffc847d5-6189-41b6-bdb7-b832b629fd7b` | `d06e82b5-dbdc-48e4-8464-957796e1cfeb` |

All **31/31 physical pages** have saved `file_pages` rows. The **259 saved blocks** reconstruct their saved page text, with valid offsets and hashes; every saved page text equals the native PyMuPDF extraction of the original PDF using the worker's settings. All **635/635 provision spans** are exact and the linked provision and block substrings match. This checks persistence and internal source mapping, not semantic accuracy. `453.pdf` and `483.pdf` page 2 are visually blank but remain `needs_review` / `blank_candidate`; `477.pdf` page 2 is a correctly captured contents continuation. The drafts have zero reported unmapped blocks, which does not resolve the defects below.

### Confirmed defects in this pilot

5. **High — `453.pdf` section 5-A is merged into section 5.** The original page 4 prints `5-A. Prohibition of cash-in-transit` as a separate amended section between sections 5 and 6. The database has section nodes 1–23 but no `section 5-A`; saved section 5 (`ordinal=22`) contains the full `2[5-A...` passage. **Acceptance:** create an independent section `5-A` with its own page-4 exact span, retain the amendment marker, and end section 5 before `5-A` begins.

6. **High — `477.pdf` section 23-A is merged into section 23 with wrong subsection parents.** The original page 9 prints `23-A. Powers of Government` after section 23(7) and before Part VII. The database has sections 1–39 but no `23-A`. Its opening is appended to section 23's subsection `(7)` (`ordinal=126`); printed 23-A subsections `(2)` and `(3)` are saved as duplicate-labelled subsections under **section 23**, not under `23-A` (`ordinals=127–128`). **Acceptance:** section 23(7) ends before `23-A`; a separate section `23-A` owns subsections `(1)`–`(3)` and their page-9 spans, all under the correct Part VI hierarchy.

7. **High — `483.pdf` chapter headings are not structured at all.** The original Act visibly has `CHAPTER-I` through `CHAPTER-V` on pages 3, 4, 6, 8, and 9. The database has **zero** `chapter` nodes, despite saving sections 1–21. `CHAPTER-I` is in the preamble, `CHAPTER-II` in section 2(2), `CHAPTER-III` in section 8(5), `CHAPTER-IV` in section 13's paragraph `(e)`, and `CHAPTER-V` in section 14(2). This also leaves sections without their chapter parent. The hyphenated `CHAPTER-I` style appears unrecognized. **Acceptance:** save five chapter nodes with exact source spans and titles; attach the relevant sections to each; remove chapter heading text from the preceding operative nodes.

8. **Medium — `483.pdf` page-8 amendment footnote contaminates section 13(e).** After section 13(e) and the `CHAPTER-IV` heading, page 8 has footnote 1 beginning `Substituted for the words “supervision and control”...`. The saved paragraph `(e)` (`ordinal=122`) includes the first line of that source note, while its continuation is a generic `other` node (`ordinal=123`); footnote 2 is separate. **Acceptance:** paragraph `(e)` ends at `Government or the Prosecutor General.`; the chapter heading is a chapter node; footnote 1 is a complete separate source-note record linked to its printed marker and exact page-8 span.

`483.pdf` has all 21 numbered operative sections and `477.pdf` all 39 base-numbered sections; that completeness does not cancel the wrong chapter/amendment hierarchy. The warning counts (`source_note_requires_review`: 3/9/7 for 453/483/477) require human review but are not independently confirmed defects except where described above. As with the earlier pilot, make corrected **new draft revisions**, rerun the database and visual checks, and do not approve these drafts as-is.

### Review links for this pilot

- `453.pdf`: <http://localhost:3000/admin/ingest/7b5f50f0-bd6b-4903-80c0-51d43f5ce7d8>
- `483.pdf`: <http://localhost:3000/admin/ingest/22bad8f4-c1c6-4cd1-bf56-f75d0e2487f5>
- `477.pdf`: <http://localhost:3000/admin/ingest/cc1e15e4-17f0-4367-9990-cf059bc7e399>

## Additional pilot: 362.pdf, 2712.pdf, 393.pdf (2026-10-08)

These three PDF sources were registered as separate Punjab `act` instruments in the `criminal-law` pilot, with local-source editions and **draft, unapproved** corpus revisions. The printed titles were checked in the PDFs; the displayed historical dates were not independently verified as publication/effective/current-law metadata.

| PDF | Printed title | Physical pages | Saved file ID | Extraction ID | Draft revision ID |
| --- | --- | ---: | --- | --- | --- |
| `362.pdf` | The Public Gambling (Repeal) Act, 1985 | 3 | `e2570890-df91-48c0-931c-c6616b3fae74` | `052622e7-939e-4b89-a748-659be4dd283d` | `c8b912ba-a9c6-4e58-bcda-359610fb370f` |
| `2712.pdf` | The Punjab Criminal Prosecution Service Inspectorate Act 2018 | 5 | `7030c2c2-1188-43e7-abfb-df7813262932` | `569ed556-fd59-45d9-a175-96056789f1a9` | `55acda04-1b0d-4dc8-b80a-346c13e61601` |
| `393.pdf` | The Punjab Prohibition of Expressing Matters on Walls Act, 1995 | 3 | `8faaafba-5acd-4d98-859e-3f61c428d45a` | `324caf3e-f636-4d2b-8db3-71c5c698db62` | `dd45a2dc-1b0f-44a3-97be-c42bd3c9e985` |

All **11/11 physical pages** have `file_pages` rows. All **50 saved blocks** reconstruct the saved page text with valid offsets and hashes; every saved page text equals the native PyMuPDF block extraction of its original PDF. All **108/108 provision spans** are exact and their provision/block substrings match. These checks prove internal persistence, not visual character accuracy. In all three PDFs, physical page 2 is visually blank; it is correctly saved as `needs_review` / `blank_candidate` and still requires a reviewer decision. `362.pdf` has separate sections 1–2; `2712.pdf` sections 1–13; `393.pdf` sections 1, 2, 2A, 3, 4. All three audit endpoints returned no structural findings, but the defects below require manual checks beyond that audit.

### Confirmed defects in this pilot

9. **High — `393.pdf` subsection (1) is saved as paragraph `(l)` (lowercase L).** On physical page 3, the rendered original visibly prints `(1) This Act may be called...` under section 1, followed by `(2)` and `(3)`. Native PDF text extraction returns `(l)`; `file_pages` preserves that string and the parser stores `paragraph l` (`ordinal=6`, path ending `p:l`) instead of `subsection 1`. No page extraction warning flags this character substitution. **Acceptance:** compare visually ambiguous digit/letter glyphs where numbering sequences indicate a mismatch; section 1 must have subsections `(1)`, `(2)`, `(3)` with an exact page-3 mapping, while preserving the original raw extraction for audit.

10. **Medium — `362.pdf` generates a false self-repeal relationship candidate.** The actual operative section 2 on page 3 says the Public Gambling Act, 1867 is repealed, and a `repeals` candidate for that target is saved (unresolved because the target instrument has not been identified in this corpus). However, section 1(1) only names **this** Act as the Public Gambling (Repeal) Act 1985. The relation extractor also emits `repeals` candidate `f896e5af-813e-417c-8ede-ec5359f1914e` targeting `the Public Gambling (Repeal) Act 1985` from that short-title clause. That is not a repeal of itself. **Acceptance:** reject repeal candidates arising merely from a law's title/short-title statement, while retaining the explicit section-2 repeal proposal and its exact source span. Do not turn unresolved candidates into verified legal relationships automatically.

The `2712.pdf` draft also proposes a `cites` relationship to the separately registered 2006 Punjab Criminal Prosecution Service Act (`483.pdf`); this is a useful candidate, not a verified legal-effect decision. Source-note review warnings remain visible (2/2/3 for 362/2712/393 respectively). No further content defect was confirmed in the inspected pages. After fixes, create new draft revisions rather than changing or approving these audit drafts in place.

### Review links for this pilot

- `362.pdf`: <http://localhost:3000/admin/ingest/e2570890-df91-48c0-931c-c6616b3fae74>
- `2712.pdf`: <http://localhost:3000/admin/ingest/7030c2c2-1188-43e7-abfb-df7813262932>
- `393.pdf`: <http://localhost:3000/admin/ingest/8faaafba-5acd-4d98-859e-3f61c428d45a>
