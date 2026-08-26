# PoisonedRAG integration

RAGnarok pins the official repository and preserves its three BEIR datasets (NQ, HotpotQA, and MS MARCO), Contriever retrieval, released targeted adversarial passages, prompt template, ranking logic, and native ASR/retrieval metrics. The final LLM transport is replaced by RAGnarok's provider interface.

The device adapter executes the same Contriever model and tensor operations on NVIDIA/CUDA, AMD/ROCm, or CPU, selecting the best available backend automatically. It does not modify the pinned upstream source, substitute a retriever, or approximate the attack. `ragnarok setup` downloads all assets and reconstructs the poisoned top-5 once; `ragnarok run` is cache-only and gives the identical frozen contexts to every selected model and quantization.

The released clean Contriever rankings determine the five clean document IDs and scores for each target case. RAGnarok streams each BEIR `corpus.jsonl` and retains only those referenced documents, then applies the official query/adversarial-passage embeddings, dot scores, merge, and descending top-5. Streaming changes only corpus I/O and avoids loading multi-gigabyte corpora into RAM. Verified ZIP archives are removed after preparation.
