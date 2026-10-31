#!/usr/bin/env python3
"""Compare local detector models on your own labelled samples.

Why this exists: community audio-deepfake models vary enormously in what they
generalise to, and the most downloaded one is not necessarily the best. The
first model tried during development flagged 0 of 3 real neural-TTS samples
and 1 of 6 genuine human samples -- anti-correlated, and worse than useless.
Choosing a model without measuring it is how a demo ends up confidently
calling a human caller an AI.

Point this at two directories of 16 kHz mono WAV files -- one of genuine human
speech, one of AI-generated speech from whatever engine you actually care about
-- and it reports, per model, the detection rate, the false-alarm rate and the
separation between the two classes.

Usage:
    python scripts/benchmark_detectors.py --human-dir wav/human --fake-dir wav/ai
    python scripts/benchmark_detectors.py --human-dir h --fake-dir f \
        --models mo-thecreator/Deepfake-audio-detection,Hemgg/Deepfake-audio-detection

Convert samples first if needed:
    ffmpeg -i input.mp3 -ar 16000 -ac 1 -c:a pcm_s16le output.wav
"""

from __future__ import annotations

import argparse
import pathlib
import statistics
import struct
import sys
import wave

DEFAULT_MODELS = [
    "mo-thecreator/Deepfake-audio-detection",
    "MelodyMachine/Deepfake-audio-detection-V2",
    "Hemgg/Deepfake-audio-detection",
    "Gustking/wav2vec2-large-xlsr-deepfake-audio-classification",
    "Om-Parab/distilhubert-finetuned-audio-deepfake-in-the-wild",
]

WINDOW_SECONDS = 4.0
SAMPLE_RATE = 16000

# A usable model needs separated score CLUSTERS, not merely correct ordering.
MIN_USABLE_MARGIN = 0.2


def read_window(path: pathlib.Path, seconds: float) -> list[float] | None:
    """Read the first `seconds` of a 16 kHz mono 16-bit WAV as floats."""
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getframerate() != SAMPLE_RATE:
                print(f"  skipping {path.name}: {handle.getframerate()} Hz, need {SAMPLE_RATE}")
                return None
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                print(f"  skipping {path.name}: need mono 16-bit")
                return None
            raw = handle.readframes(int(seconds * SAMPLE_RATE))
    except (wave.Error, OSError) as exc:
        print(f"  skipping {path.name}: {exc}")
        return None

    count = len(raw) // 2
    if count < SAMPLE_RATE:  # under one second is not worth scoring
        return None
    return [s / 32768.0 for s in struct.unpack(f"<{count}h", raw[: count * 2])]


def auc(fake_scores: list[float], human_scores: list[float]) -> float:
    """Probability a random AI clip scores above a random human clip.

    1.0 is perfect separation, 0.5 is a coin flip, below 0.5 means the model
    is inverted on this data.
    """
    if not fake_scores or not human_scores:
        return float("nan")
    wins = 0.0
    for f in fake_scores:
        for h in human_scores:
            if f > h:
                wins += 1.0
            elif f == h:
                wins += 0.5
    return wins / (len(fake_scores) * len(human_scores))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--human-dir", required=True, help="directory of genuine human WAVs")
    parser.add_argument("--fake-dir", required=True, help="directory of AI-generated WAVs")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--window-seconds", type=float, default=WINDOW_SECONDS)
    parser.add_argument("--thresholds", default="0.5,0.9")
    args = parser.parse_args(argv[1:])

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
    from app.live_call.local_model_client import ModelEngine  # noqa: PLC0415
    from app.live_call.resemble_client import DetectorUnavailable  # noqa: PLC0415

    human_dir = pathlib.Path(args.human_dir)
    fake_dir = pathlib.Path(args.fake_dir)
    thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]

    print("loading samples")
    human = [(p, w) for p in sorted(human_dir.glob("*.wav"))
             if (w := read_window(p, args.window_seconds)) is not None]
    fake = [(p, w) for p in sorted(fake_dir.glob("*.wav"))
            if (w := read_window(p, args.window_seconds)) is not None]
    print(f"  {len(human)} human, {len(fake)} AI-generated\n")
    if not human or not fake:
        print("FAIL: need at least one usable sample in each directory")
        return 1

    summary = []
    for model_id in [m.strip() for m in args.models.split(",") if m.strip()]:
        print("=" * 76)
        print(model_id)
        print("=" * 76)
        try:
            engine = ModelEngine.load(model_id, "cpu")
        except DetectorUnavailable as exc:
            print(f"  UNUSABLE: {exc}\n")
            summary.append((model_id, None, None, None, str(exc), 0.0))
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  LOAD FAILED: {type(exc).__name__}: {exc}\n")
            summary.append((model_id, None, None, None, type(exc).__name__, 0.0))
            continue

        print(f"  labels: {({int(k): v for k, v in sorted(engine.label_map.items())})}")

        human_scores, fake_scores = [], []
        for path, window in human:
            human_scores.append(engine.classify(window).p_fake)
        for path, window in fake:
            fake_scores.append(engine.classify(window).p_fake)

        print(f"  human p_fake: mean={statistics.fmean(human_scores):.4f} "
              f"min={min(human_scores):.4f} max={max(human_scores):.4f}")
        print(f"  AI    p_fake: mean={statistics.fmean(fake_scores):.4f} "
              f"min={min(fake_scores):.4f} max={max(fake_scores):.4f}")

        separation = auc(fake_scores, human_scores)
        print(f"  AUC (1.0 perfect, 0.5 coin flip, <0.5 inverted): {separation:.3f}")

        # AUC measures ORDERING only. A model can rank every AI clip above
        # every human clip while squeezing all scores into a band too narrow
        # to place a threshold in -- one measured model scored AUC 0.922 with
        # every value between 0.479 and 0.492. Report the usable margin too.
        margin = min(fake_scores) - max(human_scores)
        print(f"  margin (lowest AI score - highest human score): {margin:+.3f}")
        if margin <= 0:
            print("    the two classes overlap; no threshold separates them cleanly")
        elif margin < MIN_USABLE_MARGIN:
            print(f"    below {MIN_USABLE_MARGIN}: too narrow to survive codec or noise shifts")

        best = None
        for threshold in thresholds:
            detected = sum(1 for s in fake_scores if s >= threshold)
            false_alarms = sum(1 for s in human_scores if s >= threshold)
            print(f"  threshold {threshold:<5} detections {detected}/{len(fake_scores)}"
                  f"   false alarms {false_alarms}/{len(human_scores)}")
            if best is None:
                best = (threshold, detected, false_alarms)

        # Recommend the midpoint of the empty band when there is one.
        if margin > 0:
            suggested = round((max(human_scores) + min(fake_scores)) / 2, 2)
            print(f"  suggested LIVE_CALL_LOCAL_FAKE_THRESHOLD: {suggested}")

        summary.append((model_id, separation, best, len(fake_scores), None, margin))
        print()

    # --- verdict ----------------------------------------------------------
    print("=" * 76)
    print("SUMMARY (ranked by separation)")
    print("=" * 76)
    usable = [s for s in summary if s[1] is not None and s[1] == s[1]]
    # Rank by margin, then AUC: a model you cannot place a threshold in is of
    # no use however well it orders the data.
    usable.sort(key=lambda s: (s[5], s[1]), reverse=True)
    for model_id, separation, best, total, _, margin in usable:
        threshold, detected, false_alarms = best
        if separation < 0.5:
            verdict = "INVERTED"
        elif margin < MIN_USABLE_MARGIN:
            verdict = "NO-MARGIN"
        elif separation >= 0.95:
            verdict = "USABLE"
        elif separation >= 0.7:
            verdict = "WEAK"
        else:
            verdict = "POOR"
        print(f"  {verdict:<9} AUC={separation:.3f} margin={margin:+.3f}  {model_id}")
        print(f"            at threshold {threshold}: {detected}/{total} detected, "
              f"{false_alarms} false alarms")
    for model_id, separation, _, _, reason, _m in summary:
        if separation is None:
            print(f"  UNUSABLE  {model_id}  ({reason})")

    if not usable:
        print("\nNo model could be scored.")
        return 1
    if usable[0][5] < MIN_USABLE_MARGIN or usable[0][1] < 0.7:
        print("\nNo model separated these samples well enough to demo. Do not ship a")
        print("verdict from any of them on this data. A high AUC with a small margin")
        print("means correct ordering but no usable threshold. Try more/different")
        print("samples, or a different model family.")
        return 2
    print(f"\nBest on this data: {usable[0][0]}")
    print("Set it with LIVE_CALL_LOCAL_MODEL_ID, and tune")
    print("LIVE_CALL_LOCAL_FAKE_THRESHOLD from the table above.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
