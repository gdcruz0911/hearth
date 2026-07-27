# Evaluation

## Purpose

The evaluation harness provides a deterministic check of the current import, retrieval, citation, and abstention behavior using synthetic or public documents.
It is separate from future model-quality evaluation.

## Run the baseline

```bash
PYTHONPATH=src .venv/bin/python -m hearth.cli --database /private/tmp/hearth-evaluation.sqlite evaluate tests/fixtures/public/baseline-evaluation.json
```

The baseline contains one supported case with an expected citation and one unsupported case that must abstain.
For passing cases, the CLI prints the case ID and pass status.
For failing cases, it also prints validation errors that can identify the expected source document and page.

To exercise the experimental local generator on the same public corpus from an interactive macOS terminal, add a pre-provisioned local model path.

```bash
PYTHONPATH=src .venv/bin/python -m hearth.cli --generator-model /path/to/local/model --database /private/tmp/hearth-generator-evaluation.sqlite evaluate tests/fixtures/public/baseline-evaluation.json
```

Run the generator-specific public corpus to test an answer with evidence and an unsupported attribute despite related retrieved evidence.

```bash
PYTHONPATH=src .venv/bin/python -m hearth.cli --generator-model /path/to/local/model --database /private/tmp/hearth-generator-evaluation.sqlite evaluate tests/fixtures/public/generator-evaluation.json
```

This host-side check is required before any generator model can become the product default.

## Corpus rules

Version-controlled corpora may contain only synthetic or public documents.
Do not add private PDFs, notes, prompts, responses, OCR output, embeddings, indexes, or local absolute paths.
Each supported case must identify an expected document name, page number, and quoted substring.
Each abstained case must define no expected citations.

## Current coverage

The unit suite covers exact note citations, unsupported-question abstention, PDF page metadata, OCR fallback behavior, removal, reindexing, and structured-generator response validation.
The baseline evaluation corpus verifies one supported citation and one abstention through the deterministic scaffold.
The generator-specific corpus verifies a supported citation and an abstention when related source text is retrieved but does not contain the requested attribute.

## Future release gate

Before selecting a local model configuration, extend the corpus with public or synthetic cases for OCR warnings, removal, reindexing, citation validity, and unsupported questions.
Record model, retrieval, chunking, and prompt configuration alongside results without storing private prompts or responses in Git.
