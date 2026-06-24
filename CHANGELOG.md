# Changelog

All notable changes to this project are documented in this file.
The format groups changes by release and focuses on user-facing behavior.

---

## [v2.8.x] — 2026-02

### Quality & reliability
- **Smarter timeout handling**: long-running synthesis (Judge) now fails fast and falls back to the best single answer instead of stalling, so slow questions still return promptly.
- **Cross-family evaluation**: quality scoring uses a different model family than the one being scored, removing same-family self-preference bias.
- **Pairwise win-rate as the primary quality metric**: rankings are based on head-to-head comparisons rather than absolute scores, making results robust to scorer style.
- **Tiered request timeouts**: fast / standard / reasoning / heavy models each get an appropriate timeout instead of a single global value.
- **Error messages clarified**: quota-exhausted and unreachable-backend cases now show actionable messages instead of a generic system error.

### Security
- **FastAPI/Starlette upgraded** to close a known CVE.
- **CSRF hardening**: removed an IP-based CSRF exemption that could weaken protection behind a reverse proxy.
- **Output sanitization**: streaming and error paths no longer leak raw exception text to clients.
- **Strict CORS handling**: credentialed requests require an explicit origin allowlist.

---

## [v2.8.0] — 2026-02-20

### Persistent conversations + Perplexity integration
- **Persistent multi-turn storage**: Light / Deep / Research conversations are now saved to disk and survive restarts (previously in-memory only).
- **Perplexity Sonar**: added to Light and Research modes as a real-time search contributor.
- **Frontend**: concurrent multi-question queries, Mermaid diagrams, chart visualization, multimodal file upload, PWA support, and an error boundary.

---

## [v2.7.x] — 2026-02

### Accounts, quotas, and the growth dashboard
- **User accounts**: registration/login with HttpOnly session cookies and per-user profile isolation.
- **Auth middleware**: Bearer token + cookie dual-mode authentication with a public-path allowlist.
- **API server**: 20+ endpoints with SSE streaming, rate limiting, and security headers.
- **Quotas**: per-user daily/monthly API call limits.
- **Admin dashboard**: aggregate usage overview.
- **Cognitive profile & behavior analytics**: four-quadrant cognitive distribution, comfort/growth-zone topic tracking, and a capability-map growth dashboard.
- **Proactive coach**: optional learning-suggestion nudges.
- **Content safety**: safety rules applied across all model call sites.

---

## [v2.5.0] — 2026-02-15

### Socratic mode
- **Two-phase pipeline**: a fast fan-out phase followed by a lightweight per-turn dialogue phase.
- **Divergence analysis**: surfaces points of disagreement to guide deeper thinking.
- **Socratic guide**: fast, low-latency follow-up questioning.

---

## [v2.4.3] — 2026-02-14

### Two-layer aggregation + adaptive pipeline
- **MoA layer 2**: in Research/Deep modes, each model can improve its answer after seeing the others' responses.
- **Adaptive aggregation**: pipeline behavior adjusts by question type (factual / analytical / technical / controversial / creative).
- **Smart routing**: skips unnecessary synthesis/refinement steps for simple question types.

---

## [v2.3.0] — 2026-02-13

### Multi-round refinement + Quality Gate
- **Answer critic → judge refine**: up to 2 refinement rounds in Deep mode and up to 3 in Research mode.
- **Quality Gate**: chooses between best-single, synthesized, and low-confidence response paths.
- **Pairwise ranking**: head-to-head comparison to reduce same-family bias.

---

## [v2.0.x] — 2026-02-12

### Dual-mode architecture + core pipeline
- **Dual intent**: Answer Intent (answer quality) and Growth Intent (cognitive development).
- **Five modes**: Light, Deep, Research, Auto, and Socratic.
- **Universal model adapter**: a single OpenAI-compatible adapter manages all models, each with its own credentials and endpoint, with stateless calls for role isolation.
- **LLM judge & metadata extraction**: mode-specific synthesis plus parallel extraction of key insights, topic tags, and confidence.
- **Orchestrator**: parallel fan-out, N-of-M racing (use the fastest N responses), Quality Gate integration, conditional answer-critic/refinement, judge fallback, and graceful degradation when models fail.
- **Streaming pipeline**: progressive, event-driven output with token-by-token judge synthesis.
- **CLI**: `agoracle ask` runs the full pipeline end-to-end, with depth control, a models status command, feedback rating, and a streaming toggle.

---

## [v1.0.0] — 2026-02-11

### Initial release
- Hexagonal architecture with a pipeline core and event-driven side effects.
- Core domain: router, quality gate, event bus, and types.
- Three primary modes (Light, Deep, Research) plus Auto.
- YAML + environment configuration loader.
- CLI entry point and JSON session storage.
- Initial role prompt templates.
