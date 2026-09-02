# TODO

Future work for `ccsearch`. These items are intentionally focused on high-value follow-up work that was not completed in the current iteration.

## Search Quality

- Add optional reranking hooks for search-style engines.
  - Candidates: Brave results, Brave LLM Context results, or fetched chunk sets.
  - Keep this as a post-processing stage, not a separate engine.
- Add stronger result filtering controls.
  - Domain bias controls
  - Duplicate-domain suppression
  - Optional "prefer official docs" mode
- Add query-time sorting/selection helpers for agents.
  - Example: prefer newest results when freshness is set
  - Example: prefer documentation/reference-style pages

## Fetch Quality

- Improve citation precision for `fetch` chunks.
  - Better source offsets
  - More stable section anchoring
  - Better chunk-to-link provenance
- Improve table fidelity.
  - Preserve multi-row headers more accurately
  - Preserve more table semantics from Markdown / HTML
- Improve code extraction further.
  - Better language detection
  - Better handling for nested/preformatted docs layouts
- Expand binary document coverage and edge-case handling.
  - More PDF edge cases
  - More Office export edge cases
  - Better fallback behavior for unsupported binary formats

## API / MCP / CLI

- Validate `fetch` inputs with parsed `http`/`https` schemes and a hostname; the current shared validator only checks the string prefix.
- Coerce malformed batch option types per item so one bad `cache_ttl`, `semantic_threshold`, or `offset` value cannot abort the whole batch before per-request isolation applies.
- Audit external integration templates for `/batch` and `/diagnostics`; both checked-in skill files already include first-class examples.
- Consider adding a dedicated `result_format` or `compact` mode for lighter agent payloads.
- Consider exposing an explicit chunk-focused fetch mode for agent workflows that do not need the full `content` body.

## Operations

- Keep FlareSolverr loopback-only on every host. A1-JP, the A1-US rollback copy, and Pi5 now bind `127.0.0.1:8191`; do not republish `0.0.0.0:8191` or `[::]:8191` if compose is recreated.
- Replace MCP URL-path authentication with a mechanism that does not place the shared API key in access logs, or add reliable access-log redaction across Uvicorn and Cloudflare.
- Replace the Flask development server with a production WSGI/ASGI deployment setup.
  - Gunicorn/Uvicorn worker model
  - Clear service documentation for production mode
- Add structured logging.
  - Request id
  - Engine
  - Cache status
  - Latency
  - Error classification
- Add metrics.
  - Request counts by engine
  - Cache hit rates
  - Batch dedupe rate
  - Fetch fallback rates
- Add rate limiting / abuse protection for public HTTP usage.

## Testing

- Add more integration tests against a live temporary API server.
- Add regression tests for more real-world HTML fixtures.
- Add load/concurrency tests for batch execution and cache behavior.
- Add deployment smoke tests for systemd-managed services.

## Documentation

- Add a deployment guide that separates:
  - local development
  - self-hosted personal deployment
  - public Internet-facing deployment
- Add an environment matrix documenting which optional dependencies unlock which features.
- Keep the existing engine-selection and batch examples synchronized across README and both checked-in skill variants.

## Nice-to-Have

- Add a small benchmark script for:
  - Brave vs. LLM Context latency
  - exact cache vs. semantic cache
  - fetch direct vs. FlareSolverr fallback
- Add a compatibility note for MCP clients that only support SSE vs. Streamable HTTP.
