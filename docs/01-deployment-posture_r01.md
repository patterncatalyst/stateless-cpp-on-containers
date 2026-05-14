# 01 — Stateless vs Stateful as Deployment Posture, Not a Code Property

## Thesis

In a Kubernetes or container-orchestrator context, "stateless" and "stateful" are properties of the deployment, not of the code. The same C++ binary can be deployed as a stateless `Deployment` — interchangeable replicas, horizontal scale, rolling restarts without ceremony — or as a member of a `StatefulSet` with stable identity, ordered rollout, and a per-pod persistent volume claim. The C++ standard says nothing about either; the orchestrator's manifest decides. This single fact gets missed surprisingly often by C++ developers coming from monolithic server backgrounds, who tend to write code that assumes a long-lived, single-instance process — and then are surprised when their service either won't scale or loses data on restart.

This document establishes the vocabulary used throughout the rest of the set. Subsequent documents go deep on the C++ mechanics: RAII discipline (Doc 02), per-request arenas (Doc 03), the boundary between request-scoped and process-scoped state (Doc 04), the threading model that crosses both (Doc 05), the 12-factor implications for C++ (Doc 06), and so on. The point of starting here is that none of those mechanics matter if the architectural posture is wrong: a binary that *cannot* be deployed stateless will have its design problems amplified by every clever per-request optimization, not solved by them.

## What "stateless" actually means

The word is overloaded. Three meanings are worth distinguishing because conflating them is the source of most confusion.

The first is *no state at all*. A purely functional binary with no mutable storage anywhere. Rare in practice — virtually any non-trivial service has at least connection pools, caches, and observability state.

The second is *no state that depends on prior process-local interactions with this specific client*. A request can be served by any replica because the request itself carries — directly or via a token — everything needed. This is the meaning operative in cloud-native architecture and the one Kubernetes encodes in its `Deployment` primitive.

The third is *no state that depends on prior in-process activity at all*. A stronger condition: cold-start a replica and the very first request behaves exactly as the millionth would. Rarely achieved and rarely needed, because process-scoped state — connection pools, tracer providers, JIT-warmed code paths — is normal and acceptable as long as it is rebuildable from configuration on cold start.

The middle definition is the practical one. It permits an in-process cache as long as the cache is best-effort: a miss falls through to the authoritative store, and the cache going away with the process causes a transient performance dip, not a correctness violation. It rules out an in-process session table, because losing that table on restart causes user-visible failure.

A useful test: if a replica is killed and re-spawned with no warning, do clients notice anything other than (at worst) a small latency spike on the first few requests? If yes, the service is not stateless in this sense — regardless of what its developers say.

A note on vocabulary before going further: "container" is overloaded. In this document set, *std container* refers to `std::vector` and friends, *OS container* refers to an OCI-format unit run by Podman or Docker, and *Kubernetes container* refers to a container inside a Kubernetes pod. Where the meaning is obvious from context, the bare word is fine; where it could go either way, the docs are explicit.

## The orchestrator's line: posture in Podman and Kubernetes

The project's primary container stack is Podman with podman-compose. Kubernetes is noted where it materially differs. The architectural point — same binary, different posture — applies in both.

### Podman / podman-compose

Podman expresses posture implicitly through a handful of options on `podman run` or in `compose.yaml`. A service is stateless to the extent that none of these conditions hold:

A named volume is mounted that the service writes to (the volume survives container restart by design).

A bind mount targets a host path that the service writes to.

The container has a fixed `container_name` rather than being scaled.

Recreate-on-update behaviour is configured to preserve identity.

If none of those apply — the rootfs is `read_only: true`, any writable paths are `tmpfs:` mounts, and the service is scaled via replicas — the deployment is effectively stateless, and any number of replicas can be spun up or down without coordination. Podman doesn't formalize "stateless vs stateful" as separate object types; the posture is the sum of these implicit choices.

There is no Podman analogue to a Kubernetes `StatefulSet`. Podman pods (the OCI/k8s pod concept, supported via `podman pod`) group containers by network namespace but don't add ordinal identity or per-pod persistent volumes. Workloads that genuinely need ordinal identity tend to outgrow podman-compose for production and end up on Kubernetes, where `podman generate kube` is the convenient bridge.

### Kubernetes (where it differs)

Kubernetes formalizes the distinction in two API objects.

A `Deployment` manages a `ReplicaSet`, which in turn manages a pool of pods. Pods are interchangeable. Their names contain a hash and an ordinal, but the names are not stable — a rescheduled pod gets a new name. Pods do not have stable DNS records unless you build them via service discovery yourself. Persistent volumes, if any, are shared or absent. Rolling updates create new pods and delete old ones; ordering between pods is not guaranteed. Scaling is symmetric and instantaneous from the orchestrator's perspective: `kubectl scale --replicas=20` on a 10-replica Deployment means ten new identical pods will be scheduled with no coordination required.

A `StatefulSet`, by contrast, gives each pod a stable, ordinal identity. Pod names are `<name>-0`, `<name>-1`, `<name>-2`. Each pod gets its own persistent volume claim bound by ordinal: `data-<name>-0` always attaches to pod `<name>-0` even after rescheduling. DNS records are stable; `<name>-0.<service>.<namespace>.svc.cluster.local` resolves to whichever pod currently holds that ordinal. Pods are created in order (`-0` first, then `-1`, then `-2`) and deleted in reverse order. This is intended for workloads where pods are not interchangeable: primary/replica databases, quorum-based systems like etcd or ZooKeeper, sharded services with affinity to ordinal-bound data.

The crucial observation, in either runtime, is that *this distinction is purely orchestrator-side*. The C++ binary in both cases is the same `main()`, the same gRPC service, the same handlers. What changes is the manifest. The binary may inspect its own pod or container name to learn its ordinal — set via Podman's `--hostname` or Kubernetes' downward API into an environment variable — and behave differently accordingly, but that adaptation is environment-driven, not baked in.

## Same binary, three manifests

Consider a small gRPC service. The intent is for it to be deployable into any of these postures. Here is the skeleton, in C++20:

```cpp
// service.cpp — same source compiles to a binary
// usable in either deployment posture.

#include <cstdlib>
#include <memory>
#include <string>
#include <string_view>
#include <grpcpp/grpcpp.h>
#include <grpcpp/health_check_service_interface.h>

#include "compute.grpc.pb.h"

namespace {

std::string env_or(std::string_view key, std::string_view fallback) {
    if (const char* v = std::getenv(key.data())) {
        return std::string{v};
    }
    return std::string{fallback};
}

class ComputeService final : public compute::Compute::CallbackService {
public:
    explicit ComputeService(std::string instance_id)
        : instance_id_{std::move(instance_id)} {}

    grpc::ServerUnaryReactor* Square(
        grpc::CallbackServerContext* ctx,
        const compute::SquareRequest* req,
        compute::SquareResponse* resp) override {
        // Pure function of the input. No process-local state read or written.
        resp->set_value(req->input() * req->input());
        resp->set_served_by(instance_id_);
        auto* reactor = ctx->DefaultReactor();
        reactor->Finish(grpc::Status::OK);
        return reactor;
    }

private:
    std::string instance_id_;
};

}  // namespace

int main() {
    // INSTANCE_ID is whatever the orchestrator chose to call us — pod name
    // under Kubernetes (via downward API), container name or hostname under
    // Podman. Falls back to a sentinel for bare local runs.
    const auto instance_id = env_or("INSTANCE_ID", "local-unknown");
    const auto listen      = env_or("LISTEN_ADDR", "0.0.0.0:50051");

    grpc::EnableDefaultHealthCheckService(true);

    ComputeService service{instance_id};

    grpc::ServerBuilder builder;
    builder.AddListeningPort(listen, grpc::InsecureServerCredentials());
    builder.RegisterService(&service);

    auto server = builder.BuildAndStart();
    server->Wait();
}
```

The handler is a pure function of its input. There is no per-client state, no in-process cache, no shared mutable data. The only process-local datum is `instance_id_`, which is informational, not behavioral — it is included in the response so callers can verify load distribution.

Three manifests follow. The first is `compose.yaml` for podman-compose — the primary stack for local development and single-host deployment:

```yaml
# compose.yaml — podman-compose, stateless posture
services:
  compute:
    image: registry.example.com/compute:1.0.0
    read_only: true
    tmpfs:
      - /tmp
    deploy:
      replicas: 4
      resources:
        limits:
          cpus: "0.5"
          memory: 256M
        reservations:
          cpus: "0.25"
          memory: 128M
    environment:
      INSTANCE_ID: "${HOSTNAME}"
      LISTEN_ADDR: "0.0.0.0:50051"
    ports:
      - "50051"
    healthcheck:
      test: ["CMD", "/bin/grpc_health_probe", "-addr=:50051"]
      interval: 5s
      timeout: 2s
      retries: 3
      start_period: 2s
```

The `read_only: true` flag makes the rootfs immutable so accidental writes fail loudly rather than silently disappearing on restart (Doc 08 develops this). The `tmpfs` mount provides a legitimately-writable `/tmp`. The `deploy.resources` block sets the OS container's CPU and memory budget, which the C++ side must respect when sizing process-scoped state — connection pools, in-process caches, the PMR upstream resource (Doc 04 covers this in detail). The `healthcheck` uses the bundled `grpc_health_probe` binary against the standard gRPC health protocol (Doc 09).

For Kubernetes, the same image runs under either a `Deployment` (stateless scaling) or a `StatefulSet` (ordinal identity). A `Deployment` manifest:

```yaml
# deployment.yaml — Kubernetes, stateless posture
apiVersion: apps/v1
kind: Deployment
metadata:
  name: compute
spec:
  replicas: 4
  selector:
    matchLabels: { app: compute }
  template:
    metadata:
      labels: { app: compute }
    spec:
      containers:
        - name: compute
          image: registry.example.com/compute:1.0.0
          ports:
            - containerPort: 50051
          env:
            - name: INSTANCE_ID
              valueFrom:
                fieldRef: { fieldPath: metadata.name }
            - name: LISTEN_ADDR
              value: "0.0.0.0:50051"
          resources:
            requests:
              cpu: "250m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "256Mi"
          securityContext:
            readOnlyRootFilesystem: true
          volumeMounts:
            - name: tmp
              mountPath: /tmp
          readinessProbe:
            grpc: { port: 50051 }
            initialDelaySeconds: 1
          livenessProbe:
            grpc: { port: 50051 }
            initialDelaySeconds: 5
      volumes:
        - name: tmp
          emptyDir: { medium: Memory }
```

The Kubernetes manifest expresses the same three things — read-only rootfs, RAM-backed `/tmp`, CPU/memory budget — using its own vocabulary. `readOnlyRootFilesystem` mirrors Podman's `read_only: true`. `emptyDir: { medium: Memory }` is the analogue of Podman's `tmpfs:`. `resources.requests`/`limits` is the more granular cousin of compose's `deploy.resources`. Probes are richer than Podman's single-check model — startup, liveness, and readiness are separate, and gRPC-native probes (since Kubernetes 1.24) avoid needing `grpc_health_probe` as an exec.

A `StatefulSet`, for the case where the instance ordinal is meaningful — for example, a sharded computation where input keys hash to ordinals:

```yaml
# statefulset.yaml — Kubernetes, stateful posture (illustrative)
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: compute
spec:
  serviceName: compute
  replicas: 4
  selector:
    matchLabels: { app: compute }
  template:
    metadata:
      labels: { app: compute }
    spec:
      containers:
        - name: compute
          image: registry.example.com/compute:1.0.0
          ports:
            - containerPort: 50051
          env:
            - name: INSTANCE_ID
              valueFrom:
                fieldRef: { fieldPath: metadata.name }
            # The binary can read its ordinal from INSTANCE_ID's suffix.
            - name: LISTEN_ADDR
              value: "0.0.0.0:50051"
          resources:
            requests: { cpu: "250m", memory: "128Mi" }
            limits:   { cpu: "500m", memory: "256Mi" }
          readinessProbe:
            grpc: { port: 50051 }
          livenessProbe:
            grpc: { port: 50051 }
```

The binary is identical across all three manifests. The deployment posture differs entirely in the orchestration. Under podman-compose with `replicas: 4`, `INSTANCE_ID` comes from the hostname Podman assigns and is hash-suffixed. Under a Kubernetes `Deployment`, it comes from `metadata.name` and is also hash-suffixed. Under a `StatefulSet`, it ends with a stable ordinal (`compute-0`, `compute-1`, …) that the binary could parse and use for sharding. Whether that adaptation lives in code is a design decision; the point here is that the C++ side is parameterized over what role this instance plays, not hard-coded to one or the other.

> **Opinion.** A well-designed C++ service should default to deployment-agnostic: write code that does the right thing under podman-compose or a Kubernetes `Deployment`, and bring in `StatefulSet`-specific logic only when there is a concrete reason — sharding, leader election, persistent local state that must survive restart. Defaulting to a `StatefulSet` posture "to be safe" is a common anti-pattern. It costs you horizontal scaling agility and introduces ordering constraints you have not asked for.

## A counterexample worth recognizing

Some code is silently incompatible with stateless deployment, and the failure mode is rarely a crash — it's a correctness drift that only shows up under load or after a rolling deploy. Consider this innocuous-looking handler:

```cpp
// Counterexample — looks fine, isn't stateless-safe.

class GreeterService final : public greeter::Greeter::CallbackService {
public:
    grpc::ServerUnaryReactor* Hello(
        grpc::CallbackServerContext* ctx,
        const greeter::HelloRequest* req,
        greeter::HelloResponse* resp) override {

        const auto& name = req->name();

        // "Performance optimization": remember each user's last greeting time.
        {
            std::lock_guard lock{mtx_};
            auto& last = last_seen_[name];
            const auto now = std::chrono::steady_clock::now();
            const auto delta = now - last;
            last = now;
            if (delta < std::chrono::seconds{5}) {
                resp->set_greeting("Welcome back, " + name + "!");
            } else {
                resp->set_greeting("Hello, " + name + "!");
            }
        }

        auto* r = ctx->DefaultReactor();
        r->Finish(grpc::Status::OK);
        return r;
    }

private:
    std::mutex                                                          mtx_;
    std::unordered_map<std::string,
                       std::chrono::steady_clock::time_point>           last_seen_;
};
```

In a single-instance monolith, this works as written. Deployed under a 10-replica `Deployment` behind a load balancer, it does not: a client sending two requests one second apart is likely to hit two different replicas, neither of which sees the other's `last_seen_` entry, so both replicas say "Hello" rather than "Welcome back." The behaviour appears to work in development (single replica), drifts under load, and is essentially impossible to debug from logs because each replica's view is internally consistent.

The fix is not to remove the cache; it is to move it. A Redis cache keyed by user name, with a five-second TTL, gives the same behaviour and is correct across replicas. Doc 07 develops this pattern. The point here is that the *code* did not change posture — the *deployment* changed posture, from one replica to many, and exposed an assumption the code had silently encoded.

## Vocabulary: scopes

The doc set leans heavily on a three-level distinction. Establishing it here so subsequent documents can use it without redefining.

**Request scope.** State that lives for exactly one inbound RPC or HTTP request. Examples: the parsed request message, intermediate computation, a per-request span context, a per-request PMR arena (Doc 03), a checked-out database connection. In gRPC C++, the `CallbackServerContext` or `ServerContext` lifetime defines this scope; the handler's stack frame and the request and response objects passed in are all request-scoped.

**Process scope.** State that lives for the lifetime of the process and rebuilds from configuration on cold start. Examples: the OpenTelemetry `TracerProvider`, the cache of gRPC `Channel` objects keyed by target, the Redis or PostgreSQL connection pool, the upstream `pmr::memory_resource` from which per-request arenas pull when they overflow, prepared statement caches, JIT-warmed branches in the CPU.

**Deploy-time scope.** Configuration and identity that come from the deployment manifest and are fixed for the life of this pod. Examples: the pod's name and namespace, the cluster name, the service version, the container image tag, environment variables that came from a `ConfigMap` or `Secret`. From C++'s perspective these arrive via `std::getenv`, mounted files, or command-line arguments at startup.

The three scopes have very different lifetimes and very different cost models. Request scope is cheap, plentiful, and short-lived — billions of these per pod lifetime is unremarkable. Process scope is moderately expensive to construct (a `Channel` is multiple TCP/HTTP2 setup roundtrips plus TLS handshake; a connection pool's first connection is similar) but constructed rarely — once per cold start. Deploy-time scope is fixed once and read.

The vocabulary becomes operationally important when you ask: "where should this datum live?" For each piece of state, the answer narrows the design space dramatically. An `unordered_map<UserId, SessionInfo>` shared across handlers is process-scoped, which means it does not survive pod restart and cannot be replicated across replicas — so if user-visible behavior depends on it, the design has a problem regardless of how well the C++ is written.

## "Behind every stateless service is a stateful one"

The frame, from the *Kubernetes Patterns* book (ch. 11), is one of the more useful framings in cloud-native architecture. A pool of stateless web servers, API gateways, or business-logic services is typically backed by some smaller number of stateful systems: a primary database, a Redis cluster, a Kafka cluster, an object store. The state has to live somewhere; the design choice is *where*, and the answer is almost always "not in this process."

For C++ services specifically, this resolves a question that comes up often in design review: "Should I cache this in-process for performance?" The answer is rarely "no, never," but the right answer is rarely "yes, in a `static` map" either. The right answer is usually: cache in-process as a best-effort optimization that misses to an authoritative external store, sized to fit within the pod's memory limit, with eviction that does not depend on any other replica's behavior. Subsequent documents (especially Docs 04 and 06) develop this pattern.

## Where monolith intuitions mislead

C++ developers from monolithic-server backgrounds — long-running daemons, dedicated hardware, single-instance processes — bring a set of assumptions that don't carry over to containerized deployment cleanly. Six recur often enough to be worth naming.

The first is that the process lives forever. In a monolith, `main()` is entered once on the morning of go-live and exited only when someone deploys a new version, typically months later. In Kubernetes, the kubelet may kill and restart your pod for any number of reasons: node draining, OOM kills, liveness-probe failures, voluntary disruption budgets, image updates, autoscaling churn, even node patching. Designing for "long-lived process" is designing for the rare case; the common case is a process that exits within hours or days.

The second is that disk writes persist. They do not. The container's root filesystem is an overlay that dies with the container. Anything written outside an explicitly mounted volume disappears. Doc 08 covers this in detail.

The third is that a `static` map is fine for caching. It works correctly in a single-instance monolith because there is exactly one process. It is at best a per-replica cache in a containerized deployment, and at worst a correctness bug if other replicas can see inconsistent data via it — exactly the failure mode in the counterexample above. The `static` is not the problem per se; the problem is the assumption that *the process is the right granularity* for the cache. Usually it is not. The right granularity is either the request (Doc 03) or the cluster (an external cache, Doc 07).

The fourth is that the singleton stores data. Singletons that hold pure configuration are usually fine. Singletons that hold *mutating* data — a session table, a counter, a queue of pending work — are essentially in-process state stores, and as such inherit all the problems of in-process state across replicas. Doc 06 discusses the singleton anti-pattern in detail.

The fifth is that the file is the log. In a monolith, you write logs to `/var/log/myservice/myservice.log` and a logrotate cron handles rotation. In a container, you write to `stdout` and the orchestrator collects, ships, and rotates. The 12-factor "logs as event streams" principle (Doc 06, Doc 08) is operationally enforced by the platform — fighting it leads to losing log data on restart.

The sixth is that the thread pool sizes itself to the box. On dedicated hardware, `std::thread::hardware_concurrency()` returned a number that matched what the OS would actually schedule. Under a container with CPU limits — `--cpus=0.5` in Podman, `resources.limits.cpu: "500m"` in Kubernetes — `hardware_concurrency()` still returns the host's CPU count, not the cgroup quota. A pool sized to the host count oversubscribes the actual budget, and the kernel's CFS throttling shows up as tail-latency spikes that are hard to attribute. The same gotcha applies to gRPC's default thread pool, to jemalloc/tcmalloc arena counts, and to any library that probes the CPU count at construction. The fix is to size pools explicitly from a `CPU_LIMIT` environment variable set by the orchestrator. Relatedly, `thread_local` storage was effectively request-local in many monolith server models (thread-per-request); in a thread-pool model under either runtime, `thread_local` is process-scoped and persists across requests that happen to share a thread. Doc 05 covers both gotchas in detail.

Each of these intuitions is correct in its original context. The point is not that the developer was wrong, but that the constraints have changed.

## Hybrid postures

Real systems are rarely purely stateless. Several common patterns blend the two.

A *leader-elected* service runs N replicas of a stateless binary, but at any moment one is "leader" — perhaps elected via etcd, ZooKeeper, or a Kubernetes lease. The leader does coordinated work; the followers either standby or share load on non-coordinated work. From the orchestrator's view this is typically a `Deployment` plus a lease, not a `StatefulSet`, because the pods are still interchangeable — any can become leader.

A *sharded cache* service runs N replicas where each owns a key range, by consistent hashing or static assignment. Requests are routed to the replica that owns the relevant key. This often appears as a `StatefulSet` because clients need stable DNS to address a specific shard, but the pods are otherwise stateless within their shard — they reload from an authoritative store on startup.

A *primary-replica database* (Postgres with streaming replication, MySQL with binlog replication, Redis with sentinel) is unambiguously stateful and lives in a `StatefulSet`.

For a typical microservice in the user's stack — gRPC service, OTel-instrumented, observed via Grafana — the default posture is stateless: podman-compose with `read_only: true` and named replicas, or a Kubernetes `Deployment`. `StatefulSet` is reached for only when there is a concrete reason. The rest of this document set assumes the stateless default unless otherwise noted.

## Recommendation summary

A few practical guidelines that the rest of the doc set builds on.

Default to deployment-agnostic code that runs correctly under podman-compose or a Kubernetes `Deployment`. Treat `StatefulSet` as something to opt into for a specific reason.

Use the three-scope vocabulary — request, process, deploy-time — when reasoning about where any given piece of state should live. The right scope for a datum is the one with the shortest lifetime that still allows correct operation.

Treat in-process caches as performance optimizations that miss to an authoritative external store. Never treat them as authoritative.

Treat the OS container as ephemeral. The reasonable upper bound on its lifetime is hours. Design accordingly — fast startup, fast graceful shutdown, no on-disk state that you would miss.

Use the orchestrator's primitives — environment variables, Podman `--hostname` or the Kubernetes downward API, env files or ConfigMaps/Secrets, healthchecks or probes — as the C++ binary's interface to its environment. Subsequent docs cover each in detail.

Size process-scoped state — connection pools, in-process caches, prepared-statement caches, the PMR upstream resource — as an explicit fraction of the OS container's memory limit. An unbounded `std::unordered_map` reached for by reflex is a future OOM. Doc 04 develops this.

Choose `std::` containers deliberately, not by reflex. `std::vector` with a sized `reserve()` beats `std::deque`/`std::list` in nearly all handler paths. `std::flat_map` (C++23) beats `std::map` for small N. PMR variants beat default-allocator variants for per-request scratch. Docs 03 and 04 develop this.

Mind the cost of constructors and destructors in the hot path. RAII is correctness machinery, but the constants matter: trivially-destructible types cost nothing to destroy, PMR-allocated containers reclaim in one arena release rather than N node destructors, and `noexcept` move operations prevent silent copy fallback in standard containers. Doc 02 develops this.

Treat threading infrastructure — pools, executors, fiber schedulers — as process-scoped state, and treat thread-local storage as process-scoped rather than request-scoped. A `thread_local` variable used to carry a correlation ID across a handler will silently bleed across requests sharing a thread, and across `co_await` suspension points in a coroutine. Doc 05 develops this.

## Cross-references

Doc 02 develops the RAII discipline that keeps request-scoped state contained, and the performance side of construction and destruction in hot paths.

Doc 03 covers the PMR per-request arena pattern that gives request scope its operational efficiency, and the choice of `std::` container types in handlers.

Doc 04 expands the process-scoped state column of the architecture table and covers OS container requests/limits in detail, including the CPU-limit gotcha around `std::thread::hardware_concurrency()`.

Doc 05 covers threading and concurrency in a stateless service — thread pools, TLS as process-scoped state, stack-based vs stackless concurrency, gRPC's threading model, I/O waits, and cooperative cancellation.

Doc 06 examines what 12-factor means for C++ specifically — especially around config (compile-time vs runtime), the singleton problem, and startup cost.

Doc 07 covers externalization of state to Redis, PostgreSQL, and similar backing services, including the cache-fix for the counterexample above.

Doc 08 covers the ephemeral filesystem and why disk writes are different in containers, including Podman `read_only` and Kubernetes `readOnlyRootFilesystem`.

Doc 09 covers health checks — Podman `HEALTHCHECK` and Kubernetes probes — which are the orchestrator's view of whether your service is doing its job.

Doc 10 is the integrating gRPC C++ document where the patterns from all prior docs land in a single working service.

Doc 11 (build tooling appendix) covers the Conan and CMake setup behind the code samples in this and other documents.

## Annotated bibliography

**Iglberger, *C++ Software Design*.** Particularly the chapters on the Single-Responsibility Principle and on dependency management. The framing of "who owns this state, and what is its lifetime" is core to the request/process/deploy-time scope vocabulary used throughout this doc set. The Singleton chapter (covered more in Doc 06) is also relevant background here for the discussion of in-process state.

**Yonts, *100 C++ Mistakes and How to Avoid Them*.** The mistakes around static initialization order, raw-pointer ownership, and lifetimes inform the gotchas section above. Useful as a checklist when reviewing existing code for monolith-era assumptions; relevant entries cluster in the chapters on memory, lifetime, and modern C++ practice.

**Geewax, *API Design Patterns*.** The chapters on resource lifecycle, idempotency, and the standard methods — Get, List, Create, Update, Delete — frame the stateless-API contract that the C++ service is implementing. Worth re-reading once before designing any new RPC surface.

**"C++ High Performance" (2nd edition).** The chapters on memory layout and process-level concerns are useful background, though the book's focus is mostly on hot-path performance rather than deployment architecture. Will be cited more in Docs 03 and 04.

**Enberg, *Latency*.** Indirectly relevant here; will be cited more in Docs 03 and 06 where per-request arenas and external state access dominate the tail-latency picture.

**"Building Low Latency Applications with C++".** Background reference for the IO and concurrency chapters; will be cited more directly in Docs 06 and 09.

**Kubernetes Patterns (O'Reilly), ch. 11 "Stateful Service".** Not in the user's reference list but the framing "behind every stateless service is a stateful one" comes from here and is worth knowing for anyone working on cloud-native architecture.

**The Twelve-Factor App (`12factor.net`).** Factors VI (Processes — stateless) and IX (Disposability) are the canonical statements; Doc 06 examines what they mean concretely for C++.
