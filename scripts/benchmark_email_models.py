#!/usr/bin/env python3
"""Benchmark pretrained phishing-text models on labelled sample emails.

Why this exists: on audio, 8 of 11 community models were inverted on our data,
so no model is trusted until it has been measured here. Each model's label
names are read from its own config (never assumed), a model whose labels cannot
be mapped is refused, and results are reported as AUC, a separation margin,
and false alarms on the *hard* legitimate samples.

Usage (from the repo root, with the project venv):
    .venv\\Scripts\\python.exe scripts\\benchmark_email_models.py
    .venv\\Scripts\\python.exe scripts\\benchmark_email_models.py --models a/b c/d --show-errors

Samples default to the synthetic set in scripts/email_samples.py. Pass
--samples-dir with human/ and phish/ .txt files to benchmark real mail.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from email_samples import LEGIT, PHISH, SAMPLES, as_text  # noqa: E402

DEFAULT_MODELS = [
    "cybersectony/phishing-email-detection-distilbert_v2.4.1",
    "ptouch/phishing-distilbert-cyber207",
    "ealvaradob/bert-finetuned-phishing",
    "mrm8488/bert-tiny-finetuned-sms-spam-detection",
]
MAX_TOKENS = 512
MIN_USABLE_AUC = 0.85

_POSITIVE = re.compile(r"(unsafe|phish|spam|malicious|fraud|scam)", re.IGNORECASE)
_NEGATIVE = re.compile(r"(legit|ham|benign|safe|genuine|clean|normal|not[ _-]|non[ _-])", re.IGNORECASE)


def classify_label(name: str) -> str | None:
    """'pos' (phishing-like), 'neg' (legitimate-like) or None when uninformative."""
    text = str(name)
    negated = re.search(r"(not|non)[ _-]?(phish|spam|malicious|fraud|scam)", text, re.IGNORECASE)
    if negated or (_NEGATIVE.search(text) and not re.match(r"unsafe", text, re.IGNORECASE)):
        return "neg"
    if _POSITIVE.search(text):
        return "pos"
    return None


def label_map(id2label: dict) -> dict[int, str] | None:
    mapping = {int(i): classify_label(n) for i, n in id2label.items()}
    kinds = set(mapping.values())
    if None in kinds or "pos" not in kinds or "neg" not in kinds:
        return None
    return mapping  # type: ignore[return-value]


def auc(pos: list[float], neg: list[float]) -> float:
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def load_samples(samples_dir: str | None, which: str = "email") -> list[tuple[str, str, bool, str]]:
    """(id, label, hard, text)."""
    if not samples_dir:
        if which == "chat":
            import chat_samples  # noqa: PLC0415

            return [(s[0], s[1], s[2], chat_samples.as_text(s)) for s in chat_samples.SAMPLES]
        return [(s[0], s[1], s[2], as_text(s)) for s in SAMPLES]
    root = pathlib.Path(samples_dir)
    out = []
    for label, folder in ((LEGIT, "human"), (PHISH, "phish")):
        for path in sorted((root / folder).glob("*.txt")):
            out.append((path.stem, label, False, path.read_text(encoding="utf-8", errors="replace")))
    return out


def score_model(model_id: str, samples) -> dict:
    import torch  # noqa: PLC0415
    from transformers import AutoModelForSequenceClassification, AutoTokenizer  # noqa: PLC0415

    started = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForSequenceClassification.from_pretrained(model_id).eval()
    id2label = dict(model.config.id2label)
    result = {"model": model_id, "labels": id2label, "load_s": time.monotonic() - started}

    mapping = label_map(id2label)

    all_probs = []
    started = time.monotonic()
    for _id, _label, _hard, text in samples:
        encoded = tokenizer(text, truncation=True, max_length=MAX_TOKENS, return_tensors="pt")
        with torch.no_grad():
            all_probs.append(torch.softmax(model(**encoded).logits, dim=-1)[0].tolist())
    result["ms_per_email"] = 1000 * (time.monotonic() - started) / len(samples)
    result["probs"] = all_probs

    if mapping is None:
        result["refused"] = "labels do not identify a phishing and a legitimate class"
        return result
    result["scores"] = [sum(p for i, p in enumerate(probs) if mapping[i] == "pos") for probs in all_probs]
    return result


def index_report(result: dict, samples) -> list[tuple[int, float]]:
    """AUC of each output index used alone as the 'phishing' score.

    For a model whose labels are uninformative this is the only evidence of what
    each index means. It is fitted on these samples, so treat it as a lead to be
    confirmed on real mail, never as proof.
    """
    probs = result["probs"]
    rows = []
    for index in range(len(probs[0])):
        pos = [p[index] for p, s in zip(probs, samples) if s[1] == PHISH]
        neg = [p[index] for p, s in zip(probs, samples) if s[1] == LEGIT]
        rows.append((index, auc(pos, neg)))
    return rows


def summarise(result: dict, samples, threshold: float) -> dict:
    scores = result["scores"]
    pos = [s for s, x in zip(scores, samples) if x[1] == PHISH]
    neg = [s for s, x in zip(scores, samples) if x[1] == LEGIT]
    hard_neg = [s for s, x in zip(scores, samples) if x[1] == LEGIT and x[2]]
    hard_pos = [s for s, x in zip(scores, samples) if x[1] == PHISH and x[2]]
    return {
        "auc": auc(pos, neg),
        "margin": min(pos) - max(neg),
        "detect": sum(s >= threshold for s in pos),
        "n_pos": len(pos),
        "false_alarm": sum(s >= threshold for s in neg),
        "n_neg": len(neg),
        "hard_fa": sum(s >= threshold for s in hard_neg),
        "n_hard_neg": len(hard_neg),
        "hard_miss": sum(s < threshold for s in hard_pos),
        "n_hard_pos": len(hard_pos),
        "median_pos": sorted(pos)[len(pos) // 2],
        "median_neg": sorted(neg)[len(neg) // 2],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", dest="which", choices=["email", "chat"], default="email",
                        help="built-in synthetic sample set to use when --samples-dir is not given")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--samples-dir", help="folder with human/ and phish/ .txt files")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--show-errors", action="store_true", help="list misclassified samples per model")
    args = parser.parse_args()

    samples = load_samples(args.samples_dir, args.which)
    n_pos = sum(s[1] == PHISH for s in samples)
    print(f"{len(samples)} samples ({n_pos} phishing, {len(samples) - n_pos} legitimate), threshold {args.threshold}\n")

    rows = []
    for model_id in args.models:
        print(f"== {model_id}")
        try:
            result = score_model(model_id, samples)
        except Exception as exc:  # noqa: BLE001 - network, licence gate, bad repo
            print(f"   FAILED to load/score: {type(exc).__name__}: {exc}\n")
            continue
        print(f"   labels: {result['labels']}   load {result['load_s']:.1f}s")
        if "refused" in result:
            print(f"   REFUSED: {result['refused']}")
            for index, value in index_report(result, samples):
                note = "separates phishing high" if value >= 0.85 else "separates legitimate high" if value <= 0.15 else "weak"
                print(f"   index {index} as phishing score: AUC {value:.3f} ({note})")
            print("   -> labels are a guess; do not use without confirming on real mail\n")
            continue
        summary = summarise(result, samples, args.threshold)
        flags = []
        if summary["auc"] < 0.5:
            flags.append("INVERTED")
        elif summary["auc"] < MIN_USABLE_AUC:
            flags.append("WEAK")
        print(
            f"   AUC {summary['auc']:.3f}  margin {summary['margin']:+.3f}  "
            f"detected {summary['detect']}/{summary['n_pos']}  "
            f"false alarms {summary['false_alarm']}/{summary['n_neg']} "
            f"(hard legit {summary['hard_fa']}/{summary['n_hard_neg']})  "
            f"hard phishing missed {summary['hard_miss']}/{summary['n_hard_pos']}  "
            f"{result['ms_per_email']:.0f} ms/email  {' '.join(flags)}"
        )
        print(f"   median score: phishing {summary['median_pos']:.3f}, legitimate {summary['median_neg']:.3f}")
        if args.show_errors:
            for score, (sid, label, hard, text) in zip(result["scores"], samples):
                wrong = (label == PHISH and score < args.threshold) or (label == LEGIT and score >= args.threshold)
                if wrong:
                    # Private samples are never echoed: id and score only.
                    preview = "" if args.samples_dir else f": {text[:70]!r}"
                    print(f"     wrong: {sid} {'(hard) ' if hard else ''}{label} scored {score:.3f}{preview}")
        print()
        rows.append((model_id, summary))

    if not rows:
        print("No model produced a usable result.")
        return 2
    rows.sort(key=lambda r: (r[1]["auc"], r[1]["margin"]), reverse=True)
    print("Ranking (AUC, then margin):")
    for model_id, s in rows:
        print(f"  {s['auc']:.3f}  {s['margin']:+.3f}  {model_id}")
    best = rows[0][1]
    if best["auc"] < MIN_USABLE_AUC:
        print(f"\nNo model reached AUC {MIN_USABLE_AUC}. Do not ship a model verdict on this evidence.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
