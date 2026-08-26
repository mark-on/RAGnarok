"""Thesis-facing RAG security taxonomy and benchmark coverage declarations."""

BENCHMARK_COVERAGE = {
    "poisonedrag": {
        "track": "Indirect (classic RAG)",
        "provenance": "sleeepeer/PoisonedRAG @ f660d721",
        "adapter": "Unchanged Contriever algorithm; CPU/CUDA/ROCm device and provider transport adapter",
        "entry_vectors": ["Knowledge-base poisoning", "Adversarial retrieval targeting"],
        "pipeline_stages": ["Knowledge base", "Retriever", "Retrieved context", "Generator output"],
        "objectives": ["Knowledge corruption", "Evidence manipulation", "Goal hijacking"],
        "techniques": ["Embedding optimization", "Retrieval bait", "Fabricated evidence"],
        "cia": ["Integrity"],
    },
    "mpib": {
        "track": "Direct + Indirect (classic RAG)",
        "provenance": "jhlee0619/mpib-eval @ ad615aae; dataset v1.1",
        "adapter": "Official blind prompt; paper Appendix E Judge rubric; gated V2 payload validation",
        "entry_vectors": ["Direct prompt injection", "Indirect injection through retrieved documents"],
        "pipeline_stages": ["User query", "Retrieved context", "Generator output"],
        "objectives": ["Instruction override", "Clinical harm", "Evidence manipulation"],
        "techniques": [
            "Urgency", "Authority", "Rule inversion", "Format coercion", "System contradiction",
            "Benign-looking override", "Fabricated citations", "Provenance spoofing", "Dose tweak",
        ],
        "cia": ["Integrity"],
    },
    "spikee": {
        "track": "Direct",
        "provenance": "ReversecLabs/spikee v0.9.1; seeds-cybersec-2026-01",
        "adapter": "Official dataset generator, target, and native basic judges; frozen deterministic 300-case profile",
        "entry_vectors": ["Direct prompt injection", "Adversarial application input"],
        "pipeline_stages": ["User query", "Application input", "Generator output"],
        "objectives": ["System prompt leakage", "Data exfiltration", "XSS", "Resource exhaustion"],
        "techniques": ["Instruction override", "Canary extraction", "Markup injection", "Resource abuse"],
        "cia": ["Confidentiality", "Integrity", "Availability"],
    },
    "agentdojo": {
        "track": "Agentic",
        "provenance": "ethz-spylab/agentdojo v0.1.35; benchmark v1.2.2",
        "adapter": "Official task suites, agent pipeline, tool environment, attacks, and security checks",
        "entry_vectors": ["Indirect injection through untrusted tool data"],
        "pipeline_stages": ["Tool output", "Agent context", "Tool invocation", "External state"],
        "objectives": ["Unauthorized action", "Goal hijacking", "Data modification"],
        "techniques": ["Tool-knowledge attack", "Injected external content", "Instruction override"],
        "cia": ["Confidentiality", "Integrity"],
    },
}

KNOWN_GAPS = [
    "Dedicated RAG knowledge-base extraction",
    "PDF and multimodal prompt injection",
    "Persistent memory poisoning and model backdoors",
    "Cross-tenant isolation failures",
]
