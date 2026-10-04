"""
OWN INTENT MODEL -- Phase 6 (Stage 4 of GENERAL_LANGUAGE_BRAIN.md §6).

A small TRAINED classifier over sentence embeddings: multinomial
logistic regression (softmax), L2-regularised, full-batch gradient
descent, plus temperature scaling on a validation split so its
confidence can be compared with the LLM's and with the cluster matcher.

Why this and not a bigger model: it is the smallest thing that is
genuinely *learned* (weights fitted to verified labels) rather than
nearest-neighbour lookup, runs on numpy alone (already a dependency),
trains in seconds, is deterministic, and serialises to plain JSON so it
can be versioned in `language_models`. The choice of technology is NOT
final (§6 defers it); everything downstream talks to the small
interface below, so a different model can replace it.

Interface:  IntentModel.fit(X, y) / predict_proba(X) / to_dict / from_dict

What this is NOT:
  * not wired into serving -- nothing on the request path loads it;
  * not multilingual by itself -- it inherits whatever the embedding
    model gives; per-language quality is measured in model_eval.py;
  * not trusted -- it must pass model_eval.passes_gate() and then go
    through shadow/canary (model_registry.py) before it may answer.

Privacy: the artifact holds weights and label names only. No training
text, no tenant text, no person-related field (§5.3).

Pure module: numpy + stdlib. Deterministic (no randomness at all).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import numpy as np

MODEL_KIND = "softmax_regression_v1"


class ModelError(ValueError):
    """Bad shapes / labels / artifact. Training data problems are loud."""


def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def _normalize_rows(X: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(X, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return X / n


@dataclass
class IntentModel:
    labels: list[str] = field(default_factory=list)
    dim: int = 0
    W: np.ndarray | None = None          # (dim, n_labels)
    b: np.ndarray | None = None          # (n_labels,)
    temperature: float = 1.0             # >1 softens; fitted on validation
    l2: float = 1e-3
    epochs: int = 300
    lr: float = 0.5
    trained_on: int = 0

    # ── training ──────────────────────────────────────────────────
    def fit(self, X, y: list[str]) -> "IntentModel":
        X = np.asarray(X, dtype=np.float64)
        if X.ndim != 2 or X.shape[0] == 0:
            raise ModelError("X must be a non-empty 2-D array")
        if len(y) != X.shape[0]:
            raise ModelError("X and y length differ")
        if not np.isfinite(X).all():
            raise ModelError("X contains NaN/inf")
        labels = sorted(set(y))
        if len(labels) < 2:
            raise ModelError("need at least 2 distinct labels to train")
        idx = {l: i for i, l in enumerate(labels)}
        t = np.array([idx[l] for l in y])
        Xn = _normalize_rows(X)
        n, d = Xn.shape
        k = len(labels)
        Y = np.zeros((n, k))
        Y[np.arange(n), t] = 1.0
        # class-balanced weights: a rare intent must not be drowned out.
        counts = Y.sum(axis=0)
        cw = (n / (k * counts))[t]
        cw = cw / cw.mean()
        W = np.zeros((d, k))
        b = np.zeros(k)
        for _ in range(self.epochs):
            P = _softmax(Xn @ W + b)
            G = (P - Y) * cw[:, None] / n
            W -= self.lr * (Xn.T @ G + self.l2 * W)
            b -= self.lr * G.sum(axis=0)
        self.labels, self.dim, self.W, self.b = labels, d, W, b
        self.trained_on = int(n)
        self.temperature = 1.0
        return self

    def calibrate_temperature(self, X, y: list[str]) -> float:
        """Fit a single temperature by grid search on validation NLL.
        Labels unseen in training are skipped (they cannot be scored)."""
        self._require_fitted()
        X = np.asarray(X, dtype=np.float64)
        keep = [i for i, l in enumerate(y) if l in self.labels]
        if len(keep) < 2:
            return self.temperature
        Z = self._logits(X[keep])
        t = np.array([self.labels.index(y[i]) for i in keep])
        best_T, best = 1.0, float("inf")
        for T in np.arange(0.5, 5.01, 0.1):
            P = _softmax(Z / T)
            nll = -np.log(np.clip(P[np.arange(len(t)), t], 1e-12, 1.0)).mean()
            if nll < best:
                best, best_T = nll, float(T)
        self.temperature = round(best_T, 2)
        return self.temperature

    # ── inference ─────────────────────────────────────────────────
    def _require_fitted(self) -> None:
        if self.W is None or self.b is None:
            raise ModelError("model is not fitted")

    def _logits(self, X) -> np.ndarray:
        X = np.asarray(X, dtype=np.float64)
        if X.ndim == 1:
            X = X[None, :]
        if X.shape[1] != self.dim:
            raise ModelError(f"expected dim {self.dim}, got {X.shape[1]}")
        return _normalize_rows(X) @ self.W + self.b

    def predict_proba(self, X) -> np.ndarray:
        self._require_fitted()
        return _softmax(self._logits(X) / self.temperature)

    def predict(self, X) -> list[tuple[str, float]]:
        """[(label, calibrated confidence)] per row."""
        P = self.predict_proba(X)
        return [(self.labels[int(i)], float(P[r, i])) for r, i in enumerate(P.argmax(axis=1))]

    # ── serialisation ─────────────────────────────────────────────
    def to_dict(self) -> dict:
        self._require_fitted()
        return {
            "kind": MODEL_KIND,
            "labels": list(self.labels),
            "dim": self.dim,
            "temperature": self.temperature,
            "trained_on": self.trained_on,
            "W": np.round(self.W, 6).tolist(),
            "b": np.round(self.b, 6).tolist(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "IntentModel":
        if not isinstance(d, dict) or d.get("kind") != MODEL_KIND:
            raise ModelError("unknown model artifact kind")
        try:
            W = np.array(d["W"], dtype=np.float64)
            b = np.array(d["b"], dtype=np.float64)
            labels = [str(x) for x in d["labels"]]
            dim = int(d["dim"])
            temp = float(d["temperature"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError(f"malformed model artifact: {exc}") from exc
        if W.shape != (dim, len(labels)) or b.shape != (len(labels),):
            raise ModelError("artifact shapes do not match labels/dim")
        if not (np.isfinite(W).all() and np.isfinite(b).all()) or temp <= 0:
            raise ModelError("artifact has non-finite values or bad temperature")
        m = cls(labels=labels, dim=dim, W=W, b=b, temperature=temp)
        m.trained_on = int(d.get("trained_on", 0))
        return m

    def fingerprint(self) -> str:
        """Stable content hash of the artifact (identity for the registry)."""
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()
