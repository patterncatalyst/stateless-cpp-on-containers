# 09 — Health Checks as the Public API of Statelessness

## Thesis

The health-check endpoint is the orchestrator's API into the service. It is the mechanism by which Podman or Kubernetes decides whether to route traffic to a replica, whether to restart a sick one, and whether a newly-started one has finished initializing. The rest of the doc set has been about getting the internals of statelessness right — RAII, PMR, process-scoped state, threading, externalization, ephemeral filesystem. The health check is where those internals meet the orchestrator. Get it wrong and the architectural work doesn't matter: a service that's correctly designed but reports wrong health gets killed during deploys, takes traffic before it can serve, or fails to be removed from rotation when it goes bad.

The health-check semantics are more subtle than "is the service up." There are at least three distinct questions an orchestrator wants to ask — should I kill this replica, should I route traffic to it, has it finished starting up — and conflating them produces failure modes that look correct in development and fail in production. Kubernetes exposes the three as separate probes (startup, liveness, readiness); Podman's `HEALTHCHECK` directive collapses them into one. Either way, the C++ service has to think about each distinctly and answer them correctly.

This document covers the health-check model: the three probes and what each means, the gRPC standard health protocol that's become the canonical answer, the same-port-vs-separate-port trade-off, what "alive" and "ready" actually mean for a C++ service, and the graceful-shutdown sequence that ties Doc 05's `std::stop_token`, Doc 07's pool draining, and Doc 06's destruction order into a single coherent flow.

## The three probes

Kubernetes distinguishes three probes per container. Podman has a single `healthcheck`. The semantic distinctions are worth understanding even when only one of them is configurable, because they translate to the questions any orchestrator needs answered.

The **startup probe** asks "has the service finished initializing yet?" Until it succeeds, the other probes are not run. This is the probe that gives a C++ service time to construct its `TracerProvider`, build its channel cache, warm its connection pools, and load any deserialized configuration — all the process-scoped state from Doc 04. A slow-starting service that doesn't declare a startup probe risks being killed by liveness failures during its initialization. The probe is intended to be permissive: long timeout, many retries, no harm in being slow during the startup window.

The **liveness probe** asks "is the service deadlocked or otherwise unable to serve?" If it fails repeatedly, the orchestrator kills the container and starts a fresh one. The intended use is for catching processes that are running but have stopped making progress — a stuck thread holding a critical lock, an infinite loop in a background worker, a deadlock that the process can't recover from. The probe should be cheap and direct: does the process respond to a no-op call?

The **readiness probe** asks "should I send traffic to this replica?" If it fails, the orchestrator stops routing new requests to the replica (removes it from the Service's endpoint set) but does not kill it. This is the probe that handles transient unhealthiness — connection pool exhaustion, dependency outage, graceful shutdown — where the service is alive but temporarily unable to serve. The orchestrator continues to consult the probe; if the replica recovers, traffic resumes.

The composition matters: until the startup probe succeeds, the others are not consulted. Once startup succeeds, liveness and readiness run on their own schedules. Liveness failures lead to restarts; readiness failures lead to traffic removal. A correctly-designed health system has liveness rarely fail and readiness occasionally fail during deploys and transient issues.

Podman's `HEALTHCHECK CMD ...` directive (set in the Dockerfile or in compose `healthcheck:` block) is a single check that drives container restart on failure. It maps most naturally to the liveness role; for combined liveness/readiness semantics under Podman, the service exposes a richer health endpoint and the check parameterizes which question it's asking.

## What "alive" actually means

The single most common health-check bug is confusing "alive" with "everything is working." A liveness probe that fails when a downstream Redis is unreachable causes a cascading restart: the service restarts, comes up, sees Redis still down, restarts again. Multiplied across a fleet of replicas, this is how an outage in a single dependency becomes a cluster-wide restart storm.

For a C++ service, the right liveness check answers three questions only.

Does the process exist? Trivially answered by the container runtime; the process is either running or not.

Is the gRPC server accepting connections? A successful TCP connect to the listening port answers this. The gRPC standard health protocol's `Check` RPC against the empty service name answers it at a higher level (TCP connect plus gRPC framing plus protocol negotiation).

Is the server making forward progress? Harder to answer in general; in practice, "the gRPC server can complete a no-op RPC" is the strongest reasonable check. Deadlocks deep in handler code will not be caught by a no-op health check, but those are rare and usually caught by other monitoring (request timeouts, queue depth alerts).

Notably absent: backend health, recent error rates, cache hit ratios, queue depths. These are useful metrics; they are not liveness signals. A service that has not had a healthy database connection in five minutes is broken in a way the orchestrator cannot fix by restarting; restarting it makes the problem worse by removing the buffered request queue, the in-process cache, and the partially-warm channel cache.

> **Opinion.** Liveness should fail only for conditions that a restart actually fixes. In practice this means deadlocks and process-internal corruption. Dependency health, even severe dependency health, is not a liveness condition. The right response to "I can't reach Redis" is to fail readiness (stop accepting new work), continue serving in-flight requests if possible, and let the orchestrator preserve the replica until Redis recovers.

## What "ready" actually means

Readiness is more nuanced because it controls traffic flow. The right answer depends on the specific service, but a few principles generalize.

A newly-started replica is not ready until its process-scoped state is initialized. The channel cache has its primary upstreams connected; the connection pool has its minimum number of healthy connections checked in; configuration parsing succeeded; the OTel exporter has connected to the OTLP backend (or has failed and decided to retry, depending on the failure mode policy). The startup probe covers the bulk-initialization window; readiness covers the steady-state question.

A replica becomes not-ready when it cannot serve traffic at the moment. The most common case is graceful shutdown: SIGTERM arrives, the service flips readiness to NOT_SERVING immediately so the orchestrator stops routing new requests, then drains in-flight work before exiting. Doc 06 mentioned the staged startup; this is the symmetric staged shutdown. Other not-ready conditions include connection pool exhaustion (no connections available to serve), backpressure trigger (PSI-aware backoff from Doc 05), and explicit administrative drain.

A replica becomes ready again when the not-ready condition clears. Connection pool refilled, backpressure subsided, dependency reachable. The orchestrator polls the readiness probe on its configured cadence; recovery is automatic once the probe starts returning success.

The 12-factor canon's "stateless processes" principle has an operational corollary here: a replica that becomes not-ready should be able to return to ready without restart. Anything that requires a restart to recover from is closer to a liveness failure. This distinction is what keeps readiness and liveness separate.

## The gRPC standard health protocol

The canonical health-check answer for gRPC services is the standard `grpc.health.v1.Health` service. Defined in the gRPC project itself, supported by every language binding, surfaced as a `grpc_health_probe` command-line client that container orchestrators can invoke. Kubernetes 1.24+ supports a `grpc:` probe block natively; earlier versions and Podman use `grpc_health_probe` as an exec command.

The protocol has two RPCs: `Check` returns the current serving status for a named service, and `Watch` streams status changes. The status is one of `UNKNOWN`, `SERVING`, `NOT_SERVING`, or `SERVICE_UNKNOWN`. Services maintain their status; clients query it.

Implementing it in C++ uses the bundled `grpc::HealthCheckServiceInterface` and `grpc::EnableDefaultHealthCheckService`:

```cpp
#include <grpcpp/health_check_service_interface.h>
#include <grpcpp/ext/health_check_service_server_builder_option.h>

int main(int argc, char** argv) {
    const Config config = parse_config(argc, argv);

    grpc::EnableDefaultHealthCheckService(true);
    grpc::ServerBuilder builder;
    builder.AddListeningPort(config.listen_addr,
                             grpc::InsecureServerCredentials());

    MyService service{...};
    builder.RegisterService(&service);

    auto server = builder.BuildAndStart();

    // The default health service is now registered. Retrieve it
    // to set serving statuses.
    auto* health = server->GetHealthCheckService();

    // Empty service name represents the whole server.
    health->SetServingStatus("", grpc::health::v1::HealthCheckResponse::SERVING);
    health->SetServingStatus("my.package.MyService",
                             grpc::health::v1::HealthCheckResponse::SERVING);

    server->Wait();
}
```

The `SetServingStatus` API is what the application uses to signal readiness. Per-service status (the second argument) supports fine-grained reporting; a service that depends on PostgreSQL can flip its own service name to NOT_SERVING while leaving the empty-name (server-wide) status as SERVING, allowing other services on the same server to keep serving traffic if PostgreSQL is the only failed dependency.

The Podman `HEALTHCHECK` directive that calls `grpc_health_probe`:

```dockerfile
HEALTHCHECK --interval=5s --timeout=2s --start-period=30s --retries=3 \
    CMD grpc_health_probe -addr=:50051
```

The Kubernetes equivalent using native gRPC probes:

```yaml
livenessProbe:
  grpc:
    port: 50051
  initialDelaySeconds: 30
  periodSeconds: 5
readinessProbe:
  grpc:
    port: 50051
    service: my.package.MyService
  periodSeconds: 5
startupProbe:
  grpc:
    port: 50051
  failureThreshold: 30
  periodSeconds: 2
```

Note the readiness probe specifies a `service:` name — it asks the health service for the specific application service's status, not the server-wide status. The liveness probe asks for the empty-name status (the server's). This composition means: liveness fails only when the whole server is unresponsive; readiness fails when the specific application service is unhealthy, even if the server itself is fine. The behaviour matches the semantic distinction from earlier sections.

## Separate-port vs same-port

A persistent operational question: should health checks be served on a separate port from application traffic, or on the same port?

The same-port approach uses the gRPC health service on the same listening port as the application RPCs. One process, one port, simple to reason about. This is what the section above showed.

The separate-port approach exposes a small HTTP server on a different port (say :8080) for `/healthz` and `/readyz` endpoints, alongside the main gRPC server on :50051. The orchestrator's probes hit the HTTP endpoints; clients hit the gRPC endpoint. Two ports, slightly more configuration.

The trade-offs:

Same-port is simpler to deploy. One port to expose, one network policy to write, one binding to manage. For most services this is sufficient.

Separate-port is more secure when the application port is exposed to untrusted networks. The health port can be restricted to internal traffic only (the kubelet, internal monitoring) while the application port is open to external clients. Probing endpoints don't need authentication on the internal port, which simplifies the probe configuration.

Separate-port also allows different rate-limiting and timeout policies. The gRPC server's `MaxConcurrentStreams` and `KeepaliveTime` settings apply to the application port; the health port can have looser settings for the cheap probe traffic.

Separate-port is useful when probes need to survive gRPC server overload. If the gRPC server is saturated and not accepting new connections, the gRPC-on-same-port health check fails too, which causes the orchestrator to kill the replica — exactly the wrong response to overload. A separate-port HTTP probe that just checks "is the process alive" survives overload and reports correctly.

> **Opinion.** Default to same-port simplicity for internal-only services. Move to separate-port when (a) the application port is exposed externally and the security separation matters, or (b) the application port is prone to overload and the orchestrator's response to overload-induced probe failures is making things worse. In production-grade services, separate-port is usually worth the extra configuration.

A hybrid pattern is common: a small HTTP server on a separate port for liveness only, the gRPC health service on the application port for readiness. Liveness needs to survive overload (and is binary: is the process responsive at all?); readiness reports the richer service-level status.

## Defining a startup probe correctly

C++ services often have non-trivial startup time — the static initialization tax from Doc 06, plus process-scoped state construction from Doc 04, plus initial connection establishment from Doc 07. A service that takes 10 seconds to be ready and uses only a liveness probe with a 1-second timeout will get killed before it can start.

The startup probe pattern handles this. While the startup probe runs, neither the liveness nor the readiness probe runs; the orchestrator gives the service time to come up. Once startup succeeds (typically when the gRPC server reports server-wide SERVING), the other probes take over.

Kubernetes configuration:

```yaml
startupProbe:
  grpc:
    port: 50051
  failureThreshold: 30        # 30 attempts
  periodSeconds: 2            # every 2s
  # Total: up to 60 seconds for startup
```

Podman's equivalent uses the `--start-period` option on the `HEALTHCHECK` directive — during the start period, failures are not counted against the retries budget.

The application-side pattern uses the staged-startup approach from Doc 06: start the server with health reporting NOT_SERVING, do the expensive initialization, then flip health to SERVING when ready:

```cpp
int main(int argc, char** argv) {
    const Config config = parse_config(argc, argv);

    // 1. Stand up the gRPC server with health reporting NOT_SERVING.
    grpc::EnableDefaultHealthCheckService(true);
    grpc::ServerBuilder builder;
    builder.AddListeningPort(config.listen_addr,
                             grpc::InsecureServerCredentials());
    // Note: services must be registered before BuildAndStart.
    MyService service_placeholder{};  // skeleton; not yet wired
    builder.RegisterService(&service_placeholder);
    auto server = builder.BuildAndStart();

    auto* health = server->GetHealthCheckService();
    health->SetServingStatus("",
        grpc::health::v1::HealthCheckResponse::NOT_SERVING);

    // 2. The startup probe is hitting NOT_SERVING — orchestrator waits.

    // 3. Do the expensive initialization.
    auto provider  = build_tracer_provider(config);
    ChannelCache channels{config.channel_options};
    PgPool       pg{config.pg_conninfo, config.pg_pool_size};

    // 4. Wire the real service.
    service_placeholder.wire(channels, pg);  // pseudocode

    // 5. Flip health to SERVING — startup probe now succeeds.
    health->SetServingStatus("",
        grpc::health::v1::HealthCheckResponse::SERVING);
    health->SetServingStatus("my.package.MyService",
        grpc::health::v1::HealthCheckResponse::SERVING);

    install_signal_handler(server.get(), health);
    server->Wait();
}
```

The skeleton-then-wire pattern is awkward because gRPC's `ServerBuilder` requires service registration before `BuildAndStart()`. In practice, services often pre-construct dependency placeholders (default-constructed channel caches, pool with `expected_size=0`) and fill them in mid-startup. An alternative is to put the expensive work first and accept that the gRPC server doesn't bind to its port until the work is done — which loses the staged-startup property but is simpler. The right choice depends on whether the orchestrator's probe timing makes the simpler pattern feasible.

## Graceful shutdown

The orchestrator signals shutdown via SIGTERM. The container has `terminationGracePeriodSeconds` (default 30 in Kubernetes) before it gets SIGKILL'd. Inside that window, the service should drain in-flight work, refuse new work, and exit cleanly.

The graceful shutdown sequence ties together several patterns from earlier documents:

```cpp
void install_signal_handler(grpc::Server* server,
                            grpc::HealthCheckServiceInterface* health,
                            OutboxPoller* outbox,
                            std::stop_source* process_stop) {
    static grpc::Server* g_server  = server;
    static auto*         g_health  = health;
    static OutboxPoller* g_outbox  = outbox;
    static auto*         g_stop    = process_stop;

    std::signal(SIGTERM, [](int) {
        // 1. Flip readiness to NOT_SERVING. The orchestrator's next
        //    readiness probe sees this and stops routing new traffic.
        g_health->SetServingStatus("my.package.MyService",
            grpc::health::v1::HealthCheckResponse::NOT_SERVING);
        // Keep the server-wide ("") status as SERVING so liveness
        // does not fail during drain.

        // 2. Signal cooperative cancellation to background workers
        //    (outbox poller, async jobs). Doc 05's stop_token pattern.
        g_stop->request_stop();

        // 3. Ask gRPC to stop accepting new RPCs and to finish
        //    in-flight ones, up to a deadline.
        const auto drain_deadline =
            std::chrono::system_clock::now() + std::chrono::seconds{25};
        g_server->Shutdown(drain_deadline);
    });
}
```

The signal handler is intentionally minimal — signal handlers have severe restrictions on what they may call (no allocations, no thread synchronization that can deadlock against the signal-interrupted thread, no I/O beyond `write` to a known fd). The pattern above uses statics for state access; the operations performed are all safe-in-signal-context for gRPC's API.

The full sequence, end to end:

1. SIGTERM arrives at the container.
2. The signal handler flips the per-service health status to NOT_SERVING. The empty-name (server-wide) status remains SERVING.
3. The orchestrator's next readiness probe (typically within a few seconds) sees NOT_SERVING and removes the replica from the Service's endpoint set. New traffic stops arriving.
4. The signal handler signals `process_stop`. Background workers (the outbox poller from Doc 07, any async batch jobs) see `stop_requested()` and exit their loops via `std::jthread`'s cooperative cancellation. Doc 05's pattern applies directly.
5. The signal handler calls `server->Shutdown(deadline)`. gRPC stops accepting new RPCs and waits for in-flight RPCs to complete, up to the deadline.
6. `server->Wait()` returns when shutdown is complete.
7. Control returns to `main()`. Process-scoped state destructs in reverse construction order (Doc 04): the service, then the pools, then the channel cache, then the tracer provider (with a `ForceFlush` so in-flight spans get exported).
8. `main()` returns. Process exits with status 0.

The sequence respects each subsystem's contract: gRPC drains in-flight RPCs to deadline, the outbox poller finishes its current iteration before exiting, the pools close connections cleanly, the tracer flushes spans. The orchestrator sees a clean exit and proceeds with whatever it was doing (rolling deploy, scale-down, node drain).

Sizing `terminationGracePeriodSeconds` matters. The longest plausible in-flight RPC sets the lower bound; orchestrator policy (deploy speed, node drain time) sets the upper bound. 30 seconds is a reasonable default; latency-sensitive services with short RPCs can use less; services with long-running streaming RPCs need more.

> **Opinion.** Every C++ service should have this shutdown sequence wired up before it ships, not added during the first production incident. The pattern is mechanical, fits in 50 lines, and prevents an entire category of deploy-time data loss. Test it locally with `podman stop` (which sends SIGTERM with a default 10-second grace) before deploying.

## Health-check anti-patterns

Three patterns are worth naming because they show up often.

**Liveness checks that test downstream dependencies.** A liveness probe that fails when the database is unreachable causes restart-storms during database outages: every replica restarts, comes up, sees the database still down, restarts again. The orchestrator burns CPU on restarts; the database recovers slower because of the connection churn from restart-induced reconnects; the impact widens. The fix is to keep liveness narrow: process responsive, gRPC server accepting connections. Downstream issues belong in readiness or in metrics-driven alerts.

**Health checks that load the service.** A probe that runs a real query against the database, or that exercises the full request path, adds load proportional to the probe frequency. For a probe every 5 seconds across 100 replicas, that's 20 extra queries per second, every second. The check should be cheap — a no-op RPC, a "yes I'm here" response. The expensive question of "am I actually serving correctly?" belongs in synthetic monitoring run from outside the cluster, not in the orchestrator's probe loop.

**Probes too aggressive in timing.** A liveness probe with `periodSeconds: 1` and `failureThreshold: 1` will kill the service on a single failed probe — including transient network issues, GC pauses in coexisting languages, scheduler hiccups under CPU pressure. The Kubernetes defaults (`periodSeconds: 10`, `failureThreshold: 3`) are usually right; deviate down only with a specific reason and only on the liveness probe with care.

## Recommendation summary

Implement the gRPC standard health service using `grpc::EnableDefaultHealthCheckService` and the bundled `HealthCheckServiceInterface`. Set per-service status explicitly; keep the server-wide ("") status separate from per-service status.

Distinguish liveness from readiness. Liveness checks the server-wide status (process alive, gRPC accepting connections). Readiness checks per-service status (this application service can serve traffic). Liveness failures restart the container; readiness failures remove from traffic but preserve the replica.

For services with non-trivial startup time, configure a startup probe with permissive timeout. Use the staged-startup pattern: server up with status NOT_SERVING, do expensive initialization, flip to SERVING.

Wire the graceful-shutdown sequence: SIGTERM → flip readiness to NOT_SERVING → signal `std::stop_source` for background workers → `server->Shutdown(deadline)` → reverse-order destruction. Size `terminationGracePeriodSeconds` to the longest plausible in-flight RPC plus headroom.

Default to same-port health checks for simplicity. Move to separate-port (small HTTP endpoint for liveness, gRPC health service for readiness) when external exposure or overload-survival matters.

Keep liveness checks narrow. Do not let downstream-dependency health affect liveness. Use cheap probes (no-op RPCs, status reads), not load-generating ones.

Test the shutdown path locally with `podman stop` before deploying. The pattern is mechanical; getting it wrong is an unforced error.

## Cross-references

Doc 02 establishes the RAII discipline that underpins the destruction-order guarantees of the shutdown sequence.

Doc 04 covers process-scoped state, including the `main()`-owned wiring pattern that the shutdown sequence destroys in reverse.

Doc 05 covers `std::stop_token` and `std::jthread` cooperative cancellation, which the shutdown sequence uses to drain background workers cleanly.

Doc 06 covers the disposability factor of 12-factor, including the staged-startup pattern for cold-start latency and the destruction-order story for shutdown.

Doc 07 covers backing-service connection pools and the outbox poller, both of which participate in the graceful-shutdown sequence.

Doc 08 covers ephemeral filesystem, which interacts with restart semantics — shutdown should not depend on writes to ephemeral storage surviving.

Doc 10 (gRPC microservices) shows the complete `main()` wiring with health checks, signal handlers, and the shutdown sequence end-to-end.

Doc 11 (build tooling appendix) covers the gRPC, OTel, and signal-handling library dependencies via Conan.

## Annotated bibliography

**gRPC health checking protocol documentation (`grpc.io/docs/guides/health-checking/`).** The canonical reference for the standard health protocol. Short, direct, worth reading once. Linked from there is the `grpc_health_probe` repository on GitHub.

**Kubernetes documentation, "Configure Liveness, Readiness and Startup Probes" (`kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/`).** The reference for the three-probe model and the configuration syntax. The native gRPC probe support (Kubernetes 1.24+) is documented here.

**Podman `HEALTHCHECK` documentation.** The single-host counterpart to the Kubernetes probe model. Less feature-rich (one probe, not three) but the underlying semantics map cleanly.

**Geewax, *API Design Patterns*.** The chapters on standard methods cover the philosophical question of what kinds of operations belong as separate endpoints. The health-check protocol is a worked example.

**Iglberger, *C++ Software Design*.** The chapter on dependency injection applies to how the health service is wired through the application. The shutdown sequence is itself an exercise in reverse-order dependency teardown.

**"Building Low Latency Applications with C++".** The chapter on signal handling and graceful shutdown is the closest published reference for the C++ side of the shutdown sequence. The book leans toward custom signal-handling rather than gRPC-integrated patterns; the principles transfer.

**The Twelve-Factor App, Factor IX (Disposability).** The canonical statement that processes should be disposable: fast startup, graceful shutdown. Worth re-reading in conjunction with Doc 06.

**Linux signal-handling documentation, particularly `signal-safety(7)`.** The list of functions that are async-signal-safe. The signal handler shown above operates within these constraints; deviating from them invites deadlocks at shutdown.
