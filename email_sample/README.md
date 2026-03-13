# autorefine

An autonomous document improvement loop. An LLM agent proposes one change, a scorer evaluates the result, winners get committed to git, losers get reverted. No GPU required.

Inspired by what Tobi Lütke did with Shopify's Liquid template engine — except instead of Ruby parse benchmarks, you can point it at anything you can score.

---

## How It Works

The loop is three steps, repeated:

1. An agent reads your document and proposes one targeted change
2. A set of evaluators scores the document before and after
3. If the score went up, the change is committed. If not, it's reverted.

A second "meta-loop" fires every N iterations and rewrites the scoring criteria itself — adjusting weights based on what the inner loop has been struggling to improve.

At the end you get a git log that is a full audit trail of every decision: what the agent tried, why, and whether it worked.

---

## Quick Start

**Requirements:** Python 3.10+, an Anthropic API key, and `spamassassin` installed locally for the email demo (`sudo apt install spamassassin`).

```bash
git clone https://github.com/yourname/autorefine
cd autorefine
pip install anthropic gitpython pyyaml --break-system-packages
export ANTHROPIC_API_KEY=sk-ant-...
```

Run the email demo:

```bash
bash reset.sh
python3 autoloop_meta_eval.py \
  --artifact email_sample/artifact.html \
  --goals email_sample/goals.md \
  --eval-config email_sample/eval_config_email.yaml \
  --iterations 20 \
  --verbose
```

Test your evaluators before running:

```bash
python3 test_email_evaluators.py
```

Reset between runs:

```bash
bash reset.sh
```

---

## Project Structure

```
autorefine/
├── autoloop_meta_eval.py        # Main script — inner loop + meta-loop
├── autoloop.py                  # Simpler version, no evaluator registry
├── autoloop_meta.py             # Simpler version, no meta-loop
├── reset.sh                     # Wipes state and restores original artifact
├── test_email_evaluators.py     # Score report for the current artifact
│
├── email_sample/
│   ├── artifact.html            # The document being optimized
│   ├── artifact.original.html   # The baseline — reset.sh restores from this
│   ├── goals.md                 # Scoring rubric in plain English
│   ├── eval_config_email.yaml   # Evaluator weights
│   └── evaluator_email.py      # Domain-specific scorer functions
│
└── eval_configs/                # Example configs for other domains
    ├── ruby_perf.yaml
    ├── landing_page.yaml
    ├── pitch_deck.yaml
    └── code_quality.yaml
```

---

## Writing Your Own Evaluator

Any function with this signature can be registered:

```python
def evaluator_my_metric(artifact_path, artifact_text, config, **kwargs):
    # analyze artifact_text however you want
    score = ...       # float between 0 and 100
    reasoning = ...   # string explaining the score
    return score, reasoning
```

Add it to the registry in `autoloop_meta_eval.py`:

```python
EVALUATOR_REGISTRY["my_metric"] = evaluator_my_metric
```

Then reference it in your YAML config:

```yaml
evaluators:
  - name: my_metric
    weight: 0.40
    config:
      some_option: some_value
```

The loop doesn't care what's inside the evaluator. Deterministic rule checkers, shell commands, benchmark scripts, test runners — anything that returns a float works. See `EVALUATORS.md` for the full registry reference.

---

## The Email Demo Baseline

The demo artifact is a deliberately terrible cold outreach email. Starting scores:

| Evaluator | Baseline | After 20 iterations |
|---|---|---|
| Spam trigger words | 0/100 | 82/100 |
| CAN-SPAM compliance | 55/100 | 100/100 |
| Image alt text | 0/100 | 100/100 |
| CTA focus | 35/100 | 100/100 |
| LLM judge | ~20/100 | ~65/100 |
| **Composite** | **22.7/100** | **89.5/100** |

---

## Cost

Running on `claude-haiku-4-5-20251001`. Each inner iteration makes roughly 2 API calls (agent + judge). The meta-loop adds one call every 5 iterations.

| Run | Approximate cost |
|---|---|
| 20 iterations | < $0.05 |
| 50 iterations | < $0.15 |

---

## CLI Reference

```
python3 autoloop_meta_eval.py [options]

  --artifact PATH       Document to optimize (default: artifact.md)
  --goals PATH          Scoring rubric (default: goals.md)
  --eval-config PATH    Evaluator weights YAML (default: llm_judge only)
  --iterations N        Number of inner loop iterations (default: 20)
  --meta-every N        How often the meta-loop fires (default: 5)
  --verbose             Print evaluator reasoning each iteration
```

---

## Further Reading

- [Blog post: I Built Karpathy's Optimization Loop and a Single Prompt Beat It](#) — the full writeup including why this lost to a one-shot prompt, and when it actually makes sense to use it
- `EVALUATORS.md` — complete evaluator registry documentation
