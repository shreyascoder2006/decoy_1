# Email phishing-text model: what was measured and what it means

**Date:** 2026-10-31 · **Reproduce:** `python scripts/benchmark_email_models.py --show-errors`

The email check scores links, sender and attachments with fixed rules and adds a
pretrained text model's verdict on the *wording* as one more finding
(`MODEL_PHISHING_TEXT`). This page records how the model was chosen and why it
is capped.

## Evidence base, and its limit

60 emails **written by the assistant** (`scripts/email_samples.py`): 30
legitimate, 30 phishing, many deliberately hard (bank OTPs, "sale ends
tonight", password resets, polite fraud with no pressure words). They are
synthetic, so this is a first screen, not an accuracy figure. Real mail is
messier. Run `scripts/mbox_to_samples.py` on your own Gmail export and re-run the
benchmark before trusting any model.

## Candidates

| Model | Result |
|---|---|
| `cybersectony/phishing-email-detection-distilbert_v2.4.1` | Generic labels (`LABEL_0..3`). Best index (1) AUC 0.78. At 0.5: caught 22/30, **11/30 false alarms** (OTP, shipping, receipts). Rejected. |
| `ptouch/phishing-distilbert-cyber207` | Generic labels (`LABEL_0/1`). Index 1 AUC 0.77 (index 0 is its mirror, 0.23). At 0.5: caught 15/30, **3/30 false alarms** (a sale email, an accountant reminder, a refund notice). **Chosen**, as the least noisy. |
| `ealvaradob/bert-finetuned-phishing` | Failed to load (needs a `pytorch_model.bin` that this environment could not load). Not measured. |
| `mrm8488/bert-tiny-finetuned-sms-spam-detection` | Generic labels; not usable without a measured override. Not pursued. |

Both usable models have **generic labels**, so which index means "phishing" is
not stated by the model. `EMAIL_MODEL_PHISHING_INDEXES=1` is an explicit choice
fitted on these 60 samples, and the backend refuses a generic-label model
without it. Neither model cleared the 0.85 AUC bar the benchmark sets, so this
is a weak signal, kept deliberately small.

## How it is used

- Points = `round(40 x P(phishing))`, only when P >= 0.5. The 40-point cap sits
  **below the 55-point "high" bar**: the model alone can make an email amber,
  never red. A red banner needs a corroborating rule.
- Scores from this model are near 0 or 1 (over-confident), so treat the
  percentage as the model's own number, not a probability.
- Failure is never a verdict: if the model cannot load or run, the response says
  `model: unavailable` and the rules still score the email.
- Chat has its own model settings (`CHAT_MODEL_*`), measured separately; see below.

## Observed through the real endpoint (first look, 8 samples)

```text
classic phishing (P01, P03)      model 99%+  -> high  (model + urgency/credential/payment rules)
tricky legit: "sale ends tonight" model 99.98% -> medium, from the model alone  (FALSE ALARM)
polite fraud (P04, P15, P16)      model ~0%   -> missed by the model; P04 caught by a rule, P15/P16 not at all
latency                           ~50-150 ms per email after a ~12 s first load
```

## WhatsApp chat text

Measured separately on 40 synthetic chat-style messages (`scripts/chat_samples.py`:
20 legitimate, 20 scam, including tricky ones such as OTP messages, "call me
urgently", "this is my new number" and money between friends). Run:
`python scripts/benchmark_email_models.py --set chat --show-errors`.

| Model (index 1 = scam) | AUC | Scam caught | False alarms on legitimate chat |
|---|---|---|---|
| `ptouch/phishing-distilbert-cyber207` @ 0.5 | 0.938 | 13/20 | 2/20 (money between friends, a promo) |
| `ptouch/phishing-distilbert-cyber207` @ **0.95** | 0.938 | 12/20 | **0/20** |
| `cybersectony/...v2.4.1` @ 0.5 | 0.892 | 19/20 | **9/20**, rejected |
| `mrm8488/bert-tiny-finetuned-sms-spam-detection` | 0.752 | not pursued | not pursued |

`ptouch` is used for chat at a **0.95 threshold**, a strict setting fitted on
these 40 samples (one tricky promo scored 0.939, just under it, so the margin is
thin). It does better on chat than on email because chat scams are short and
blunt. Same cap as email: at most 40 points, below the "high" bar, so the model
needs a corroborating rule to turn a message red.

Observed through the real endpoint: bulk-scam wording and gift-card/OTP requests
reach high; "Hi Mum, I lost my phone" (model ~17%) lands on rules alone at
medium; a QR-code refund scam (model ~53%) is missed entirely. Latency is about
80-100 ms per message.

## Known limits

1. It misses polite business-email-compromise wording entirely.
2. It false-alarms on promotional and notice-style legitimate mail.
3. Fitted and measured on the same 60 synthetic samples, so the numbers flatter it.
4. English only, most likely.
5. Other model families exist; none better was found that loaded cleanly here.
