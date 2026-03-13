#!/usr/bin/env python3
"""
test_email_evaluators.py — Standalone test for all email evaluators.

Run from the autoloop/ directory:
    python3 test_email_evaluators.py

Tests each evaluator independently so you can confirm they're working
before running the full loop. Postmark requires internet access.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from email_sample.evaluator_email import (
    evaluator_email_spam_score,
    evaluator_email_deliverability,
    evaluator_email_accessibility,
    evaluator_email_cta_focus,
    evaluator_postmark_spam,
)

ARTIFACT_PATH = "email_sample/artifact.html"

# ── Load artifact ──────────────────────────────────────────────────────────────

with open(ARTIFACT_PATH, encoding="utf-8") as f:
    html = f.read()

print(f"Loaded: {ARTIFACT_PATH} ({len(html):,} bytes)\n")
print("=" * 60)

# ── Run each evaluator ─────────────────────────────────────────────────────────

evaluators = [
    ("email_spam_score     (local)",  evaluator_email_spam_score,     {}),
    ("email_deliverability (local)",  evaluator_email_deliverability,  {}),
    ("email_accessibility  (local)",  evaluator_email_accessibility,   {}),
    ("email_cta_focus      (local)",  evaluator_email_cta_focus,       {}),
    ("postmark_spam        (API)  ",  evaluator_postmark_spam, {
        "subject":      "Quick question about your team's workflow",
        "from_address": "alex@syncflow.io",
        "sa_threshold": 10.0,
    }),
]

results = []
all_passed = True

for label, fn, config in evaluators:
    print(f"\n▶ {label}")
    try:
        score, reasoning = fn(ARTIFACT_PATH, html, config, verbose=True)
        status = "✓" if isinstance(score, float) and 0 <= score <= 100 else "✗ BAD SCORE"
        print(f"  Score:    {score}/100  {status}")
        print(f"  Reasoning: {reasoning[:120]}")
        results.append((label, score, None))
        if "✗" in status:
            all_passed = False
    except Exception as e:
        print(f"  ✗ ERROR: {e}")
        results.append((label, None, str(e)))
        all_passed = False

# ── Summary ────────────────────────────────────────────────────────────────────

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)

for label, score, error in results:
    if error:
        print(f"  ✗  {label}  →  ERROR: {error}")
    else:
        bar = "█" * int(score / 5)
        print(f"  ✓  {label}  →  {score:5.1f}/100  {bar}")

print()
if all_passed:
    print("✅ All evaluators working. You're good to run the loop.")
else:
    print("⚠  Some evaluators failed. Fix errors above before running the loop.")
print()
