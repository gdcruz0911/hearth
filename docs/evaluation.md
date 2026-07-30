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

To exercise the experimental local semantic index, supply both a pre-provisioned embedding model and a local derived-index directory.

```bash
PYTHONPATH=src .venv/bin/python -m hearth.cli \
  --embedding-model /path/to/local/embedding-model \
  --index-directory /path/to/local/semantic-index \
  --database /private/tmp/hearth-semantic-evaluation.sqlite \
  evaluate tests/fixtures/public/baseline-evaluation.json
```

The evaluation import rebuilds the derived index from the corpus documents.
The command requires a host macOS terminal with Apple Silicon GPU access and does not download a model.

The multi-document semantic corpus adds source disambiguation and unrelated-question coverage.

```bash
PYTHONPATH=src .venv/bin/python -m hearth.cli \
  --embedding-model /path/to/local/embedding-model \
  --index-directory /path/to/local/semantic-index \
  --database /private/tmp/hearth-semantic-evaluation.sqlite \
  evaluate tests/fixtures/public/semantic-evaluation.json
```

To evaluate the full experimental path, provide both local model options and run the generator-specific corpus.

```bash
PYTHONPATH=src .venv/bin/python -m hearth.cli \
  --generator-model /path/to/local/generator-model \
  --embedding-model /path/to/local/embedding-model \
  --index-directory /path/to/local/semantic-index \
  --database /private/tmp/hearth-semantic-generator-evaluation.sqlite \
  evaluate tests/fixtures/public/generator-evaluation.json
```

After a structured-generator contract change, rerun every host-side generator corpus before relying on previously recorded generator results.

Run the experimental local claim-support corpus separately from document retrieval and answer generation.

```bash
PYTHONPATH=src .venv/bin/python -m hearth.cli \
  --generator-model /path/to/local/generator-model \
  evaluate-claim-support tests/fixtures/public/claim-support-evaluation.json
```

The corpus supplies each question, proposed claim, and evidence directly to the checker.
It includes exact support, explicit cross-document support, an extra duty, missing qualifiers, a contradiction, a missing quantity, and instruction-like text inside evidence.
The result is not used to accept, reject, or modify user answers.

Run the real local OCR integration test only on a machine with Poppler and OCRmyPDF installed.

```bash
HEARTH_RUN_OCR_INTEGRATION=1 PYTHONPATH=src .venv/bin/python \
  -m unittest discover -s tests -p test_ocr_integration.py -v
```

## Corpus rules

Version-controlled corpora may contain only synthetic or public documents.
Do not add private PDFs, notes, prompts, responses, OCR output, embeddings, indexes, or local absolute paths.
Each supported case must identify an expected document name, page number, and quoted substring.
Each abstained case must define no expected citations.

## Current coverage

The unit suite covers exact note citations, native PDF page boundaries and citations, unsupported-question abstention, OCR fallback behavior, removal, explicit reindexing, source-change attention, fingerprint-verified source relinking, legacy source-fingerprint migration, semantic-index rebuilding after OCR reindexing, and structured-generator response validation.
The baseline evaluation corpus verifies one supported citation and one abstention through the deterministic scaffold.
The generator-specific corpus verifies a supported citation and an abstention when related source text is retrieved but does not contain the requested attribute.
The semantic-index regression test verifies that unrelated retrieved evidence causes abstention while question-term-supported evidence remains answerable.
The opt-in local MLX semantic integration test verifies real reindex replacement, deletion, and fail-closed manifest mismatch handling with synthetic temporary content.
The multi-document semantic corpus verifies source disambiguation, exact citation provenance, and unrelated-question abstention.
The opt-in OCR integration test uses a synthetic image-only PDF and verifies local OCR citation metadata, record replacement after reindexing, default temporary-output deletion, explicit output retention, and one surviving inspected document.
The MVP workflow test exercises import, metadata-only health, source-change attention, fingerprint-verified relinking, citation-first search output, reindexing, removal, and an empty-collection health result through the CLI using a public fixture copied into temporary local runtime storage.
Structured-generator tests reject a paraphrase even when it has an approved citation ID and accept only normalized verbatim evidence spans.
Reranker tests reject invalid scores, preserve deterministic tie order, and verify one real local relevance ordering.
Claim-support tests reject malformed model output and cover a separate public corpus of exact and adversarial claim-evidence pairs.

## Recorded public result

On 2026-07-27, before ADR-0010, the Qwen3 8B 4-bit MLX candidate passed the generator-specific public corpus on the current Apple Silicon Mac.
The run passed `supported-deployment-owner` and `abstained-owner-phone-number` for a result of 2/2.
This demonstrates the local adapter’s structured response parsing, citation resolution, and related-evidence abstention path.
It does not establish production-quality generation, broad retrieval quality, semantic claim support, or robust behavior against adversarial document text.

On 2026-07-29, the Qwen3 Embedding 0.6B 4-bit MLX candidate passed the baseline public corpus through the local flat semantic index on the current Apple Silicon Mac.
The run passed `supported-deployment-owner` and `abstained-annual-budget` for a result of 2/2.
The run used the ADR-0009 lexical evidence-sufficiency gate.
It confirms the observed unrelated-retrieval failure now abstains on the host MLX path.
It does not establish a calibrated semantic relevance threshold, broad retrieval quality, multilingual behavior, or paraphrase robustness.

On 2026-07-29, before ADR-0010, the Qwen3 8B 4-bit generator and Qwen3 Embedding 0.6B 4-bit MLX candidates passed the generator-specific public corpus together on the current Apple Silicon Mac.
The run passed `supported-deployment-owner` and `abstained-owner-phone-number` for a result of 2/2.
This confirms the combined local retrieval and structured-generator path for those narrow public cases.
It does not establish claim-level support for arbitrary generated wording, broad retrieval quality, or model-default suitability.

On 2026-07-29, the Qwen3 Embedding 0.6B 4-bit MLX candidate passed the multi-document semantic corpus on the current Apple Silicon Mac.
The run passed five cases for a result of 5/5.
It verifies source disambiguation and unrelated-question abstention through the local semantic index.
It does not evaluate generated answers or establish a production model default.

On 2026-07-29, the local Poppler and OCRmyPDF image-only-PDF integration test passed after reindexing the public fixture.
It verifies that reindexing keeps one collection record, preserves OCR page provenance, returns a cited answer from the reindexed content, deletes temporary derived PDFs by default, and retains one only when explicitly configured.

On 2026-07-30, the local Poppler native-text PDF integration test passed against a public two-page fixture.
It verifies page-isolated native extraction, collection metadata, and page-specific citations for two distinct questions.

On 2026-07-30, the Qwen3 Embedding 0.6B 4-bit MLX candidate passed the local semantic lifecycle integration test on the current Apple Silicon Mac.
It verifies real reindex replacement, deletion abstention, and fail-closed manifest mismatch handling with synthetic temporary content.
It does not establish production-default suitability, broader retrieval quality, or a model-selection decision.

On 2026-07-30, the Qwen3 8B 4-bit generator candidate passed the expanded four-case public generator corpus on the current Apple Silicon Mac.
The local generator and reranker combination also passed all four cases.
The cases verify two verbatim supported excerpts and two related unsupported questions that abstain.
This does not establish broad generation quality, reranking quality, claim support, or a production model default.

On 2026-07-30, the Qwen3 8B 4-bit candidate passed five of eight cases in the expanded experimental local claim-support corpus on the current Apple Silicon Mac.
It correctly rejected a contradiction, a missing quantity, and instruction-like evidence, but incorrectly marked the three adversarial overclaims as supported.
The checker remains evaluation-only and cannot replace or relax ADR-0010's verbatim evidence requirement.

## Future release gate

Before selecting a local model configuration, extend the corpus with public or synthetic cases for OCR warnings, removal, reindexing, citation validity, and unsupported questions.
Do not promote a claim-support checker until it meets an explicit adversarial-corpus threshold defined by a new ADR.
Record model, retrieval, chunking, and prompt configuration alongside results without storing private prompts or responses in Git.
