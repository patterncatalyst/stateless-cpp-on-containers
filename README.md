# Stateless C++ on Containers

A reference documentation set on C++20/23 service design for deployment to Linux containers, focused on **statelessness as a deployment property** — what makes a service safe to kill, replace, and replicate at the orchestrator's discretion, and how C++ language features, library choices, and operational patterns either support or quietly undermine that property.

This is a companion to the broader *Optimizing C++ on Containers* work; this repository is the statelessness-focused subset.

Twelve documents, ~42,000 words. Reference-style with opinions clearly marked. Code samples in C++20-portable form; C++23 features called out where they meaningfully help.

## Reading the set

Documents are numbered for sequential reading. The full guide and reading paths by scenario live in [`docs/00-index_r01.md`](docs/00-index_r01.md).

| # | Topic | File |
|---|---|---|
| 00 | Index and reading guide | [`00-index_r01.md`](docs/00-index_r01.md) |
| 01 | Stateless vs stateful as deployment posture | [`01-deployment-posture_r01.md`](docs/01-deployment-posture_r01.md) |
| 02 | RAII as the foundation for safe stateful work | [`02-raii_r01.md`](docs/02-raii_r01.md) |
| 03 | PMR's `monotonic_buffer_resource` as architectural statelessness | [`03-pmr_r01.md`](docs/03-pmr_r01.md) |
| 04 | Process-scoped state that's still stateless | [`04-process-scoped-state_r01.md`](docs/04-process-scoped-state_r01.md) |
| 05 | Threading and concurrency in a stateless service | [`05-threading_r01.md`](docs/05-threading_r01.md) |
| 06 | 12-Factor adapted to C++ | [`06-twelve-factor_r01.md`](docs/06-twelve-factor_r01.md) |
| 07 | State externalization patterns | [`07-state-externalization_r01.md`](docs/07-state-externalization_r01.md) |
| 08 | The ephemeral filesystem trap | [`08-ephemeral-filesystem_r01.md`](docs/08-ephemeral-filesystem_r01.md) |
| 09 | Health checks as the public API of statelessness | [`09-health-checks_r01.md`](docs/09-health-checks_r01.md) |
| 10 | Microservices with gRPC and C++ (capstone integration) | [`10-grpc-microservices_r01.md`](docs/10-grpc-microservices_r01.md) |
| 11 | Build tooling appendix | [`11-build-tooling_r01.md`](docs/11-build-tooling_r01.md) |

**If you read only three documents**, read 01 (vocabulary), 04 (the State Architecture Table), and 10 (the integration). They cover the architectural shape without the depth on individual mechanisms.

## Audience

Experienced C++ developers and architects building services for cloud-native deployment. Familiarity with RAII, move semantics, templates, and the standard library is assumed. Familiarity with gRPC, OpenTelemetry, and a Grafana-stack observability environment helps but isn't strictly required.

## Stack assumptions

- **Language**: C++20 portable baseline; C++23 features called out where they help
- **Compiler**: GCC 14+ (preferred) or Clang 18+ with libc++
- **Build**: Conan 2.x, CMake 3.27+, Ninja
- **Runtime**: Linux with cgroups v2
- **Services**: gRPC C++ (callback API), OpenTelemetry C++ SDK
- **Backing services**: PostgreSQL (libpqxx), Redis (redis-plus-plus), Kafka (librdkafka)
- **Orchestration**: Podman + podman-compose (primary), Kubernetes (noted where it materially differs)
- **Observability**: Grafana stack — Loki for logs, Tempo for traces, Mimir for metrics

## Cross-cutting themes

Eleven themes recur across the documents:

1. Orchestrator state ≠ language state — statelessness is a deployment configuration choice
2. Podman primary, Kubernetes noted where it materially differs
3. Request-scope vs process-scope is the practical seam, enforced by RAII
4. Threading is the third axis of statelessness (TLS, coroutines, gRPC threading model)
5. RAII has a performance side, not just a correctness side
6. `std::` containers need explicit choices in a service context
7. OS container requests and limits shape C++ behaviour in non-obvious ways
8. "Container" is overloaded vocabulary — `std::` containers, OS containers, Kubernetes containers
9. C++ pays a "globals tax" that 12-factor languages don't
10. gRPC C++ is the through-line for the example service
11. OTel + Grafana provides external observability that makes statelessness work

## Reference books

The patterns and recommendations draw from a small number of well-thought-out sources. The annotated bibliography in each document calls out the chapters most relevant to that topic. Consolidated list:

- Iglberger, *C++ Software Design* (Addison-Wesley, 2022)
- Yonts, *100 C++ Mistakes and How to Avoid Them* (Manning, 2024)
- Enberg, *Latency: Reduce Delay in Software Systems* (Pragmatic, 2024)
- Andrist & Sehr, *C++ High Performance*, 2nd edition (Packt, 2020)
- Ghosh, *Building Low Latency Applications with C++* (Packt, 2023)
- Geewax, *API Design Patterns* (Manning, 2021)
- Vernon, *Implementing Domain-Driven Design* (Addison-Wesley, 2013) — for the Outbox pattern
- *The Twelve-Factor App* (Heroku, 2011/2017) — `12factor.net`

## Diagrams

Each content document has a paired diagram in [`_diagrams/`](_diagrams/), rendered in two formats:

- `NN-name.svg` — static SVG, renders inline in GitHub
- `NN-name.excalidraw` — Excalidraw JSON, importable into [excalidraw.com](https://excalidraw.com) or the Excalidraw plugin for VS Code/Obsidian for editing

The diagrams use a consistent color vocabulary across all 11 figures:
- **Blue** — process-scoped state
- **Green** — request-scoped state
- **Orange** — external state
- **Red** — anti-patterns and traps
- **Yellow** — accents and highlights

The diagrams are generated from a small Python library (`_diagrams/diagram_lib.py` and `_diagrams/generate.py`). To regenerate after editing:

```bash
cd _diagrams && python3 generate.py
```

This makes it easy to extend the diagram set or tweak the visual style consistently across all figures.

## Versioning convention

Documents use a `_rNN` suffix indicating the revision number. The initial baseline is `_r01`; subsequent revisions bump to `_r02`, `_r03`, etc., with prior revisions retained in git history for diff and rollback.

To revise a document, copy `XX-name_rNN.md` to `XX-name_rNN+1.md`, edit, commit. The most recent file by revision number is the current version.

## Research notes

The working notes that drove the drafting process are preserved in [`research/research-notes_r03.md`](research/research-notes_r03.md). They contain per-document research findings, framing decisions, and the open questions worked through during writing.

## License

License to be added by the repository owner.

## Status

All twelve documents at `_r01` baseline.
