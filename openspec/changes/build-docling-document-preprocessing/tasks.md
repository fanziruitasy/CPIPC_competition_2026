## 1. Environment and Repository Boundaries

- [ ] 1.1 Create a Python 3.12 project environment definition with pinned Docling, JSON Schema, YAML, testing, and logging dependencies; completion condition: a clean environment can install the project and print all dependency versions.
- [ ] 1.2 Install and verify LibreOffice headless conversion on Windows; completion condition: a test DOC converts to DOCX through soffice with a zero exit code and recorded version.
- [ ] 1.3 Create only the current-change code, config, contract, test, and data lifecycle directories defined in design.md; completion condition: a directory ownership test rejects output paths under Data/originalData, openspec, and docs.
- [ ] 1.4 Move lightweight official competition materials from the repository root into resources/competition without renaming authoritative originals, and place team-authored derivatives under docs; completion condition: the repository root contains no loose competition files or Office lock files.
- [ ] 1.5 Update .gitignore for environment files, Office lock files, model caches, Data/staging, Data/processed, Data/indexes, logs, and local secrets; completion condition: a dry run produces no large generated files in git status.

## 2. Contracts and Configuration

- [ ] 2.1 Define normalized-document v1 JSON Schema for identity, provenance, ordered content nodes, evidence, tables, resources, and diagnostics; completion condition: valid and intentionally invalid fixtures pass and fail schema validation respectively.
- [ ] 2.2 Define document-manifest and source-relation JSON Schemas; completion condition: schemas validate success, warning, failed, skipped, reused, and alternate-source examples.
- [ ] 2.3 Add document_pipeline.yaml with repository-relative input, staging, release, parser profile, concurrency, timeout, QA thresholds, and version fields; completion condition: configuration loading rejects absolute output paths and paths escaping approved roots.
- [ ] 2.4 Add sample_corpus.yaml containing 8–10 representative DOC, DOCX, PDF, long-document, table-heavy, and same-source candidates; completion condition: every configured sample resolves to exactly one original source file.

## 3. Source Inventory and Identity

- [ ] 3.1 Implement recursive discovery for DOC, DOCX, and PDF with stable ordering and unsupported-file reporting; completion condition: an inventory integration test returns only supported files in deterministic order.
- [ ] 3.2 Implement streaming SHA-256, repository-relative source paths, source_id, doc_id, node_id, config_digest, and run_id generation; completion condition: repeated runs on unchanged fixtures produce identical stable identifiers except run_id.
- [x] 3.3 Write the immutable input inventory before conversion or parsing; completion condition: a before/after hash audit proves Data/originalData is unchanged.

## 4. Format Conversion and Parsing Adapters

- [ ] 4.1 Define converter and parser boundaries that return neutral results and structured diagnostics; completion condition: pipeline tests can substitute fake adapters without importing Docling or LibreOffice.
- [ ] 4.2 Implement the LibreOffice DOC-to-DOCX adapter with per-run output paths, timeout, version capture, collision handling, and failure isolation; completion condition: success and corrupted-DOC tests record correct statuses and continue the batch.
- [ ] 4.3 Implement the Docling adapter for DOCX and text-based PDF without forced OCR; completion condition: fixtures return ordered neutral nodes, tables, resources, diagnostics, and available source locations.
- [ ] 4.4 Add text-layer and empty-output checks; completion condition: a suspected scanned or empty PDF becomes warning/failed for review rather than silently publishing an empty document.

## 5. Normalization and Artifact Export

- [ ] 5.1 Map Docling output to normalized-document v1 without leaking Docling-specific object types; completion condition: serialized fixtures validate against the contract and can load without Docling installed.
- [ ] 5.2 Normalize titles, paragraphs, clauses, lists, tables, images, footnotes, reading order, and parent-child relationships; completion condition: structural fixture assertions cover every supported node type.
- [ ] 5.3 Map PDF page evidence and Word structural locators with explicit precision; completion condition: tests prove no inferred Word page is labeled exact and PDF nodes retain page references.
- [ ] 5.4 Generate Markdown from the normalized model, using HTML for complex merged tables; completion condition: Markdown review fixtures preserve heading order, clauses, simple tables, and row/column spans.
- [ ] 5.5 Export resources under assets/<doc_id> with checksums and relative references; completion condition: referenced resources exist, unreferenced resources are reported, and empty resource sets are explicit.

## 6. Manifest, Source Relations, and Publishing

- [x] 6.1 Implement per-document manifest records with source, toolchain, version, status, artifact, warning, error, and timing fields; completion condition: any JSON artifact can be traced to one source hash and one run.
- [ ] 6.2 Implement same-source candidate detection using normalized name, title, document number, and content fingerprint with confidence and rationale; completion condition: known DOCX/PDF pairs link while low-confidence pairs remain separate.
- [ ] 6.3 Implement candidate writes and atomic per-document completion; completion condition: an injected mid-write failure leaves no publishable partial artifact.
- [ ] 6.4 Implement restart and reuse decisions using source hash, pipeline version, schema version, and config digest; completion condition: unchanged reruns report reused/skipped and changed inputs are reprocessed.
- [ ] 6.5 Implement immutable release publishing with release.json and explicit release_id; completion condition: publishing refuses to overwrite an existing release and all manifest paths are relative.

## 7. Quality Gates and Automated Tests

- [ ] 7.1 Implement automated QA metrics for coverage, success, empty output, hierarchy, table content, evidence locations, resources, schema validation, duplicates, and absolute-path leakage; completion condition: each rule has a passing and failing test fixture.
- [x] 7.2 Generate machine-readable qa_summary.json and reviewable qa_summary.md with failure and warning details; completion condition: both reports reconcile to manifest status counts.
- [ ] 7.3 Implement sample-to-full gate and explicit audited override; completion condition: a failed sample blocks default full publish and records any override reason.
- [ ] 7.4 Add unit tests for inventory, identities, mapping, exporters, manifests, QA, and path safety; completion condition: the unit suite passes in a clean environment.
- [ ] 7.5 Add integration tests for DOC conversion, DOCX parsing, PDF parsing, batch failure isolation, rerun reuse, and release publishing; completion condition: the integration suite passes and never mutates source fixtures.

## 8. Representative Sample Validation

- [ ] 8.1 Run the configured sample into an isolated staging run and archive its inventory and tool versions; completion condition: all 8–10 samples have an explicit terminal status.
- [ ] 8.2 Manually review Markdown and structured JSON for the bank confirmation guide, the 263-page attachment document, the table-bearing legacy DOC, and other configured samples; completion condition: qa_summary.md records reviewer conclusions for every required sample.
- [ ] 8.3 Tune parser and QA configuration based on sample evidence without changing the v1 contract silently; completion condition: configuration changes are versioned and the sample gate passes or remaining exceptions are explicitly accepted.

## 9. Full Corpus Release and Handoff

- [ ] 9.1 Freeze pipeline_version, schema_version, configuration digest, dependency versions, and sample QA approval; completion condition: the run metadata can reproduce the approved sample configuration.
- [x] 9.2 Process the complete docANDpdf inventory with bounded concurrency and per-file isolation; completion condition: every inventoried source has one terminal manifest status and status totals match the inventory.
- [ ] 9.3 Review full-corpus failures, warnings, same-source relations, table metrics, and evidence coverage; completion condition: unresolved exceptions are documented with disposition and owner.
- [ ] 9.4 Publish the first immutable document corpus release; completion condition: release.json, documents.jsonl, source_relations.jsonl, schemas, checksums, Markdown, structured JSON, assets, and QA reports are internally consistent.
- [ ] 9.5 Write docs/document-preprocessing.md with setup, sample, full run, publish, resume, cleanup, troubleshooting, and downstream consumption instructions; completion condition: a teammate can reproduce the sample run from the guide without editing source code.
- [ ] 9.6 Record the released release_id as the input contract for a separate build-document-chunking-pipeline OpenSpec change; completion condition: the future change references the normalized-document schema rather than raw files or Docling internals.
