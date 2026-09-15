def build_knowledge_context(chunks: list[dict]) -> str:
    """Formats re-ranked knowledge chunks into a block the LLM can cite from."""
    if not chunks:
        return "No relevant knowledge found."
    parts = [f"[{i + 1}] {c['content']}" for i, c in enumerate(chunks)]
    return "\n".join(parts)
