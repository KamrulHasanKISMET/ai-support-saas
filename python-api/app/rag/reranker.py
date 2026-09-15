class Reranker:
    """
    20 candidates -> re-rank -> top 5 -> relevance filter -> top 3
    (architecture doc section 12).

    MVP scoring: weighted sum of vector similarity + keyword rank.
    Swap this for a learned re-ranker later without touching callers.
    """

    def __init__(self, vector_weight: float = 0.7, keyword_weight: float = 0.3):
        self.vector_weight = vector_weight
        self.keyword_weight = keyword_weight

    def rerank(
        self, candidates: list[dict], top_k: int = 5, min_score: float = 0.15
    ) -> list[dict]:
        scored = [
            {
                **c,
                "score": self.vector_weight * c.get("vector_score", 0.0)
                + self.keyword_weight * c.get("keyword_score", 0.0),
            }
            for c in candidates
        ]
        scored.sort(key=lambda c: c["score"], reverse=True)
        filtered = [c for c in scored if c["score"] >= min_score]
        return filtered[:top_k]


reranker = Reranker()
