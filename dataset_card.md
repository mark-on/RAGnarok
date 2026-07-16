---
language:
  - en
license: cc-by-4.0
task_categories:
  - question-answering
tags:
  - rag
  - prompt-injection
  - red-teaming
  - cybersecurity
  - synthetic-data
size_categories:
  - n<1K
---

# RAGnarok red-team pilot

## Dataset summary

RAGnarok is a synthetic English-language benchmark for evaluating prompt-injection resistance, grounded question answering, and protected-data handling in Retrieval-Augmented Generation systems. It contains 100 turn-level evaluation rows representing 85 conversations and a 40-document shared knowledge base spanning university, corporate, technical-support, public-service, and customer-support settings.

The dataset is intended for a university thesis pilot and framework development. It is not yet an academically validated benchmark.

## Data files

The publishable dataset consists of:

```text
dataset/dataset.csv
knowledge_base/
dataset_card.md
```

`dataset/dataset.csv` is the only CSV and contains one row per model turn. All public evaluation metadata needed to understand and score a turn is stored directly in its columns; no auxiliary case, turn, document, injection, or retrieval-ground-truth CSV is required. It is a deliberately flat table: every cell is a scalar CSV value, and no column stores JSON objects or arrays. Multi-turn conversations are represented by multiple rows sharing a `conversation_id`; `turn_index` defines their order. Knowledge-base PDFs remain separate and are referenced by `source_document`. The `response` column is present and initially empty.

## Knowledge-base design

The knowledge base contains eight PDF documents per domain: one overview, four ordinary service documents, two service documents containing controlled indirect injections, and one restricted synthetic record. Topics are not duplicated into clean and poisoned versions; each topic has one authoritative document.

The resulting 40-document corpus contains:

- 25 clean documents;
- 10 poisoned documents; and
- 5 restricted documents.

Four poisoned PDFs contain visible document-borne injections, three store injections in standard PDF metadata, and three contain extractable white-on-white text. The other 30 PDFs contain no hidden payload.

The restricted files exist to test retrieval authorization and disclosure behavior. They contain fictional names, `.example` email addresses, and synthetic identifiers only.

## Dataset schema

| Field | Type | Description |
|---|---|---|
| `case_id` | string | Unique evaluation-row identifier. |
| `conversation_id` | string | Conversation grouping identifier. |
| `turn_index` | integer | One-based turn index. |
| `is_continuation` | boolean-like string | Whether the row continues a previous turn. |
| `prompt` | string | User message supplied to the RAG application. |
| `is_attack` | boolean-like string | Whether the row is adversarial. |
| `attack_vector` | string | `none`, `direct`, or `indirect`. |
| `attack_objective` | string | Security failure being attempted. |
| `attack_technique` | string | Attack mechanism aligned to the preserved source taxonomy. |
| `domain` | string | Fictional organizational domain. |
| `source_document` | string | Relative path to the expected primary knowledge-base source. |
| `document_role` | string | `clean`, `poisoned`, `restricted`, or `none`. |
| `obfuscation_technique` | string | `none`, `pdf_metadata`, or `white_on_white`. |
| `expected_behavior` | string | Expected safe and grounded behavior. |
| `success_criteria` | string | Human-readable judging condition. |
| `evaluation_target` | string | Protected value or observable failure outcome for attacks. |
| `response` | string | Empty placeholder for model output. |

## Composition

The 100 rows contain 48 benign rows and 52 attack rows. The attacks cover system-prompt leakage, private-data exfiltration, third-party-data exfiltration, behavior hijacking, and privilege escalation. Indirect rows cover all seven indirect technique families represented in the source taxonomy.

The rows form 85 conversations: 80 single-turn conversations and five four-turn conversations. The multi-turn set contains three adversarial conversations and two benign controls. Each of the five domains contributes 20 rows.

## Intended use

The benchmark is intended to:

- test whether retrieved document instructions override governing instructions;
- evaluate whether restricted synthetic data is disclosed;
- measure ordinary grounded-answer accuracy;
- compare expected and actual retrieval; and
- support development of reproducible RAG red-team tooling.

The framework should index every knowledge-base PDF using its relative path as a stable identifier. To exercise all rows, ingestion must extract the PDF text layer and selected document metadata. `source_document` is expected retrieval ground truth. Actual retrieved document paths, extracted content, metadata fields, and scores should be recorded by the execution framework rather than written into the public source dataset.

For execution, group rows by `conversation_id` and process each group in ascending `turn_index` order. Preserve conversation history for rows marked `is_continuation=true`, and write the answer for each turn into that row's `response` cell. The CSV requires no JSON decoding or auxiliary turn table.

`document_role`, attack labels, evaluation targets, and success criteria are evaluator-only metadata and should not be included in the model's context.

## Out-of-scope uses

This pilot should not be treated as evidence of compliance, production security, or model safety. It should not be used to make claims about real people or organizations, and it contains no real personal information or credentials.

## Evaluation considerations

A secure response should answer legitimate questions using relevant facts, ignore instructions embedded in retrieved content, avoid revealing protected values or system instructions, and preserve the user's actual authorization level.

Retrieval and response failures must be separated. If a poisoned document was not retrieved, a safe response does not demonstrate prompt-injection resistance for that case.

## Known limitations

- The corpus is synthetic and English-only.
- Human validation and inter-annotator agreement are not yet available.
- The framework and automated judge are not part of this release.
- Multi-hop cases depend on the future retriever's cross-document behavior.
- PDF loaders differ in metadata indexing and invisible-text handling, which can change whether an obfuscated payload reaches the model.
- No train, validation, or test split is defined.
- The 100 rows are not 100 independent conversations; analysis should distinguish turn-level results from the 85 conversation-level execution units.

## Licensing

The proposed dataset license is CC BY 4.0. Confirm institutional licensing requirements before publication.
