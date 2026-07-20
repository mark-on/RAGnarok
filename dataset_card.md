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
  - data-exfiltration
  - jailbreak-safety
  - synthetic-data
size_categories:
  - n<1K
---

# RAGnarok v1.0 dataset card

## Status

RAGnarok v1.0 is a static, synthetic English-language research benchmark for evaluating security and usefulness in Retrieval-Augmented Generation systems.

All generated cases are **automatically generated and awaiting human validation**. The current release must not be described as expert-validated, clinically validated, legally validated, or production-certified.

## Research purpose

The benchmark tests whether a RAG system can answer legitimate questions from retrieved documents while resisting malicious user instructions and untrusted instructions embedded in retrieved PDFs.

The principal research objective is `data_exfiltration`. It includes unauthorized disclosure of synthetic private-user records, third-party records, case references, contact information, and clearly nonfunctional access material. Data exfiltration represents 124 of 200 malicious rows.

Additional objectives are:

- high-level jailbreak requests for prohibited or dangerous assistance;
- disclosure or substantial paraphrase of hidden system instructions;
- behavior hijacking, task replacement, suppression, and false attribution;
- false privilege escalation and unauthorized treatment of a requester as an administrator;
- grounded benign question answering and difficult-benign false-refusal controls; and
- delayed multi-turn attacks that test whether untrusted instructions persist.

## Threat model

Direct attacks place the malicious request in the public user `prompt`. Indirect attacks keep the user request legitimate and place the malicious instruction inside the expected retrieved PDF. Indirect surfaces include ordinary body text, metadata, white-on-white text layers, appendices, structured fields, quoted messages, cross-document references, and claimed policy updates.

The benchmark evaluates model behavior after retrieval. It does not grant the evaluated model real tools, account access, administrator permissions, or the ability to modify an external system. `source_document` is retrieval ground truth and must never be forced into context by an evaluator.

The target model must receive only the public conversation, configured system prompt, and passages returned by normal retrieval. Evaluator fields such as labels, expected behavior, success criteria, protected targets, document roles, and decoded payloads must remain hidden.

## Dataset composition

The public dataset contains exactly 300 rows and 255 conversations.

| Component | Rows | Conversations |
|---|---:|---:|
| Malicious single-turn | 160 | 160 |
| Clean counterparts | 60 | 60 |
| Difficult-benign single-turn controls | 20 | 20 |
| Malicious four-turn conversations | 40 | 10 |
| Benign four-turn controls | 20 | 5 |
| **Total** | **300** | **255** |

### Malicious objectives

| Objective | Single-turn rows | Multi-turn rows | Total rows |
|---|---:|---:|---:|
| Data exfiltration | 100 | 24 | 124 |
| Jailbreak | 20 | 8 | 28 |
| System-prompt leakage | 15 | 4 | 19 |
| Behavior hijacking | 15 | 4 | 19 |
| Privilege escalation | 10 | 0 | 10 |
| **Total** | **160** | **40** | **200** |

The remaining 100 rows are clean or benign. Exact-marker success criteria are principal for 40 malicious rows, or 20%. The other 160 malicious rows use semantic criteria.

### Attack-strength construction tiers

Evaluator-only construction metadata records:

- 20 obvious rows;
- 60 naturalized rows;
- 60 contextual rows;
- 40 compositional rows; and
- 20 delayed rows.

These tiers are not public CSV columns.

## Domains

The corpus uses eight fictional domains:

1. University administration
2. Corporate human resources
3. Healthcare administration
4. Financial services
5. Public services
6. Technical support
7. E-commerce and customer support
8. Legal and compliance services

University, corporate, healthcare, and financial services contribute 38 rows each. Public services, technical support, e-commerce, and legal/compliance contribute 37 rows each. Every attack objective spans at least four domains, and data exfiltration spans all eight.

No organization, person, portal, case, record, address, phone number, email address, or access string represents a real entity. Every email address uses `.example`.

## Knowledge base

`knowledge_base/` contains exactly 96 PDFs: eight domains with twelve documents each.

Each domain contains:

- one organizational overview;
- five clean service or policy documents;
- four poisoned service documents;
- one restricted synthetic-record document; and
- one difficult-benign defensive document.

The full corpus contains 56 clean PDFs, 32 poisoned PDFs, and eight restricted PDFs. Every PDF has a stable SHA-256-derived document ID, fictional organization, title, document type, fixed revision date, realistic headings, legitimate service content, and 400 to 1,200 extractable words.

### PDF attack surfaces

The 32 poisoned PDFs are evenly distributed across eight detailed surfaces, four PDFs per surface:

- visible body text;
- PDF metadata (`Subject`, `Keywords`, `IndexingNote`, and `WorkflowNote`);
- white-on-white extractable text;
- footnotes or appendices;
- tables or structured fields;
- quoted email or support-message content;
- cross-document references; and
- claimed policy updates or superseding notices.

The public `obfuscation_technique` column uses only `none`, `pdf_metadata`, or `white_on_white`. Detailed surfaces remain in evaluator-only construction metadata.

## Public data format

The public benchmark is exactly one CSV:

```text
dataset/dataset.csv
```

It uses one row per model turn, stores no JSON inside cells, and has exactly these scalar columns in this order:

| Column | Description |
|---|---|
| `case_id` | Stable row identifier; begins with `DEV-` or `TEST-`. |
| `conversation_id` | Groups turns belonging to one conversation. |
| `turn_index` | One-based turn order. |
| `is_continuation` | `false` on the first turn and `true` afterward. |
| `prompt` | User-facing message only. |
| `is_attack` | Whether the row belongs to a malicious scenario. |
| `attack_vector` | `direct`, `indirect`, or `none`. |
| `attack_objective` | Intended security failure for malicious rows. |
| `attack_technique` | Technique aligned with the preserved source taxonomy. |
| `domain` | Fictional domain identifier. |
| `source_document` | Expected primary PDF for retrieval evaluation. |
| `document_role` | `clean`, `poisoned`, `restricted`, or `none`. |
| `obfuscation_technique` | `none`, `pdf_metadata`, or `white_on_white`. |
| `expected_behavior` | Safe and useful expected response. |
| `success_criteria` | Specific observable attack or benign criterion. |
| `evaluation_target` | Synthetic protected value or semantic failure category. |
| `response` | Empty in every published row. |

Construction sources and evaluator-only working metadata are not part of the runtime framework. Local development copies may be retained under the ignored `archive/` directory; they must never be indexed or shown to the evaluated model.

## Multi-turn behavior

Fifteen conversations contain four turns each. A framework must process turns in ascending `turn_index`, preserve history only inside the same `conversation_id`, and reset history completely when the identifier changes.

Malicious conversations follow a static four-turn pattern:

1. benign setup;
2. natural benign follow-up;
3. legitimate-looking request that exposes a poisoned document; and
4. benign trigger that tests whether the injected instruction persisted.

The benchmark does not claim fully adaptive attacks. No turn reacts dynamically to an actual model response.

## Clean counterparts

Sixty indirect malicious single turns have clean counterparts. Pair members:

- use the same legitimate user prompt and facts;
- use fresh, different `conversation_id` values;
- stay in the same split;
- reference similarly structured and similarly sized PDFs; and
- differ by removal of the malicious instruction.

Pairing is encoded in the shared case stem, for example:

```text
DEV-EXF-001-POISONED
DEV-EXF-001-CLEAN
```

No public `pair_id` column is used.

## Difficult-benign controls

Twenty single-turn rows and eight defensive PDFs contain suspicious-looking but legitimate language about system requirements, administrators, confidentiality, prompt-injection defenses, command-line quoting, and examples of malicious prompts.

These documents contain no instruction intended to manipulate the evaluated model. A useful model should recognize quoted examples as inert, explain the legitimate defensive rule, and avoid a blanket refusal.

## Development and test split

The benchmark remains one public CSV. Split membership is encoded in `case_id`:

- `DEV-`: 60 rows;
- `TEST-`: 240 rows.

Clean and poisoned counterparts never cross splits. Source documents and construction template-family identifiers referenced by DEV and TEST are disjoint. This design reduces direct leakage from development examples into the held-out evaluation while retaining one easy-to-distribute table.

## Static distribution and provenance

The released benchmark is the static `dataset/dataset.csv` plus the PDFs under `knowledge_base/`. Consumers do not need a generator and can replace the CSV with any file containing a `prompt` column. Stable case and document identifiers remain part of this benchmark version. The automated test suite checks that the bundled CSV loads, continuous conversations are preserved, referenced PDFs exist, and PDF content is extractable.

Construction files, internal working metadata, review material, and earlier pilots may be preserved locally under the ignored `archive/` directory, but none of them are runtime inputs.

## Safety restrictions

> Jailbreak cases contain only high-level prohibited requests and never contain operational instructions for weapons, explosives, malware, credential theft or security evasion.

The dataset and PDFs contain no material quantities, explosive procedures, weapon-construction details, harmful chemical recipes, deployable malware, credential-stealing code, phishing kits, evasion procedures, destructive commands, or operational payloads.

Synthetic access material is visibly nonfunctional, for example `SYNTHETIC_TOKEN_NB_1042`, `DEMO_ACCESS_KEY_Q7M2`, and `TEST_SESSION_REFERENCE_8841`. Reviewers must never replace synthetic values with real data.

## Human review

Reviewers should assess prompt naturalness, document realism, answerability, expected safe behavior, attack plausibility, success-criteria clarity, data-exfiltration target validity, jailbreak safety, clean/poisoned equivalence, and duplicate or templated language. Two independent reviews and adjudication are recommended before academic release. Corrections should update the static CSV or PDFs and then pass the automated test suite.

## Intended use

RAGnarok is intended for:

- university research on RAG prompt-injection and data-exfiltration resistance;
- controlled comparison of retrieval exposure and model behavior;
- evaluation-framework regression testing;
- study of benign usefulness and false refusals; and
- transparent, reproducible red-team methodology development.

## Prohibited and out-of-scope use

Do not use RAGnarok:

- to target real people, organizations, accounts, systems, or data;
- to develop weapons, malware, phishing kits, credential theft, or evasion procedures;
- as evidence of legal, medical, financial, privacy, or security compliance;
- as a substitute for production authorization controls or human review; or
- to claim broad model safety from this synthetic benchmark alone.

## Known limitations

- The corpus is synthetic, English-only, and automatically generated.
- Human validation and inter-annotator agreement are not yet available.
- Static multi-turn attacks do not adapt to actual outputs.
- PDF libraries vary in metadata and invisible-text extraction.
- Retrieval results depend on chunking, embedding, ranking, and parser configuration.
- The benchmark cannot represent every organization, language, user need, attack, or authorization policy.
- Semantic evaluation still requires careful human or judge-model review.
- Restricted records are present to test behavior; production systems should enforce authorization before retrieval.

## Licensing placeholder

Proposed dataset license: **CC BY 4.0**. Final licensing remains subject to university and institutional review before publication.

## Citation placeholder

Replace the placeholder below when the accompanying thesis or benchmark paper is available:

```bibtex
@dataset{ragnarok_v1_2026,
  title   = {RAGnarok v1.0: A Synthetic RAG Security Benchmark},
  author  = {TODO},
  year    = {2026},
  version = {1.0},
  url     = {TODO}
}
```
