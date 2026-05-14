# Research Notes — Statelessness in C++ Services on Containers

Working notes for the "Optimizing C++ on Containers" project. Organized per planned doc; each section captures core findings, the authoritative sources to lean on, code-pattern anchors for drafting, and open questions before drafting begins.

Convention: book references (Iglberger, Yonts, Enberg "Latency", Geewax "API Design Patterns", "C++ High Performance 2e", "Building Low Latency Apps") are tagged inline as the book is relevant to a finding. They are not all confirmed citations until drafting — the bibliography per doc will check chapter-level.

---

## Cross-cutting themes that surfaced

Eleven observations recurred across topics and should be threaded through the doc set:

1. **The orchestrator's view of "state" is different from the language's view of "state."** Podman/podman-compose (the project's primary stack) and Kubernetes both decide "stateless" by where data lives across restart: named volumes, bind mounts, tmpfs, ephemeral overlay. Kubernetes formalizes the distinction further via Deployment vs StatefulSet. None of that is enforced by C++. A binary with mutable globals, an on-disk cache, and a process-local session table can still be run as a stateless service if those globals don't survive restart in a way clients depend on. This is the single most important framing of the project.

2. **Podman is primary; Kubernetes is noted where it differs.** Examples and `compose.yaml` snippets target podman-compose. Kubernetes differences are flagged inline when they meaningfully change the picture — probes (richer model vs Podman's `HEALTHCHECK`), Deployment vs StatefulSet (no real Podman analogue), ConfigMaps/Secrets (compose `env_file` and bind-mounts are the rough equivalent), and resource requests/limits (Podman uses `--cpus`, `--memory` and compose `deploy.resources`, Kubernetes uses `resources.requests`/`limits`). `podman generate kube` is the bridge worth mentioning when relevant.

3. **Request-scoped vs process-scoped state is the practical seam.** Everything operational in a C++ service slots into one or the other, and RAII is what enforces the seam cleanly. Per-request arenas (PMR), per-request span contexts, per-request DB transactions, per-request gRPC ServerContext — all destruction-on-scope-exit. Process-scoped things — channel caches, tracer providers, allocator pools — must be re-buildable from configuration on every cold start without correctness loss.

4. **Threading is the third axis of statelessness.** Threads, thread pools, executors, and fiber schedulers are process-scoped infrastructure. Thread-local storage is process-scoped — `thread_local` variables persist across requests that share a thread, which is a major source of subtle bugs. Stack-based concurrency (`std::thread`, `std::jthread`, Boost.Fiber) and stackless concurrency (C++20 coroutines) have different interactions with TLS and request scope: a stackless coroutine can resume on a different thread than it suspended on, making TLS-based assumptions unstable across `co_await`. I/O waits couple request scope to the OS scheduler in ways that determine tail latency. gRPC's callback API runs handlers on its own thread pool and forbids blocking, which dictates the application threading model. All of this gets its own treatment in Doc 05; other docs cross-reference.

5. **RAII has a performance side, not just a correctness side.** Constructors and destructors run on every scope entry and exit, and in a hot path that runs billions of times the constants matter. Move semantics, `noexcept`, trivial destructibility, small-buffer optimizations, and allocator awareness all interact with the request/process scope split. A `std::string` constructed in a request handler has a different cost profile than a `std::pmr::string` over an arena. This is the angle the brief asked us to address explicitly and gets its own treatment in Doc 02 with callbacks from 03 and 10.

6. **`std::` containers need explicit choices in a service context.** `vector` vs `deque` vs `flat_*` vs `unordered_*` vs PMR variants vs `std::array`/`std::span` all have different allocation, locality, and lifetime profiles. The choice is rarely defaulted well by reflex — `std::map` and `std::unordered_map` are reached for too often where `std::flat_map` (C++23) or a fixed `std::array` would serve better. Doc 03 covers the PMR cases; Doc 04 covers the process-scoped cases (caches, prepared statements, channel registries) where sizing has to interact with the OS container's memory limit.

7. **OS container requests and limits shape C++ behaviour in non-obvious ways.** CPU limits via cgroup CFS quotas mean `std::thread::hardware_concurrency()` returns the host's CPU count, not the budget you're actually allowed — a classic gotcha that produces oversubscription, throttling, and tail-latency spikes. Memory limits set the OOM threshold, which interacts with PMR upstream sizing, connection pool sizing, in-process cache sizing, and the allocator's arena strategy. Ephemeral-storage limits cap stdout log volume. Doc 04 covers this in depth; Doc 05 develops the threading consequences; other docs cross-reference.

8. **"Container" is overloaded vocabulary.** `std::vector` is a container; an OCI/Podman/Docker container is a container; a Kubernetes container is a container inside a pod. Where ambiguity is possible, the docs say "std container" / "OS container" / "Kubernetes container" explicitly. This is more a writing convention than a finding, but worth establishing once.

9. **C++ pays a "globals tax" that other 12-factor languages don't.** Static initialization order fiasco, global ctor pile-up at startup, Meyers-singleton synchronization on every call — these are real costs that affect cold-start latency under startup probes and that shape how config and dependencies should be wired. This is the gap the brief flagged ("No existing source treats this well"). Doc 06 covers it.

10. **gRPC C++ is the through-line.** It's the canonical microservice transport in this stack, owns several of the process-scoped state hotspots (channels, completion queues, sub-channels), defines the health check protocol Kubernetes uses natively since 1.24 (Podman's `HEALTHCHECK` can invoke `grpc_health_probe` as a command), and its callback API is where modern code lives. Every doc except possibly the ephemeral-filesystem one will touch gRPC at some point.

11. **OTel + Grafana stack provides external observability that lets statelessness work.** When OS containers are interchangeable, logs/metrics/traces must aggregate externally. The OTel C++ SDK pattern (TracerProvider as process-scoped singleton, span context as request-scoped, span propagation often via TLS) maps onto both the request/process state model and the threading-scope discussion in Doc 05.

---

## Doc 01 — Stateless vs stateful as deployment posture

### Thesis to defend

C++ doesn't impose state. Stateless is a property of the orchestrator's expectations and the observable contract with clients, not a property of the binary. The same binary can be run under podman-compose with `read_only: true` and no named volumes (effectively stateless), with named volumes that survive restart (locally stateful), or in Kubernetes as either a Deployment (interchangeable replicas) or a StatefulSet (stable identity, per-pod PVC). The decision lives in the orchestration manifest, not in the code. C++ developers from monolithic backgrounds frequently miss this and write code that "feels stateful" — process-local caches, in-memory session tables, mutable singletons — without realizing this is a deployment decision, not a code decision.

### Key findings

- Podman/podman-compose: posture is implicit, decided by which volumes are named, whether `read_only: true` is set, whether `tmpfs:` is used, and whether `--replicas` is used. No first-class "stateless vs stateful" type.
- Kubernetes formalizes the distinction in two API objects: Deployment (interchangeable pods, no stable identity, no per-pod PVC) and StatefulSet (ordinal identity, stable DNS `<name>-0`/`<name>-1`, per-pod PVC bound by ordinal). Both run the same OCI image; the manifest decides posture.
- "Stateless" in cloud-native parlance means request-handling that doesn't depend on prior in-process state. It does NOT mean "no state anywhere" — backing stores are universal. Canonical reference: 12-factor "Processes" factor (`12factor.net/processes`).
- The pattern most worth citing is "behind every scalable stateless service is a stateful one" (Kubernetes Patterns, ch. 11).
- `podman generate kube` translates a running pod/container set into Kubernetes YAML, which is a useful bridge for moving from local dev to production.

### Code-pattern anchors

- A small gRPC service in C++20 whose handler is a pure function of its input. Same binary, three orchestration manifests:
  - `compose.yaml` for podman-compose (primary).
  - `deployment.yaml` for Kubernetes (production scale-out).
  - `statefulset.yaml` for Kubernetes (where ordinal identity matters).
- A counterexample handler with a process-local `unordered_map` cache that silently breaks under multi-replica deployment.
- Forward references to: Doc 02 for ctor/dtor performance on request-scope objects; Doc 04 for the impact of OS container resource limits on process-scoped state sizing.

### Book references

- Iglberger ch. on dependency management and SRP — the idea that "who owns this state" is a design choice, not an accident.
- Geewax "API Design Patterns" — stateless API surface as a design contract.
- "C++ High Performance 2e" — chapters on memory and design, the costs of process-local data.

### Open questions / drafting notes

- Should the doc spend any time on hybrid postures (mostly stateless, with leader-elected master)? **Recommend brief mention, no deep treatment.**
- Recommend introducing the request-scope/process-scope/deploy-time-scope vocabulary here so subsequent docs can use it without redefining.
- Recommend introducing the "container" overload (std vs OS vs Kubernetes) here, briefly, to clear writing ambiguity for the rest of the set.

---

## Doc 02 — RAII as the foundation for safe stateful work in a stateless service

### Thesis

A "stateless" service still has plenty of state inside a single request — DB transactions, per-request arenas, gRPC server context, span context, locks. RAII is what makes that state safe: scope-bound construction and destruction guarantees that none of it leaks out of the request boundary or persists across pod restarts. The discipline is to map state to scope, and to make scope match the conceptual boundary (request, RPC, transaction, span).

### Key findings

- RAII is the C++ Standard's three-level exception safety story applied to resources: basic guarantee (no leak, valid state), strong guarantee (transactional), and nothrow (destructors). Standard reference: any C++ idiom guide; book-level: Iglberger ch. on resource management, Yonts mistake #s relating to ownership, "C++ High Performance 2e" RAII chapter.
- The "request scope" abstraction is implicit in every modern C++ web/RPC framework but rarely named: a server context object holds per-request state, destroyed when the handler returns or the reactor's `Finish` is invoked.
- gRPC's `ServerContext`/`CallbackServerContext` is the canonical per-request RAII container in this stack. Its lifetime defines the request scope.
- Common mistakes (per Yonts-style "100 Mistakes"): destructors that throw, resources held by raw pointer across exception boundaries, lock ordering issues across scopes, locks held across `co_await`.

### Performance angle (RAII cost in hot paths)

This is the angle the brief asked for explicitly. RAII is correctness machinery, but in a hot path running billions of times, the constants of construction and destruction matter.

- **Constructor cost.** Each per-request RAII object pays its constructor on entry. Heap allocations inside that ctor (e.g., `std::string` over default allocator, `std::vector` reserving on the heap) dominate. Mitigations: SBO-friendly types (small `std::string` instances stay on the stack on most libstdc++/libc++ implementations up to ~15-22 bytes), `reserve()` with a known cap to avoid mid-handler reallocation, PMR allocators backed by a per-request arena (Doc 03), and `std::array`/`std::span` where the size is fixed.
- **Destructor cost.** Destructors run in reverse order at scope exit. A `std::unordered_map` with 100k entries destroys all of them individually; a PMR-backed map over a `monotonic_buffer_resource` is reclaimed by one arena `release()` regardless of entry count. The cost asymmetry is a major argument for PMR in handlers that build large transient structures.
- **`noexcept` and exception paths.** Destructors should be `noexcept` (the default) and not throw. Marking move operations `noexcept` lets `std::vector` use moves instead of copies during reallocation — a frequent invisible cost. Yonts covers the common mistakes; the practical rule is `noexcept` everything in the move and destruction path.
- **Trivial destructibility.** Types that are trivially destructible cost nothing to destroy. Aggregates of trivial types (POD-style `struct`s) get this for free; classes with `std::string` members or `std::vector` members lose it. For per-request scratch structures, prefer designs that stay trivially destructible where possible.
- **Allocator awareness vs allocation churn.** Default-allocator `std::string` and `std::vector` in a handler hit the global heap, which means: lock contention in glibc malloc, fragmentation across thread arenas, and unpredictable tail latency. Allocator-aware variants (PMR or custom) move the allocation budget into a place you can reason about per request.
- **Move semantics across scope boundaries.** When a per-request object needs to outlive its construction scope (e.g., handed to an async continuation), move is correct; copy is rarely correct. `noexcept` moves prevent silent copy fallback in standard containers.

### Code-pattern anchors

- A `RequestContext` class holding: per-request PMR arena, span, deadline, correlation ID, scoped DB transaction handle. All destruction-by-scope.
- Contrast: a counterexample that holds a `static` cache `unordered_map` for "performance," which then survives across requests and breaks the stateless contract when the cache returns stale data after a rolling update of an upstream.
- Show RAII-guarded `gRPC ClientContext` with deadline propagation from incoming `ServerContext`.
- A micro-benchmark sketch: `std::string` ctor cost over default allocator vs PMR arena, illustrating constants.

### Book references

- Iglberger: chapters on ownership, the value-semantics chapter, the chapter on dependency injection. All directly relevant.
- Yonts: mistakes on raw pointers, dangling references, lifetime confusion, and exception-path resource handling.
- "C++ High Performance 2e": RAII deep-dive chapter; also the chapters on move semantics and allocator awareness.
- Enberg, *Latency*: directly applicable to the destructor-cost-asymmetry argument.

### Open questions

- How much should this doc anticipate Doc 03 (PMR)? **Recommend it teases the concept, says "we'll see one specific RAII container — the PMR arena — in detail next."**
- Should the performance section live in 02 or be a sidebar in 03? **Recommend 02 — RAII is the conceptual home; 03 is the specific PMR realization.**

---

## Doc 03 — PMR's `monotonic_buffer_resource` as architectural statelessness

### Thesis

`std::pmr::monotonic_buffer_resource` is the in-language realization of "request brings its own arena, all releases together, no persistent state crosses boundaries." Construct an arena at request entry, allocate everything (containers, strings, parsed proto fields, intermediate computations) from it via `std::pmr::polymorphic_allocator`, destroy at request exit. No deallocation overhead in the hot path because deallocation is a no-op until destruction. The arena IS the statelessness boundary — when it dies, every request-scoped allocation is reclaimed in one bump-pointer reset.

### Key findings

- `monotonic_buffer_resource`: bump-allocator semantics; `do_deallocate` is a no-op; release-all on destruction or `release()` call. Can be initialized with a stack/static buffer for zero heap on the happy path; falls through to upstream resource only when buffer exhausted. (cppreference, libstdc++ docs, badlydrawnrod.github.io)
- Common combo: monotonic on the bottom, `unsynchronized_pool_resource` layered on top to give per-size-class reuse within a single request — pool's freelist sits on top of monotonic's giant slab. (Software Architecture with C++, packt; that's the canonical recipe)
- `std::pmr::null_memory_resource()` as upstream gives you "stack-only, never heap" arenas — useful for hard latency caps where heap allocation is unacceptable.
- C++23 added `std::pmr::stacktrace` (a PMR-allocated stacktrace type), which means even fault-handling can stay arena-local.
- Pitfalls (runebook.dev, MC++ blog): forgetting that individual `deallocate` is a no-op (so `std::pmr::vector` shrinking doesn't reclaim), falling off the buffer into upstream (negating perf wins), and the lifetime trap (arena outlives nothing, so capture-by-reference of arena-allocated objects into a longer-lived structure is UB).

### Code-pattern anchors

- A `RequestArena` RAII class wrapping a stack-allocated `std::array<std::byte, N>` buffer and a `monotonic_buffer_resource`; passed to all per-request containers.
- A `pmr_string`, `pmr_vector<pmr_string>` example showing parse → process → respond with zero heap on the happy path.
- A two-level resource: monotonic + unsynchronized_pool layered above for size-class reuse within the request.
- A counterexample showing what breaks when you keep a non-PMR `std::string` reference from an arena-allocated buffer and read it after request exit (UB).

### Book references

- Iglberger: ch. on allocators / value semantics.
- "C++ High Performance 2e": memory chapter explicitly covers PMR.
- Enberg "Latency": this is where PMR shines for tail latency. Worth citing.
- "Building Low Latency Apps in C++": allocator chapter.

### Std-container choices in PMR context

PMR is the gateway to making `std::` container choices that actually serve a request handler well.

- `std::pmr::vector<T>` over arena: bump-allocated on push_back until the arena is exhausted; geometric reallocation moves still happen, but the freed blocks stay in the arena (no-op deallocate). Reserve to the expected size on construction to avoid reallocations entirely.
- `std::pmr::string`: small-string optimization still applies on most implementations; only longer strings hit the arena.
- `std::pmr::unordered_map`: each node allocates from the arena; lookup/insert cost is unchanged, but destruction is one arena release instead of N node destructors. Big win when handlers build large transient maps.
- `std::pmr::flat_map` (C++23 in `<flat_map>`): backed by a single `std::pmr::vector`, much better cache behaviour than `unordered_map` for small N (< ~64). Worth mentioning in the doc as a modern alternative.
- `std::array` / `std::span` for fixed-size scratch: zero allocation, trivially destructible, perfect for known-size intermediate results.
- `std::pmr::deque`, `std::pmr::list`: rarely the right choice in handlers; mention only as anti-recommendation.

The "decision tree" for a per-request container in a handler is the kind of thing the doc should leave the reader with.

### Open questions

- Should this doc include benchmark numbers (cppreference shows ~3× on `list<int>` push-back)? **Yes — but reproduced with disclaimers; benchmark methodology matters.**
- Should the std-container decision tree be its own section or fold into the code-pattern anchors? **Recommend its own section — it's a take-away.**

---

## Doc 04 — Process-scoped state that's still stateless from the orchestrator's view

### Thesis

Not all state is request-scoped. There is legitimate process-scoped state — `TracerProvider`, gRPC channels and sub-channels, connection pools, PMR upstream resources, JIT-compiled regex caches, prepared statement caches — that exists for the lifetime of the process and rebuilds from configuration on cold start. This state is "expensive to rebuild" but not "must be replicated." Confusing the two is one of the biggest architectural mistakes a monolith-background C++ dev makes when moving to containers.

### Key findings

- **OTel C++ TracerProvider is the canonical process-scoped singleton.** Pattern: create once in `main()` before any tracer-using code, set as global via `opentelemetry::trace::Provider::SetTracerProvider`, clear at shutdown for clean ForceFlush. The lifecycle is process-scoped and explicit — neither a Meyers singleton nor a global, but a structured `main()`-owned object. (opentelemetry.io/docs/languages/cpp, signoz.io)
- **gRPC channels must be reused, not recreated.** `Channel` is heavy (TCP/HTTP2 setup, TLS handshake, name resolution). Best practice: one channel per (target, options) tuple per process, cached for the process lifetime. Stub creation off a channel is cheap. (grpc.io performance docs)
- **Channel pools for high-load paths.** Single channel ≈ single HTTP/2 connection ≈ stream cap (typically 100 concurrent streams). Heavy load needs a pool of channels distinguished by a dummy channel arg. (grpc.io performance, also Microsoft docs)
- **Connection pools to backing services** (Redis, Postgres) are process-scoped. `redis-plus-plus` has a connection pool API; libpqxx requires user-written pooling (multiple OSS examples: pq-richquery, pqxx_pool). Pool size, idle policy, and validation queries are config concerns.
- **Allocator state itself** (PMR upstream resources, jemalloc tcache, glibc malloc arenas) is process-scoped and important to size/configure for container memory limits.

### The crucial distinction

| State type | Process-scoped | Request-scoped |
|---|---|---|
| TracerProvider | ✓ | — |
| Span / span context | — | ✓ |
| gRPC Channel | ✓ | — |
| gRPC ServerContext / ClientContext | — | ✓ |
| Redis/Postgres connection pool | ✓ | — |
| Single pooled connection (checked out) | — | ✓ (RAII) |
| Prepared statement cache | ✓ | — |
| PMR upstream (heap) | ✓ | — |
| PMR `monotonic_buffer_resource` arena | — | ✓ |

This table should anchor the whole doc — it's the most useful single artifact for someone trying to think about state architecture.

### OS container requests and limits — the budget for process-scoped state

This subsection is the one the user explicitly asked for. Every piece of process-scoped state has to fit inside an OS container's resource budget; that budget is set by the runtime (Podman flags or compose `deploy.resources`, Kubernetes `resources.requests`/`limits`). Several C++-specific gotchas:

- **CPU limits and `std::thread::hardware_concurrency()`.** This is the headline gotcha. On Linux, `hardware_concurrency()` typically returns the host CPU count, not the cgroup quota. A container with `--cpus=1` running gRPC's default thread-pool sizing (which uses `hardware_concurrency()` internally) will oversubscribe and pay CFS throttling penalties. Mitigations: read `/sys/fs/cgroup/cpu.max` (cgroup v2) or pass an explicit pool size via env var. Same gotcha applies to jemalloc/tcmalloc arena counts.
- **Memory limits and the OOM killer.** Limit set via `--memory` or `mem_limit` in compose, or `resources.limits.memory` in k8s. C++ allocators don't know about this; once the limit is hit, the OOM killer terminates the process. Mitigations: size in-process caches conservatively, set PMR upstream sizing as a fraction of the memory limit, configure jemalloc/tcmalloc with explicit caps if used, monitor RSS via OTel.
- **CPU requests vs limits.** Requests reserve scheduling capacity; limits cap consumption. Setting requests but not limits ("burstable") is the common pattern for stateless services. Setting limits == requests ("guaranteed" QoS in k8s) is right for latency-sensitive paths.
- **`GOMEMLIMIT`-style soft limits.** Go has runtime-level memory limit awareness; C++ does not. There is no language-level equivalent. Mitigations: explicit allocator caps; cgroup-aware sizing logic in `main()`.
- **Ephemeral-storage limits** cap writable-layer + emptyDir + container logs (Doc 08 covers this). A C++ service that writes verbose logs to stdout can blow the ephemeral-storage limit; mitigation is log-rate sampling or structured log levels.
- **Podman vs Kubernetes specifics.** Podman: `--cpus`, `--memory`, `--memory-swap`, `--pids-limit` on the CLI; compose: `deploy.resources.limits` and `deploy.resources.reservations`. Kubernetes: `resources.requests` and `resources.limits` per container, plus pod-level `ephemeral-storage`. Semantics are mostly the same; the manifest syntax differs.

### Sizing std containers against the limit

Process-scoped `std::` containers (LRU caches, channel maps, prepared-statement caches) must be sized as a fraction of the memory budget. Practical rules:

- Use `reserve()` aggressively on `vector`/`unordered_map` at construction so growth doesn't surprise you.
- Bound any unbounded structure. An `unordered_map<UserId, SessionInfo>` that grows without eviction is an OOM crash waiting for one bad day's traffic.
- Choose container types deliberately: `std::flat_map` (C++23) for small N (< ~64) with rare insertion; `std::unordered_map` for larger N; consider Abseil's `flat_hash_map` for pointer-stable iteration with better density (mention as third-party option).
- For caches: prefer libraries with explicit memory budgets (e.g., a fixed-size ring or a third-party LRU with a byte budget) over `unordered_map` plus ad-hoc eviction.

### Book references

- Iglberger: ch. on Strategy/dependency injection — applies to wiring process-scoped collaborators.
- Geewax: chapters on resource lifecycle and idempotency in API design.
- "C++ High Performance 2e": connection management chapter; also the chapter on cache-friendly data structures applies directly to the sizing discussion.
- Enberg, *Latency*: the cost of unbounded structures and the value of explicit sizing.

### Open questions

- The cold-start cost of rebuilding all this process-scoped state is what makes startup probes vs liveness probes important (Doc 09 will reference back). **Recommend forward-link.**
- The CPU-limit gotcha may deserve a short standalone code snippet showing cgroup v2 detection in C++. **Recommend yes — it's the most concrete "trap" in this doc.**

---

## Doc 05 — Threading and concurrency in a stateless service

### Thesis

Threading is the dimension of statelessness that catches C++ developers off-guard. Threads and their associated infrastructure — pools, executors, fiber schedulers — are all process-scoped state. Thread-local storage looks request-scope but is not. Stack-based and stackless concurrency primitives have different interactions with TLS, RAII, and request scope. I/O waits couple request scope to the OS scheduler in ways that determine tail latency. Doing concurrency correctly in a stateless service requires understanding which concurrency primitives carry which state across what scope, and arranging for that state to be either cleared at request boundaries or kept legitimately process-scoped.

### Key findings

**TLS is process-scoped, not request-scoped.**

- `thread_local` variables persist across requests handled by the same thread in a pool. Classic bug: set a `thread_local` correlation ID in handler A, forget to clear it, handler B inherits the stale value.
- Mitigation: RAII guards (set-on-construct, clear-on-destruct), or avoid TLS for request data and use a request-scoped context object (Doc 02).
- The OpenTelemetry C++ SDK uses TLS for active-span context internally, which is correct for the framework. User code interacting with it must respect scope boundaries — losing a span over a `co_await` is a known source of broken trace propagation.

**Stack-based vs stackless concurrency.**

- OS threads (`std::thread`, `std::jthread`): own stack (~1–8MB default), kernel-scheduled. Pool them; never create per request.
- Stackless coroutines (C++20 `co_await`): compiler-generated state machine, frame on heap or arena. Can resume on a different thread than they started. TLS reads before/after `co_await` may differ — this is the most-bitten gotcha.
- Stackful coroutines / fibers (Boost.Fiber, Boost.Context): own small stack (~64–256KB), cooperative scheduling in user space. More expressive than stackless; can be pinned to a thread or migrated.
- C++26 stackful coroutine support (P0876) explicitly prohibits cross-thread migration — the TLS migration problem is the named reason.

**gRPC C++ threading model.**

- Callback API: handlers run on gRPC-owned thread pool. Must not block. Long work is dispatched to a separate executor (Asio thread pool, custom executor, fiber scheduler).
- Sync API blocks a gRPC thread per RPC — fine for low-concurrency services, fatal for high-fan-in.
- asio-grpc adapts gRPC's `CompletionQueue` to Boost.Asio, enabling `co_await` over gRPC operations and a uniform executor model.
- This dictates the application architecture: gRPC threads handle I/O, application threads handle compute, the boundary between them is a queue or an executor.

**Waiting on I/O.**

- Sync I/O blocks the calling thread → starves a gRPC handler pool → tail latency spike for all in-flight requests.
- Async I/O: epoll (libuv, Asio), io_uring (Linux 5.1+, increasing traction), or library executors.
- File I/O is harder than network I/O — `std::filesystem` is sync; portable async file I/O really requires io_uring or per-OS APIs. For C++ services that touch local files (config reload, log rotation), this matters less; for services that stream large files, design carefully.
- C++26 `std::execution` (senders/receivers, P2300) will eventually be the standard answer; not yet portable in GCC/Clang 14/15.

**Inter-thread communication.**

- Mutex + condition variable: classical, well-understood, process-scoped state. RAII via `std::lock_guard`, `std::unique_lock`, `std::scoped_lock`. Locks held across `co_await` are a known footgun — the coroutine may suspend with the lock held and resume on a different thread.
- Lock-free queues (Boost.lockfree, moodycamel::ConcurrentQueue): higher throughput, harder to reason about, no `std::` equivalent yet.
- Channels: no `std::channel`. Various third-party implementations (rigtorp::SPSCQueue, etc.). Natural fit for producer/consumer between gRPC threads and worker threads.
- Atomics: for trivial shared counters (request count, metrics); not a general substitute for locks.

**OS container CPU limits and thread budget — the deep version.**

The Linux CFS scheduler enforces CPU limits via a quota/period mechanism. A container with `--cpus=2` (Podman) or `resources.limits.cpu: "2"` (Kubernetes) is given a quota of 200ms per 100ms period — meaning it can consume 200ms of aggregate CPU time before being throttled until the next period boundary. Thread parallelism does not beat this budget: eight threads each running 25ms in a 100ms period have already consumed the 200ms quota and the kernel suspends all of them until the next period boundary, which can be up to ~75ms away for whichever thread happened to be scheduled last.

The consequences for C++ service design are concrete:

- **The thread pool size is not the host's CPU count.** `std::thread::hardware_concurrency()` lies under cgroup limits. Same gotcha for any library that probes at startup: gRPC's default sync server thread pool, jemalloc/tcmalloc/mimalloc arena counts, OpenMP parallel regions, Intel TBB task arenas. All of them need explicit sizing from the cgroup limit, not the host probe.
- **Oversubscription is worse than undersubscription.** A pool of 16 threads on `--cpus=2` does not get 16× the work done. It competes for the 2 cores' worth of quota, adds context-switch overhead, pollutes the cache, and amplifies throttling pauses across more threads. A pool of 2 threads doing the same work is typically faster.
- **CPU-bound vs I/O-bound rules.** For CPU-bound work, target `N = floor(CPU_LIMIT)` (in cores). For I/O-bound work that blocks, `N` can be larger but is bounded by memory (thread stack cost) and by contention overhead. The right answer for mixed workloads is usually to split into two pools — a small CPU pool sized to the limit, plus a larger I/O pool — or better, async I/O on the CPU pool's threads via coroutines, eliminating the I/O pool entirely.

**CFS throttling and tail latency.**

When a container hits its CPU quota mid-period, the kernel throttles all of the container's threads until the next period boundary. With the default 100ms period, the worst-case pause is just under 100ms — a tail-latency disaster for any service whose SLO is "P99 < 50ms". Symptoms in monitoring: P99 spikes that do not correlate with request volume or backend issues, but do correlate with CPU usage approaching the limit.

Mitigations:

- Oversize the CPU limit relative to mean utilization. Run at 60-70% mean utilization, leaving headroom for bursts.
- Under Kubernetes, consider the "burstable" QoS class (set `resources.requests` but not `resources.limits.cpu`) for latency-sensitive paths. The pod gets scheduling priority via requests but can burst up to whatever the node has available. The trade-off is unbounded blast radius if the service goes runaway — usually worth it for user-facing services, often not for batch.
- Reduce the CFS period (`cpu.cfs_period_us` set to e.g. 10ms instead of 100ms) to shrink worst-case throttling pauses at the cost of more scheduler overhead. Less commonly available in managed Kubernetes; useful in self-managed environments.
- Tune `kernel.sched_cfs_bandwidth_slice_us` to alter how quota is distributed across CPUs, but this is rarely a clean win.

The default advice is "more headroom." Running close to the limit is usually the actual problem.

**Memory limits and thread stack budget.**

Each `std::thread` defaults to a stack of 1MB on glibc, up to 8MB on some platforms. The stack is reserved as virtual memory but pages are only committed as used, so the impact on resident set size is smaller than the address-space reservation suggests. Even so, under tight memory limits (`--memory=256M` on Podman, `resources.limits.memory: "256Mi"` on Kubernetes), a 100-thread pool can commit hundreds of MB of address space, which interacts badly with gRPC's send/receive buffers, connection pools, the PMR upstream resource, and any in-process caches.

Mitigations:

- Smaller per-thread stacks via `pthread_attr_setstacksize` for application pools where the call depth is known and bounded. 128KB is often plenty for handler code that doesn't recurse deeply.
- Fewer threads. Async I/O via C++20 coroutines reduces the thread count dramatically — one thread per CPU core plus a few I/O threads can replace dozens of blocking threads.
- Fibers (Boost.Fiber) with smaller default stacks (~64-256KB) as a middle ground for codebases not yet ready for coroutines.

**Allocator awareness of container limits.**

Modern allocators (jemalloc, tcmalloc, mimalloc) create per-CPU arenas at init to reduce lock contention. Default sizing uses `sysconf(_SC_NPROCESSORS_ONLN)` or similar, which returns the host count. On a 64-core host with `--cpus=2`, the default creates 64 arenas that mostly sit idle while consuming address space and resident memory. Explicit configuration:

- **jemalloc**: `MALLOC_CONF=narenas:4` or `MALLOC_CONF=narenas:auto` (newer versions read cgroup limits).
- **tcmalloc**: build-time configuration; the gperftools fork reads `TCMALLOC_NUM_THREADS_PER_CPU`.
- **mimalloc**: `MIMALLOC_LIMIT_OS_ALLOC` and `MIMALLOC_RESERVE_HUGE_OS_PAGES` interact with container memory limits.
- **glibc malloc**: `MALLOC_ARENA_MAX=4` is the blunt instrument. Default is `8 * NPROCS`, which is catastrophic on a 64-core host with a 2-core container.

A general rule: set arena counts to `2 * CPU_LIMIT` for most workloads, then tune from there.

**Pressure Stall Information (PSI) for adaptive sizing.**

The Linux kernel exposes pressure metrics at `/proc/pressure/cpu`, `/proc/pressure/memory`, `/proc/pressure/io` (system-wide) and in cgroup-scoped form under `/sys/fs/cgroup/{path}/cpu.pressure` etc. (cgroup v2). Each reports the fraction of time tasks were stalled waiting for the resource over 10s/60s/300s windows. A service that monitors its own PSI can shed load, reduce concurrency, or back off when pressure rises — strictly better than waiting for OOM kill or throttle-induced latency spikes.

Most services don't need this. For high-fan-in services running near their budget, PSI-aware backpressure is the difference between graceful degradation and cliff-edge failure.

**Detection in C++ — code sketch.**

A minimal cgroup v2 CPU limit reader:

```cpp
#include <fstream>
#include <optional>
#include <string>

// Returns the CPU limit in cores, or std::nullopt if running unconstrained.
std::optional<double> cgroup_v2_cpu_limit() {
    std::ifstream f{"/sys/fs/cgroup/cpu.max"};
    if (!f.is_open()) return std::nullopt;
    std::string max_str;
    long period{};
    f >> max_str >> period;
    if (max_str == "max" || period <= 0) return std::nullopt;
    return std::stol(max_str) / static_cast<double>(period);
}
```

A production-grade version handles cgroup v1 (`cpu.cfs_quota_us` / `cpu.cfs_period_us`), detects whether we're actually in a container (`/proc/1/cgroup` inspection), falls back to `sched_getaffinity()` when cgroup paths are unavailable, and treats a CPU limit smaller than 1.0 as a special case (round up to 1 thread minimum). Doc 11 covers vendoring the full helper.

The natural place to call this is in `main()` before any thread pool is constructed:

```cpp
int main() {
    const double cpu_budget = cgroup_v2_cpu_limit().value_or(
        static_cast<double>(std::thread::hardware_concurrency()));
    const std::size_t cpu_pool = std::max<std::size_t>(1,
        static_cast<std::size_t>(std::floor(cpu_budget)));
    const std::size_t io_pool  = std::max<std::size_t>(2, 2 * cpu_pool);
    // Wire cpu_pool into gRPC SyncServerOption, application worker pool, etc.
    // Wire io_pool into the Asio io_context thread count if using asio-grpc.
}
```

**Trap: gRPC's default sync server thread pool.**

gRPC C++'s synchronous server uses an internal thread pool sized via `ResourceQuota` and the `MaxThreads` setting in `ServerBuilder`. If unset, it scales freely up to internal defaults that don't respect cgroup limits. For production, set explicit limits:

```cpp
grpc::ResourceQuota quota{"server_quota"};
quota.SetMaxThreads(cpu_pool * 4);  // headroom for I/O blocking
grpc::ServerBuilder builder;
builder.SetResourceQuota(quota);
builder.SetSyncServerOption(
    grpc::ServerBuilder::NUM_CQS, cpu_pool);
builder.SetSyncServerOption(
    grpc::ServerBuilder::MIN_POLLERS, cpu_pool);
builder.SetSyncServerOption(
    grpc::ServerBuilder::MAX_POLLERS, cpu_pool * 2);
```

Callback API services have their own internal pool but inherit the `ResourceQuota`; same sizing principle applies.

**Forward and back references.** Doc 04 covers the same limits from the resource-budget angle (memory for caches, CPU for SLO planning). Doc 10 (gRPC microservices) shows the wiring code in context.

**Cooperative cancellation.**

- `std::stop_token` (C++20) and `std::jthread` give portable cancellation.
- Pattern: incoming deadline → request scope holds a `std::stop_source` → handler passes `stop_token` to async work → deadline expiry signals stop → workers unwind via RAII.
- Composes with gRPC's `ServerContext::IsCancelled()` and the standard deadline-propagation idiom.

**Boost.Fiber as middle ground.**

- Cooperative scheduling, smaller memory footprint than OS threads but larger than stackless coroutines.
- Useful for code that wants synchronous-looking I/O without rewriting the whole codebase to coroutines.
- Statelessness implication: fibers multiplex onto OS threads, so they inherit the OS thread's TLS. Same scope-discipline applies.
- Trade-off vs C++20 coroutines: fibers are easier to introduce incrementally; coroutines integrate more cleanly with modern executor models and don't carry the stack memory cost.

### Code-pattern anchors

- A request-scoped logging context using RAII to set/clear TLS — the "TLS guard" pattern.
- A gRPC handler that dispatches CPU work to a sized thread pool and awaits via `stop_token` + `std::future`.
- A coroutine handler over asio-grpc demonstrating the TLS-resume gotcha and how to avoid it: capture context explicitly into the coroutine frame rather than reading TLS after `co_await`.
- A cgroup v2 CPU-limit reader plus pool sizing in `main()`, supporting cgroup v1 fallback and `sched_getaffinity()` as a last resort.
- A `ServerBuilder` configuration showing explicit `ResourceQuota::SetMaxThreads`, `NUM_CQS`, `MIN_POLLERS`, `MAX_POLLERS` sized from the CPU budget.
- An allocator-config example (`MALLOC_CONF=narenas:N` for jemalloc, `MALLOC_ARENA_MAX=N` for glibc) wired from the same CPU budget.
- A demonstration of CFS throttling: a benchmark that runs near the CPU limit and shows P99 latency spikes when the pool is oversized vs properly sized.
- A graceful-shutdown sequence: `stop_source.request_stop()` → all workers unwind → pool joins → process exits cleanly.
- A producer/consumer between gRPC handler threads and a worker pool over a lock-free queue.
- A PSI reader for `/sys/fs/cgroup/{cpu,memory,io}.pressure` driving adaptive concurrency (sidebar example for advanced users).

### Book references

- "C++ High Performance 2e": concurrency, coroutine, and executor chapters — directly relevant.
- "Building Low Latency Apps in C++": threading model and I/O wait modes.
- Enberg, *Latency*: I/O wait modes, tail-latency consequences of blocking.
- Iglberger: dependency injection chapter for wiring executors and pools; the chapter on strategy for swapping concurrency models.
- Yonts: mistakes around `thread_local`, locks-across-suspension, and shared mutable state.

### Open questions

- asio-grpc vs raw callback API depth? **Default plan: cover both briefly. asio-grpc is the modern recommendation; the raw callback API is what the gRPC docs ship with.**
- Coverage of `std::execution` (senders/receivers, P2300, C++26)? **Default plan: mention as the future direction, don't deep-dive — too unstable in current compilers.**
- Should this doc include an io_uring example for file I/O? **Default plan: brief mention, link to userspace libraries (liburing, Asio's io_uring backend). Full coverage is a separate topic.**
- Boost.Fiber vs C++20 coroutines as the recommended default? **Default plan: present both with their trade-offs; lean coroutines as the strategic direction, fibers as the migration-friendly option for older codebases.**
- Depth of PSI coverage? **Default plan: introduce in a sidebar with a code sketch; flag as advanced and not necessary for most services.**
- Whether to include a benchmark demonstrating CFS-throttling tail-latency spikes? **Default plan: yes — it's the most concrete way to show why pool sizing matters. Reproduce with caveats about benchmark methodology.**

---

## Doc 06 — 12-Factor adapted to C++

### Thesis

The user's brief flagged this as "no existing source treats it well." The 12-factor methodology was written by Heroku engineers for languages with REPLs, runtime config, and no compile-time/link-time distinction. Three principles collide hard with C++ realities:

1. **Config (Factor III): "Store config in env vars."** Maps cleanly in dynamic languages: read env at startup, use it. In C++: env reads are easy (`std::getenv`), but the deeper question is *compile-time vs runtime config*. Templates, `if constexpr`, `consteval`, link-time selection of allocator/exception model — these are config that's baked at build time, not env-time. The 12-factor canon doesn't have a vocabulary for this. The right framing: 12-factor's "config" means *deploy-varying* config, and the C++ design choice is which slice of config lives where (compile-time, link-time, env-time, runtime-fetched from a config service).
2. **Processes (Factor VI) + the singleton problem.** Meyers singleton is thread-safe-init since C++11, but: (a) every call path that hits `getInstance()` pays an atomic flag check after init, (b) the instance lives until process exit, (c) it's notoriously hard to test, (d) it makes dependency wiring implicit. 12-factor "stateless processes" plays badly with codebases full of singletons. The modern alternative is explicit dependency injection — pass dependencies as constructor args from `main()`. Iglberger covers this in the DI chapter.
3. **Disposability (Factor IX): "Fast startup, graceful shutdown."** C++ has a startup tax other languages don't: static initialization (especially the SIOF), library-level constructors in shared libs, dynamic loader work for many `.so` deps. Each global ctor that calls into `malloc` or opens a file or contacts a server adds to cold-start latency, which Kubernetes measures via `startupProbe` and which affects the SLO around rolling restarts. The remedy is the same as the singleton remedy: prefer explicit `main()`-owned objects over file-scope statics; use `constinit` for things that genuinely need to be globally available before `main`.

### Key findings

- 12-factor canonical text: `12factor.net`. The "Config" section explicitly endorses env vars and rejects grouped "environment" config (test/staging/prod) because of combinatorial explosion. (12factor.net/config)
- SIOF is well-known and well-documented (cppreference, isocpp FAQ, MC++ blog) but the C++20-era solutions worth covering are: `constinit` for compile-time-constant init; "construct on first use" via function-local statics; and explicit init from `main()`.
- Kubernetes ConfigMaps + Secrets are the orchestrator-side equivalent of env-var injection; the C++ side reads them either as env vars or as mounted-file ConfigMaps.
- The `startupProbe` (introduced k8s 1.16, GA later) is the right Kubernetes primitive for slow-starting services: lets pods take up to N×P seconds to start before liveness kicks in.

### Code-pattern anchors

- A `Config` struct populated in `main()` from env vars and CLI flags, passed by const reference to constructors of `TracerProviderHolder`, `ChannelCache`, `RedisPool`, etc. No globals.
- A `constinit` example for genuinely-required-before-main constants.
- A counterexample: a `Logger::instance()` Meyers singleton called from a destructor of another global → SIOF-adjacent UB.
- Build-time vs runtime config: a `policy<T>` template parameter for the allocation strategy chosen at build, plus an env-var-set buffer size for runtime — same service, two config dimensions.

### Book references

- Iglberger: chapters on dependency injection, on Singleton-as-antipattern. Directly relevant.
- Yonts: mistakes around globals, lifetimes, and order of init.
- Geewax: "Configuration" sections — design philosophy.

### Open questions

- This is the chapter most likely to be opinionated / argumentative. The brief calls it "the first careful treatment." **Recommend giving it 3500-4000 words with a clear point of view, not a survey.**

---

## Doc 07 — State externalization patterns in C++

### Thesis

If the service is to be stateless, the state has to live somewhere — Redis, Postgres, S3, a queue, or another microservice. Each external dependency brings (a) a connection-pool RAII story, (b) a failure-mode policy (fail-fast vs retry-with-backoff vs circuit-breaker), (c) an exception-safety burden where errors cross the network boundary and have to be translated into something the caller can act on. The C++ approach is RAII + `std::expected` (C++23) for clean error propagation without exceptions across network boundaries.

### Key findings

- **Redis options.** `redis-plus-plus` (sewenew) is the most modern, hiredis-based, with a built-in connection pool, RESP3 support since hiredis 1.0.2+, sentinel and cluster support, coroutine support in newer releases. `cpp-hiredis-cluster` is alternative. `hiredispool` (aclisp) is a thread-safe wrapper. The pool config (max size, idle policy, blocking timeout) is config; the per-use pattern is RAII-checkout / RAII-return.
- **Postgres options.** `libpqxx` is the mature choice; user-supplied pooling required (community libraries like `pqxx_pool`). Newer: pq-richquery does pooling on top of libpq directly.
- **Exception safety across network boundaries.** Two schools:
  - Throw on hard errors, propagate, let RAII clean up — works well if you control both endpoints and the call site can catch reasonably.
  - Use `std::expected<T, ErrorCode>` for expected failures (timeout, unavailable, retryable) and reserve exceptions for truly exceptional cases (bugs, OOM). This composes better with retry/backoff middleware.
- **Retry-with-backoff vs fail-fast** is a per-call-site decision. Idempotent reads (cache lookups, read-replicated DB reads) want retry; non-idempotent writes want fail-fast unless they're explicitly idempotent (e.g., have an idempotency key in the API per Geewax). gRPC has a built-in retry policy with `initialBackoff`, `backoffMultiplier`, `maxAttempts`, and retry-throttle tokens (grpc.io/docs/guides/retry).
- **Deadline propagation** is the right primitive for "don't keep retrying past the caller's deadline." gRPC propagates deadlines through `ClientContext::set_deadline` or by deriving from `ServerContext::deadline()`. If a doc has only one diagram, deadline propagation through a chain of services is the one (oneuptime.com/blog 2026-01-24).

### Code-pattern anchors

- A `ScopedConnection<T>` RAII type: constructor acquires from pool with a deadline, destructor returns (or invalidates on error).
- A retry helper taking a callable + retry policy + `std::stop_token` (C++20) for cooperative cancellation.
- Deadline propagation: `ClientContext::set_deadline(ctx.deadline())` — copy the inbound deadline onto every outbound call.
- An `Either<Value, Error>` (a.k.a. `std::expected<Value, Error>`) error model showing how the call site avoids exception unwinding for normal-failure paths.

### Book references

- Iglberger: ch. on error handling, on strategy.
- Yonts: relevant mistakes on exception safety and resource ownership in error paths.
- Geewax: chapters on idempotency, retries, error responses, deadlines — directly applicable.
- "Building Low Latency Apps": ch. on network IO patterns.

### Open questions

- Should this include sample Conan recipes for `redis-plus-plus` and `libpqxx`? **Recommend a small appendix snippet, not deep coverage.**
- Coverage of MQ (NATS, RabbitMQ, Kafka)? **Recommend brief mention; full coverage would balloon this doc.**

---

## Doc 08 — The ephemeral filesystem trap

### Thesis

Writes to `/var`, `/tmp`, `/log`, or anywhere on the OS container's root filesystem disappear on restart. C++ devs from server backgrounds reach for filesystem-based caches, log files, work directories, lockfiles, and PID files without thinking — and the silent failure mode is the worst: it "works" until something rotates and weeks of data is gone. The hardening pattern, in both Podman and Kubernetes, is to make the rootfs immutable so writes fail loudly at first attempt — converting a latent disaster into an immediate, debuggable error.

### Key findings

- Container rootfs is an overlay that dies with the OS container. Data written outside an explicit volume disappears.
- **Podman: `--read-only` flag on `podman run`, or `read_only: true` in `compose.yaml` for the service.** Writes to the rootfs fail with `EROFS`. Combined with `tmpfs:` mounts for legitimately-writable paths (e.g., `/tmp`), this gives a clear "writes only where allowed" posture.
- **Kubernetes: `securityContext.readOnlyRootFilesystem: true` on the container.** Combined with `emptyDir` (or generic ephemeral) volumes for any path that must be writable. Semantically equivalent to Podman.
- Podman `tmpfs:` and Kubernetes `emptyDir: { medium: Memory }` both give RAM-backed temp space that counts against the memory limit, not disk. Useful for performance-sensitive tmp.
- Quirks: `emptyDir` is mounted world-writable without sticky bit, which breaks some Ruby/Java tmp-dir checks; the workaround is generic ephemeral volumes. Less of a concern in C++ but worth noting.
- Resource limits: `resources.limits.ephemeral-storage` in k8s caps writable-layer + emptyDir + log volume. Podman has no direct equivalent but `--read-only` plus explicit `tmpfs` sizes provide similar bounding. C++ apps that write growing on-disk caches (e.g., RocksDB embedded) need explicit sizing either way.
- Logs: 12-factor Factor XI says treat logs as event streams; in practice, C++ apps should write to stdout/stderr and let Podman/the orchestrator pick them up. Loki via Promtail/Alloy is the user's target.

### Code-pattern anchors

- A `compose.yaml` snippet (primary): service with `read_only: true`, a small `tmpfs:` for `/tmp`, stdout/stderr logging.
- A Kubernetes manifest pair (note inline): pod with `securityContext.readOnlyRootFilesystem: true` and `emptyDir` for `/tmp`.
- A C++ utility for creating a per-request scratch file under a mounted tmpfs, with RAII cleanup; demonstrates how to keep filesystem state request-scoped even when you must write to disk.
- A counterexample: a service that writes a "session cache" to `/var/cache/myapp/...` and loses it on every restart.
- A `Containerfile`/`Dockerfile` showing the pattern that pairs with the compose snippet.

### Book references

- Less of a direct C++ book topic. Reference: 12-factor Factors XI (Logs) and VI (Processes).
- "Building Low Latency Apps in C++" ch. on IO if relevant; otherwise this is more of a container-systems doc with C++ examples.

### Open questions

- Should this include logging-to-stdout vs file-with-rotation comparison? **Yes — short section.**
- Should rootless Podman get a callout? **Brief — UID mapping and `~/.local/share/containers` come up enough to warrant a paragraph.**

---

## Doc 09 — Health checks as the public API of statelessness

### Thesis

Health endpoints are the contract between an "I am OK" process and its supervisor — Podman, Kubernetes, or a load balancer. They are part of the public surface of statelessness: they tell the supervisor "remove me," "restart me," or "send me traffic." The transports (HTTP, gRPC, exec-command) and probe types differ between Podman and Kubernetes, but the semantics — liveness ("am I still alive?") vs readiness ("am I ready to serve?") — are the same. The C++ side is the same in both cases.

### Key findings

- **Podman healthcheck model.** Single check definition in `Containerfile` (`HEALTHCHECK` directive) or compose (`healthcheck:` block) with `test:`, `interval:`, `timeout:`, `retries:`, `start_period:`. Test types: `CMD` (exec a command) and `CMD-SHELL`. The check produces a healthy/unhealthy state visible via `podman ps`. No native distinction between liveness and readiness — one check covers both.
- **Kubernetes probe model (richer).**
  - **startupProbe**: only runs during cold start; once it succeeds, liveness takes over. Use for C++ services with slow init (TracerProvider, channel pool, large data load).
  - **livenessProbe**: detects deadlock / unrecoverable state. Failure → container restart. Should be cheap and shallow — checking only "the event loop is responsive." A liveness probe that talks to the DB is an anti-pattern: DB outage cascades into pod-restart storms.
  - **readinessProbe**: detects "ready to serve" (warmed caches, connected to backends). Failure → removed from Service endpoints but NOT restarted. Can be deeper (depends-on checks).
- **gRPC native probes.** Kubernetes since 1.24: `livenessProbe: { grpc: { port: 50051 } }`. Server implements the standard `grpc.health.v1.Health` service. Podman has no native gRPC probe but can invoke `grpc_health_probe` as a `CMD` healthcheck. The C++ server side is identical.
- **The standard gRPC Health Checking Protocol**: `Check(HealthCheckRequest{ service: "" })` returns `SERVING`/`NOT_SERVING`/`SERVICE_UNKNOWN`. Per-service health (`service: "MyService"`) lets multi-service binaries report granularly.
- **Same-port vs separate-port.** Same port (gRPC service on 50051, health service on 50051): simpler manifest, fewer ports to expose. Separate port (gRPC on 50051, HTTP `/healthz` on 8080): lets you use HTTP probes even when the main service is gRPC, useful behind a service mesh that may TLS-terminate gRPC; also useful for splitting metrics endpoint, debug endpoints. Modern recommendation: same-port gRPC native probe unless there's a specific reason otherwise. Note Podman's single-check model: separate-port is harder to express usefully.
- **What "alive" actually means.** Not "all dependencies are healthy" (that's readiness), but "this process is still responsive and not in an unrecoverable state." Common pitfall: a liveness probe that depends on Redis being up → Redis hiccup → cascading restarts. Liveness is local, readiness is dependency-aware. In Podman's single-check world, lean toward the liveness semantics and let the readiness aspect happen via separate metrics/log signals.
- **Graceful shutdown.** Mark readiness as `NOT_SERVING` first, wait for in-flight requests to drain (gRPC `Server::Shutdown(deadline)`), then exit. This is the pattern that makes rolling deploys actually safe. Podman: `--stop-signal=SIGTERM --stop-timeout=30`. Kubernetes: `terminationGracePeriodSeconds: 30` plus a `preStop` hook if needed.

### Code-pattern anchors

- A `HealthService` implementing `grpc::health::v1::Health` with per-service status via `grpc::HealthCheckServiceInterface`.
- A startup sequence that sets `SERVING` only after the TracerProvider, channels, and connection pools are wired.
- Graceful shutdown: signal handler → `health.SetServing(false)` → `server.Shutdown(deadline)` → tracer flush.
- Podman compose snippet with `healthcheck:` using `grpc_health_probe` (primary).
- Kubernetes manifest with `startupProbe`/`livenessProbe`/`readinessProbe` using native gRPC probes (note).

### Book references

- Geewax: chapters on health/status endpoints, idempotency, deadlines.
- "Building Low Latency Apps": ch. on graceful shutdown.

### Open questions

- Should this doc cover sidecar-style metrics endpoint (for Prometheus/Mimir scraping) on the separate-port pattern? **Yes — short section.**

---

## Doc 10 — Microservices with gRPC and C++

### Thesis

This is the integrating doc. gRPC C++ is where every prior topic concretely lands: channels are process-scoped (Doc 04), `ServerContext` is request-scoped RAII (Doc 02), per-RPC PMR arenas live for the call (Doc 03), the binary is stateless from the orchestrator's view (Doc 01), the threading model dictates how handlers integrate with application work (Doc 05), config comes from env (Doc 06), external state is reached via channels (Doc 07), no disk writes happen on the hot path (Doc 08), and health is reported via the standard protocol (Doc 09). This doc shows the patterns end-to-end with a realistic service skeleton.

### Key findings

- gRPC C++ has three APIs: sync, completion-queue async (legacy), and **callback async (recommended for new code)**. The callback API is described in `grpc.io/docs/languages/cpp/callback` and the proposal `grpc/proposal/L67-cpp-callback-api.md`. It removes the manual event loop while keeping async performance.
- Callbacks run on gRPC-owned thread pools; **must not block**. Long work has to be dispatched off-thread. This shapes how DB calls, slow computations, etc., are handled.
- For unary calls: implement a method that returns a `ServerUnaryReactor*`. For streaming: implement reactors with `OnReadDone`/`OnWriteDone`/`OnCancel`. The lastviking.eu series is a solid practical walkthrough.
- **Coroutine integration**: `asio-grpc` (the Boost.Asio adapter) gives `co_await` over the gRPC completion queue. This is where C++20 coroutines really land in gRPC services. Code stays linear; gRPC's threading and lifetime semantics are wrapped in Asio's executor model.
- **Custom memory allocators per method** (callback API): `SetMessageAllocatorFor_*` and `SetContextAllocator` let gRPC use a user-supplied allocator for request/response message objects and the `CallbackServerContext`. This is the direct hook for per-RPC PMR arena allocation — a major perf win on hot paths.
- **Deadline propagation**: `ClientContext::set_deadline(server_ctx.deadline())` is the one-line idiom for "don't outlive my caller."
- **Retry**: configured via gRPC service config JSON, not code. Allows declarative `retryPolicy` (initialBackoff, maxAttempts, retryableStatusCodes) and `retryThrottling` (token-based throttle to prevent retry storms).
- **Keepalive**: `GRPC_ARG_KEEPALIVE_TIME_MS` channel arg keeps idle HTTP/2 connections warm so first call after idle doesn't pay reconnect latency.

### Code-pattern anchors

- A complete callback-API service: `RouteGuide`-style with unary, server-streaming, client-streaming, bidi.
- Wiring `main()`: parse config → init TracerProvider → init ChannelCache → init RedisPool → register HealthService and RouteGuide → `Server::Wait` → on signal: mark unhealthy, shutdown with deadline, flush tracer.
- Per-RPC PMR arena via custom message allocator.
- Service-config-driven retry on a client.
- `co_await`-style coroutine handler using `asio-grpc` for users who want it.

### Book references

- Geewax: protocol design, service surfaces, deadlines, retries, errors.
- Iglberger: design of the handler classes themselves.
- "C++ High Performance 2e": coroutine and async chapters.
- "Building Low Latency Apps": gRPC and network-IO chapters if applicable.

### Open questions

- Should there be an Asio-grpc deep example or only mention? **Recommend mention + small example; deep coverage is a separate tutorial.**
- Server reflection? **No — out of scope.**

---

## Doc 00 — Index

### Suggested structure

1. **Thesis** (one paragraph): statelessness as deployment posture, with C++-specific obstacles and idioms.
2. **Reading order**:
   - For monolith-background C++ devs: 01 → 02 → 04 → 05 → 08 → 03 → 06 → 07 → 09
   - For container-experienced devs new to C++ services: 01 → 03 → 02 → 04 → 09 → 06 → 08 → 07 → 05
   - For latency-focused work: 03 → 06 → 09 → 04 → 02
3. **Vocabulary**: request-scope / process-scope / deploy-time-scope distinction; the State Architecture Table from Doc 04.
4. **Demo cross-reference**: which docs reference which demos (03 → demo-03; 03 → demo-06).
5. **Tooling stack assumed**: C++20/23, GCC/Clang, CMake, Ninja, Conan, gRPC, OTel C++ SDK, Grafana stack.
6. **What this doc set is not**: not a gRPC tutorial, not a Kubernetes tutorial, not a 12-factor primer — assumed reader is a C++ architect.

---

## Drafting plan

Given the depth target (3000+ words, multiple code samples) and 11 docs (12 with index), drafting will be substantial. Sequential order, with each doc building on its predecessors:

1. **Doc 01** — sets vocabulary (done).
2. **Doc 02** — RAII as the discipline that enforces request scope; performance side of ctor/dtor.
3. **Doc 03** — PMR as the specific RAII pattern for per-request arenas; std-container choices.
4. **Doc 04** — process-scoped state, the State Architecture Table, OS container resource budget.
5. **Doc 05** — threading and concurrency; depends on Doc 04's process-scope concept and on Doc 02's RAII discipline.
6. **Doc 06** — 12-factor adapted to C++; references singleton (Doc 05) and config (Doc 04).
7. **Doc 07** — state externalization (Redis, PostgreSQL, etc.); cache-fix for the Doc 01 counterexample.
8. **Doc 08** — ephemeral filesystem trap.
9. **Doc 09** — health checks.
10. **Doc 10** — gRPC microservices integration; the capstone doc that pulls everything together.
11. **Doc 11** — build tooling appendix (Conan, CMake).
12. **Doc 00** — index, written last once cross-references are stable.

Per-doc budget: ~3500 words including code; bibliography ~150–300 words. Total ~40K–45K words across the set.

## Confirmed decisions

These were originally open questions; locked in before drafting started:

1. **Demo references dropped.** Code examples are written fresh and stand alone; no references to demo-03 or demo-06.
2. **Build tooling.** Brief mention in docs where relevant; one consolidated `11-build-tooling.md` appendix covers Conan and CMake in depth.
3. **Code language standard.** C++20-portable as baseline; C++23 features called out where they meaningfully improve the example (`std::expected`, `std::flat_map`, `<stacktrace>`, `std::print`, `std::pmr::stacktrace`).
4. **Tone.** Reference-style throughout, opinions clearly marked in `> **Opinion.**` callouts.
5. **Drafting cadence.** One doc at a time with review checkpoints between each.
6. **Container stack.** Podman + podman-compose as primary; Kubernetes-specific notes inline where they materially differ.
7. **Threading as a dedicated doc.** Doc 05 covers threading and concurrency in its own right; threaded through the others as cross-references.

## Remaining open per-doc questions

These remain for the user to clarify before or during drafting of the specific doc:

- **Doc 02:** Depth of micro-benchmark for ctor/dtor cost? **Default plan: a short example with explanatory prose, not a fully-tooled benchmark — that belongs in a separate perf-tooling note.**
- **Doc 03:** Whether to include the `std::flat_map` (C++23) example alongside `std::pmr::*` containers. **Default plan: yes, briefly, as the modern small-N alternative.**
- **Doc 04:** Should the cgroup v2 CPU-limit detection snippet show a vendored helper or recommend a library? **Default plan: vendor a small self-contained helper, then mention `concurrencpp`/Abseil as alternatives.**
- **Doc 05:** asio-grpc vs raw callback API depth? Boost.Fiber treatment? `std::execution` mention? **Default plans: cover both gRPC APIs briefly; present fibers and stackless coroutines as alternatives with trade-offs; mention `std::execution` as the future direction without deep coverage.**
- **Doc 06:** How hard to lean on `constinit` vs `constexpr` distinction? **Default plan: cover both, since the difference matters for startup cost.**
- **Doc 07:** Cover Kafka/NATS/RabbitMQ? **Default plan: brief mention only; full coverage is its own document.**
- **Doc 08:** Rootless Podman callout depth? **Default plan: one paragraph noting UID mapping and storage location.**
- **Doc 10:** Depth of asio-grpc coroutine example? **Default plan: small example, mention as the modern shape, not deep coverage. Coordinate with Doc 05's coverage.**
