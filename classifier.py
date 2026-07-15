#!/usr/bin/env python3
"""
classifier.py
--------------
Step 2 of the news pipeline. Takes the JSON produced by fetch.py (a query
result containing an "articles" array) and annotates every article with an
AI-assigned category, confidence score, optional secondary categories, and
which model produced the classification.

This script is designed to be a reliable, non-silent middle step in a
multi-stage pipeline: any unrecoverable failure exits with a non-zero status
and a clear error message on stderr, rather than writing partial/corrupt
output.

USAGE
    python classifier.py --input demo_output.json --output classified.json
    python classifier.py --input demo_output.json --in-place
    python classifier.py --input demo_output.json --output out.json \\
        --categories "Funding" "Regulation" "Product Launches"

CONFIGURATION
    All tunable knobs live in the CONFIG block below. Command-line flags
    override these defaults where noted.

MODEL BACKENDS
    BaseClassifierModel (below) is an abstract base class. Every backend
    (Gemini, Groq, or a future custom/proprietary model) implements the same
    classify_batch() contract, so the orchestration code never needs to know
    which provider it's talking to. To add your own model, subclass
    BaseClassifierModel, implement `name` and `_call_api`, and add an
    instance to build_default_model_chain().
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import requests

try:
    import dotenv
    dotenv.load_dotenv()
except ImportError:
    pass

# ==========================================================================
# CONFIGURATION  (edit these to change default behavior)
# ==========================================================================

# Default categories used when --categories is not passed on the CLI.
# Chosen for competitive-intelligence / business-analytics use cases.
DEFAULT_CATEGORIES = [
    "Funding & Investment",       # raises, acquisitions, valuations
    "Partnerships & Alliances",   # joint ventures, integrations, collaborations
    "Regulatory & Policy",        # government/regulatory action, compliance, legal rulings
    "Product Launches",           # new products, features, major updates
    "Market & Competitive Moves", # market share shifts, competitor strategy, pricing
    "Leadership & Talent",        # executive changes, hiring, layoffs, org changes
    "Research & Technology",      # R&D breakthroughs, technical papers, new capabilities
]

# How many articles to send to the model per API call. Batching reduces
# API call volume (important on free tiers) at the cost of a larger prompt
# per call. Overridable with --batch-size.
DEFAULT_BATCH_SIZE = 10

# Max characters of `description` sent to the model. Set to -1 to send the
# full, untruncated description. Overridable with --description-limit.
DEFAULT_DESCRIPTION_LIMIT = 500

# Per-model-attempt retry count for *retryable* errors (e.g. 429, transient
# network issues, transient malformed output) before moving to the next
# model in the chain.
MAX_RETRIES_PER_MODEL = 2

# Base delay (seconds) for exponential backoff between retries.
RETRY_BACKOFF_BASE_SECONDS = 2.0


# ==========================================================================
# MODEL BACKENDS
# ==========================================================================

class ModelError(Exception):
    """Raised when a model backend fails to produce a usable classification.

    Distinguishing `retryable` lets the orchestrator decide whether to retry
    the same model (e.g. transient 503) or move straight to the next
    fallback model (e.g. invalid API key -- retrying won't help).
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ClassificationResult:
    index: int
    category: str
    confidence: float
    secondary_categories: list[str]


def _build_prompt(articles: list[dict], categories: list[str]) -> str:
    """Builds the classification prompt using prompt-engineering best practices:
    explicit role/task framing, a closed label set, a strict output schema,
    a worked example, and an explicit instruction to return JSON only.
    """
    category_list = "\n".join(f"- {c}" for c in categories)
    articles_block = json.dumps(
        [{"index": a["index"], "title": a["title"], "description": a["description"]} for a in articles],
        ensure_ascii=False,
        indent=2,
    )

    return f"""You are a precise news-classification engine for a competitive intelligence system.

TASK: Classify each article below into exactly one PRIMARY category from this fixed list, and optionally 1-2 SECONDARY categories from the same list if they also meaningfully apply. Do not invent categories outside this list.

CATEGORIES:
{category_list}

RULES:
1. "category" must be exactly one of the category strings above (case-sensitive match).
2. "confidence" is your calibrated confidence in the PRIMARY category, a float between 0.0 and 1.0 (use the full range; do not default to 0.9 for everything).
3. "secondary_categories" is a list of zero or more OTHER categories from the list above that also apply. Omit categories that are only weakly related. Use an empty list if none apply.
4. Base your judgment only on the title and description provided. If the description is empty, classify from the title alone and lower your confidence accordingly.
5. Return EVERY article from the input, in the same order, referenced by its "index" field.

OUTPUT FORMAT: Return ONLY a raw JSON array (no markdown fences, no commentary, no explanation before or after). Each element must match this exact schema:
{{"index": <int>, "category": "<string>", "confidence": <float 0-1>, "secondary_categories": ["<string>", ...]}}

EXAMPLE OUTPUT for two articles:
[
  {{"index": 0, "category": "Product Launches", "confidence": 0.93, "secondary_categories": ["Funding & Investment"]}},
  {{"index": 1, "category": "Regulatory & Policy", "confidence": 0.81, "secondary_categories": []}}
]

ARTICLES TO CLASSIFY:
{articles_block}

Return the JSON array now.
"""


def _extract_json_array(raw_text: str) -> list[dict]:
    """Strips common LLM wrapping (markdown fences, stray prose) and parses JSON.
    Raises ModelError if no valid JSON array can be recovered.
    """
    text = raw_text.strip()

    # Strip markdown code fences if present (```json ... ``` or ``` ... ```)
    fence_match = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        # Fallback: grab the first '[' to the last ']' in case of stray prose
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ModelError(f"Model output was not valid JSON: {e}", retryable=False)

    if not isinstance(parsed, list):
        raise ModelError("Model output JSON was not a list as required.", retryable=False)

    return parsed


def _validate_and_normalize(
    parsed: list[dict], articles: list[dict], categories: list[str]
) -> list[ClassificationResult]:
    """Validates model output against the expected schema and category set.
    Repairs minor issues (out-of-range confidence, unknown secondary categories)
    where safe; raises ModelError when the output can't be trusted.
    """
    expected_indices = {a["index"] for a in articles}
    by_index: dict[int, dict] = {}

    for item in parsed:
        if not isinstance(item, dict) or "index" not in item:
            continue
        by_index[item["index"]] = item

    missing = expected_indices - set(by_index.keys())
    if missing:
        raise ModelError(
            f"Model output is missing classifications for article indices: {sorted(missing)}",
            retryable=True,
        )

    results: list[ClassificationResult] = []
    category_set_lower = {c.lower(): c for c in categories}

    for idx in sorted(expected_indices):
        item = by_index[idx]
        raw_category = str(item.get("category", "")).strip()
        category = category_set_lower.get(raw_category.lower())
        if category is None:
            raise ModelError(
                f"Model returned an unrecognized category '{raw_category}' for article index {idx}.",
                retryable=True,
            )

        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        secondary_raw = item.get("secondary_categories", []) or []
        secondary = []
        if isinstance(secondary_raw, list):
            for s in secondary_raw:
                matched = category_set_lower.get(str(s).strip().lower())
                if matched and matched != category and matched not in secondary:
                    secondary.append(matched)

        results.append(
            ClassificationResult(
                index=idx,
                category=category,
                confidence=round(confidence, 3),
                secondary_categories=secondary,
            )
        )

    return results


class BaseClassifierModel(ABC):
    """Abstract base class for all classification backends.

    To add a custom/proprietary model, subclass this, implement `name` and
    `_call_api`, and add an instance to build_default_model_chain() below.
    """

    #: Human-readable identifier written into each article's "categorized_by" field.
    name: str = "base"

    @abstractmethod
    def _call_api(self, prompt: str) -> str:
        """Calls the underlying LLM API with the given prompt and returns raw text output.
        Must raise ModelError on any failure (auth, network, rate limit, etc).
        """
        raise NotImplementedError

    def classify_batch(self, articles: list[dict], categories: list[str]) -> list[ClassificationResult]:
        prompt = _build_prompt(articles, categories)
        raw_output = self._call_api(prompt)
        parsed = _extract_json_array(raw_output)
        return _validate_and_normalize(parsed, articles, categories)


class GeminiModel(BaseClassifierModel):
    """Google Gemini backend, called via the plain REST generateContent endpoint."""

    name = "gemini"

    def __init__(self, model_id: str, api_key: str | None = None, timeout: int = 30):
        self.model_id = model_id
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.timeout = timeout

    def _call_api(self, prompt: str) -> str:
        if not self.api_key:
            raise ModelError("GEMINI_API_KEY is not set.", retryable=False)

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_id}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise ModelError(f"Gemini network error: {e}", retryable=True)

        if resp.status_code == 401 or resp.status_code == 403:
            raise ModelError(f"Gemini auth error ({resp.status_code}): {resp.text[:300]}", retryable=False)
        if resp.status_code == 429:
            raise ModelError(f"Gemini rate limited (429): {resp.text[:300]}", retryable=True)
        if resp.status_code >= 500:
            raise ModelError(f"Gemini server error ({resp.status_code}): {resp.text[:300]}", retryable=True)
        if resp.status_code != 200:
            raise ModelError(f"Gemini error ({resp.status_code}): {resp.text[:300]}", retryable=False)

        try:
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, ValueError) as e:
            raise ModelError(f"Unexpected Gemini response shape: {e}", retryable=True)

        return text


class GroqModel(BaseClassifierModel):
    """Groq Cloud backend (OpenAI-compatible chat completions endpoint)."""

    name = "groq"

    def __init__(self, model_id: str, api_key: str | None = None, timeout: int = 30):
        self.model_id = model_id
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.timeout = timeout

    def _call_api(self, prompt: str) -> str:
        if not self.api_key:
            raise ModelError("GROQ_API_KEY is not set.", retryable=False)

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        body = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise ModelError(f"Groq network error: {e}", retryable=True)

        if resp.status_code == 401 or resp.status_code == 403:
            raise ModelError(f"Groq auth error ({resp.status_code}): {resp.text[:300]}", retryable=False)
        if resp.status_code == 429:
            raise ModelError(f"Groq rate limited (429): {resp.text[:300]}", retryable=True)
        if resp.status_code >= 500:
            raise ModelError(f"Groq server error ({resp.status_code}): {resp.text[:300]}", retryable=True)
        if resp.status_code != 200:
            raise ModelError(f"Groq error ({resp.status_code}): {resp.text[:300]}", retryable=False)

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise ModelError(f"Unexpected Groq response shape: {e}", retryable=True)

        return text


# Ordered fallback chain of models to try per batch. The first entry is
# tried first; if it raises ModelError, the next is tried, and so on.
# To add your own model: subclass BaseClassifierModel above, then append
# an instance here (or insert it wherever you want it in the order).
def build_default_model_chain() -> list[BaseClassifierModel]:
    return [
        GeminiModel(model_id="gemini-3.1-flash-lite"),
        GroqModel(model_id="llama-3.3-70b-versatile"),
    ]


# Registry used by --provider to pin the run to a single named backend,
# bypassing the fallback chain entirely. Keys are the CLI-facing provider
# names; add an entry here for any custom model you want selectable by name.
def build_model_by_provider_name(provider: str) -> BaseClassifierModel:
    registry: dict[str, BaseClassifierModel] = {
        "gemini": GeminiModel(model_id="gemini-3.1-flash-lite"),
        "groq": GroqModel(model_id="llama-3.3-70b-versatile"),
    }
    model = registry.get(provider.lower())
    if model is None:
        valid = ", ".join(sorted(registry.keys()))
        fatal_error(f"Unknown --provider '{provider}'. Valid options: {valid}.")
    return model


# ==========================================================================
# ORCHESTRATION
# ==========================================================================

def truncate_description(description: str, limit: int) -> str:
    if description is None:
        return ""
    if limit is None or limit < 0:
        return description
    return description[:limit]


def chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def classify_all_batches(
    articles: list[dict],
    categories: list[str],
    model_chain: list[BaseClassifierModel],
    batch_size: int,
    description_limit: int,
) -> dict[int, dict]:
    """Classifies every article, batch by batch, trying each model in the
    fallback chain in order. Returns a dict mapping article index -> result
    dict (category, confidence, secondary_categories, categorized_by).

    Aborts the whole run (via fatal_error) if EVERY model in the chain fails
    for a given batch -- this is treated as a fatal, whole-run failure
    rather than silently skipping articles, per the "should break loudly"
    requirement.
    """
    indexed_articles = [
        {
            "index": i,
            "title": a.get("title", "") or "",
            "description": truncate_description(a.get("description", "") or "", description_limit),
        }
        for i, a in enumerate(articles)
    ]

    batches = chunk(indexed_articles, batch_size)
    results: dict[int, dict] = {}

    for batch_num, batch in enumerate(batches, start=1):
        batch_indices = [a["index"] for a in batch]
        print(
            f"[classifier] Batch {batch_num}/{len(batches)} "
            f"(articles {batch_indices[0]}-{batch_indices[-1]})...",
            file=sys.stderr,
        )

        batch_succeeded = False
        last_error: Exception | None = None

        for model in model_chain:
            for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
                try:
                    classifications = model.classify_batch(batch, categories)
                    for c in classifications:
                        results[c.index] = {
                            "category": c.category,
                            "confidence": c.confidence,
                            "secondary_categories": c.secondary_categories,
                            "categorized_by": model.name,
                        }
                    print(
                        f"[classifier]   -> succeeded via '{model.name}' "
                        f"(attempt {attempt})",
                        file=sys.stderr,
                    )
                    batch_succeeded = True
                    break
                except ModelError as e:
                    last_error = e
                    print(
                        f"[classifier]   ! '{model.name}' failed (attempt {attempt}/"
                        f"{MAX_RETRIES_PER_MODEL}): {e}",
                        file=sys.stderr,
                    )
                    if e.retryable and attempt < MAX_RETRIES_PER_MODEL:
                        delay = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                        time.sleep(delay)
                        continue
                    break  # not retryable, or out of retries -> try next model
            if batch_succeeded:
                break

        if not batch_succeeded:
            fatal_error(
                f"All configured models failed to classify batch {batch_num}/{len(batches)} "
                f"(articles {batch_indices[0]}-{batch_indices[-1]}). "
                f"Last error: {last_error}"
            )

    return results


def fatal_error(message: str) -> None:
    """Aborts the run loudly. This is a mid-pipeline step, so a failure here
    must be unambiguous (non-zero exit, clear stderr message) rather than
    producing partial or silently-wrong output for the next stage to choke on.
    """
    print(f"[classifier] FATAL: {message}", file=sys.stderr)
    sys.exit(1)


def load_input(input_path: Path) -> dict:
    if not input_path.exists():
        fatal_error(f"Input file not found: {input_path}")
    try:
        with input_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        fatal_error(f"Input file is not valid JSON: {e}")
    except OSError as e:
        fatal_error(f"Could not read input file: {e}")

    if "articles" not in data or not isinstance(data["articles"], list):
        fatal_error("Input JSON does not contain an 'articles' array. Was this produced by fetch.py?")

    return data


def write_output(data: dict, output_path: Path) -> None:
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        fatal_error(f"Could not write output file '{output_path}': {e}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify articles in a fetch.py JSON output into categories using an LLM."
    )
    parser.add_argument("--input", "-i", required=True, type=Path, help="Path to input JSON file (from fetch.py).")
    parser.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Path to write classified JSON output. Required unless --in-place is set.",
    )
    parser.add_argument(
        "--in-place", action="store_true",
        help="Overwrite the input file with the classified output instead of writing to --output.",
    )
    parser.add_argument(
        "--categories", "-c", nargs="+", default=None,
        help="Custom list of category names to classify into. Space-separated, quote multi-word categories. "
             "Defaults to a built-in 7-category competitive-intelligence set if omitted.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help=f"Number of articles per classification API call (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--description-limit", type=int, default=DEFAULT_DESCRIPTION_LIMIT,
        help=f"Max characters of each article description sent to the model. "
             f"Use -1 to send the full description (default: {DEFAULT_DESCRIPTION_LIMIT}).",
    )
    parser.add_argument(
        "--provider", "-p", default=None,
        help="Pin classification to a single named model provider (e.g. 'gemini' or 'groq'), "
             "skipping the fallback chain entirely -- if that provider fails, the run fails. "
             "If omitted, the default fallback chain is used (Gemini first, then Groq).",
    )
    args = parser.parse_args()

    if not args.in_place and args.output is None:
        parser.error("either --output must be provided, or --in-place must be set.")
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1.")

    return args


def main() -> None:
    args = parse_args()
    categories = args.categories if args.categories else DEFAULT_CATEGORIES

    data = load_input(args.input)
    articles = data["articles"]

    if not articles:
        print("[classifier] No articles found in input; nothing to classify.", file=sys.stderr)
    else:
        if args.provider:
            model_chain = [build_model_by_provider_name(args.provider)]
            print(f"[classifier] --provider set: pinned to '{args.provider}' (no fallback).", file=sys.stderr)
        else:
            model_chain = build_default_model_chain()
        results = classify_all_batches(
            articles=articles,
            categories=categories,
            model_chain=model_chain,
            batch_size=args.batch_size,
            description_limit=args.description_limit,
        )

        output_data = copy.deepcopy(data)
        for i, article in enumerate(output_data["articles"]):
            r = results.get(i)
            if r is None:
                # Should be unreachable given classify_all_batches' fatal_error
                # behavior, but guard against it rather than writing bad data.
                fatal_error(f"Internal error: no classification result for article index {i}.")
            article["category"] = r["category"]
            article["confidence"] = r["confidence"]
            article["secondary_categories"] = r["secondary_categories"]
            article["categorized_by"] = r["categorized_by"]

        output_data["classification_meta"] = {
            "categories_used": categories,
            "batch_size": args.batch_size,
            "description_limit": args.description_limit,
        }
        data = output_data

    output_path = args.input if args.in_place else args.output
    write_output(data, output_path)
    print(f"[classifier] Done. Wrote {len(articles)} classified articles to {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
