"""Normalise a model's own class names onto our two-label vocabulary.

This module exists because community audio-deepfake models do not agree on
label order or wording, and some are mutually inverted:

    mo-thecreator/Deepfake-audio-detection      {0: fake,     1: real}
    MelodyMachine/Deepfake-audio-detection-V2   {0: fake,     1: real}
    Hemgg/Deepfake-audio-detection              {0: AIVoice,  1: HumanVoice}
    Gustking/wav2vec2-large-xlsr-...            {0: real,     1: fake}   <-- inverted
    Om-Parab/distilhubert-...-in-the-wild       {0: bonafide, 1: spoof}

Assuming "index 0 means fake" would make the Gustking model report every human
caller as AI and every AI caller as human, with no error anywhere. So the
label map is always derived from the loaded model's own ``id2label``, and a
model whose labels carry no meaning (the ``LABEL_0`` / ``LABEL_1`` default) is
refused rather than guessed at.
"""

from __future__ import annotations

import re

LABEL_FAKE = "fake"
LABEL_REAL = "real"

_FAKE_WORDS = frozenset(
    {
        "fake",
        "spoof",
        "spoofed",
        "spoofing",
        "synthetic",
        "synthesised",
        "synthesized",
        "ai",
        "aivoice",
        "aigenerated",
        "deepfake",
        "generated",
        "tts",
        "clone",
        "cloned",
        "machine",
        "robot",
        "attack",
    }
)

_REAL_WORDS = frozenset(
    {
        "real",
        "bonafide",
        "bonfide",
        "human",
        "humanvoice",
        "genuine",
        "authentic",
        "natural",
        "live",
        "original",
        "pristine",
        "target",
    }
)


class UnmappableLabels(ValueError):
    """The model's labels cannot be mapped onto fake/real with confidence."""


def _canonical(raw: str) -> str:
    """Lowercase and strip everything that is not a letter or digit."""
    return re.sub(r"[^a-z0-9]", "", str(raw).strip().lower())


def normalise_label(raw: str) -> str | None:
    """Map one model class name onto ``fake``, ``real`` or None."""
    token = _canonical(raw)
    if not token:
        return None
    if token in _FAKE_WORDS:
        return LABEL_FAKE
    if token in _REAL_WORDS:
        return LABEL_REAL

    # Fall back to substring matching for compound names such as
    # "fake_audio", "human-speech" or "class_spoof".
    fake_hit = any(word in token for word in _FAKE_WORDS if len(word) > 3)
    real_hit = any(word in token for word in _REAL_WORDS if len(word) > 3)
    if fake_hit and not real_hit:
        return LABEL_FAKE
    if real_hit and not fake_hit:
        return LABEL_REAL
    # Ambiguous or meaningless (e.g. "LABEL_0"): refuse to guess.
    return None


def build_label_map(id2label: dict) -> dict[int, str]:
    """Derive {class index -> fake|real} from a model config's ``id2label``.

    Raises :class:`UnmappableLabels` unless every class maps and both of our
    labels are present exactly once. Anything less certain than that would be
    a coin flip on the direction of the verdict.
    """
    if not id2label:
        raise UnmappableLabels("the model config declares no id2label mapping")

    mapping: dict[int, str] = {}
    unmapped: list[str] = []
    for key, raw in id2label.items():
        try:
            index = int(key)
        except (TypeError, ValueError) as exc:
            raise UnmappableLabels(f"non-integer class index {key!r}") from exc
        normalised = normalise_label(raw)
        if normalised is None:
            unmapped.append(str(raw))
        else:
            mapping[index] = normalised

    if unmapped:
        raise UnmappableLabels(
            "cannot tell which class means synthetic speech: "
            f"unrecognised label(s) {unmapped!r}. "
            "Set LIVE_CALL_LOCAL_LABEL_OVERRIDE to e.g. '0=fake,1=real' "
            "after confirming the model's own documentation."
        )

    values = list(mapping.values())
    if LABEL_FAKE not in values or LABEL_REAL not in values:
        raise UnmappableLabels(
            f"the model does not expose both a fake and a real class: {mapping!r}"
        )
    if values.count(LABEL_FAKE) > 1 or values.count(LABEL_REAL) > 1:
        raise UnmappableLabels(
            f"ambiguous label map with duplicate classes: {mapping!r}"
        )
    return mapping


def parse_label_override(spec: str) -> dict[int, str]:
    """Parse an explicit override such as ``"0=fake,1=real"``.

    The escape hatch for a model whose labels are uninformative but whose
    documentation states the order.
    """
    mapping: dict[int, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise UnmappableLabels(f"malformed override entry {part!r}; expected index=label")
        index_text, label_text = part.split("=", 1)
        try:
            index = int(index_text.strip())
        except ValueError as exc:
            raise UnmappableLabels(f"non-integer index in override {part!r}") from exc
        normalised = normalise_label(label_text)
        if normalised is None:
            raise UnmappableLabels(
                f"override label {label_text!r} is not recognisable as fake or real"
            )
        mapping[index] = normalised
    if not mapping:
        raise UnmappableLabels("the override is empty")
    values = list(mapping.values())
    if LABEL_FAKE not in values or LABEL_REAL not in values:
        raise UnmappableLabels(f"override must name both classes: {mapping!r}")
    return mapping
