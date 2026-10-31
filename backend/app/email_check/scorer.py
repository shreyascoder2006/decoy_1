"""Rule-based phishing / scam signal scoring for a single email.

Pure functions only: no network, no model, no I/O, so every rule is unit
testable and the result is reproducible. The score is a sum of fixed rule
weights capped at 100. It is a *triage signal*, not a probability, and a low
score never means an email is safe.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from app.email_check.schemas import EmailAnalyzeRequest, Finding

HIGH_AT = 55
MEDIUM_AT = 25

# Brand token -> registrable domains that legitimately send as that brand.
# Deliberately small: a display name that claims a brand from any other domain
# is the signal, so the list only needs the brands scammers impersonate most.
BRAND_DOMAINS: dict[str, tuple[str, ...]] = {
    "paypal": ("paypal.com",),
    "microsoft": ("microsoft.com", "microsoftonline.com", "outlook.com"),
    "google": ("google.com", "gmail.com", "youtube.com"),
    "amazon": ("amazon.com", "amazon.in", "amazon.co.uk", "amazonses.com"),
    "apple": ("apple.com", "icloud.com"),
    "netflix": ("netflix.com",),
    "whatsapp": ("whatsapp.com", "meta.com", "facebookmail.com"),
    "facebook": ("facebook.com", "facebookmail.com", "meta.com"),
    "dhl": ("dhl.com",),
    "fedex": ("fedex.com",),
    "hdfc": ("hdfcbank.com", "hdfcbank.net"),
    "icici": ("icicibank.com",),
    "sbi": ("sbi.co.in",),
}

SHORTENERS = frozenset(
    {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "cutt.ly", "rb.gy", "ow.ly"}
)
SUSPICIOUS_TLDS = frozenset(
    {"zip", "mov", "top", "xyz", "click", "icu", "rest", "gq", "tk", "ml", "cf"}
)
# Second-level suffixes where the registrable domain has three labels.
TWO_LEVEL_SUFFIXES = frozenset(
    {"co.uk", "org.uk", "co.in", "com.au", "co.jp", "com.br", "co.za", "com.sg"}
)

EXECUTABLE_EXTENSIONS = frozenset(
    {"exe", "scr", "js", "vbs", "bat", "cmd", "jar", "iso", "lnk", "msi", "html", "htm"}
)
MACRO_EXTENSIONS = frozenset({"docm", "xlsm", "pptm"})
DOCUMENT_EXTENSIONS = frozenset({"pdf", "doc", "docx", "xls", "xlsx", "txt", "jpg", "png"})

URGENCY = re.compile(
    r"\b(urgent(ly)?|immediately|act now|final notice|account "
    r"(?:(?:is|was|has been|been) )?(?:suspended|locked|disabled)|"
    r"within \d+ hours?|verify your account|"
    r"last warning)\b",
    re.IGNORECASE,
)
CREDENTIALS = re.compile(
    r"\b(password|one[- ]time (?:password|code)|otp|verify your identity|"
    r"card number|cvv|social security|login to confirm|confirm your login)\b",
    re.IGNORECASE,
)
PAYMENT_FRAUD = re.compile(
    r"\b(new bank details|changed (?:our )?bank|updated bank (?:details|account)|"
    r"wire transfer|gift cards?|bitcoin|crypto(?:currency)? wallet|"
    r"send (?:the )?payment to)\b",
    re.IGNORECASE,
)

# Chat-only: patterns that are routine scam openers on messaging apps but
# unremarkable in email, so they are scored only for the chat channel.
SCAM_OFFER = re.compile(
    r"\b(you(?:'ve| have)? won|lottery|claim your (?:prize|reward|gift)|"
    r"work from home|part[- ]time job|guaranteed returns?|investment opportunity|"
    r"double your money|kyc (?:update|verification|expired)|"
    r"earn\s+\S*\d[\d,]*\s*(?:per|a|/)\s*(?:day|hour|week))\b",
    re.IGNORECASE,
)
FAMILY_IMPERSONATION = re.compile(
    r"\b(hi (?:mum|mom|dad)|(?:this is )?my new (?:phone )?number|"
    r"lost my phone|changed my number)\b",
    re.IGNORECASE,
)

URL_LIKE_TEXT = re.compile(
    r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?#]\S*)?$", re.IGNORECASE
)

MAX_LINK_FINDINGS = 3


@dataclass(frozen=True)
class _Host:
    host: str
    registrable: str


def registrable_domain(host: str) -> str:
    """Approximate eTLD+1. Handles common two-level suffixes, not the full PSL."""
    labels = host.lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _unwrap_redirect(href: str) -> str:
    """Gmail and WhatsApp wrap links in redirects (google.com/url?q=, l.wl.co/l?u=); judge the target."""
    try:
        parsed = urlparse(href)
        domain = registrable_domain(parsed.hostname) if parsed.hostname else ""
        query = parse_qs(parsed.query)
        target = ""
        if domain == "google.com" and parsed.path == "/url":
            target = (query.get("q") or query.get("url") or [""])[0]
        elif domain == "wl.co" and parsed.path.startswith("/l"):
            # WhatsApp wraps outbound links as l.wl.co/l?u=<real>.
            target = (query.get("u") or [""])[0]
        if target.startswith(("http://", "https://")):
            return target
    except ValueError:
        pass
    return href


def _host_of(url: str) -> _Host | None:
    candidate = url if "://" in url else f"http://{url}"
    try:
        host = urlparse(candidate).hostname
    except ValueError:
        return None
    if not host:
        return None
    return _Host(host=host, registrable=registrable_domain(host))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _sender_findings(request: EmailAnalyzeRequest) -> list[Finding]:
    findings: list[Finding] = []
    email = request.sender_email.lower()
    if "@" not in email:
        return findings
    sender_domain = registrable_domain(email.rsplit("@", 1)[1])
    name = request.sender_name.lower()

    for brand, domains in BRAND_DOMAINS.items():
        if re.search(rf"\b{re.escape(brand)}\b", name) and sender_domain not in domains:
            findings.append(
                Finding(
                    code="SENDER_BRAND_MISMATCH",
                    message=(
                        f"Sender name mentions {brand.title()} but the address is "
                        f"from {sender_domain}."
                    ),
                    points=30,
                )
            )
            break

    # A display name that is itself an address/domain different from the real one.
    shown = re.search(r"[a-z0-9._%+-]+@([a-z0-9.-]+\.[a-z]{2,})", name)
    if shown and registrable_domain(shown.group(1)) != sender_domain:
        findings.append(
            Finding(
                code="SENDER_NAME_SPOOFS_ADDRESS",
                message=(
                    f"Sender name shows an address at {shown.group(1)} but the "
                    f"message came from {sender_domain}."
                ),
                points=30,
            )
        )
    return findings


def _link_findings(request: EmailAnalyzeRequest) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()

    for link in request.links:
        href = _unwrap_redirect(link.href.strip())
        target = _host_of(href)
        if target is None:
            continue
        found: list[Finding] = []
        text = link.text.strip()

        if URL_LIKE_TEXT.match(text):
            shown = _host_of(text)
            if shown and shown.registrable != target.registrable:
                found.append(
                    Finding(
                        code="LINK_TEXT_MISMATCH",
                        message=(
                            f"A link reads {shown.registrable} but goes to "
                            f"{target.registrable}."
                        ),
                        points=35,
                    )
                )
        if _is_ip(target.host):
            found.append(
                Finding(
                    code="LINK_IP_HOST",
                    message="A link points to a raw IP address instead of a domain.",
                    points=25,
                )
            )
        if "xn--" in target.host:
            found.append(
                Finding(
                    code="LINK_PUNYCODE",
                    message=f"A link uses an internationalised look-alike domain ({target.host}).",
                    points=20,
                )
            )
        if target.registrable in SHORTENERS:
            found.append(
                Finding(
                    code="LINK_SHORTENER",
                    message=f"A link is shortened ({target.registrable}), hiding its destination.",
                    points=10,
                )
            )
        if target.host.rsplit(".", 1)[-1] in SUSPICIOUS_TLDS:
            found.append(
                Finding(
                    code="LINK_SUSPICIOUS_TLD",
                    message=f"A link uses the .{target.host.rsplit('.', 1)[-1]} domain ending, common in scams.",
                    points=10,
                )
            )
        if href.lower().startswith("http://"):
            found.append(
                Finding(
                    code="LINK_NOT_HTTPS",
                    message="A link does not use HTTPS.",
                    points=10,
                )
            )

        for finding in found:
            key = f"{finding.code}:{target.host}"
            if key in seen:
                continue
            seen.add(key)
            findings.append(finding)

    return findings[: MAX_LINK_FINDINGS * 2]


def _attachment_findings(request: EmailAnalyzeRequest) -> list[Finding]:
    findings: list[Finding] = []
    for name in request.attachments:
        parts = name.lower().strip().rsplit(".", 2)
        ext = parts[-1] if len(parts) > 1 else ""
        previous = parts[-2] if len(parts) > 2 else ""
        if ext in EXECUTABLE_EXTENSIONS and previous in DOCUMENT_EXTENSIONS:
            findings.append(
                Finding(
                    code="ATTACHMENT_DOUBLE_EXTENSION",
                    message=f"Attachment '{name}' disguises a .{ext} file as a document.",
                    points=35,
                )
            )
        elif ext in EXECUTABLE_EXTENSIONS:
            findings.append(
                Finding(
                    code="ATTACHMENT_EXECUTABLE",
                    message=f"Attachment '{name}' is a .{ext} file that can run code.",
                    points=30,
                )
            )
        elif ext in MACRO_EXTENSIONS:
            findings.append(
                Finding(
                    code="ATTACHMENT_MACRO",
                    message=f"Attachment '{name}' is a macro-enabled Office file.",
                    points=20,
                )
            )
    return findings[:3]


def _text_findings(request: EmailAnalyzeRequest) -> list[Finding]:
    text = f"{request.subject}\n{request.body}"
    findings: list[Finding] = []
    urgency = {m.group(0).lower() for m in URGENCY.finditer(text)}
    if urgency:
        findings.append(
            Finding(
                code="URGENCY_LANGUAGE",
                message="Pressure wording: " + ", ".join(sorted(urgency)[:3]) + ".",
                points=10 if len(urgency) == 1 else 20,
            )
        )
    if CREDENTIALS.search(text):
        findings.append(
            Finding(
                code="CREDENTIAL_REQUEST",
                message="Asks for a password, code or identity/card details.",
                points=15,
            )
        )
    if PAYMENT_FRAUD.search(text):
        findings.append(
            Finding(
                code="PAYMENT_REQUEST",
                message="Mentions changed bank details, wire transfers, gift cards or crypto.",
                points=25,
            )
        )
    return findings


REASONS: dict[str, str] = {
    "SENDER_BRAND_MISMATCH": (
        "Scammers put a trusted company's name in the display name because most "
        "mail apps show only the name. The real sending address belongs to an "
        "unrelated domain, so the message is not from that company."
    ),
    "SENDER_NAME_SPOOFS_ADDRESS": (
        "The display name shows one email address while the message was sent from "
        "another. This is a deliberate trick to look legitimate at a glance."
    ),
    "LINK_TEXT_MISMATCH": (
        "The visible link text names one website but clicking goes to another. "
        "This is the most common phishing technique: you expect a familiar site "
        "and land on a look-alike that collects your login."
    ),
    "LINK_IP_HOST": (
        "Genuine companies link to named domains. A raw IP address usually means "
        "a throwaway server set up to collect data, and it dodges domain-based "
        "reputation checks."
    ),
    "LINK_PUNYCODE": (
        "Domains starting with xn-- can display look-alike characters (for "
        "example a Cyrillic 'a' for a Latin 'a'), making a fake site appear to be "
        "a real one."
    ),
    "LINK_SHORTENER": (
        "Shortened links hide the real destination, so neither you nor a mail "
        "filter can see where it leads before you click."
    ),
    "LINK_SUSPICIOUS_TLD": (
        "These domain endings are cheap or free and are over-represented among "
        "throwaway scam sites. On its own this is weak evidence."
    ),
    "LINK_NOT_HTTPS": (
        "The link is unencrypted. Reputable sites that ask for logins or "
        "payments use HTTPS, so this is a weak signal that adds to others."
    ),
    "ATTACHMENT_DOUBLE_EXTENSION": (
        "A name like invoice.pdf.exe shows a document ending first while the "
        "file is really a program. The disguise exists to get you to run it."
    ),
    "ATTACHMENT_EXECUTABLE": (
        "Programs and scripts can run code on your computer when opened, and "
        "legitimate senders rarely email them."
    ),
    "ATTACHMENT_MACRO": (
        "Macro-enabled Office files can run code once macros are enabled and "
        "are a common route for delivering malware."
    ),
    "URGENCY_LANGUAGE": (
        "Pressure to act immediately is used to stop you from checking first. "
        "Genuine services rarely threaten closure within hours. Wording alone is "
        "weak evidence, because real alerts can also sound urgent."
    ),
    "CREDENTIAL_REQUEST": (
        "Legitimate companies do not ask you to send passwords, one-time codes or "
        "card details by email. Asking for them is the core of credential phishing."
    ),
    "PAYMENT_REQUEST": (
        "Changed bank details, wire transfers, gift cards and crypto are the "
        "standard routes for invoice and impersonation fraud, because those "
        "payments are hard to reverse."
    ),
}


REASONS["SCAM_OFFER"] = (
    "Prize wins, easy-money jobs, guaranteed returns and 'KYC expired' notices "
    "are the standard openers of messaging-app scams. They are sent in bulk to "
    "strangers and aim to move you to a payment or a link."
)
REASONS["FAMILY_IMPERSONATION"] = (
    "'Hi Mum, this is my new number' is a well-known scam: the sender poses as a "
    "relative on a new number, then asks for urgent money. Confirm by calling "
    "the person's old number."
)


REASONS["MODEL_PHISHING_TEXT"] = (
    "A pretrained language model, trained on phishing and legitimate emails, "
    "rates the wording of this message as phishing-like. It reads only the text, "
    "so it cannot see hidden link targets or the real sender; its confidence is "
    "the model's own score, not a calibrated probability."
)


def with_model_finding(
    findings: list[Finding], p_phishing: float, *, threshold: float, max_points: int
) -> tuple[int, str, list[Finding], int]:
    """Add the model's verdict as a finding and re-score. Returns score, level, findings, raw.

    Points scale with the model's confidence and are capped at ``max_points``,
    so the model alone stays below the "high" bar and needs corroboration.
    """
    combined = list(findings)
    if p_phishing >= threshold:
        combined.append(
            Finding(
                code="MODEL_PHISHING_TEXT",
                message=f"Language model rates the wording as phishing-like ({round(100 * p_phishing)}% confidence).",
                points=round(max_points * p_phishing),
                reason=REASONS["MODEL_PHISHING_TEXT"],
            )
        )
    combined.sort(key=lambda f: f.points, reverse=True)
    raw = sum(f.points for f in combined)
    score = min(100, raw)
    return score, level_for(score), combined, raw


def level_for(score: int) -> str:
    if score >= HIGH_AT:
        return "high"
    if score >= MEDIUM_AT:
        return "medium"
    return "low"


def _chat_findings(request: EmailAnalyzeRequest) -> list[Finding]:
    findings: list[Finding] = []
    if SCAM_OFFER.search(request.body):
        findings.append(
            Finding(
                code="SCAM_OFFER",
                message="Reads like a prize, easy-money or fake-notice scam message.",
                points=25,
            )
        )
    if FAMILY_IMPERSONATION.search(request.body):
        findings.append(
            Finding(
                code="FAMILY_IMPERSONATION",
                message="Claims to be a relative on a new number.",
                points=20,
            )
        )
    return findings


def score_email(
    request: EmailAnalyzeRequest, *, chat: bool = False
) -> tuple[int, str, list[Finding], int]:
    """Return (score 0-100, level, findings sorted by weight, raw points).

    ``chat=True`` adds the messaging-scam rules; everything else is shared.
    """
    if not request.body.strip() and not request.links and not request.attachments:
        return 0, "unknown", [], 0
    findings = (
        _sender_findings(request)
        + _link_findings(request)
        + _attachment_findings(request)
        + _text_findings(request)
        + (_chat_findings(request) if chat else [])
    )
    findings = [f.model_copy(update={"reason": REASONS.get(f.code, "")}) for f in findings]
    findings.sort(key=lambda f: f.points, reverse=True)
    raw = sum(f.points for f in findings)
    score = min(100, raw)
    return score, level_for(score), findings, raw
