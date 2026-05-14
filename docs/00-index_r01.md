# 00 — Index and Reading Guide

## Purpose

This document set covers C++20/23 service design for deployment to Linux containers. Its through-line is *statelessness* as a deployment property — what makes a service safe to kill, replace, and replicate at the orchestrator's discretion — and how the C++ language features, library choices, and operational patterns either support that property or quietly undermine it.

The treatment is opinionated and reference-style. Where positions are taken, they are marked as `> **Opinion.**` callouts. Where multiple acceptable approaches exist, both are described with their trade-offs. The goal is that an experienced C++ developer or architect can read the set straight through to understand the design space, or jump to a single document to solve a specific problem.

## Audience

The intended reader is a seasoned C++ developer or architect who knows the language well — RAII, move semantics, templates, the standard library — and is now building or operating services that deploy to Podman or Kubernetes. Familiarity with gRPC, OpenTelemetry, and a Grafana-stack (Loki, Tempo, Mimir) observability environment is assumed; the toolchain references are GCC/Clang, Conan, CMake, Ninja, with C++20 as the portable baseline and C++23 features called out where they meaningfully help.

Developers coming from monolith-heavy C++ backgrounds will recognize a recurring theme: instincts that served well in long-running daemons on dedicated hardware — Meyers singletons, file-based logging, static caches, "the process lives forever" — break in subtle ways under containerization. Each document calls out where the instinct misleads and what to use instead.

## Reading order

The documents are numbered for sequential reading. Each builds on the prior; Doc 01 sets vocabulary, Doc 02–03 develop the request-scope discipline, Doc 04–05 develop the process-scope discipline, Doc 06–09 cover specific concerns, Doc 10 integrates everything, Doc 11 is the build-tooling appendix.

For a reader with limited time, the irreducible minimum is Doc 01 (vocabulary), Doc 04 (process-scoped state and the State Architecture Table), and Doc 10 (the integration). These three together cover the architectural shape without the depth on individual mechanisms.

For specific problems, the scenario guide below jumps to the relevant document.

## At a glance

**Doc 01 — Stateless vs stateful as deployment posture.** The vocabulary doc. Statelessness is not a code property; it's a deployment posture that the same C++ binary can or cannot satisfy depending on what state it holds. Establishes the three-scope vocabulary (request-scope, process-scope, deploy-time-scope) used throughout, and the six "monolith intuitions that mislead" under containerization — including the thread pool one that ties to Doc 05.

**Doc 02 — RAII as the foundation for safe stateful work in a stateless service.** The mechanism that makes request scope concrete. RAII binds resource lifetime to C++ scope; the `RequestContext` pattern bundles per-request state into one RAII type. Covers the three exception-safety guarantees, the gRPC callback API as the request boundary, the common mistakes (throwing destructors, raw pointers, locks across `co_await`, manual try/catch cleanup, missing `noexcept` moves), and the performance side of construction and destruction in hot paths.

**Doc 03 — PMR's `monotonic_buffer_resource` as architectural statelessness.** PMR is the in-language realization of "request brings its own memory, all releases together." Covers `monotonic_buffer_resource` mechanics, the request arena RAII pattern, the canonical layered monotonic + `unsynchronized_pool_resource` recipe, the choice of `std::` container types over PMR (`pmr::vector`, `pmr::flat_map`, `pmr::unordered_map`, with the destruction-asymmetry win explained), the lifetime trap counterexample, and C++23 additions (`std::pmr::stacktrace`, `std::flat_map`).

**Doc 04 — Process-scoped state that's still stateless from the orchestrator's view.** Not all state is request-scoped; some legitimately lives for the process lifetime. The State Architecture Table is introduced here. Covers `TracerProvider`, gRPC channels, connection pools, prepared-statement caches, PMR upstream resources, in-process caches, parsed configuration. The `main()`-owned wiring pattern. OS container resource limits as the *budget* for sizing process-scoped state (memory, ephemeral storage; CPU coverage deferred to Doc 05). Bounded vs unbounded structures.

**Doc 05 — Threading and concurrency in a stateless service.** The cross-cutting concern that interacts with statelessness in non-obvious ways. Covers TLS as process-scoped (with the `CorrelationGuard` pattern), stack-based vs stackless concurrency (OS threads, C++20 stackless coroutines, Boost.Fiber stackful coroutines, the TLS-across-`co_await` gotcha), gRPC's threading model and the no-block rule, inter-thread communication and I/O waits. The deep treatment of CPU limits: CFS quota mechanics, throttling tail latency, thread stack memory budget, allocator arena counts under cgroup limits, the cgroup-detection helper, `std::stop_token` cooperative cancellation, the gRPC sync-server `ResourceQuota` configuration trap, PSI sidebar.

**Doc 06 — 12-Factor adapted to C++.** The opinionated document. Most of the twelve factors map directly to C++; three collide: Config (the compile-time/link-time/env-time/runtime split), Processes (Meyers singleton critique and dependency-injection alternative), Disposability (C++ startup tax, the staged-startup pattern, `constinit`). Brief tour of the other factors.

**Doc 07 — State externalization patterns in C++.** What goes outside the process: authoritative session state, counters, durable workflow, queues, business data. Connection pools as process-scoped infrastructure with per-handler RAII checkout. The `ScopedConnection` pattern with `invalidate()` for broken-connection handling. Idempotency keys, deadlines propagating to backing services, retry-with-backoff vs fail-fast. The cache-fix for Doc 01's counterexample. The Outbox pattern for atomic DB-write-plus-event-emission.

**Doc 08 — The ephemeral filesystem trap.** The container filesystem is ephemeral. The overlayfs layer model, `read_only: true` as the forcing function, what goes where (rootfs read-only, `/tmp` tmpfs, persistent volumes for what survives), logs to stdout, the C++ library traps (spdlog file defaults, crash dumps, ML model caches, coverage data, prometheus_cpp textfiles), Kubernetes ephemeral-storage budgets, rootless Podman's UID-mapping story, restart semantics as a feature.

**Doc 09 — Health checks as the public API of statelessness.** The orchestrator's API into the service. The three probes (startup, liveness, readiness) and what each means. The gRPC standard health protocol with the `HealthCheckServiceInterface` C++ API. Separate-port vs same-port trade-offs. The graceful-shutdown sequence that ties together `std::stop_token` (Doc 05), pool draining (Doc 07), and reverse-order destruction (Doc 04). Health-check anti-patterns.

**Doc 10 — Microservices with gRPC and C++.** The capstone integration. A realistic order-pricing service skeleton: proto, `Config`, `RequestContext`, process-scoped state wiring, the full handler with PostgreSQL access and outbound gRPC tax calculation and idempotency, the complete `main()` with twelve cross-referenced steps, the signal handler, full `podman-compose.yaml`, full Kubernetes Deployment and Service manifest. Every prior pattern composed in one place.

**Doc 11 — Build tooling appendix.** Conan 2.x setup with version-pinned dependencies, dev and release profiles (AddressSanitizer in dev, LTO + hardening in release), CMake structure with proto codegen, the C++23 feature support table (GCC 14+ for `<stacktrace>` and `std::pmr::stacktrace`, GCC 15+ for `std::flat_map`), standard-library choice (libstdc++ vs libc++), multi-stage Containerfile pattern, full source for the vendored helpers (`cgroup_helper`, `psi_reader`, `otel_propagator`), test infrastructure, common gotchas.

## The State Architecture Table

Reproduced from Doc 04 for quick reference. The canonical design check: every piece of state belongs in one of these three columns.

| State type | Process-scoped | Request-scoped | External |
|---|:---:|:---:|:---:|
| OpenTelemetry `TracerProvider` | ✓ | — | — |
| OpenTelemetry `Tracer` (per library) | ✓ | — | — |
| Active span / span context | — | ✓ | — |
| gRPC `Channel` cache | ✓ | — | — |
| gRPC `ClientContext` (per outbound RPC) | — | ✓ | — |
| gRPC `CallbackServerContext` (per inbound RPC) | — | ✓ | — |
| Redis / PostgreSQL connection pool | ✓ | — | — |
| Single pooled connection (checked out) | — | ✓ (RAII) | — |
| Prepared-statement cache | ✓ | — | — |
| In-process best-effort cache | ✓ | — | — |
| Parsed configuration | ✓ | — | — |
| PMR upstream resource (heap, pool) | ✓ | — | — |
| PMR `monotonic_buffer_resource` arena | — | ✓ | — |
| Per-request parsed request / response | — | ✓ | — |
| Per-request intermediate computations | — | ✓ | — |
| Authoritative session / user state | — | — | ✓ |
| User-visible counters, rate-limit windows | — | — | ✓ |
| Durable workflow state | — | — | ✓ |
| Queue items between services | — | — | ✓ |
| Authoritative business data | — | — | ✓ |
| `thread_local` storage | ✓ | — | — |
| Thread pool, executor, fiber scheduler | ✓ | — | — |

The bottom two rows on threading state are worth highlighting: `thread_local` looks request-scoped to a developer reading the code, and it is one of the most common silent-bug sources. The `CorrelationGuard` pattern from Doc 05 puts a `thread_local` write inside an RAII type whose scope is the handler, which is the legitimate way to use TLS in a request handler.

## Cross-cutting themes

Eleven themes recur through the document set; they're worth keeping in mind across topics.

1. **Orchestrator state ≠ language state.** Statelessness is decided by deployment configuration (volumes, StatefulSet status, no persistent mounts), not by what the C++ code does internally. The same binary can be deployed both ways.

2. **Podman primary, Kubernetes noted where it differs.** This stack favors Podman for development and Kubernetes for production; the documents lead with Podman/compose syntax and add Kubernetes notes where the semantics or syntax materially differ.

3. **Request-scope vs process-scope is the practical seam.** Every piece of operational state slots into one or the other, and RAII enforces the seam cleanly.

4. **Threading is the third axis of statelessness.** Thread pools and TLS are process-scoped infrastructure; coroutines complicate TLS reads across `co_await`; gRPC's threading model dictates the application architecture.

5. **RAII has a performance side, not just a correctness side.** Constructor/destructor cost, `noexcept` moves, trivial destructibility, PMR destruction-asymmetry — all interact with the request/process scope split.

6. **`std::` containers need explicit choices in a service context.** Default reflexes (`std::map`, `std::unordered_map`) are often suboptimal; `pmr::vector`, `pmr::flat_map`, Abseil hash maps each have specific contexts where they shine.

7. **OS container requests and limits shape C++ behaviour in non-obvious ways.** CPU limits via CFS quotas, memory limits as OOM ceiling, ephemeral-storage as log-volume budget — all affect how process-scoped state must be sized.

8. **"Container" is overloaded vocabulary.** `std::vector` is a container; an OCI/Podman/Docker container is a container; a Kubernetes container is a container inside a pod. Where ambiguity is possible, the documents say "std container" / "OS container" / "Kubernetes container" explicitly.

9. **C++ pays a "globals tax" that 12-factor languages don't.** Static initialization, the SIOF, Meyers singleton synchronization, global ctor pile-up — all affect cold-start latency that orchestrators measure.

10. **gRPC C++ is the through-line.** The canonical transport in this stack, owns several process-scoped state hotspots, defines the standard health protocol, drives the application threading model.

11. **OTel + Grafana stack provides external observability that makes statelessness work.** When OS containers are interchangeable, logs/metrics/traces aggregate externally. The OTel `TracerProvider` is process-scoped (Doc 04); span context is request-scoped (Doc 02); the OTel `Scope` type is the TLS guard pattern (Doc 05) the framework itself uses.

## Glossary

**Request scope.** The lifetime of a single inbound RPC, bounded by the handler's stack frame (or the reactor's lifetime for streaming RPCs). State here lives in `RequestContext` members or PMR arenas; destroyed on handler return. Doc 02, Doc 03.

**Process scope.** The lifetime of the OS container process, bounded by `main()`'s scope. State here is constructed once at startup, lives across many requests, destroyed at process exit. Doc 04.

**Deploy-time scope.** The lifetime of a particular deployment, often longer than any single container instance. Configuration values that vary between dev and production but not between two replicas of the same service live here, in environment variables or config maps. Doc 06.

**External state.** State that lives outside any process — Redis, PostgreSQL, Kafka, S3. Lifetime decoupled from any one container; authoritative for cross-replica consistency. Doc 07.

**OS container.** An OCI-compatible container instance — Podman, Docker, containerd. Has a process tree, a filesystem (with an ephemeral overlayfs writable layer), a network namespace, cgroup-enforced resource limits. The "container" in cloud-native parlance.

**Kubernetes container.** A container inside a pod. Has the OS container's properties plus pod-level networking, volume mounts, and probe configuration. Multiple containers per pod is unusual for application code but common for sidecars (Envoy, otel-collector).

**`std::` container.** A C++ standard-library container: `std::vector`, `std::map`, `std::unordered_map`, `std::pmr::vector`, etc. Not to be confused with OS containers, despite sharing the word.

**Stateless deployment posture.** A deployment configuration under which the orchestrator can kill any replica without correctness loss, because any authoritative state is externally stored and process-scoped state is rebuildable from configuration. Doc 01.

## Reading paths by scenario

**Building a new C++ gRPC service from scratch.** Read Doc 01 (vocabulary), Doc 02 (RAII), Doc 04 (process state + table), Doc 09 (health checks), Doc 10 (integration). Use Doc 11 as the build-tooling reference while setting up the project. Doc 03, 05, 06, 07, 08 can be read as needed when specific concerns surface.

**Migrating a C++ monolith to containers.** Read Doc 01 (especially the monolith-intuitions section), Doc 06 (12-factor adaptation, where most of the migration tension lives), Doc 07 (state externalization, the biggest mechanical refactor), Doc 08 (ephemeral filesystem — the silent bug source in migrations). Doc 04 establishes the State Architecture Table that drives the audit.

**Troubleshooting tail-latency spikes.** Read Doc 05 first (CFS throttling, oversubscription, thread pool sizing). Then Doc 03 (PMR destruction asymmetry, allocator contention). Doc 04's CPU-limits section gives the budget framing.

**Troubleshooting OOM kills or memory bloat.** Read Doc 04 (memory limits as the hard ceiling, the bounded-vs-unbounded audit). Then Doc 05's allocator-arena section (jemalloc/tcmalloc/glibc arena counts under cgroup limits). Then Doc 03 (PMR upstream resource sizing, inline buffer tuning).

**Troubleshooting deploy failures or rolling-restart issues.** Read Doc 09 (probes, especially the liveness-checks-downstream anti-pattern). Doc 06's disposability section covers the C++ startup tax that affects probe timing.

**Troubleshooting "logs disappear after restart" or filesystem-related issues.** Doc 08 directly.

**Adopting C++23 in an existing project.** Doc 11's feature-support table maps requirements to compiler versions. Doc 02 and Doc 03 cover the specific C++23 features used across the doc set (`std::expected`, `<stacktrace>`, `std::pmr::stacktrace`, `std::flat_map`).

**Just need the gRPC patterns.** Doc 10 is the integration; Doc 05 has the threading model and the gRPC server sizing trap; Doc 07 has the deadline-propagation pattern; Doc 09 has the health protocol.

## Out of scope

A few topics the document set deliberately doesn't cover. They're worth flagging so the reader knows the boundary.

**Non-gRPC transports.** HTTP/REST via Drogon, Crow, or restbed; GraphQL via cppgraphqlgen; raw socket protocols. The patterns transfer but the specific API surfaces differ.

**Other backing services.** MongoDB, Cassandra, ScyllaDB, Elasticsearch, ClickHouse. The connection-pool and deadline-propagation patterns from Doc 07 apply; the specific C++ client libraries vary in quality and Conan availability.

**Service mesh integration.** Istio, Linkerd, Cilium. The mesh handles much of what Doc 07's retry-with-backoff and Doc 09's health-check coordination cover at the transport layer; the C++ service's job becomes simpler under a mesh, but the mesh adds operational complexity that's its own topic.

**Authentication and authorization in depth.** mTLS for gRPC, OAuth2/OIDC token validation, RBAC. Doc 10's `InsecureServerCredentials` placeholder is replaced with `SslServerCredentials` in production; the rest of the patterns are independent of auth.

**Multi-region deployment.** Active-active across regions, data replication strategies, cross-region latency budgets. The single-region patterns scale up; the cross-region considerations are an additional layer.

**CI/CD pipelines.** The build artifacts produced by Doc 11's pipeline plug into Tekton, ArgoCD, GitHub Actions, GitLab CI, Jenkins. The build-side concerns are addressed; the deploy-side automation is platform-specific.

**Production debugging tools.** `perf`, eBPF/bpftrace, `gdb` against core dumps, ASan in production. Useful tools, separate topic.

**Database schema migration.** Liquibase, Flyway, hand-rolled. Doc 07's admin-process mention in the 12-factor tour is the only coverage; full migration tooling is its own document.

## Consolidated bibliography

The books referenced across the document set, with the chapters most often cited.

**Iglberger, *C++ Software Design* (Addison-Wesley, 2022).** Chapters on the Single Responsibility Principle, value semantics, dependency injection, strategy pattern, type erasure, and the critique of Singleton. Cited in nearly every document; the strongest single reference for service-architecture decisions in modern C++.

**Yonts, *100 C++ Mistakes and How to Avoid Them* (Manning, 2024).** Catalog of common errors, including throwing destructors, raw `new`/`delete`, missing `noexcept` on moves, exception safety, `thread_local` misuse. Useful as a code-review checklist. Cited in Doc 02, 03, 05, 07.

**Pekka Enberg, *Latency: Reduce Delay in Software Systems* (Pragmatic, 2024).** Framing of tail latency as a first-class property, with chapters on allocator predictability, threading under throttling, and async I/O. Cited in Doc 03, 04, 05, 07.

**"C++ High Performance" 2nd edition (Andrist & Sehr, Packt, 2020).** Chapters on memory layout, PMR, concurrency, coroutines, network programming. The textbook reference for performance-aware modern C++. Cited in nearly every document.

**"Building Low Latency Applications with C++" (Sourav Ghosh, Packt, 2023).** Chapters on the threading model, persistent state, memory layout for long-lived processes. Cited in Doc 03, 05, 08.

**Geewax, *API Design Patterns* (Manning, 2021).** Chapters on idempotency, standard methods, error handling, long-running operations. The reference for cross-cutting concerns that this set operationalizes in C++. Cited in Doc 02, 04, 07, 09, 10.

**Vaughn Vernon, *Implementing Domain-Driven Design* (Addison-Wesley, 2013).** The Outbox pattern is from here. Cited in Doc 07.

**The Twelve-Factor App (`12factor.net`, Heroku, 2011/2017).** The canonical 12-factor statement. Doc 06 is essentially the C++ adaptation; Doc 08 covers Factor XI (Logs) in operational depth; Doc 09 covers Factor IX (Disposability) on the shutdown side.

Online references used across the set:

- gRPC C++ documentation and best practices (`grpc.io/docs/languages/cpp/`)
- OpenTelemetry C++ SDK documentation (`opentelemetry.io/docs/instrumentation/cpp/`)
- Kubernetes documentation on probes, resource management, downward API (`kubernetes.io/docs/`)
- Podman documentation, especially `--read-only`, `--tmpfs`, rootless mode
- Linux kernel documentation on cgroups v2, CFS, PSI (`kernel.org/doc/`)
- cppreference (`en.cppreference.com`) for C++ standard library specifics
- Conan 2 documentation (`docs.conan.io`) and CMake documentation
- redis-plus-plus, libpqxx, librdkafka, spdlog, Abseil — each library's own README and API docs

The bibliography is short by design: the patterns in this document set come from a small number of well-thought-out sources rather than a sprawl of blog posts and Stack Overflow answers. For specific questions not covered, the books above are the strongest starting points.
