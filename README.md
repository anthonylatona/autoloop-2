# autoloop-2

An autonomous document improvement loop built on top of the Anthropic API. An LLM agent proposes one change at a time to a document, a set of evaluators scores the result, and the change either gets committed to git or reverted. No GPU, no fine-tuning, no infrastructure.

The demo optimizes a cold outreach email from a score of 22.7 to 89.5 over 20 iterations using a mix of deterministic rule-based evaluators and an LLM judge.

---

## How It Works

The inner loop runs a fixed number of iterations:

1. The agent reads the current document and proposes one targeted change
2. Every configured evaluator scores the result and returns a 0–100 number
3. Those scores are combined into a weighted composite
4. If the composite went up, the change is committed to git. If not, the document reverts.

The outer meta-loop fires every N iterations and rewrites the scoring criteria in `goals.md` based on what the inner loop has been struggling with. If a dimension is already maxed out, the meta-agent shifts weight toward whatever still has room to improve.

The git log at the end is a complete audit trail — every winning change, the hypothesis behind it, and the score delta.

---

## Requirements

- Python 3.10+
- An Anthropic API key
- SpamAssassin installed locally for the email demo: `sudo apt install spamassassin`

```bash
pip install anthropic gitpython pyyaml --break-system-packages
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## Running the Email Demo

```bash
bash reset.sh

python3 autoloop_meta_eval.py \
  --artifact email_sample/artifact.html \
  --goals email_sample/goals.md \
  --eval-config email_sample/eval_config_email.yaml \
  --iterations 20 \
  --verbose
```

Check your evaluators are working before you start a run:

```bash
python3 test_email_evaluators.py
```

Reset everything between runs (restores original artifact, wipes git history and loop log):

```bash
bash reset.sh
```

---

## Project Structure

```
autoloop-2/
├── autoloop_meta_eval.py         # The main script — inner loop + meta-loop
├── reset.sh                      # Restores original artifact and wipes run state
├── test_email_evaluators.py      # Runs all evaluators and prints a score report
├── loop_log.json                 # Full record of every iteration (auto-generated)
│
└── email_sample/
    ├── artifact.html             # The document being optimized
    ├── artifact.original.html    # Baseline — reset.sh restores from this
    ├── goals.md                  # Scoring rubric in plain English
    ├── eval_config_email.yaml    # Evaluator names and weights
    └── evaluator_email.py        # The scorer functions
```

---

## Writing Your Own Evaluator

Any function with this signature works:

```python
def evaluator_my_metric(artifact_path, artifact_text, config, **kwargs):
    score = ...      # float, 0–100
    reasoning = ...  # string explaining the score
    return score, reasoning
```

Register it in `autoloop_meta_eval.py`:

```python
EVALUATOR_REGISTRY["my_metric"] = evaluator_my_metric
```

Then add it to your YAML config with a weight:

```yaml
evaluators:
  - name: my_metric
    weight: 0.40
```

The loop does not care what is inside the evaluator. Regex checkers, shell commands that output a benchmark number, test suite pass rates, Lighthouse scores — anything that returns a float between 0 and 100 is a valid evaluator.

---

## CLI Reference

```
python3 autoloop_meta_eval.py [options]

  --artifact PATH       Document to optimize
  --goals PATH          Scoring rubric (plain English, used by the LLM judge)
  --eval-config PATH    Evaluator weights YAML
  --iterations N        Number of inner loop iterations (default: 20)
  --meta-every N        How often the meta-loop fires (default: 5)
  --verbose             Print evaluator reasoning each iteration
```
