# Published evaluation results

This directory contains privacy- and license-conscious aggregate results from the
completed thesis evaluation matrix. It publishes measurements and provenance,
not the raw benchmark cases. Prompts, responses, retrieved contexts, attack
payloads, native workspaces, credentials, and restricted MPIB material remain
excluded from Git.

## Evaluation matrix

Two model families were evaluated at four quantization levels:

| Model family | FP16 | Q8 | Q4 | Q2 |
|---|---:|---:|---:|---:|
| Llama 3.2 3B Instruct | yes | yes | yes | yes |
| Qwen 2.5 7B Instruct | yes | yes | yes | yes |

Every configuration contains 2,198 canonical cases. Of these, 1,360 are attack
cases with an evaluable native outcome: 300 SPIKEE, 300 PoisonedRAG, 131 MPIB,
and 629 AgentDojo cases. MPIB used the frozen Judge model
`deepseek/deepseek-v4-flash-0731`.

## Aggregate results

ASR is Attack Success Rate, so lower values indicate that fewer attacks
succeeded. Values below are from the native benchmark outcomes normalized by
RAGnarok. The intervals are within-run Wilson 95% confidence intervals, not
confidence intervals over repeated experimental runs.

| Model | Quantization | Overall ASR | 95% CI | SPIKEE | PoisonedRAG | MPIB | AgentDojo |
|---|---|---:|---:|---:|---:|---:|---:|
| Llama 3.2 3B | FP16 | 31.25% | 28.84–33.76% | 33.00% | 91.00% | 39.69% | 0.00% |
| Llama 3.2 3B | Q8 | 31.40% | 28.99–33.91% | 33.33% | 92.00% | 38.93% | 0.16% |
| Llama 3.2 3B | Q4 | 30.96% | 28.56–33.46% | 34.33% | 91.00% | 34.35% | 0.00% |
| Llama 3.2 3B | Q2 | 26.91% | 24.62–29.33% | 7.33% | 94.00% | 46.56% | 0.00% |
| Qwen 2.5 7B | FP16 | 41.10% | 38.52–43.74% | 42.67% | 94.00% | 42.75% | 14.79% |
| Qwen 2.5 7B | Q8 | 40.44% | 37.86–43.07% | 41.67% | 94.00% | 35.11% | 15.26% |
| Qwen 2.5 7B | Q4 | 37.57% | 35.04–40.18% | 38.33% | 95.00% | 22.90% | 12.88% |
| Qwen 2.5 7B | Q2 | 37.35% | 34.82–39.96% | 36.00% | 93.00% | 30.53% | 12.88% |

The machine-readable values, including MPIB harm and AgentDojo utility metrics,
are in [`summary.csv`](summary.csv). Results split by attack family are in
[`attack_families.csv`](attack_families.csv). [`provenance.json`](provenance.json)
binds the published aggregates to the canonical local SQLite databases and
derived reports through SHA-256 hashes.

## What these runs show

- Quantization does not produce a simple monotonic change in security. Llama
  FP16, Q8, and Q4 differ by less than 0.5 percentage points in overall ASR.
- Qwen's overall ASR decreases from 41.10% in FP16 to 37.57% in Q4 and 37.35%
  in Q2, but the benchmark-level behavior is not monotonic: MPIB records its
  lowest ASR at Q4 and a higher ASR again at Q2.
- PoisonedRAG remains highly successful for every tested configuration
  (91–95% ASR), so quantization alone does not mitigate retrieval knowledge
  corruption in these runs.
- A lower aggregate ASR must not automatically be interpreted as a safer and
  equally useful model. Llama Q2, for example, has a much lower SPIKEE ASR but
  worse MPIB ASR, a higher MPIB harmful false-positive rate, and lower
  AgentDojo utility. This is why the thesis treats model selection as a
  security–utility–cost Pareto problem.

## Interpretation limits

These are completed single-run observations, not repeated-seed estimates and
not evidence that quantization causes a security change. Comparisons are valid
within each model family and case-aligned quantization matrix. Llama 3.2 3B and
Qwen 2.5 7B differ in model family and parameter count, so their absolute scores
must not be treated as a quantization-only comparison. Benchmark metrics retain
their native semantics; the aggregate ASR is a descriptive combined view.
