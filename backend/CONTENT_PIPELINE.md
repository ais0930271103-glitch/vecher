# Public content analysis and author-feed intake

This pipeline processes the public catalogue used by **«Интересное»**. It never
reads messages, private answers, photographs, calls or a partner's personal
interests. Publication still uses the existing reviewed snapshot and atomic
`app.publish_content` command. These commands do not deploy an application.

## What works without a model

`app.content_analysis.analyze_content(publication)` reads an explicit allowlist:
title, summary/description, public text blocks, and an explicitly supplied public
transcript when the caller actually has one. It produces:

- normalized topics (`cooking → food`, `science → curiosity`), without mapping
  unrelated `hobbies` or `nature` into another topic;
- weighted keywords and lexical similarity for related public materials;
- the actual analysis basis (`title`, `summary`, `article_text`,
  `provided_transcript`), matched topic words and an analyser version;
- a content fingerprint, so changed public text invalidates cached features.

The default is **a lexical baseline, not a neural model**. Its `confidence`
field is an uncalibrated evidence indicator, not a probability that a topic or a
claim is correct. Simple vocabulary and inflection rules can miss meaning,
irony or negation. `lexical_query_terms` preserves negation tokens for the search
layer to interpret; it does not itself understand an entire request.
Numbers remain literal query tokens: «до 20 минут» is not parsed as a numeric
less-than-or-equal duration filter.

A video link alone supplies no audio or frames. A reviewed video bridge is
analysed from its catalogue title and description. No transcription, video
download, AI model service or public-platform scraping is silently started.

## Optional local semantic index

An administrator may install `sentence-transformers` in a separate analysis
worker and provide an already downloaded, reviewed local model directory. This
package is not added to the API's mandatory dependencies. There is no automatic
model download; `local_files_only=True` and `trust_remote_code=False` are used.

```bash
python -m app.content_analysis \
  --source /srv/vecher/content/publications.json \
  --model-path /srv/vecher/models/local-sentence-model \
  --model-id local-sentence-model-immutable-revision \
  --output /srv/vecher/content/embeddings.json
```

Use an immutable `--model-id` for the exact weights/tokenizer revision. Changing
weights requires a new ID. This command indexes the approved unified catalogue,
including reviewed video bridges, and encodes only changed text when the previous
index has the same model/analyser version. The index contains item IDs, text
fingerprints and vectors, not user preferences or private profiles.

Point the API to the generated regular file:

```text
VECHER_CONTENT_EMBEDDINGS=/srv/vecher/content/embeddings.json
```

`load_embedding_index()` reads bounded, validated public vectors without loading
a model. The recommender uses the same loaded index for a request and includes
its revision in cursor identity. Missing, malformed or stale entries fall back
to lexical similarity. Rebuilding an index changes its revision; an old cursor
then requests a refreshed list instead of silently mixing rankings. A missing
model or index does not disable the ordinary feed. The local model has not been
installed or benchmarked by this change.

Sentence Transformers documents the local-only and remote-code switches at:
<https://www.sbert.net/docs/package_reference/sentence_transformer/model.html>.

## Explicit source registry

`app.content_ingestion` is an administrator-only CLI. The application has no
user-controlled URL-fetch endpoint. Each source must be explicitly registered,
and its author must already exist in the editorial catalogue. An empty registry
is valid and performs no network requests:

```json
{"version": 1, "timeout_seconds": 15, "sources": []}
```

The following **illustrates the format**; `author.example.org` is not a connected
source and must be replaced with a feed actually provided by an authorized
author:

```json
{
  "version": 1,
  "timeout_seconds": 15,
  "sources": [
    {
      "id": "author-feed",
      "url": "https://author.example.org/vecher-feed.json",
      "author_id": "registered-author-id",
      "signing_key_env": "VECHER_AUTHOR_FEED_KEY"
    }
  ]
}
```

`signing_key_env` is optional. When configured, the named worker environment
variable must contain a shared secret of at least 32 UTF-8 bytes. The author
returns `X-Vecher-Signature: sha256=<hex HMAC-SHA256 of the exact response body>`.
Secrets never belong in source JSON, draft files, URLs or logs. Without the
signature option, trust is restricted to the explicitly registered HTTPS endpoint
plus schema validation; it is not described as a signed feed.

Fetch protections: HTTPS/443 only, no credentials or fragments, no IP-literal
URLs, public-only DNS results, connection pinned to the checked IP with the
original TLS hostname, no redirects, no proxy-environment fallback, no compressed
responses, bounded DNS/connect/read waiting and a hard socket deadline. The
response must be JSON, at most 2 MiB and 100 materials per source. There may be at
most 20 registered sources. Source errors are isolated, and logs omit response
bodies and secrets.

## Author JSON format

The envelope has exactly `version: 1` and `items: [...]`. Each item uses the
existing `Publication` fields except `author_id`, `active` and `reviewed_at`.
Those three fields are rejected, so a source cannot impersonate another author
or approve itself. Required content fields are:

| Field | Meaning |
| --- | --- |
| `id` | Stable author-local slug; intake assigns a source-scoped public ID. |
| `kind` | `post`, `guide` or `video`. |
| `title`, `summary`, `blocks` | Real public text in the existing plain-text block format. |
| `topics` | Registered topic IDs; may be omitted/empty for evidence-based lexical classification into registered topics. |
| `language` | `ru`, `en` or `none`. |
| `published_at`, `updated_at` | Actual timezone-qualified source timestamps. |
| `reading_minutes` | Integer 1–60. |
| `sources` | Optional attributed HTTPS source links. |
| `video_id` | Required only for `video`; refers to a separately reviewed video catalogue entry. |

Series fields retain the existing all-or-none rule. Unknown JSON keys, duplicate
keys/IDs, non-finite numbers, oversized bodies and invalid publication structures
are rejected. Insufficient topic evidence leaves the source refresh unsuccessful
instead of inventing a topic. An author-provided transcript can be placed in
the public text blocks. Intake does not fabricate a transcript from a URL.

## Refresh and scheduling

Create a managed output directory and run the worker with the same editorial
catalogue selection as the API (`VECHER_CONTENT_CATALOGUE` when overridden):

```bash
python -m app.content_ingestion refresh \
  --config /srv/vecher/content/author-feeds.json \
  --drafts /srv/vecher/content-review/drafts.json
```

The command performs one refresh and exits nonzero if any source failed. Add
`--interval-seconds 21600` to repeat six hours after each completed refresh;
intervals must be at least 300 seconds. Run that process under the deployment's
normal supervisor, or schedule the single-run command externally. No schedule
or real source is enabled automatically by this change.

Successful refreshes upsert source-scoped drafts with source/content hashes and
analysis evidence. The same item produces the same identity and content hash.
A source failure preserves its existing pending drafts. Removing a source from
the registry drops its pending queue entries. None of these actions withdraws,
replaces or activates anything in the live published catalogue.

## Review and publication

Inspect `drafts.json`, its actual text, attribution and analysis evidence. For
each approved draft, copy the exact `id` and `content_hash`. Export only the
reviewed versions:

```bash
python -m app.content_ingestion review-export \
  --drafts /srv/vecher/content-review/drafts.json \
  --approve DRAFT_ID:EXACT_CONTENT_SHA256 \
  --reviewed-at YYYY-MM-DD \
  --output /srv/vecher/content-review/reviewed-publications.json
```

Repeat `--approve` for multiple materials. A changed hash, missing item, duplicate
approval or future review date is rejected. The command prepares a candidate
snapshot and prints `EXPECTED_PREVIOUS_REVISION`; it does not install it.

Use the existing validation and compare-and-swap publisher:

```bash
python -m app.publish_content \
  --source /srv/vecher/content-review/reviewed-publications.json --check-only

python -m app.publish_content \
  --source /srv/vecher/content-review/reviewed-publications.json \
  --destination /srv/vecher/content/publications.json \
  --expected-revision EXACT_PREVIOUS_REVISION
```

The publisher verifies video references, catalogue consistency and the exact
previous revision, then replaces the snapshot atomically. A concurrent editorial
change requires a fresh review/export. Draft refresh, analysis and publication
do not connect new external video platforms or bypass their access conditions.
