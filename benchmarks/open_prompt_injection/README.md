# Open-Prompt-Injection benchmark package

`upstream/` is an unmodified Git submodule pinned to commit `95290f7ce3794c4c52ad3fe8113db2bfcdfe89e0` from the official Open-Prompt-Injection repository.

RAGnarok does not store replacement cases or prompts in this directory. Official dataset loaders create ignored `.npz` caches below the upstream `data/` directory on first use. The adapter is implemented in `src/ragnarok/benchmarks/open_prompt_injection.py`.

The current adapter follows the pinned repository's `run.py` experiment path: the `combine` attack, no defense, three inference phases, and the native `PNA-T`, `PNA-I`, `ASV`, and `MR` evaluator outputs.

Native Windows cannot represent the upstream cache names used by SMS Spam and HSOL because their official `icl_split` contains `:`. Those task configurations must be run under Linux or WSL; RAGnarok does not rename the path.
