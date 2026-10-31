"""Pretrained phishing-text classifier, run in-process.

The model reads the wording of a message. It cannot see where a link really
goes, who really sent the mail or what an attachment really is, so its verdict
is added as one more finding next to the rule findings and never replaces them.

Properties carried over from the audio detector:

* **Label meaning comes from the model, never from an assumption.** Names are
  read from the model's own config. A model whose labels are generic
  (``LABEL_0``) is refused unless ``EMAIL_MODEL_PHISHING_INDEXES`` states which
  output indexes mean phishing -- an explicit, visible decision.
* **Failure is never a verdict.** If the model cannot load or run, the caller
  gets ``None`` and the response says the model was unavailable; no score is
  invented.
* torch and transformers are imported lazily so the backend and its tests run
  without them.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass

logger = logging.getLogger("app.email_check.model")

MAX_TOKENS = 512

_POSITIVE = re.compile(r"(unsafe|phish|spam|malicious|fraud|scam)", re.IGNORECASE)
_NEGATIVE = re.compile(r"(legit|ham|benign|safe|genuine|clean|normal|not[ _-]|non[ _-])", re.IGNORECASE)
_NEGATED_POSITIVE = re.compile(r"(not|non)[ _-]?(phish|spam|malicious|fraud|scam)", re.IGNORECASE)


class ModelUnavailable(Exception):
    """The model could not be loaded or its labels could not be understood."""


def classify_label(name: str) -> str | None:
    """'pos' (phishing-like), 'neg' (legitimate-like) or None when uninformative."""
    text = str(name)
    if _NEGATED_POSITIVE.search(text):
        return "neg"
    if re.match(r"unsafe", text, re.IGNORECASE):
        return "pos"
    if _NEGATIVE.search(text):
        return "neg"
    if _POSITIVE.search(text):
        return "pos"
    return None


def build_phishing_indexes(id2label: dict, override: str = "") -> set[int]:
    """Indexes whose probability mass counts as 'phishing'.

    Raises ModelUnavailable when the labels do not say and there is no override.
    """
    indexes = {int(i) for i in id2label}
    if override.strip():
        chosen = {int(part) for part in override.split(",") if part.strip()}
        if not chosen or not chosen <= indexes or chosen == indexes:
            raise ModelUnavailable(
                f"EMAIL_MODEL_PHISHING_INDEXES={override!r} must name some, but not all, of {sorted(indexes)}"
            )
        return chosen
    kinds = {int(i): classify_label(name) for i, name in id2label.items()}
    if None in kinds.values() or set(kinds.values()) != {"pos", "neg"}:
        raise ModelUnavailable(
            "the model's labels do not say which class is phishing; set "
            "EMAIL_MODEL_PHISHING_INDEXES after checking it with "
            "scripts/benchmark_email_models.py"
        )
    return {i for i, kind in kinds.items() if kind == "pos"}


@dataclass
class EmailModel:
    model_id: str
    tokenizer: object
    model: object
    phishing_indexes: set[int]

    def p_phishing(self, text: str) -> float:
        """Probability mass on the phishing classes. Synchronous; call in a thread."""
        import torch  # noqa: PLC0415

        encoded = self.tokenizer(
            text, truncation=True, max_length=MAX_TOKENS, return_tensors="pt"
        )
        with torch.no_grad():
            probs = torch.softmax(self.model(**encoded).logits, dim=-1)[0].tolist()
        return float(sum(p for i, p in enumerate(probs) if i in self.phishing_indexes))


_cache: dict[tuple[str, str], EmailModel] = {}
_lock = threading.Lock()


def load_model(model_id: str, phishing_indexes_override: str = "") -> EmailModel:
    """Load (once per process) and return the model. Raises ModelUnavailable."""
    key = (model_id, phishing_indexes_override)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
        try:
            import torch  # noqa: PLC0415, F401
            from transformers import (  # noqa: PLC0415
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )
        except ImportError as exc:
            raise ModelUnavailable(
                "the email model needs torch and transformers: "
                "pip install -r requirements-local-model.txt"
            ) from exc

        logger.info("loading email model %s", model_id)
        try:
            tokenizer = AutoTokenizer.from_pretrained(model_id)
            model = AutoModelForSequenceClassification.from_pretrained(model_id).eval()
        except Exception as exc:  # noqa: BLE001 - network, disk, bad repo
            raise ModelUnavailable(f"could not load {model_id!r}: {type(exc).__name__}") from exc

        indexes = build_phishing_indexes(dict(model.config.id2label), phishing_indexes_override)
        logger.info("email model ready (phishing indexes %s)", sorted(indexes))
        loaded = EmailModel(model_id, tokenizer, model, indexes)
        _cache[key] = loaded
        return loaded
