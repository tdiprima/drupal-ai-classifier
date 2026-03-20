# drupal_ai_scanner.py

`drupal_ai_scanner.py` is a post-import enrichment script that fetches Drupal nodes, sends each product to Azure OpenAI for AI-feature classification, and PATCHes the result back onto the node through Drupal JSON:API.

It does not create nodes. It reads existing Drupal content, decides whether each product appears to use AI, and writes a small set of review fields back to Drupal.

## What It Does

For each pending Drupal node in the target content type:

1. Fetches the node from Drupal JSON:API.
2. Extracts vendor, product, and description.
3. Sends that data to Azure OpenAI using a fixed analyst-style prompt.
4. Parses the model response into `has_ai`, `confidence`, and `reason`.
5. Maps that result into Drupal field values.
6. PATCHes the fields back onto the same node.
7. Records the node UUID in a progress file so later runs skip it.

The fields it writes are:

- `field_ai_application`
- `field_confidence`
- `field_reason`
- `field_risk_review_required`

## How It Works

### 1. Loads Drupal and Azure configuration

The script reads all required settings from `.env`:

- `DRUPAL_BASE_URL`
- `DRUPAL_USERNAME`
- `DRUPAL_PASSWORD`
- `DRUPAL_CONTENT_TYPE`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT`

It exits if any are missing.

Those settings define:

- which Drupal site and content type to scan
- which Drupal account to use
- which Azure OpenAI endpoint and deployment to call

### 2. Uses a progress file to make scanning resumable

The scanner keeps state in:

```text
../ai_scan_progress.json
```

That file stores:

- the set of completed Drupal node UUIDs
- whether this is effectively the first run
- last-run timestamps and summary stats

Because completion is tracked by UUID, reruns skip nodes that were already scanned successfully and retry only the ones that failed or were never reached.

### 3. Limits the first run by default

If no `--limit` is given and no progress file exists yet, the script processes only the first 10 nodes.

That behavior is intentional. It acts as a safety check before scanning the entire dataset with a live model and writing results back to Drupal.

After the first run:

- later runs process all remaining nodes by default
- `--limit` can still override the batch size explicitly

### 4. Fetches pending nodes from Drupal

The script queries:

```text
/jsonapi/node/<content_type>
```

and requests only the fields it needs:

- `id`
- `title`
- `field_vendor_name`
- `field_product_name`
- `field_description`

It follows JSON:API pagination automatically and filters out nodes whose UUIDs are already in the completed set.

Each pending node is reduced to a compact scan entry containing:

- `uuid`
- `title`
- `vendor`
- `product`
- `description`

### 5. Builds an Azure OpenAI client

The script uses the `openai` Python client with `AzureOpenAI` and a fixed API version:

```text
2024-02-15-preview
```

It then calls the configured Azure deployment for each product record.

### 6. Sends a conservative classification prompt

The actual AI prompt comes from `ai_scanner_core.py`.

For each product, it sends:

- vendor
- product
- description, if present

The model is asked to determine whether the software contains:

- embedded AI or machine learning
- AI assistants or chatbots
- transcription or speech-to-text
- prediction or recommendation models
- AI OCR or document processing
- NLP features
- cloud AI API usage
- computer vision or image recognition

The prompt requires this exact output shape:

```text
HAS_AI: YES or NO or UNKNOWN
CONFIDENCE: HIGH, MEDIUM, or LOW
REASON: One concise sentence
```

The instructions are intentionally conservative:

- if there is a reasonable chance the software has AI features, answer `YES`
- if the software is not recognized, answer `UNKNOWN`

### 7. Parses and normalizes the model response

`ai_scanner_core.check_for_ai()` parses the model output line by line and extracts:

- `has_ai`
- `confidence`
- `reason`

If parsing is imperfect, the code falls back to:

- `UNKNOWN` for `has_ai`
- `LOW` for `confidence`
- a generic fallback reason

If the API call itself fails, the function returns:

- `has_ai = ERROR`

That is treated as a failed scan, and the node is not marked complete.

The reason is also truncated defensively to stay within the expected size limit.

### 8. Maps AI output to Drupal fields

The script converts the parsed AI result into Drupal attributes with a conservative policy:

- `YES` -> `field_ai_application = yes`
- `NO` -> `field_ai_application = no`
- `UNKNOWN` -> `field_ai_application = yes`

`UNKNOWN` is intentionally treated as a potential AI application rather than a safe negative.

It also maps:

- `field_confidence` from `low`, `medium`, or `high`
- `field_reason` from the model's reason text
- `field_risk_review_required = yes` for `YES` and `UNKNOWN`
- `field_risk_review_required = no` for `NO`

If confidence is outside the expected Drupal keys, it is clamped to `low`.

If the reason exceeds 256 characters, it is truncated at a word boundary and ended with `...`.

### 9. PATCHes the node through Drupal JSON:API

For successful AI results, the script sends a PATCH request to:

```text
/jsonapi/node/<content_type>/<uuid>
```

using a payload like:

```json
{
  "data": {
    "type": "node--<content_type>",
    "id": "<uuid>",
    "attributes": {
      "field_ai_application": "yes",
      "field_confidence": "medium",
      "field_reason": "Short explanation",
      "field_risk_review_required": "yes"
    }
  }
}
```

If the PATCH succeeds:

- the node UUID is added to the completed set
- progress is saved immediately

If the PATCH fails:

- the failure is logged
- the node is left incomplete
- a later run will retry it

### 10. Supports dry runs and reset behavior

With `--dry-run`, the script fetches pending nodes and logs which titles it would scan, but it does not call Azure OpenAI and does not PATCH Drupal.

With `--reset`, it deletes the progress file first, which causes the scanner to treat the next run as a fresh pass.

## Usage

```bash
python drupal_ai_scanner.py
python drupal_ai_scanner.py --limit 25
python drupal_ai_scanner.py --reset
python drupal_ai_scanner.py --dry-run
python drupal_ai_scanner.py --debug
```

### Arguments

- `--dry-run`: list pending nodes without scanning or patching
- `--debug`: enable verbose logging, including raw AI responses
- `--reset`: delete scan progress and start over
- `--limit N`: process only the first `N` pending nodes

## What The Summary Means

At the end of a run, the script logs:

- how many nodes were processed in the current run
- how many succeeded
- how many failed
- how many total UUIDs are now marked complete
- total elapsed time

If failures occurred, the intended recovery path is to rerun the script.

## Important Behavioral Details

This scanner is intentionally conservative and operationally simple.

Important consequences:

- `UNKNOWN` is treated as review-worthy, not safe
- only successful PATCHes mark a node complete
- AI API failures do not poison progress state
- the first run defaults to a 10-node sanity-check batch
- only a small field subset is fetched and updated
- SSL verification is disabled for the current internal Drupal environment

That means the script is designed more as a cautious enrichment pass than as a high-throughput batch inference pipeline. Its default behavior prioritizes safe rollout and easy retry over aggressive parallel processing or speculative updates.
