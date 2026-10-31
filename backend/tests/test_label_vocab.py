"""Label normalisation tests.

These use the *actual* id2label maps published by real community models. Two
of them are mutually inverted, which is exactly the failure this module exists
to prevent: assuming "index 0 means fake" would make the Gustking model report
every human caller as AI and every AI caller as human, silently.
"""

from __future__ import annotations

import pytest

from app.live_call.label_vocab import (
    LABEL_FAKE,
    LABEL_REAL,
    UnmappableLabels,
    build_label_map,
    normalise_label,
    parse_label_override,
)

# Taken from each model's published config.json.
REAL_WORLD_CONFIGS = {
    "mo-thecreator/Deepfake-audio-detection": (
        {"0": "fake", "1": "real"},
        {0: LABEL_FAKE, 1: LABEL_REAL},
    ),
    "MelodyMachine/Deepfake-audio-detection-V2": (
        {"0": "fake", "1": "real"},
        {0: LABEL_FAKE, 1: LABEL_REAL},
    ),
    "Hemgg/Deepfake-audio-detection": (
        {"0": "AIVoice", "1": "HumanVoice"},
        {0: LABEL_FAKE, 1: LABEL_REAL},
    ),
    "Gustking/wav2vec2-large-xlsr-deepfake-audio-classification": (
        {"0": "real", "1": "fake"},
        {0: LABEL_REAL, 1: LABEL_FAKE},
    ),
    "Om-Parab/distilhubert-finetuned-audio-deepfake-in-the-wild": (
        {"0": "bonafide", "1": "spoof"},
        {0: LABEL_REAL, 1: LABEL_FAKE},
    ),
}


@pytest.mark.parametrize(
    "model_id,config,expected",
    [(name, cfg, exp) for name, (cfg, exp) in REAL_WORLD_CONFIGS.items()],
)
def test_real_world_model_label_maps(model_id, config, expected):
    assert build_label_map(config) == expected, model_id


def test_the_two_inverted_models_map_to_opposite_indices():
    """The regression that matters most: inversion must be detected."""
    forward = build_label_map({"0": "fake", "1": "real"})
    inverted = build_label_map({"0": "real", "1": "fake"})
    assert forward[0] == LABEL_FAKE and inverted[0] == LABEL_REAL
    assert forward[1] == LABEL_REAL and inverted[1] == LABEL_FAKE
    assert forward != inverted


# --- single label normalisation -----------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "fake", "FAKE", " Fake ", "spoof", "spoofed", "synthetic", "AIVoice",
        "ai_voice", "AI-Voice", "deepfake", "tts", "cloned", "generated",
        "fake_audio", "class_spoof",
    ],
)
def test_fake_synonyms(raw):
    assert normalise_label(raw) == LABEL_FAKE


@pytest.mark.parametrize(
    "raw",
    [
        "real", "REAL", "bonafide", "bona_fide", "HumanVoice", "human",
        "genuine", "authentic", "natural", "human-speech", "real_audio",
    ],
)
def test_real_synonyms(raw):
    assert normalise_label(raw) == LABEL_REAL


@pytest.mark.parametrize("raw", ["LABEL_0", "LABEL_1", "0", "1", "", "   ", "class_a", "xyz"])
def test_meaningless_labels_are_not_guessed(raw):
    assert normalise_label(raw) is None


def test_a_label_naming_both_classes_is_ambiguous():
    # "real_vs_fake" names both; guessing either way would be a coin flip.
    assert normalise_label("real_vs_fake") is None


# --- map construction refusals -------------------------------------------


def test_default_huggingface_labels_are_refused():
    """A model that never set its labels must not be used."""
    with pytest.raises(UnmappableLabels, match="unrecognised label"):
        build_label_map({"0": "LABEL_0", "1": "LABEL_1"})


def test_the_refusal_explains_the_escape_hatch():
    with pytest.raises(UnmappableLabels, match="LIVE_CALL_LOCAL_LABEL_OVERRIDE"):
        build_label_map({"0": "LABEL_0", "1": "LABEL_1"})


def test_empty_config_is_refused():
    with pytest.raises(UnmappableLabels, match="no id2label"):
        build_label_map({})


def test_single_class_model_is_refused():
    with pytest.raises(UnmappableLabels, match="both"):
        build_label_map({"0": "fake"})


def test_duplicate_classes_are_refused():
    with pytest.raises(UnmappableLabels, match="duplicate"):
        build_label_map({"0": "fake", "1": "spoof", "2": "real"})


def test_non_integer_index_is_refused():
    with pytest.raises(UnmappableLabels, match="non-integer"):
        build_label_map({"first": "fake", "second": "real"})


def test_integer_keys_also_work():
    # transformers sometimes hands back int keys rather than strings.
    assert build_label_map({0: "fake", 1: "real"}) == {0: LABEL_FAKE, 1: LABEL_REAL}


def test_a_multiclass_model_with_two_real_classes_is_refused():
    """Refusing beats summing: a 3-class head needs a deliberate decision.

    The error points at the override, so a user who knows the model's
    semantics can still use it.
    """
    with pytest.raises(UnmappableLabels, match="duplicate"):
        build_label_map({"0": "fake", "1": "real", "2": "human"})


# --- override parsing ----------------------------------------------------


def test_override_parses():
    assert parse_label_override("0=fake,1=real") == {0: LABEL_FAKE, 1: LABEL_REAL}
    assert parse_label_override("1=fake, 0=real") == {1: LABEL_FAKE, 0: LABEL_REAL}


def test_override_tolerates_whitespace_and_trailing_comma():
    assert parse_label_override(" 0 = spoof , 1 = bonafide , ") == {
        0: LABEL_FAKE,
        1: LABEL_REAL,
    }


@pytest.mark.parametrize(
    "spec,match",
    [
        ("", "empty"),
        ("0fake,1real", "malformed"),
        ("x=fake,1=real", "non-integer"),
        ("0=banana,1=real", "not recognisable"),
        ("0=fake,1=spoof", "both"),
    ],
)
def test_bad_overrides_are_refused(spec, match):
    with pytest.raises(UnmappableLabels, match=match):
        parse_label_override(spec)
