# 06 — 12-Factor Adapted to C++

## Thesis

The 12-factor methodology was written by Heroku engineers in 2011, codifying patterns for cloud-native services. It targets languages with REPLs, runtime introspection, and no compile-time/link-time distinction — Ruby, Python, Node.js, Java to a lesser extent. C++ is none of those things. Most of the twelve factors still apply directly; three collide hard with C++ realities and need careful translation.

The three collisions are Factor III (Config), Factor VI (Processes), and Factor IX (Disposability). Config because C++ has compile-time, link-time, env-time, and runtime-fetched configuration dimensions that the 12-factor canon collapses to a single "environment variable" answer. Processes because the C++ instinct for "one of these per process" is the Meyers singleton, and singletons defeat much of what stateless processes are supposed to give. Disposability because C++ has a startup tax that other 12-factor languages don't pay — static initialization, the SIOF, library-level constructors in shared objects, dynamic loader work — all of which add to cold-start latency that orchestrators measure via startup probes.

This document covers all twelve factors briefly, lingers on the three that collide, and develops the C++-specific patterns: where configuration lives, why dependency injection beats singletons, and how to design for fast startup. The treatment is opinionated by design — the brief flagged this as the chapter that needed a clear point of view, not a survey.

## A brief tour of the non-controversial factors

Eight of the twelve factors map cleanly to C++ practice and warrant short treatment.

**Factor I (Codebase).** One codebase tracked in version control, many deploys. Same in C++ as anywhere — a monorepo or per-service repo, deploys distinguished by build-time and runtime configuration. Doc 11 covers the build-tooling side (Conan, CMake).

**Factor II (Dependencies).** Explicitly declare and isolate dependencies. C++ has historically been bad at this; Conan and vcpkg have largely fixed it for new code. A `conanfile.txt` or `conanfile.py` declaring every external dependency by name and version, with locked transitive dependencies, satisfies the spirit of the factor. The container image is the isolation boundary — system libraries the binary links against are part of the image, not the host.

**Factor IV (Backing services).** Treat backing services as attached resources. C++ services connect to Redis, PostgreSQL, Kafka, other gRPC services via configuration-driven endpoints; swapping a development Redis for a production Redis cluster is a config change, not a code change. Doc 04 (process-scoped state) and Doc 07 (externalization) develop this in detail.

**Factor V (Build, release, run).** Strict separation of build, release, run stages. C++ builds produce a binary; release combines the binary with config to produce an image; run executes the image. CI/CD pipelines enforce the separation. Same as elsewhere.

**Factor VII (Port binding).** Services bind to a port and serve. gRPC C++ binds via `ServerBuilder::AddListeningPort`; the port number comes from config. Trivially aligned.

**Factor X (Dev/prod parity).** Keep development, staging, and production as similar as possible. Podman locally and Kubernetes in production give acceptable parity for most cases. `podman generate kube` produces a Kubernetes manifest from a compose file when literal parity is needed.

**Factor XI (Logs).** Treat logs as event streams. Write structured JSON to stdout; let the orchestrator (Podman's logging driver, Kubernetes' container runtime, then Loki) collect and route. Do not write log files inside the container. Doc 08 covers this from the ephemeral-filesystem angle.

**Factor XII (Admin processes).** Run admin and management tasks as one-off processes. In C++ services, this typically means a separate binary (or the same binary with a different command-line subcommand) for database migrations, cache priming, scheduled jobs. Same image, different entry point.

That leaves Factors III (Config), VI (Processes), VIII (Concurrency), and IX (Disposability). Concurrency was covered substantially in Doc 05; this document touches on the parts specific to the 12-factor framing. The other three get the rest of the document.

## Factor VIII: Concurrency, briefly

The 12-factor formulation says "scale out via the process model" — run more processes to handle more load, distinguish process types by workload (web, worker, scheduler). For a C++ service this is mostly the same, with one wrinkle: C++ has true in-process multi-threading, which the 12-factor canon downplays because the target languages either don't (Ruby MRI's GIL, Python's GIL) or do but expensively (Node.js cluster mode).

The right reading for C++ is that *scaling out* is via the process model — more replicas, more pods, more containers — while *internal concurrency* is via threads, coroutines, or fibers within each process. The two are not in conflict. A C++ service that uses C++20 coroutines on a small thread pool for in-process concurrency, replicated horizontally for load, is fully 12-factor compliant. Doc 05 covers the in-process side in depth.

The one place this matters operationally: the unit of scaling is the process, not the thread. When the orchestrator decides to add capacity, it adds another OS container. Each container has its own thread pool, its own connection pool, its own arena resources. State that needs to be shared across replicas is in external storage (Doc 07). State that's per-replica is process-scoped (Doc 04). Threads do not cross replica boundaries.

## Factor III: Config and the compile-time/runtime split

The 12-factor canon says: "Store config in the environment." Environment variables, read at startup, used at runtime. Database URLs, API keys, feature flags, log levels — all of it. The principle is that the same binary runs in dev and production, distinguished only by environment.

In a dynamic language, this is straightforward. There is one place config lives — runtime — and one mechanism to inject it — environment variables. C++ has at least four distinct config dimensions, and lumping them all under "environment variables" loses meaningful structure.

**Compile-time config** is what the type system sees: `if constexpr`, `consteval`, template parameters, `concept` constraints. The choice of allocator strategy (`std::pmr::polymorphic_allocator` vs a custom one), the choice of exception model (`-fno-exceptions` vs default), the choice of standard library (libstdc++ vs libc++), the choice of optimization level — these are baked at build time. They cannot vary by environment without a separate build.

**Link-time config** is what the linker sees: which `.o` files are combined, which shared libraries are linked, which symbols are resolved to which implementations. Static vs dynamic linking, LTO settings, the choice of allocator implementation (link in jemalloc, tcmalloc, or rely on glibc malloc) — baked at link time, one step after compile.

**Env-time config** is what `std::getenv` reads at process startup. The bind address, the upstream service URLs, log level, feature flags. This is the dimension 12-factor's "environment variables" refers to.

**Runtime-fetched config** is what the service reads from a config service (Consul, etcd) or a control plane after startup. Feature flags that can change mid-run, A/B test assignments, dynamically-loaded policies. Most C++ services don't reach for this; some do.

The 12-factor principle, translated faithfully to C++, says: *deploy-varying* config goes in env-time. Config that varies between dev and production but not between two production replicas of the same service belongs in environment variables. Config that varies between two production replicas (assignment to a specific user shard, for example) usually belongs in runtime-fetched config. Config that doesn't vary across deploys at all — the type system's view of the world — belongs in compile-time.

The practical pattern is to parse env-time config once in `main()` into a `Config` struct, then pass that struct (by reference, by value, by const-ref to subsystems) down through the construction graph. No subsystem reaches for `std::getenv` after `main()` has run. The struct is the boundary.

```cpp
struct Config {
    std::string                     listen_addr;
    std::string                     redis_uri;
    std::vector<std::string>        upstream_targets;
    std::chrono::milliseconds       default_deadline;
    std::size_t                     redis_pool_size;
    std::size_t                     cpu_limit;        // from CPU_LIMIT env
    spdlog::level::level_enum       log_level;
    bool                            enable_tracing;
    // ...
};

Config parse_config(int argc, char** argv) {
    Config c;
    c.listen_addr      = getenv_or("LISTEN_ADDR", "0.0.0.0:50051");
    c.redis_uri        = getenv_or("REDIS_URI", "tcp://localhost:6379");
    c.upstream_targets = split_csv(getenv_or("UPSTREAM_TARGETS", ""));
    c.default_deadline = parse_duration(
        getenv_or("DEFAULT_DEADLINE", "5s"));
    c.redis_pool_size  = parse_size(getenv_or("REDIS_POOL_SIZE", "16"));
    c.cpu_limit        = parse_size(getenv_or("CPU_LIMIT", "1"));
    c.log_level        = parse_log_level(getenv_or("LOG_LEVEL", "info"));
    c.enable_tracing   = parse_bool(getenv_or("ENABLE_TRACING", "true"));
    validate(c);
    return c;
}
```

> **Opinion.** The single most useful refactor in a legacy C++ service migrating to containers is to eliminate every `std::getenv` outside `main()` and replace them with `Config` references plumbed through the construction graph. This is mechanical, low-risk, and pays back immediately in testability — `parse_config` is replaced with a test fixture, every subsystem becomes testable in isolation.

For compile-time config — the dimensions that genuinely cannot move to runtime — keep it explicit. A template parameter or `if constexpr` branch on `Policy` is fine; a `#ifdef` strewn through implementation files is not. Doc 11 covers the Conan/CMake side of managing build variants.

For runtime-fetched config — the rare case where config genuinely needs to change mid-process — fetch via a dedicated subsystem that exposes the same `Config&`-style interface to the rest of the code. The handlers should not know whether the value came from an env var read at startup or a control plane fetched five seconds ago.

## Factor VI: Processes and the singleton problem

The 12-factor formulation says: "Execute the app as one or more stateless processes." The principle is that any process should be killable and replaceable; state must live externally. C++ services satisfy the letter of this directly — the binary runs, handles requests, dies cleanly, is replaced. The spirit is harder.

The C++ instinct for "one of these per process" is the Meyers singleton: a function-local `static` that initializes on first call, lives until process exit, accessed via a global function. This has been thread-safe-init since C++11 — the standard guarantees that initialization happens exactly once, with a synchronization barrier on the flag check. The pattern looks like:

```cpp
class Logger {
public:
    static Logger& instance() {
        static Logger inst;
        return inst;
    }
    // ...
private:
    Logger();
};

// Usage:
Logger::instance().log("...");
```

The problems with this pattern in service code are well-documented elsewhere; the summary version is four-fold.

First, every call path that hits `getInstance()` pays an atomic flag check after the first call. The check is cheap — typically a single relaxed load — but it is on the hot path of every log call, every metrics increment, every tracer access. In aggregate this is not free.

Second, the instance lives until process exit. Destruction order at process exit is the reverse of construction order, which is determined by first-access. In a service with several singletons that reference each other, this becomes a destruction-order puzzle that produces use-after-free bugs at shutdown. The bugs are hard to debug because they only manifest during the shutdown phase that monitoring doesn't usually cover.

Third, the singleton is hard to test. A test that wants a different `Logger` configuration has to either reach into the singleton's internals or run in a separate process. Both are friction; both push tests toward integration-test-only coverage.

Fourth, the singleton makes dependency wiring implicit. The code that calls `Logger::instance()` doesn't declare its dependency on the logger; the dependency is hidden in the implementation. Refactoring becomes harder because every singleton access is a hidden coupling.

The replacement is dependency injection — pass dependencies as constructor arguments from `main()`. Doc 04 showed the wiring pattern; the short version is that every process-scoped object is constructed by name in `main()`, held by a local variable, and passed by reference to the things that need it.

```cpp
int main(int argc, char** argv) {
    const Config config = parse_config(argc, argv);

    Logger        logger{config.log_level};
    ChannelCache  channels{config.channel_options};
    Redis         redis{config.redis_uri, redis_pool_opts_from(config)};

    MyService     service{logger, channels, redis};

    // ... build gRPC server with &service, run ...
}
```

`MyService` knows it depends on `Logger&`, `ChannelCache&`, and `Redis&` because those are its constructor parameters. Tests construct `MyService` with test doubles. Production constructs it with the real types. Destruction order is reverse construction order, which is the order that makes sense given the dependency graph.

> **Opinion.** Use Meyers singletons only when an external API requires global access — the OTel `Provider::SetTracerProvider`/`GetTracerProvider` pattern is the canonical case. Even then, the *ownership* lives in `main()`; the global is just an access mechanism the framework imposes. Everywhere else, dependency injection. The verbosity is the price; the testability and shutdown sanity are the return.

The 12-factor "stateless processes" principle in C++ terms means: process-scoped state is fine (Doc 04 covered this), but its *ownership* should be explicit in `main()`, not hidden in singletons. The process is killable because every process-scoped object is reconstructible from configuration; the construction graph is visible at the entry point. Hidden singletons defeat both properties.

For things that genuinely need to be globally available before `main()` runs — a small number of cases involving compile-time constants used in static initialization elsewhere — C++20's `constinit` is the right tool. It enforces compile-time initialization without forcing `const`:

```cpp
// Global, initialized at compile-time, mutable at runtime if needed.
constinit std::atomic<bool> g_shutdown_requested{false};
```

This avoids the SIOF (static initialization order fiasco) for the constant, gives the compiler permission to optimize aggressively, and is honest about the fact that the variable is genuinely global. Reach for `constinit` rarely; reach for it deliberately when you do.

## Factor IX: Disposability and the C++ startup tax

The 12-factor formulation says: "Maximize robustness with fast startup and graceful shutdown." The principle is that processes should start quickly (so scaling out is fast) and shut down cleanly (so deploys and node failures don't lose data). C++ has a graceful-shutdown story that is straightforward (Doc 09 covers it); the fast-startup story has a problem the 12-factor canon doesn't anticipate.

The problem is the C++ startup tax. Before `main()` runs, every global and namespace-scope object with a non-trivial constructor is constructed. The order is the SIOF: deterministic within a translation unit, undefined across translation units. Every shared library the binary links against runs its own static constructors when loaded. The dynamic loader resolves symbols. Initialization-on-first-use guards inside Meyers singletons aren't run yet, but the storage and the type information are.

A small C++ service binary typically takes 50-200 ms to reach `main()` on a warm cache, longer on cold start. This sounds trivial, and it is until you put it next to:

- Kubernetes default startup probe: 10-second initialDelaySeconds, 10-second period, 3 failures before kill. A pod that takes 30+ seconds to be ready risks getting killed.
- Container creation overhead: image pull (potentially many seconds), filesystem setup, network setup. The pre-`main()` C++ time stacks on top of this.
- Rolling deploy SLO: how long it takes to roll a fleet of 100 replicas one at a time. Each replica's startup time multiplies.

The mitigations are several.

First, audit static constructors. Every namespace-scope object with a non-trivial constructor is a cost. A `std::unordered_map<std::string, RegexCompiler> g_regex_cache` that "lazily" populates on first use actually allocates the map at startup. A `Logger g_logger;` runs the `Logger` constructor before `main()`. Move these into function-local statics (with the singleton caveats above), or better, into `main()`-owned objects.

Second, use `constinit` for things that genuinely need compile-time initialization. The atomic flag example above is one case; tables of compile-time constants are another. `constinit` makes the cost zero (compile-time-only) rather than deferred to startup.

Third, minimize shared library count. Each `.so` adds dynamic loader work. Static linking, where feasible, eliminates this entirely. The trade-off is binary size and update granularity; for a service binary distributed as a container image, the size cost is usually acceptable.

Fourth, defer expensive process-scoped initialization until after the health endpoint is responding. The gRPC server can start serving the health check before the upstream channel cache is fully populated. A pattern:

```cpp
int main() {
    auto config = parse_config(argc, argv);

    // 1. Construct the bare minimum to serve a health check.
    HealthService health;
    health.SetServingStatus("", grpc::health::v1::ServingStatus::NOT_SERVING);

    grpc::ServerBuilder builder;
    builder.AddListeningPort(config.listen_addr, ...);
    builder.RegisterService(&health);
    auto server = builder.BuildAndStart();

    // 2. The startupProbe now hits the health endpoint and gets NOT_SERVING.
    //    The pod is alive but not yet receiving traffic.

    // 3. Do the expensive process-scoped initialization.
    auto channels = build_channel_cache(config);
    auto redis    = build_redis_pool(config);
    auto service  = MyService{channels, redis};

    // 4. Register the real service and flip health to SERVING.
    server->RegisterService(&service);  // pseudo-code; real path uses
                                        // ServerBuilder before Start
    health.SetServingStatus("", grpc::health::v1::ServingStatus::SERVING);

    server->Wait();
}
```

The pseudocode is rough — gRPC's `ServerBuilder` requires services to be registered before `BuildAndStart()`, so the actual pattern involves more care — but the principle is real: separate "process is alive" from "process is ready to serve real traffic." Kubernetes startup, liveness, and readiness probes are designed for exactly this split. Doc 09 covers the probe model in detail.

> **Opinion.** Most C++ services pay an unnecessary startup tax from globals that didn't need to be globals. The first pass at fixing cold start is not optimization tricks — it is eliminating non-essential static constructors. The build-tooling appendix (Doc 11) covers tools like `nm -C --size-sort` and the linker `--gc-sections` flag for finding the offenders.

The Disposability factor's other half — graceful shutdown — is C++'s strength rather than weakness. Destructors run in reverse construction order, RAII ensures cleanup is exception-safe, `std::stop_token` from Doc 05 propagates cancellation to in-flight work. The shutdown sequence in `main()` looks like the reverse of the startup sequence: signal arrives, flip health to NOT_SERVING, drain in-flight RPCs with a deadline, server `Shutdown()`, destructors run in reverse, process exits cleanly. Doc 09 develops this end-to-end.

## Recommendation summary

Treat 12-factor as the default and translate the three C++ collisions explicitly.

For Config, distinguish compile-time, link-time, env-time, and runtime-fetched dimensions. Put deploy-varying config in env-time, parsed once into a `Config` struct in `main()` and plumbed by reference. Eliminate `std::getenv` calls outside `main()`.

For Processes, use dependency injection: construct process-scoped objects in `main()` by name, hold them in local variables, pass by reference to subsystems. Avoid Meyers singletons except where a framework requires global access; even then, ownership lives in `main()`. Use `constinit` for the small set of things that genuinely need to be globally available before `main()`.

For Disposability, audit static constructors and eliminate the ones that don't need to be global. Minimize shared library count where feasible. Defer expensive initialization until after the health endpoint is serving NOT_SERVING. Treat fast startup as a deploy-velocity question, not a micro-optimization.

For the other nine factors, follow the 12-factor canon directly. Conan for dependencies, structured logs to stdout, services as one-off binaries or subcommands, Podman locally and Kubernetes in production for parity.

## Cross-references

Doc 02 covers RAII discipline, which underpins the destruction-order guarantees that make `main()`-owned construction work correctly.

Doc 03 covers PMR, which is one of the compile-time vs runtime decisions: the allocator strategy is chosen at compile-time (template parameter or PMR resource type), the actual resource is chosen at runtime (which `memory_resource` instance backs the arena).

Doc 04 covers process-scoped state and the `main()`-owned wiring pattern in concrete detail; this document develops the philosophy behind that pattern.

Doc 05 covers the threading model that the concurrency factor implies for C++ services.

Doc 07 covers state externalization — the backing-services factor in concrete C++ terms.

Doc 08 covers ephemeral filesystems and the logs-as-event-streams factor.

Doc 09 covers health checks, startup probes, and the disposability factor's shutdown half.

Doc 11 covers Conan and CMake — the build-tooling side of dependencies, builds, and compile-time configuration.

## Annotated bibliography

**The Twelve-Factor App (`12factor.net`).** The canonical reference. Worth reading once end-to-end before this document; the factors as written are short and clear, and the translation to C++ goes faster when the original is fresh.

**Iglberger, *C++ Software Design*.** The chapter on the Singleton pattern is the most rigorous critique of Meyers singletons in current C++ literature. The dependency-injection chapter develops the alternative covered in this document. The chapters on value semantics frame the construction-graph approach in `main()`.

**Yonts, *100 C++ Mistakes and How to Avoid Them*.** The entries on global state, on initialization-order issues, and on hidden dependencies are directly applicable. The entries on `constinit`, `constexpr`, and `consteval` use are useful for the compile-time-vs-runtime config discussion.

**Geewax, *API Design Patterns*.** The chapter on standard methods covers the admin-process and one-off-task patterns. The configuration chapters are written for a different audience but the underlying principles inform the compile-time/runtime split.

**Kubernetes documentation, "Configure Liveness, Readiness and Startup Probes".** The canonical reference for the probe model that the Disposability factor leans on. The interaction between startup probes and C++ cold-start time is documented there, though not in C++-specific terms.

**Podman documentation on `HEALTHCHECK` directives and compose `healthcheck` blocks.** The single-host equivalent to the Kubernetes probe model. Same semantics, different syntax.

**"C++ High Performance" (2nd edition).** The chapter on application startup and initialization is relevant background for the cold-start discussion. The book's treatment of compilation models and link-time optimization is useful for the compile-time-config dimension.
