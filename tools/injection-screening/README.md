# Prompt-injection screening prototypes

Evaluation prototypes for
[PLAN-prompt-injection-screening.md](../../docs/plans/PLAN-prompt-injection-screening.md).
These are measurement tools, not shipped classifier code; results are
recorded in the plan's findings documents.

## tripwire.py — the deterministic regex/Unicode tripwire

Zero heavyweight dependencies (needs only divergulent importable, e.g. the
`.tox/py3` venv). Scans every deduplicated patch body in a corpus for
injection-shaped text — instruction phrases, chat-template role markers,
invisible/bidi Unicode, large base64 blobs — reporting hits per pattern
family and per region (the LLM-visible diff body vs the human-visible
DEP-3 header).

```bash
.tox/py3/bin/python tools/injection-screening/tripwire.py \
    /path/to/reviews/corpus --json /tmp/tripwire-hits.json
```

Full-corpus scan of ~60k patches takes about 70 seconds.

## model_scorer.py — learned encoder-classifier scoring

Runs a prompt-injection encoder classifier over a seeded random sample of
diff bodies (plus every fingerprint in the tripwire's hit list, plus
synthetic positives), scoring 512-token chunks and taking the worst chunk
per patch. Defaults to LLM Guard's underlying ungated model
(`protectai/deberta-v3-base-prompt-injection-v2`).

Torch and transformers must NEVER reach divergulent's own dependency set;
use a scratch venv:

```bash
python3 -m venv /tmp/pi-venv
/tmp/pi-venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
/tmp/pi-venv/bin/pip install transformers
/tmp/pi-venv/bin/pip install -e .   # divergulent itself, for diff_body semantics
/tmp/pi-venv/bin/python tools/injection-screening/model_scorer.py \
    /path/to/reviews/corpus --sample 1000 --include-json /tmp/tripwire-hits.json
```

Notes:

- `pip install llm-guard` itself does not build on Python 3.13 (it pins
  `spacy==3.7.1`, whose `blis` 0.7 build fails); we drive its underlying
  model directly instead, which is the same scorer without the wrapper.
- `meta-llama/Llama-Prompt-Guard-2-86M` is behind a manual Hugging Face
  license gate; pass `--model` once a token with access is configured
  (`hf auth login`). We do not use unofficial re-uploads of gated
  weights — poor provenance is the thing this project exists to measure.
