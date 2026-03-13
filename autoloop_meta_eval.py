#!/usr/bin/env python3
"""
autoloop_meta_eval.py — Meta optimization loop with pluggable evaluator registry.

Extends autoloop_meta.py with a composable evaluator system. Instead of a single
LLM judge, you can combine any mix of evaluators — each returns a 0-100 score,
and they're weighted together into a final composite score.

Built-in evaluators:
  llm_judge       — LLM scores the artifact against goals.md (default)
  readability     — Flesch-Kincaid readability (no API needed)
  word_count      — Proximity to a target word count
  pytest          — Test pass rate from a shell command
  benchmark       — Any shell command that outputs METRIC value=number
  lighthouse      — Lighthouse performance score via CLI

Configure evaluators in eval_config.yaml (see EVALUATORS.md for full docs).

Usage:
    python3 autoloop_meta_eval.py --iterations 20
    python3 autoloop_meta_eval.py --iterations 30 --meta-every 5 --verbose
    python3 autoloop_meta_eval.py --iterations 20 --eval-config eval_config.yaml
"""

import anthropic
import git
import json
import argparse
import re
import subprocess
import sys
import os
import math
from pathlib import Path
from datetime import datetime

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# ── Config ─────────────────────────────────────────────────────────────────────

ARTIFACT_PATH  = "artifact.md"   # overridden by --artifact CLI arg
GOALS_PATH     = "goals.md"      # overridden by --goals CLI arg
LOG_PATH       = "loop_log.json"
MODEL          = "claude-haiku-4-5-20251001"

# Auto-register email evaluators if the module is present
try:
    import sys as _sys
    import os as _os
    _sys.path.insert(0, _os.path.dirname(__file__))
    from email_sample.evaluator_email import EMAIL_EVALUATORS as _EMAIL_EVALUATORS
    _EMAIL_EVALUATORS_AVAILABLE = True
except ImportError:
    _EMAIL_EVALUATORS_AVAILABLE = False

# Constraints the meta-agent must respect
MIN_WEIGHT     = 5    # no criterion can drop below 5 points
MAX_WEIGHT     = 60   # no criterion can dominate above 60 points
WEIGHT_SUM     = 100  # weights must always sum to exactly 100

# ── Evaluator Registry ─────────────────────────────────────────────────────────
#
# Each evaluator is a function with this signature:
#   fn(artifact_path, artifact_text, config) -> (score: float, reasoning: str)
#
# score must be in the range 0.0–100.0
# reasoning is a short human-readable string explaining the score
# config is the evaluator's config dict from eval_config.yaml (may be empty)
#
# To add your own: define a function below and register it in EVALUATOR_REGISTRY.

def evaluator_llm_judge(artifact_path, artifact_text, config, client=None, goals="", verbose=False):
    """
    Default evaluator. LLM scores the artifact against goals.md criteria.
    This is the only evaluator that requires the Anthropic client and goals.
    """
    prompt = f"""You are a rigorous document evaluator. Score this document 0-100.

Use EXACTLY the scoring criteria and weights defined below. Do not use your own
judgment about what matters — follow the weights as written.

SCORING CRITERIA:
{goals}

DOCUMENT:
{artifact_text}

RESPOND IN THIS EXACT FORMAT:
SCORE: [number 0-100]
REASONING: [2-3 sentences — what dragged it down, what held it up]"""

    if verbose:
        print("      [llm_judge] Calling judge...")

    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text
    try:
        score     = float(raw.split("SCORE:")[1].split("\n")[0].strip())
        reasoning = raw.split("REASONING:")[1].strip() if "REASONING:" in raw else ""
    except (IndexError, ValueError):
        return 0.0, "parse error"
    return score, reasoning


def evaluator_readability(artifact_path, artifact_text, config, **kwargs):
    """
    Flesch-Kincaid readability score. No API needed — computed locally.
    Scores higher when the text hits the target reading level.

    Config options:
      target_grade: target grade level (default 10 — accessible but professional)
      tolerance:    grade levels of tolerance before penalizing (default 3)
    """
    target_grade = config.get("target_grade", 10)
    tolerance    = config.get("tolerance", 3)

    words     = artifact_text.split()
    sentences = [s.strip() for s in re.split(r'[.!?]+', artifact_text) if s.strip()]
    syllables = sum(_count_syllables(w) for w in words)

    if not sentences or not words:
        return 0.0, "Could not parse text into sentences/words"

    asl = len(words) / len(sentences)       # avg sentence length
    asw = syllables / len(words)            # avg syllables per word
    fk_grade = 0.39 * asl + 11.8 * asw - 15.59

    distance = abs(fk_grade - target_grade)
    if distance <= tolerance:
        score = 100.0
    else:
        penalty = (distance - tolerance) * 8
        score   = max(0.0, 100.0 - penalty)

    reasoning = (
        f"FK grade level: {fk_grade:.1f} (target: {target_grade}, tolerance: ±{tolerance}). "
        f"Avg sentence length: {asl:.1f} words, avg syllables/word: {asw:.2f}."
    )
    return round(score, 1), reasoning


def _count_syllables(word: str) -> int:
    """Rough syllable counter for readability scoring."""
    word = word.lower().strip(".,!?;:'\"")
    if not word:
        return 0
    vowels = "aeiouy"
    count  = 0
    prev_vowel = False
    for ch in word:
        is_vowel = ch in vowels
        if is_vowel and not prev_vowel:
            count += 1
        prev_vowel = is_vowel
    if word.endswith("e") and count > 1:
        count -= 1
    return max(1, count)


def evaluator_word_count(artifact_path, artifact_text, config, **kwargs):
    """
    Scores proximity to a target word count.
    Useful for keeping pitch decks concise or essays at a target length.

    Config options:
      target:    target word count (default 500)
      tolerance: words either side before penalizing (default 100)
    """
    target    = config.get("target", 500)
    tolerance = config.get("tolerance", 100)

    count    = len(artifact_text.split())
    distance = abs(count - target)

    if distance <= tolerance:
        score = 100.0
    else:
        over   = (distance - tolerance)
        score  = max(0.0, 100.0 - (over / target) * 200)

    reasoning = f"Word count: {count} (target: {target} ±{tolerance})"
    return round(score, 1), reasoning


def evaluator_benchmark(artifact_path, artifact_text, config, **kwargs):
    """
    Runs an arbitrary shell command and reads a METRIC line from stdout.
    The command must output at least one line in the format:
        METRIC name=value
    where value is a number. Lower is better by default (e.g. ms, errors).

    Config options:
      command:      shell command to run (required)
      metric_name:  which METRIC line to read (default: first one found)
      lower_better: if True, lower values score higher (default: True)
      baseline:     value that maps to score 0 (default: auto-detected first run)
      ceiling:      value that maps to score 100 (default: 0 if lower_better)
    """
    verbose = kwargs.get("verbose", False)
    command = config.get("command")
    if not command:
        return 0.0, "benchmark evaluator requires 'command' in config"

    metric_name  = config.get("metric_name", None)
    lower_better = config.get("lower_better", True)
    baseline     = config.get("baseline", None)
    ceiling      = config.get("ceiling", 0 if lower_better else None)

    if verbose:
        print(f"      [benchmark] Running: {command}")

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return 0.0, "benchmark command timed out after 120s"
    except Exception as e:
        return 0.0, f"benchmark command failed: {e}"

    # Parse METRIC lines
    value = None
    for line in output.splitlines():
        if line.startswith("METRIC "):
            parts = line[7:].split("=")
            if len(parts) == 2:
                name, val_str = parts[0].strip(), parts[1].strip()
                if metric_name is None or name == metric_name:
                    try:
                        value = float(val_str)
                        break
                    except ValueError:
                        continue

    if value is None:
        return 0.0, f"No METRIC line found in output. Got: {output[:200]}"

    # Score relative to baseline and ceiling
    if baseline is None:
        # No baseline set — store value, return neutral score
        config["baseline"] = value
        return 50.0, f"Baseline established: {value}. Score will be meaningful next iteration."

    if lower_better:
        if value <= (ceiling or 0):
            score = 100.0
        elif value >= baseline:
            score = 0.0
        else:
            score = 100.0 * (baseline - value) / (baseline - (ceiling or 0))
    else:
        best = ceiling if ceiling is not None else baseline * 2
        if value >= best:
            score = 100.0
        elif value <= baseline:
            score = 0.0
        else:
            score = 100.0 * (value - baseline) / (best - baseline)

    reasoning = f"Metric '{metric_name or 'first'}': {value} (baseline: {baseline}, {'lower' if lower_better else 'higher'} is better)"
    return round(max(0.0, min(100.0, score)), 1), reasoning


def evaluator_pytest(artifact_path, artifact_text, config, **kwargs):
    """
    Runs pytest and scores by pass rate.
    Useful when the artifact is code being optimized for correctness.

    Config options:
      test_path: path to test file or directory (default: "tests/")
      timeout:   seconds before giving up (default: 60)
    """
    verbose   = kwargs.get("verbose", False)
    test_path = config.get("test_path", "tests/")
    timeout   = config.get("timeout", 60)

    if verbose:
        print(f"      [pytest] Running tests in {test_path}...")

    try:
        result = subprocess.run(
            ["python", "-m", "pytest", test_path, "--tb=no", "-q"],
            capture_output=True, text=True, timeout=timeout
        )
        output = result.stdout
    except subprocess.TimeoutExpired:
        return 0.0, f"pytest timed out after {timeout}s"
    except FileNotFoundError:
        return 0.0, "pytest not found — install with: pip install pytest"

    # Parse "X passed, Y failed" from pytest output
    passed = failed = 0
    for line in output.splitlines():
        m = re.search(r'(\d+) passed', line)
        if m:
            passed = int(m.group(1))
        m = re.search(r'(\d+) failed', line)
        if m:
            failed = int(m.group(1))

    total = passed + failed
    if total == 0:
        return 50.0, "No tests found or collected"

    score     = (passed / total) * 100
    reasoning = f"pytest: {passed}/{total} tests passing ({score:.0f}%)"
    return round(score, 1), reasoning


def evaluator_lighthouse(artifact_path, artifact_text, config, **kwargs):
    """
    Runs Google Lighthouse CLI and returns the performance score.
    Requires: npm install -g lighthouse

    Config options:
      url:      URL to audit (required)
      category: which score to use: performance, accessibility, seo, best-practices (default: performance)
    """
    verbose  = kwargs.get("verbose", False)
    url      = config.get("url")
    category = config.get("category", "performance")

    if not url:
        return 0.0, "lighthouse evaluator requires 'url' in config"

    if verbose:
        print(f"      [lighthouse] Auditing {url}...")

    try:
        result = subprocess.run(
            ["lighthouse", url, "--output=json", "--quiet", "--chrome-flags=--headless"],
            capture_output=True, text=True, timeout=120
        )
        data  = json.loads(result.stdout)
        score = data["categories"][category]["score"] * 100
        reasoning = f"Lighthouse {category}: {score:.0f}/100"
        return round(score, 1), reasoning
    except subprocess.TimeoutExpired:
        return 0.0, "lighthouse timed out"
    except (json.JSONDecodeError, KeyError) as e:
        return 0.0, f"Could not parse lighthouse output: {e}"
    except FileNotFoundError:
        return 0.0, "lighthouse not found — install with: npm install -g lighthouse"


# Registry — maps name → function
EVALUATOR_REGISTRY = {
    "llm_judge":   evaluator_llm_judge,
    "readability": evaluator_readability,
    "word_count":  evaluator_word_count,
    "benchmark":   evaluator_benchmark,
    "pytest":      evaluator_pytest,
    "lighthouse":  evaluator_lighthouse,
}

# Merge email evaluators if available
if _EMAIL_EVALUATORS_AVAILABLE:
    EVALUATOR_REGISTRY.update(_EMAIL_EVALUATORS)

# Default config if no eval_config.yaml is present
DEFAULT_EVAL_CONFIG = {
    "evaluators": [
        {"name": "llm_judge", "weight": 1.0}
    ]
}


def load_eval_config(config_path: str) -> dict:
    """Load evaluator config from YAML, fall back to default."""
    if not config_path or not Path(config_path).exists():
        return DEFAULT_EVAL_CONFIG

    if not YAML_AVAILABLE:
        print("  ⚠ PyYAML not installed. Using default (llm_judge only).")
        print("    Install with: pip install pyyaml --break-system-packages")
        return DEFAULT_EVAL_CONFIG

    with open(config_path) as f:
        return yaml.safe_load(f)


def run_evaluators(artifact_path, artifact_text, eval_config, client, goals, verbose):
    """
    Run all configured evaluators and return a weighted composite score.
    Returns (composite_score, breakdown_dict, combined_reasoning)
    """
    evaluators = eval_config.get("evaluators", [{"name": "llm_judge", "weight": 1.0}])
    total_weight = sum(e.get("weight", 1.0) for e in evaluators)

    scores    = {}
    reasonings = {}
    composite  = 0.0

    for ev_config in evaluators:
        name   = ev_config.get("name")
        weight = ev_config.get("weight", 1.0)
        cfg    = ev_config.get("config", {})

        fn = EVALUATOR_REGISTRY.get(name)
        if fn is None:
            print(f"  ⚠ Unknown evaluator '{name}' — skipping")
            continue

        try:
            # Pass _last_score so API evaluators can fall back to it on timeout
            cfg_with_last = {**cfg, "_last_score": ev_config.get("_last_score")}
            score, reasoning = fn(
                artifact_path, artifact_text, cfg_with_last,
                client=client, goals=goals, verbose=verbose
            )
            # Persist the score only if this wasn't itself a fallback
            if "previous score" not in reasoning and "no prior score" not in reasoning:
                ev_config["_last_score"] = score
        except Exception as e:
            print(f"  ⚠ Evaluator '{name}' raised an error: {e}")
            score, reasoning = 0.0, f"error: {e}"

        scores[name]     = score
        reasonings[name] = reasoning
        composite       += (weight / total_weight) * score

        if verbose:
            print(f"      [{name}] score: {score:.1f} | {reasoning[:80]}")

    composite = round(composite, 1)
    combined_reasoning = " | ".join(
        f"{n}: {s:.0f}" for n, s in scores.items()
    )
    return composite, scores, combined_reasoning



def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")

def write_file(path: str, content: str):
    Path(path).write_text(content, encoding="utf-8")

def get_git_repo() -> git.Repo:
    try:
        return git.Repo(".")
    except git.exc.InvalidGitRepositoryError:
        print("  → Initializing git repo...")
        repo = git.Repo.init(".")
        repo.index.add([ARTIFACT_PATH, GOALS_PATH])
        repo.index.commit("Initial: baseline artifact + goals before meta-optimization loop")
        print("  → Git repo initialized.")
        return repo

def get_iteration_history(repo: git.Repo, limit: int = 15) -> list:
    history = []
    try:
        for commit in list(repo.iter_commits())[:limit]:
            history.append({
                "message": commit.message.strip(),
                "time": commit.committed_datetime.isoformat()
            })
    except Exception:
        pass
    return history

def load_log() -> list:
    if Path(LOG_PATH).exists():
        return json.loads(Path(LOG_PATH).read_text())
    return []

def save_log(log: list):
    Path(LOG_PATH).write_text(json.dumps(log, indent=2))

def print_divider(char="─"):
    print("\n" + char * 60 + "\n")

def calculate_win_rate(log: list, last_n: int) -> float:
    """Win rate of the last N inner loop iterations."""
    inner = [e for e in log if e.get("type") == "inner"][-last_n:]
    if not inner:
        return 0.0
    wins = sum(1 for e in inner if e.get("outcome") == "kept")
    return wins / len(inner)

# ── Weight Validation ──────────────────────────────────────────────────────────

def extract_weights_from_goals(goals: str) -> dict:
    """
    Parse weights from goals.md. Looks for lines like:
    1. **Criterion Name** (35%) — description
    Returns dict of {criterion_name: weight_int} or empty dict if parsing fails.
    """
    weights = {}
    # Match patterns like (25%) or (35 points) or 25 points each
    pattern = r'\*\*([^*]+)\*\*\s*\((\d+)(?:%| points)?\)'
    matches = re.findall(pattern, goals)
    for name, weight in matches:
        weights[name.strip()] = int(weight)
    return weights

def validate_weights(goals: str) -> tuple[bool, str]:
    """
    Returns (is_valid, reason).
    Checks: weights sum to 100, none below MIN_WEIGHT, none above MAX_WEIGHT.
    """
    weights = extract_weights_from_goals(goals)

    if not weights:
        return False, "Could not parse any weights from goals.md"

    total = sum(weights.values())
    if total != WEIGHT_SUM:
        return False, f"Weights sum to {total}, must sum to {WEIGHT_SUM}"

    for name, w in weights.items():
        if w < MIN_WEIGHT:
            return False, f"'{name}' weight {w} is below minimum {MIN_WEIGHT}"
        if w > MAX_WEIGHT:
            return False, f"'{name}' weight {w} exceeds maximum {MAX_WEIGHT}"

    return True, f"Valid: {weights}"

# ── Inner Loop: Document Agent ─────────────────────────────────────────────────

def get_mutation(client, artifact: str, goals: str, history: list, verbose: bool, last_breakdown: dict = None):
    """Propose ONE targeted improvement to the document."""

    # Build history from the full log (includes reverts) so the agent
    # knows what was already tried and failed — not just what was kept.
    history_text = ""
    if history:
        history_text = "\n\nALL PREVIOUS ATTEMPTS (including reverted — do NOT repeat any of these):\n"
        for h in history[-10:]:
            outcome = h.get("outcome", "?").upper()
            score_before = h.get("score_before", "?")
            score_after  = h.get("score_after",  "?")
            summary      = h.get("change_summary", h.get("message", "?"))
            history_text += f"  [{outcome}] {summary} ({score_before} → {score_after})\n"

    breakdown_text = ""
    if last_breakdown:
        breakdown_text = "\n\nCURRENT EVALUATOR SCORES (use these to target your fix):\n"
        for name, detail in last_breakdown.items():
            breakdown_text += f"  {name}: {detail}\n"

    prompt = f"""You are an expert copywriter and email deliverability specialist optimizing a cold outreach email.

GOALS AND SCORING CRITERIA:
{goals}

CURRENT EMAIL (HTML):
{artifact}
{breakdown_text}{history_text}

YOUR TASK:
1. Review the evaluator scores and ALL previous attempts above
2. Pick the weakest dimension that has NOT already been addressed
3. State your hypothesis: what specifically is weak and why your fix will improve that score
4. Make exactly ONE targeted, surgical change to the HTML
5. Do NOT repeat any previously attempted change — even if it was reverted

RESPOND IN THIS EXACT FORMAT:

HYPOTHESIS: [one sentence — what is weak and why your fix improves the score]

CHANGE_SUMMARY: [one sentence describing the change, suitable for a git commit message]

IMPROVED_DOCUMENT:
[complete document with your one change applied]"""

    if verbose:
        print("    → Agent proposing document mutation...")

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text

    try:
        hypothesis    = raw.split("HYPOTHESIS:")[1].split("CHANGE_SUMMARY:")[0].strip()
        change_summary = raw.split("CHANGE_SUMMARY:")[1].split("IMPROVED_DOCUMENT:")[0].strip()
        improved_doc  = raw.split("IMPROVED_DOCUMENT:")[1].strip()
        # Strip markdown code fences the LLM sometimes wraps the HTML in
        if improved_doc.startswith("```"):
            lines = improved_doc.splitlines()
            lines = lines[1:]  # drop opening ```html line
            while lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]  # drop closing ``` line
            improved_doc = "\n".join(lines).strip()
        # Strip trailing explanatory text the LLM adds after </html>
        if "</html>" in improved_doc:
            improved_doc = improved_doc[:improved_doc.rfind("</html>") + 7]
    except IndexError:
        return artifact, "parse error", "parse error"

    return improved_doc, hypothesis, change_summary


def evaluate_document(client, artifact: str, goals: str, verbose: bool, eval_config: dict = None, artifact_path: str = None):
    """Wrapper that routes to the evaluator registry. Returns (score, reasoning, breakdown)."""
    if eval_config is None:
        eval_config = DEFAULT_EVAL_CONFIG
    score, breakdown, reasoning = run_evaluators(
        artifact_path or ARTIFACT_PATH,
        artifact,
        eval_config,
        client,
        goals,
        verbose
    )
    return score, reasoning, breakdown

# ── Outer Loop: Meta-Agent ─────────────────────────────────────────────────────

def get_meta_mutation(client, goals: str, artifact: str, recent_log: list, win_rate: float, verbose: bool):
    """
    Propose ONE change to the scoring criteria in goals.md.
    The meta-agent sees the iteration history and win rate to reason about
    whether the current weights are producing useful signal.
    """

    history_summary = ""
    if recent_log:
        history_summary = "\nRECENT ITERATION HISTORY:\n"
        for entry in recent_log[-10:]:
            if entry.get("type") == "inner":
                outcome = "✓ KEPT" if entry.get("outcome") == "kept" else "✗ REVERTED"
                history_summary += (
                    f"  {outcome} | score: {entry.get('score_before', 0):.1f} → "
                    f"{entry.get('score_after', 0):.1f} | "
                    f"{entry.get('hypothesis', '')[:80]}\n"
                )

    prompt = f"""You are a meta-optimizer responsible for improving the SCORING CRITERIA used
to evaluate a product specification document.

You are NOT improving the document itself. You are improving the measurement system.

CURRENT SCORING CRITERIA (goals.md):
{goals}

CURRENT DOCUMENT STATE:
{artifact}

{history_summary}
CURRENT WIN RATE (iterations where document improved): {win_rate:.0%}

YOUR TASK:
Analyze whether the current scoring weights and criteria are producing useful signal.
Signs of poor weights:
- Win rate is very low (criteria may be too hard to satisfy or poorly defined)
- Win rate is very high (criteria may be too easy or not discriminating enough)
- The same types of changes keep getting reverted (a criterion may be misleading)
- Some criteria are so vague the agent can't target them effectively

Propose EXACTLY ONE change to the scoring criteria. This could be:
- Reweight criteria (e.g., increase Specificity from 25% to 35%, decrease another)
- Reword a criterion to be more precise and actionable
- Split a vague criterion into something more measurable

HARD CONSTRAINTS — your proposed goals.md MUST follow these rules:
- All weights must sum to exactly 100
- No single criterion below {MIN_WEIGHT} points
- No single criterion above {MAX_WEIGHT} points
- Keep the same number of criteria (do not add or remove)
- Do not change the fundamental purpose of the document being evaluated

RESPOND IN THIS EXACT FORMAT:

META_HYPOTHESIS: [one sentence — what is wrong with the current weights/criteria and why your change will improve win rate or scoring quality]

META_CHANGE_SUMMARY: [one sentence describing the change, suitable for a git commit message]

IMPROVED_GOALS:
[complete new goals.md content with your one change applied]"""

    if verbose:
        print("  → Meta-agent proposing criteria mutation...")

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text

    try:
        meta_hypothesis    = raw.split("META_HYPOTHESIS:")[1].split("META_CHANGE_SUMMARY:")[0].strip()
        meta_change_summary = raw.split("META_CHANGE_SUMMARY:")[1].split("IMPROVED_GOALS:")[0].strip()
        improved_goals     = raw.split("IMPROVED_GOALS:")[1].strip()
        # Strip markdown code fences if present
        if improved_goals.startswith("```"):
            lines = improved_goals.splitlines()
            lines = lines[1:]
            while lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            improved_goals = "\n".join(lines).strip()
    except IndexError:
        return goals, "parse error", "parse error"

    return improved_goals, meta_hypothesis, meta_change_summary

# ── Main Loop ──────────────────────────────────────────────────────────────────

def run_meta_loop(iterations: int, meta_every: int, verbose: bool, eval_config_path: str = None):

    eval_config = load_eval_config(eval_config_path)
    active_evals = [e["name"] for e in eval_config.get("evaluators", [])]

    print("\n⚙  AUTOLOOP META + EVAL REGISTRY")
    print("   Inner loop: improves the document")
    print("   Outer loop: improves the scoring criteria")
    print(f"   Model: {MODEL}")
    print(f"   Inner iterations: {iterations}")
    print(f"   Meta fires every: {meta_every} iterations")
    print(f"   Active evaluators: {', '.join(active_evals)}")
    print_divider("═")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("✗ ANTHROPIC_API_KEY not set. Run: export ANTHROPIC_API_KEY=your_key")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    for path in [ARTIFACT_PATH, GOALS_PATH]:
        if not Path(path).exists():
            print(f"✗ {path} not found.")
            sys.exit(1)

    repo = get_git_repo()
    log  = load_log()

    # Baseline
    print("📊 Scoring baseline...")
    artifact       = read_file(ARTIFACT_PATH)
    goals          = read_file(GOALS_PATH)
    baseline_score, baseline_reasoning, baseline_breakdown = evaluate_document(client, artifact, goals, verbose, eval_config)
    print(f"   Score: {baseline_score:.1f}/100")
    print(f"   {baseline_reasoning}")

    # Validate initial weights
    valid, reason = validate_weights(goals)
    if valid:
        weights = extract_weights_from_goals(goals)
        print(f"   Weights: {weights}")
    else:
        print(f"   ⚠ Initial weights could not be parsed: {reason}")
        print(f"   Meta-loop will still run but weight validation may be unreliable.")

    print_divider()

    current_score     = baseline_score
    current_breakdown = baseline_breakdown
    inner_wins    = 0
    inner_losses  = 0
    meta_wins     = 0
    meta_losses   = 0
    meta_count    = 0

    for i in range(1, iterations + 1):

        # ── Meta check: fire every meta_every iterations ───────────────────────
        if i > 1 and (i - 1) % meta_every == 0:
            meta_count += 1
            print_divider("·")
            print(f"🧠 META ITERATION {meta_count} (after {i-1} inner iterations)")

            win_rate    = calculate_win_rate(log, last_n=meta_every)
            artifact    = read_file(ARTIFACT_PATH)
            goals       = read_file(GOALS_PATH)

            print(f"   Win rate last {meta_every} iterations: {win_rate:.0%}")

            improved_goals, meta_hypothesis, meta_change_summary = get_meta_mutation(
                client, goals, artifact, log, win_rate, verbose
            )

            if meta_hypothesis == "parse error":
                print("   ⚠ Meta-agent parse error. Skipping.")
                print_divider("·")
            else:
                print(f"   Hypothesis: {meta_hypothesis}")

                # Validate the proposed new weights before accepting
                is_valid, validation_reason = validate_weights(improved_goals)

                if not is_valid:
                    print(f"   ✗ REJECTED (invalid weights: {validation_reason})")
                    meta_losses += 1
                    log.append({
                        "type": "meta",
                        "meta_iteration": meta_count,
                        "hypothesis": meta_hypothesis,
                        "outcome": "rejected_invalid",
                        "reason": validation_reason,
                        "timestamp": datetime.now().isoformat()
                    })
                else:
                    new_weights = extract_weights_from_goals(improved_goals)
                    print(f"   New weights: {new_weights}")

                    # Apply new goals and run a trial window to measure impact
                    old_goals = goals
                    write_file(GOALS_PATH, improved_goals)

                    # Score the current document under the new criteria
                    trial_score, trial_reasoning, trial_breakdown = evaluate_document(
                        client, artifact, improved_goals, verbose, eval_config
                    )

                    # Accept if score is at least as good under new criteria
                    # (we're measuring quality of the lens, not gaming the score)
                    # Primary signal: did the score remain meaningful (not collapse or inflate wildly)?
                    score_delta = abs(trial_score - current_score)
                    if score_delta <= 20:
                        # Reasonable — commit the new goals
                        repo.index.add([GOALS_PATH])
                        commit_message = (
                            f"[META] {meta_change_summary} | "
                            f"win_rate: {win_rate:.0%} | weights: {new_weights}"
                        )
                        repo.index.commit(commit_message)
                        current_score = trial_score
                        meta_wins += 1
                        print(f"   Score under new criteria: {trial_score:.1f}  ✓ KEPT")
                        print(f"   {trial_reasoning}")
                        log.append({
                            "type": "meta",
                            "meta_iteration": meta_count,
                            "hypothesis": meta_hypothesis,
                            "change_summary": meta_change_summary,
                            "new_weights": new_weights,
                            "win_rate_at_time": win_rate,
                            "score_before": current_score,
                            "score_after": trial_score,
                            "outcome": "kept",
                            "timestamp": datetime.now().isoformat()
                        })
                    else:
                        # Score swung too wildly — the new criteria is distorting measurement
                        write_file(GOALS_PATH, old_goals)
                        repo.git.checkout(GOALS_PATH)
                        meta_losses += 1
                        print(f"   Score swung {score_delta:.1f} points — too disruptive  ✗ REVERTED")
                        log.append({
                            "type": "meta",
                            "meta_iteration": meta_count,
                            "hypothesis": meta_hypothesis,
                            "outcome": "reverted_score_swing",
                            "score_delta": score_delta,
                            "timestamp": datetime.now().isoformat()
                        })

                save_log(log)
                print_divider("·")

        # ── Inner iteration: document improvement ──────────────────────────────
        print(f"🔄 Iteration {i}/{iterations}")

        artifact = read_file(ARTIFACT_PATH)
        goals    = read_file(GOALS_PATH)
        # Pass the full in-memory log (includes reverts) so the agent
        # doesn't re-propose changes that already failed this run.
        improved_doc, hypothesis, change_summary = get_mutation(
            client, artifact, goals, log, verbose, last_breakdown=current_breakdown
        )

        if hypothesis == "parse error":
            inner_losses += 1
            print("   ⚠ Parse error. Skipping.\n")
            continue

        print(f"   Hypothesis: {hypothesis}")

        write_file(ARTIFACT_PATH, improved_doc)
        new_score, new_reasoning, new_breakdown = evaluate_document(client, improved_doc, goals, verbose, eval_config)

        print(f"   Score: {current_score:.1f} → {new_score:.1f}", end="  ")

        entry = {
            "type": "inner",
            "iteration": i,
            "hypothesis": hypothesis,
            "change_summary": change_summary,
            "score_before": current_score,
            "score_after": new_score,
            "reasoning": new_reasoning,
            "timestamp": datetime.now().isoformat()
        }

        if new_score > current_score:
            repo.index.add([ARTIFACT_PATH])
            repo.index.commit(
                f"Iteration {i}: {change_summary} | score: {current_score:.1f} → {new_score:.1f}"
            )
            print("✓ KEPT")
            if verbose:
                print(f"   {new_reasoning}")
            current_score     = new_score
            current_breakdown = new_breakdown
            inner_wins += 1
            entry["outcome"] = "kept"
        else:
            repo.git.checkout(ARTIFACT_PATH)
            print("✗ REVERTED")
            inner_losses += 1
            entry["outcome"] = "reverted"

        log.append(entry)
        save_log(log)
        print()

    # ── Final summary ──────────────────────────────────────────────────────────
    print_divider("═")
    print("✅ META LOOP COMPLETE")
    print()
    print(f"   Baseline score:       {baseline_score:.1f}/100")
    print(f"   Final score:          {current_score:.1f}/100")
    print(f"   Total improvement:    +{current_score - baseline_score:.1f} points")
    print()
    print(f"   Inner iterations:     {iterations}")
    print(f"   Inner wins/losses:    {inner_wins} / {inner_losses}")
    print()
    print(f"   Meta iterations:      {meta_count}")
    print(f"   Meta wins/losses:     {meta_wins} / {meta_losses}")
    print()

    # Final weights
    final_goals = read_file(GOALS_PATH)
    final_weights = extract_weights_from_goals(final_goals)
    if final_weights:
        print(f"   Final weights:        {final_weights}")
    print()

    print("   Git log:")
    try:
        for commit in list(repo.iter_commits())[:inner_wins + meta_wins + 2]:
            marker = "🧠" if "[META]" in commit.message else "📄"
            print(f"   {marker} {commit.hexsha[:7]}  {commit.message.strip()[:72]}")
    except Exception:
        pass

    print()
    print(f"   Full log: {LOG_PATH}")
    print_divider("═")


# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Double optimization: document + scoring criteria + evaluator registry")
    parser.add_argument("--artifact",    type=str, default=None,
                        help="Path to artifact file (default: artifact.md)")
    parser.add_argument("--goals",       type=str, default=None,
                        help="Path to goals file (default: goals.md)")
    parser.add_argument("--iterations",  type=int, default=15,
                        help="Number of inner iterations (default: 15)")
    parser.add_argument("--meta-every",  type=int, default=5,
                        help="Fire meta-agent every N inner iterations (default: 5)")
    parser.add_argument("--eval-config", type=str, default=None,
                        help="Path to eval_config.yaml (default: llm_judge only)")
    parser.add_argument("--verbose",     action="store_true",
                        help="Show detailed API call output")
    args = parser.parse_args()

    if args.artifact:
        ARTIFACT_PATH = args.artifact
    if args.goals:
        GOALS_PATH = args.goals

    run_meta_loop(
        iterations=args.iterations,
        meta_every=args.meta_every,
        verbose=args.verbose,
        eval_config_path=args.eval_config,
    )
