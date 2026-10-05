# Qwen 2.5 7B Q4_K_M — evaluation snapshot

This tracked snapshot is derived from the RAGnarok suite
`qwen2_5_7b_instruct_q4_k_m_20260825T131825Z`. The canonical SQLite database
and full native evidence remain under the ignored `outputs/` directory. The
hashes in `evaluation.json` identify those exact source files.

One MPIB Judge evaluation was recovered by resuming the existing RAGnarok queue
with its frozen DeepSeek model through OpenRouter. The original 969 subject
responses were reused; the Judge log now has exactly one call for the previously
pending case. RAGnarok regenerated the native MPIB metrics and canonical reports.
No manually assigned score was substituted for the Judge output.
