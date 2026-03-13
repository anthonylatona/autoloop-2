"""
evaluator_email.py — Hard metric evaluators for HTML email optimization.

These are fully deterministic — no API calls, no external services.
Each evaluator parses the HTML and returns a 0–100 score.

Evaluators:
  email_spam_score      — Counts spam trigger words and patterns
  email_deliverability  — Structural checks: unsubscribe, address, size, ratios
  email_accessibility   — Alt text coverage, semantic structure
  email_cta_focus       — Penalizes multiple competing CTAs

Usage in eval_config.yaml:
  evaluators:
    - name: email_spam_score
      weight: 0.25
    - name: email_deliverability
      weight: 0.25
    - name: email_accessibility
      weight: 0.15
    - name: email_cta_focus
      weight: 0.1
    - name: llm_judge
      weight: 0.25
"""

import re
import json
import urllib.request
from html.parser import HTMLParser


# ── HTML parsing helpers ───────────────────────────────────────────────────────

class EmailParser(HTMLParser):
    """Parses an HTML email into its components for analysis."""

    def __init__(self):
        super().__init__()
        self.text_content   = []
        self.images         = []          # list of {src, alt} dicts
        self.links          = []          # list of href strings
        self.cta_buttons    = []          # links styled as buttons
        self.has_unsubscribe = False
        self.has_address    = False
        self.has_viewport   = False
        self._in_style      = False
        self._in_script     = False
        self.style_content  = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == "style":
            self._in_style = True
        if tag == "script":
            self._in_script = True

        if tag == "meta":
            name    = attrs_dict.get("name", "").lower()
            content = attrs_dict.get("content", "").lower()
            if name == "viewport" or "width=device-width" in content:
                self.has_viewport = True

        if tag == "img":
            self.images.append({
                "src": attrs_dict.get("src", ""),
                "alt": attrs_dict.get("alt", None)   # None means missing, "" means empty
            })

        if tag == "a":
            href = attrs_dict.get("href", "")
            self.links.append(href)
            # Detect unsubscribe links
            if any(word in href.lower() or word in str(attrs_dict.get("class", "")).lower()
                   for word in ["unsubscribe", "optout", "opt-out", "remove"]):
                self.has_unsubscribe = True
            # Detect CTA buttons (by class name or inline style)
            style = attrs_dict.get("style", "").lower()
            cls   = attrs_dict.get("class", "").lower()
            if ("button" in cls or "cta" in cls
                    or "background-color" in style or "background:" in style
                    or "background-color:" in style):
                self.cta_buttons.append(href)

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_style:
            self.style_content.append(data)
            return
        if self._in_script:
            return
        stripped = data.strip()
        if stripped:
            self.text_content.append(stripped)
            # Detect physical address (rough heuristic)
            if re.search(r'\d+\s+\w+\s+(st|ave|blvd|rd|street|avenue|drive|way|lane)', stripped, re.I):
                self.has_address = True
            if any(word in stripped.lower() for word in ["unsubscribe", "opt out", "opt-out"]):
                self.has_unsubscribe = True


def parse_email(html: str) -> EmailParser:
    parser = EmailParser()
    parser.feed(html)
    return parser


def get_plain_text(parser: EmailParser) -> str:
    return " ".join(parser.text_content)


# ── Spam trigger word list ─────────────────────────────────────────────────────

SPAM_TRIGGERS = [
    # Urgency / pressure
    "urgent", "act now", "act immediately", "limited time", "expires soon",
    "don't miss out", "last chance", "now or never", "today only",
    # Money / promises
    "guaranteed", "guarantee", "100% free", "absolutely free", "free offer",
    "no obligation", "risk-free", "risk free", "double your", "triple your",
    "earn money", "make money", "extra income", "lose weight",
    # Superlatives
    "revolutionary", "once in a lifetime", "best in class", "best-in-class",
    "amazing", "incredible", "unbelievable", "mind-blowing", "game-changer",
    "award-winning", "number one", "#1 rated",
    # Spam openers
    "dear friend", "congratulations", "you have been selected", "you're a winner",
    "winner winner",
    # CAN-SPAM bait
    "click here", "click below", "click now",
    # Exclamation spam
    "!!!",
]

SPAM_PATTERNS = [
    r'\b[A-Z]{4,}\b',           # ALL CAPS WORDS (4+ chars)
    r'!{2,}',                    # Multiple exclamation marks
    r'\${1,}',                   # Dollar signs
    r'%\s*off\b',               # "% off"
    r'free\b',                   # standalone "free"
]


# ── Evaluator functions ────────────────────────────────────────────────────────

def evaluator_email_spam_score(artifact_path, artifact_text, config, **kwargs):
    """
    Counts spam trigger words and patterns in the email text and subject line.
    Each trigger found deducts points. Score starts at 100.

    Config options:
      penalty_per_trigger: points deducted per trigger found (default: 8)
      penalty_per_pattern: points deducted per pattern match (default: 5)
    """
    penalty_trigger = config.get("penalty_per_trigger", 8)
    penalty_pattern = config.get("penalty_per_pattern", 5)

    parser    = parse_email(artifact_text)
    plaintext = get_plain_text(parser).lower()
    html_lower = artifact_text.lower()

    triggers_found = []
    for trigger in SPAM_TRIGGERS:
        if trigger.lower() in plaintext:
            triggers_found.append(trigger)

    patterns_found = []
    for pattern in SPAM_PATTERNS:
        matches = re.findall(pattern, artifact_text)
        if matches:
            patterns_found.extend(matches[:3])  # cap at 3 per pattern

    total_penalty = (len(triggers_found) * penalty_trigger +
                     len(patterns_found) * penalty_pattern)
    score = max(0.0, 100.0 - total_penalty)

    if triggers_found or patterns_found:
        reasoning = (
            f"Found {len(triggers_found)} spam triggers: {', '.join(triggers_found[:5])}. "
            f"Found {len(patterns_found)} pattern matches (ALL CAPS, !!!, etc)."
        )
    else:
        reasoning = "No spam triggers or patterns detected."

    return round(score, 1), reasoning


def evaluator_email_deliverability(artifact_path, artifact_text, config, **kwargs):
    """
    Structural deliverability checks. Scores based on presence/absence of
    required elements and ratio-based signals.

    Checks:
      - Unsubscribe link present (CAN-SPAM required)
      - Physical mailing address present (CAN-SPAM required)
      - Viewport meta tag (mobile rendering)
      - HTML file size (under 102KB recommended)
      - Image-to-text ratio (too many images = spam signal)
      - Number of external links (excessive links = spam signal)
      - Alt text on images (accessibility + spam signal if missing on all)
    """
    parser    = parse_email(artifact_text)
    plaintext = get_plain_text(parser)
    issues    = []
    score     = 100.0

    # CAN-SPAM: unsubscribe link
    if not parser.has_unsubscribe:
        issues.append("Missing unsubscribe link (-20)")
        score -= 20

    # CAN-SPAM: physical address
    if not parser.has_address:
        issues.append("Missing physical mailing address (-15)")
        score -= 15

    # Mobile: viewport meta
    if not parser.has_viewport:
        issues.append("Missing viewport meta tag (-10)")
        score -= 10

    # File size (bytes)
    size_bytes = len(artifact_text.encode("utf-8"))
    size_kb    = size_bytes / 1024
    if size_kb > 102:
        penalty = min(15, int((size_kb - 102) / 10) * 3)
        issues.append(f"HTML size {size_kb:.1f}KB exceeds 102KB recommendation (-{penalty})")
        score -= penalty

    # Image to text ratio
    text_word_count = len(plaintext.split())
    img_count       = len(parser.images)
    if img_count > 0 and text_word_count < img_count * 20:
        issues.append(f"High image-to-text ratio: {img_count} images, {text_word_count} words (-8)")
        score -= 8

    # External link count
    external_links = [l for l in parser.links if l.startswith("http")]
    if len(external_links) > 8:
        penalty = min(10, (len(external_links) - 8) * 2)
        issues.append(f"{len(external_links)} external links (>8 is a spam signal) (-{penalty})")
        score -= penalty

    score = max(0.0, score)

    if issues:
        reasoning = " | ".join(issues)
    else:
        reasoning = f"All structural checks passed. Size: {size_kb:.1f}KB, {len(parser.links)} links, {img_count} images."

    return round(score, 1), reasoning


def evaluator_email_accessibility(artifact_path, artifact_text, config, **kwargs):
    """
    Checks alt text coverage on images. Missing alt text hurts both
    accessibility (screen readers) and spam filter scores.

    Score = percentage of images with non-empty alt text.
    A bonus is given if all images have meaningful (>3 char) alt text.
    """
    parser = parse_email(artifact_text)

    if not parser.images:
        return 100.0, "No images found — full score."

    with_alt     = [img for img in parser.images if img["alt"] is not None and len(img["alt"]) > 0]
    meaningful   = [img for img in parser.images if img["alt"] and len(img["alt"]) > 3]
    total        = len(parser.images)
    missing      = [img for img in parser.images if img["alt"] is None]

    coverage = len(with_alt) / total
    score    = coverage * 100

    reasoning = (
        f"{len(with_alt)}/{total} images have alt text "
        f"({len(meaningful)} meaningful, {len(missing)} missing entirely)."
    )
    return round(score, 1), reasoning


def evaluator_email_cta_focus(artifact_path, artifact_text, config, **kwargs):
    """
    Penalizes emails with multiple competing CTAs. One clear CTA = full score.
    Each additional CTA deducts points. More than 3 CTAs scores near zero.

    The ideal cold outreach email has exactly one ask.
    """
    parser   = parse_email(artifact_text)
    cta_count = len(parser.cta_buttons)

    if cta_count == 0:
        return 40.0, "No CTA buttons detected — email has no clear call to action."
    elif cta_count == 1:
        return 100.0, "Single focused CTA — ideal for cold outreach."
    elif cta_count == 2:
        return 65.0, f"Two CTAs found — split focus reduces conversion. Pick one."
    elif cta_count == 3:
        return 35.0, f"Three CTAs — overwhelming for a cold prospect. Needs one clear ask."
    else:
        return max(0.0, 100.0 - (cta_count * 20)), f"{cta_count} CTAs detected — far too many for cold outreach."


# ── Local SpamAssassin evaluator ─────────────────────────────────────────────

def evaluator_postmark_spam(artifact_path, artifact_text, config, **kwargs):
    """
    Runs the email through local SpamAssassin (spamassassin CLI).
    Same engine as Postmark's API — but local, offline, zero latency.

    Install: sudo apt install spamassassin

    SA score < 1 = clean, 5+ = likely spam, 10+ = definite spam.
    We invert to 0-100 (lower SA = higher our score).

    Config options:
      from_address: sender address for headers (default: "sender@example.com")
      sa_threshold: SA score that maps to our score of 0 (default: 10.0)
    """
    import subprocess
    from email.utils import formatdate, make_msgid

    verbose      = kwargs.get("verbose", False)
    from_addr    = config.get("from_address", "sender@example.com")
    to_addr      = config.get("to_address", "recipient@example.com")
    sa_threshold = config.get("sa_threshold", 10.0)

    # Extract subject from H1, fall back to title tag
    h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', artifact_text, re.I | re.S)
    if h1_match:
        subject = re.sub(r'<[^>]+>', '', h1_match.group(1)).strip()
    else:
        title_match = re.search(r'<title[^>]*>(.*?)</title>', artifact_text, re.I | re.S)
        subject = title_match.group(1).strip() if title_match else "Email"

    # Headers must be ASCII
    subject = (subject
        .replace('\u2014', '-').replace('\u2013', '-')
        .replace('\u2018', "'").replace('\u2019', "'")
        .replace('\u201c', '"').replace('\u201d', '"'))
    subject = subject.encode('ascii', errors='ignore').decode('ascii')

    raw_email = (
        f"From: {from_addr}\r\n"
        f"To: {to_addr}\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {formatdate()}\r\n"
        f"Message-ID: {make_msgid()}\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/html; charset=UTF-8\r\n"
        f"\r\n"
        f"{artifact_text}"
    )

    if verbose:
        print("      [postmark_spam] Running SpamAssassin locally...", end=" ", flush=True)

    try:
        result = subprocess.run(
            ["spamassassin", "-t"],
            input=raw_email,
            capture_output=True,
            text=True,
            timeout=60
        )
    except FileNotFoundError:
        return 50.0, "spamassassin not installed — run: sudo apt install spamassassin"
    except subprocess.TimeoutExpired:
        last_score = config.get("_last_score")
        if last_score is not None:
            return float(last_score), "SpamAssassin timed out — using previous score"
        return 50.0, "SpamAssassin timed out — no prior score, using 50"

    if verbose:
        print("done")

    # Parse X-Spam-Status header for score and rules
    status_match = re.search(
        r'X-Spam-Status:.*?score=([-\d.]+).*?tests=([^\n]+)',
        result.stdout, re.S
    )
    if not status_match:
        return 50.0, "Could not parse SpamAssassin output"

    sa_score = float(status_match.group(1))
    rules_raw = status_match.group(2).strip().rstrip(",")
    # SA wraps long rule lists across lines with whitespace
    rules_clean = re.sub(r'[\r\n\s]+', ' ', rules_raw).strip()
    fired = [r.strip() for r in rules_clean.split(',') if r.strip()]

    our_score = max(0.0, 100.0 * (1.0 - sa_score / sa_threshold))

    rules_str = ", ".join(fired[:8]) if fired else "none"
    reasoning = (
        f"SA score: {sa_score:.2f}/10 -> our score: {our_score:.0f}/100. "
        f"Subject: \"{subject[:50]}\". "
        f"Rules fired: {rules_str}"
    )

    return round(our_score, 1), reasoning

# ── Registry export ────────────────────────────────────────────────────────────
# Import this dict in autoloop_meta_eval.py and merge it into EVALUATOR_REGISTRY

EMAIL_EVALUATORS = {
    "email_spam_score":     evaluator_email_spam_score,
    "email_deliverability": evaluator_email_deliverability,
    "email_accessibility":  evaluator_email_accessibility,
    "email_cta_focus":      evaluator_email_cta_focus,
    "postmark_spam":        evaluator_postmark_spam,
}
