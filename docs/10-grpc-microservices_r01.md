# 10 — Microservices with gRPC and C++

## Thesis

This is the integration document. The prior nine established patterns in isolation: RAII for request scope (Doc 02), PMR arenas for per-request allocation (Doc 03), process-scoped state owned in `main()` (Doc 04), threading model (Doc 05), 12-factor adaptation (Doc 06), state externalization (Doc 07), ephemeral filesystem (Doc 08), health checks and shutdown (Doc 09). This document shows them composed in a realistic gRPC service — a small but plausible "order pricing" service that exercises every prior pattern in a single end-to-end implementation.

The shape: a service proto, a `Config` struct, a `RequestContext` type, a process-scoped wiring in `main()`, a handler that touches PostgreSQL, Redis, and an upstream gRPC service, the health-check and graceful-shutdown sequence, and a deployment wrapper in both `podman-compose.yaml` and Kubernetes manifest form. The code is C++20-portable; C++23 features called out where they meaningfully help. The implementations are sketches — production code adds more error handling and instrumentation — but they're complete enough that the composition is visible.

This document is heavier on code than the others by design. The point is to make the integration concrete, not to develop new ideas. Each block points back to the document that develops its pattern in detail.

## The example service

Take an order-pricing service. It accepts an order specification — customer ID and a list of line items — looks up customer metadata from PostgreSQL, fetches product prices from Redis (with cache-miss falling through to PostgreSQL), calls a tax-calculation service via gRPC, and returns a fully priced order. It supports idempotency via a client-supplied key, propagates deadlines through every downstream call, and exposes the standard gRPC health protocol.

The proto:

```proto
syntax = "proto3";
package pricing.v1;

service Pricing {
  rpc PriceOrder(PriceOrderRequest) returns (PriceOrderResponse);
}

message PriceOrderRequest {
  string idempotency_key = 1;
  string customer_id = 2;
  repeated LineItem line_items = 3;
  string correlation_id = 4;
}

message LineItem {
  string product_id = 1;
  int32  quantity = 2;
}

message PriceOrderResponse {
  string order_id = 1;
  int64  subtotal_cents = 2;
  int64  tax_cents = 3;
  int64  total_cents = 4;
}
```

The handler will be a callback-API implementation (Doc 05). The application logic is straightforward; what's worth showing is how the patterns from earlier compose around it.

## The Config struct

Doc 06 established the pattern: parse env-time configuration once in `main()` into an immutable struct, pass it by reference. For this service:

```cpp
struct Config {
    std::string                    listen_addr;            // LISTEN_ADDR
    std::string                    pg_conninfo;            // PG_CONNINFO
    std::size_t                    pg_pool_size;           // PG_POOL_SIZE
    std::string                    redis_uri;              // REDIS_URI
    std::size_t                    redis_pool_size;        // REDIS_POOL_SIZE
    std::string                    tax_service_addr;       // TAX_SERVICE_ADDR
    std::string                    otlp_endpoint;          // OTLP_ENDPOINT
    std::chrono::milliseconds      default_deadline;       // DEFAULT_DEADLINE
    std::size_t                    cpu_limit;              // CPU_LIMIT
    spdlog::level::level_enum      log_level;              // LOG_LEVEL
    std::string                    service_version;        // baked at build
};

Config parse_config(int argc, char** argv);  // implementation per Doc 06
```

Every subsystem receives `const Config&` (or the specific subsection it needs). No `std::getenv` calls outside `parse_config`.

## The `RequestContext`

The per-request RAII bundle from Doc 02, fleshed out with PMR (Doc 03) and OTel scope (Doc 05):

```cpp
#include <array>
#include <chrono>
#include <cstddef>
#include <memory_resource>
#include <string>

#include <grpcpp/grpcpp.h>
#include <opentelemetry/trace/provider.h>
#include <opentelemetry/trace/scope.h>

namespace otel = opentelemetry;

class RequestContext {
public:
    RequestContext(grpc::CallbackServerContext& grpc_ctx,
                   std::string correlation_id)
        : grpc_ctx_{grpc_ctx},
          correlation_id_{std::move(correlation_id)},
          deadline_{grpc_ctx.deadline()},
          monotonic_{arena_buffer_.data(), arena_buffer_.size()},
          pool_{pool_options(), &monotonic_},
          span_{tracer().StartSpan("PriceOrder",
                                   {{"correlation_id", correlation_id_}})},
          scope_{tracer().WithActiveSpan(span_)} {}

    RequestContext(const RequestContext&)            = delete;
    RequestContext& operator=(const RequestContext&) = delete;
    RequestContext(RequestContext&&)                 = delete;
    RequestContext& operator=(RequestContext&&)      = delete;
    ~RequestContext() noexcept = default;

    std::pmr::memory_resource* arena() noexcept { return &pool_; }
    std::chrono::system_clock::time_point deadline() const noexcept {
        return deadline_;
    }
    const std::string& correlation_id() const noexcept {
        return correlation_id_;
    }
    bool cancelled() const noexcept { return grpc_ctx_.IsCancelled(); }
    otel::trace::Span& span() noexcept { return *span_; }

private:
    static std::pmr::pool_options pool_options() noexcept {
        return {.max_blocks_per_chunk = 0, .largest_required_pool_block = 512};
    }
    static otel::trace::Tracer& tracer();  // process-scoped global

    grpc::CallbackServerContext&             grpc_ctx_;
    std::string                              correlation_id_;
    std::chrono::system_clock::time_point    deadline_;
    std::array<std::byte, 64 * 1024>         arena_buffer_;
    std::pmr::monotonic_buffer_resource      monotonic_;
    std::pmr::unsynchronized_pool_resource   pool_;
    otel::nostd::shared_ptr<otel::trace::Span> span_;
    otel::trace::Scope                       scope_;
};
```

The destruction order — scope first, span second, pool third, monotonic resource fourth — falls out of the member declaration order in reverse. The arena's no-op `do_deallocate` (Doc 03) means destroying the per-request `pmr::vector`s and `pmr::string`s inside the handler is essentially free; only the final monotonic-resource destruction reclaims the buffer.

The OTel `Scope` is the TLS guard from Doc 05 — constructing it makes the span active on the current thread, destructing it restores the prior active span. As long as the handler doesn't `co_await` (this example is synchronous), the active-span TLS is consistent throughout.

## Process-scoped state

The pieces from Doc 04 and Doc 07, named here so the wiring is concrete:

```cpp
class ChannelCache {
public:
    explicit ChannelCache(const grpc::ChannelArguments& args);

    // Returns a process-scoped channel for the target.
    // Channels are cached by target and reused across RPCs.
    std::shared_ptr<grpc::Channel> get_channel(std::string_view target);

private:
    grpc::ChannelArguments                                          args_;
    std::mutex                                                      mtx_;
    std::unordered_map<std::string,
                       std::shared_ptr<grpc::Channel>>              channels_;
};

class PgPool { /* as in Doc 07 */ };
// sw::redis::Redis from redis-plus-plus is its own pool already.
```

The `ChannelCache` is a thin wrapper around gRPC's `CreateChannel`. The mutex protects insertion; reads from a fully-populated cache are lock-free in practice because the map fits in a small fixed set of upstream targets known at startup. For services with dynamic upstream discovery, the same shape works with a more elaborate eviction policy.

## The handler

The application logic in callback-API form (Doc 05):

```cpp
grpc::ServerUnaryReactor* PricingService::PriceOrder(
    grpc::CallbackServerContext* ctx,
    const pricing::PriceOrderRequest* req,
    pricing::PriceOrderResponse* resp) {

    auto* reactor = ctx->DefaultReactor();

    try {
        RequestContext rc{*ctx, req->correlation_id()};

        if (req->idempotency_key().empty()) {
            reactor->Finish(grpc::Status{
                grpc::StatusCode::INVALID_ARGUMENT,
                "idempotency_key required"});
            return reactor;
        }

        // Idempotency cache check (Doc 07).
        if (auto cached = check_idempotency(rc, req->idempotency_key());
            cached) {
            *resp = std::move(*cached);
            reactor->Finish(grpc::Status::OK);
            return reactor;
        }

        // Fetch customer (PostgreSQL, deadline-propagated).
        auto customer = fetch_customer(rc, req->customer_id());

        // Fetch prices for each line item (Redis-cached, PG fallback).
        // Allocated from the request arena (Doc 03).
        std::pmr::vector<PricedItem> priced{rc.arena()};
        priced.reserve(req->line_items_size());
        for (const auto& item : req->line_items()) {
            priced.push_back(fetch_price(rc, item));
        }

        std::int64_t subtotal_cents = 0;
        for (const auto& p : priced) {
            subtotal_cents += p.unit_price_cents * p.quantity;
        }

        // Outbound gRPC to the tax service.
        const auto tax_cents =
            compute_tax(rc, customer, subtotal_cents);

        // Build response.
        resp->set_order_id(generate_order_id(rc.correlation_id()));
        resp->set_subtotal_cents(subtotal_cents);
        resp->set_tax_cents(tax_cents);
        resp->set_total_cents(subtotal_cents + tax_cents);

        // Store idempotency result for retry-safety.
        store_idempotency(rc, req->idempotency_key(), *resp);

        reactor->Finish(grpc::Status::OK);
    } catch (const grpc::Status& s) {
        reactor->Finish(s);
    } catch (const std::exception& e) {
        rc_span_record_exception(*ctx, e);
        reactor->Finish(grpc::Status{
            grpc::StatusCode::INTERNAL, e.what()});
    }
    return reactor;
}
```

Two things to notice. First, the `try`/`catch` is for error-to-status translation, not for resource cleanup — all resource cleanup happens through RAII (Doc 02). Second, the helper functions throw `grpc::Status` for protocol-level errors (deadline exceeded, unavailable) and `std::exception` for programming errors. The boundary translates both to the right wire-level status; the destructors run regardless.

## Helper: PostgreSQL access with deadline

The `fetch_customer` helper demonstrates the connection-pool checkout with deadline propagation (Doc 07):

```cpp
Customer PricingService::fetch_customer(
    RequestContext& rc, std::string_view customer_id) {

    auto conn = pg_pool_.acquire(std::chrono::milliseconds{50});

    const auto remaining = std::chrono::duration_cast<
        std::chrono::milliseconds>(
            rc.deadline() - std::chrono::system_clock::now()).count();
    if (remaining <= 0) {
        throw grpc::Status{grpc::StatusCode::DEADLINE_EXCEEDED,
                           "deadline exceeded before db query"};
    }

    try {
        pqxx::work tx{*conn};
        tx.exec("SET LOCAL statement_timeout = " + std::to_string(remaining));
        auto row = tx.exec_params1(
            "SELECT id, name, country, tax_exempt "
            "FROM customers WHERE id = $1",
            std::string{customer_id});
        tx.commit();

        return Customer{
            .id        = row[0].as<std::string>(),
            .name      = row[1].as<std::string>(),
            .country   = row[2].as<std::string>(),
            .tax_exempt = row[3].as<bool>(),
        };
    } catch (const pqxx::broken_connection&) {
        conn.invalidate();
        throw grpc::Status{grpc::StatusCode::UNAVAILABLE, "db unavailable"};
    } catch (const pqxx::sql_error& e) {
        // Connection is fine; query failed.
        throw grpc::Status{grpc::StatusCode::INTERNAL, e.what()};
    }
    // conn destructs here. Returned to the pool unless invalidate()d.
}
```

The pattern from Doc 07 is intact: deadline-based `statement_timeout`, `invalidate()` on broken connection, return-to-pool on the normal path or on SQL-error. The customer `struct` returned is a regular C++ value type — owned by the caller, lifetime separate from the connection.

## Helper: outbound gRPC with deadline and context propagation

The `compute_tax` helper demonstrates outbound gRPC, using the channel cache and propagating both deadline and trace context (Doc 04, Doc 07):

```cpp
std::int64_t PricingService::compute_tax(
    RequestContext& rc,
    const Customer& customer,
    std::int64_t subtotal_cents) {

    if (customer.tax_exempt) {
        rc.span().AddEvent("tax_exempt", {{"customer_id", customer.id}});
        return 0;
    }

    auto channel = channels_.get_channel(config_.tax_service_addr);
    auto stub    = tax::TaxService::NewStub(channel);

    grpc::ClientContext client_ctx;
    client_ctx.set_deadline(rc.deadline());
    propagate_trace_context(client_ctx);  // OTel context injection

    tax::CalculateTaxRequest treq;
    treq.set_country(customer.country);
    treq.set_subtotal_cents(subtotal_cents);

    tax::CalculateTaxResponse tresp;
    auto status = stub->CalculateTax(&client_ctx, treq, &tresp);
    if (!status.ok()) {
        throw grpc::Status{status.error_code(),
                           "tax service: " + status.error_message()};
    }
    return tresp.tax_cents();
}
```

The trace context propagation — `propagate_trace_context` — uses the OTel C++ SDK's metadata-injection helper to attach the current span's W3C TraceContext headers to the outbound RPC. The tax service sees them, links its own span to the caller's, and the trace is connected end-to-end. This is the OTel side of the per-request span the `RequestContext` constructs.

The channel comes from the process-scoped cache. Stub construction off the channel is cheap (a few hundred nanoseconds); the channel itself is the expensive process-scoped object that has been reused across thousands of RPCs.

## A note on threading

The handler shown above is synchronous-style: blocking `acquire()` on the PG pool, blocking `stub->CalculateTax()` on the outbound gRPC. Under the callback API (Doc 05), this means each in-flight `PriceOrder` occupies a gRPC thread for its full duration. For low-to-medium fan-in, this is acceptable; size the gRPC thread pool with headroom (Doc 05's pattern), and the service handles the expected load comfortably.

For higher fan-in, the same handler logic converts to coroutines via asio-grpc. The handler becomes an `asio::awaitable`; `pg_pool_.acquire_async()` and `stub->CalculateTax_async()` `co_await` their completion; the gRPC thread pool serves more concurrent calls because none are blocked on I/O. The TLS-across-`co_await` gotcha applies — the `RequestContext`'s OTel scope must be captured into the coroutine frame rather than relied upon via TLS after a suspension. Doc 05 covers the migration.

For this document, the sync version stays. The coroutine version is a mechanical refactor once the team is ready.

## Full `main()`

The wiring that ties everything together (Doc 04, Doc 06, Doc 09):

```cpp
int main(int argc, char** argv) try {
    const Config config = parse_config(argc, argv);

    // 1. Configure logging to stdout (Doc 08).
    configure_logging(config.log_level);

    // 2. Detect the CPU budget (Doc 05) and size pools from it.
    const double cpu_budget = cgroup_v2_cpu_limit().value_or(
        static_cast<double>(std::thread::hardware_concurrency()));
    const std::size_t cpu_pool = std::max<std::size_t>(
        1, static_cast<std::size_t>(std::floor(cpu_budget)));

    // 3. Configure the allocator's arena count from the CPU budget.
    //    Done before any allocator-using code runs in earnest.
    const std::string narenas = "narenas:" + std::to_string(2 * cpu_pool);
    setenv("MALLOC_CONF", narenas.c_str(), 1);  // jemalloc

    // 4. Construct the OTel TracerProvider.
    auto exporter  = OtlpGrpcExporterFactory::Create(otlp_options_from(config));
    auto processor = BatchSpanProcessorFactory::Create(
        std::move(exporter), batch_options_from(config));
    std::shared_ptr<otel::trace::TracerProvider> provider =
        TracerProviderFactory::Create(
            std::move(processor), resource_from(config));
    otel::trace::Provider::SetTracerProvider(provider);

    // 5. Construct process-scoped state (Doc 04).
    ChannelCache channels{channel_args_from(config)};

    PgPool pg{config.pg_conninfo, config.pg_pool_size};

    sw::redis::ConnectionPoolOptions redis_pool_opts;
    redis_pool_opts.size                = config.redis_pool_size;
    redis_pool_opts.wait_timeout        = std::chrono::milliseconds{50};
    redis_pool_opts.connection_lifetime = std::chrono::minutes{30};
    sw::redis::Redis redis{config.redis_uri, redis_pool_opts};

    // 6. Construct the service with explicit dependencies (Doc 06).
    PricingService service{config, channels, pg, redis};

    // 7. Build the gRPC server. Health initially NOT_SERVING (Doc 09).
    grpc::EnableDefaultHealthCheckService(true);

    grpc::ResourceQuota quota{"server_quota"};
    quota.SetMaxThreads(static_cast<int>(cpu_pool * 4));

    grpc::ServerBuilder builder;
    builder.SetResourceQuota(quota);
    builder.SetSyncServerOption(
        grpc::ServerBuilder::SyncServerOption::NUM_CQS,
        static_cast<int>(cpu_pool));
    builder.SetSyncServerOption(
        grpc::ServerBuilder::SyncServerOption::MIN_POLLERS,
        static_cast<int>(cpu_pool));
    builder.SetSyncServerOption(
        grpc::ServerBuilder::SyncServerOption::MAX_POLLERS,
        static_cast<int>(cpu_pool * 2));

    builder.AddListeningPort(config.listen_addr,
                             grpc::InsecureServerCredentials());
    builder.RegisterService(&service);

    auto server = builder.BuildAndStart();
    auto* health = server->GetHealthCheckService();

    // 8. Warm dependencies before flipping health to SERVING.
    health->SetServingStatus("",
        grpc::health::v1::HealthCheckResponse::NOT_SERVING);
    health->SetServingStatus("pricing.v1.Pricing",
        grpc::health::v1::HealthCheckResponse::NOT_SERVING);

    warm_pg_pool(pg);                 // open all connections
    warm_redis(redis);                // ping
    warm_channels(channels, config.tax_service_addr);  // resolve, connect, TLS

    health->SetServingStatus("",
        grpc::health::v1::HealthCheckResponse::SERVING);
    health->SetServingStatus("pricing.v1.Pricing",
        grpc::health::v1::HealthCheckResponse::SERVING);

    spdlog::info("pricing service ready on {}", config.listen_addr);

    // 9. Install signal handlers and start background workers.
    std::stop_source process_stop;
    install_signal_handler(server.get(), health, &process_stop);

    std::jthread outbox{[&](std::stop_token stop) {
        outbox_loop(pg, kafka_producer_, stop);
    }, process_stop.get_token()};

    // 10. Wait for shutdown.
    server->Wait();

    // 11. Flush traces before destructors run.
    if (auto* sdk =
            dynamic_cast<otel::sdk::trace::TracerProvider*>(provider.get())) {
        sdk->ForceFlush(std::chrono::seconds{5});
    }
    otel::trace::Provider::SetTracerProvider({});

    // 12. outbox jthread joins on destruction; pg, redis, channels destruct
    //     in reverse construction order. Return 0.
    return 0;
} catch (const std::exception& e) {
    spdlog::critical("fatal: {}", e.what());
    return 1;
}
```

Twelve numbered steps; every one of them maps back to a prior document. The structure is mechanical and the same shape applies to any C++ gRPC service in this stack — only the specific subsystems change.

The signal handler from Doc 09 is unchanged:

```cpp
void install_signal_handler(grpc::Server* server,
                            grpc::HealthCheckServiceInterface* health,
                            std::stop_source* process_stop) {
    static grpc::Server*                       g_server  = server;
    static grpc::HealthCheckServiceInterface*  g_health  = health;
    static std::stop_source*                   g_stop    = process_stop;

    std::signal(SIGTERM, [](int) {
        g_health->SetServingStatus("pricing.v1.Pricing",
            grpc::health::v1::HealthCheckResponse::NOT_SERVING);
        g_stop->request_stop();
        g_server->Shutdown(std::chrono::system_clock::now() +
                           std::chrono::seconds{25});
    });
    std::signal(SIGINT, std::signal(SIGTERM, SIG_DFL));  // re-bind SIGINT
}
```

## Compose deployment

The `podman-compose.yaml` wrapper, with read-only rootfs (Doc 08), resource limits (Doc 04), and the gRPC health check (Doc 09):

```yaml
services:
  pricing:
    image: pricing-service:latest
    read_only: true
    tmpfs:
      - /tmp:size=64M,mode=1777
    environment:
      LISTEN_ADDR: "0.0.0.0:50051"
      PG_CONNINFO: "host=pg port=5432 dbname=pricing user=svc"
      PG_POOL_SIZE: "16"
      REDIS_URI: "tcp://redis:6379"
      REDIS_POOL_SIZE: "8"
      TAX_SERVICE_ADDR: "tax:50051"
      OTLP_ENDPOINT: "http://otel-collector:4317"
      DEFAULT_DEADLINE: "2s"
      CPU_LIMIT: "2"
      LOG_LEVEL: "info"
      MALLOC_CONF: "narenas:4"
    healthcheck:
      test: ["CMD", "grpc_health_probe", "-addr=:50051"]
      interval: 5s
      timeout: 2s
      start_period: 30s
      retries: 3
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: "2"
        reservations:
          memory: 256M
    depends_on:
      - pg
      - redis
      - otel-collector
      - tax
    networks:
      - internal
```

The image binary depends on `grpc_health_probe` being on the `$PATH` inside the container — it's a tiny Go binary that the build adds to the image. The `start_period: 30s` matches the staged-startup window from `main()` (warm pools, connect channels, flip to SERVING).

## Kubernetes deployment

The equivalent Kubernetes resources — Deployment, Service, and the optional ConfigMap/Secret for credentials:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pricing
spec:
  replicas: 3
  selector:
    matchLabels: {app: pricing}
  template:
    metadata:
      labels: {app: pricing}
    spec:
      terminationGracePeriodSeconds: 30
      containers:
      - name: pricing
        image: pricing-service:latest
        securityContext:
          readOnlyRootFilesystem: true
          runAsNonRoot: true
          runAsUser: 1000
          allowPrivilegeEscalation: false
          capabilities:
            drop: ["ALL"]
        env:
        - name: CPU_LIMIT
          valueFrom:
            resourceFieldRef:
              resource: limits.cpu
              divisor: "1"
        - name: LISTEN_ADDR
          value: "0.0.0.0:50051"
        - name: PG_CONNINFO
          valueFrom:
            secretKeyRef: {name: pricing-secrets, key: pg_conninfo}
        - name: REDIS_URI
          value: "tcp://redis:6379"
        - name: TAX_SERVICE_ADDR
          value: "tax:50051"
        - name: OTLP_ENDPOINT
          value: "http://otel-collector:4317"
        - name: LOG_LEVEL
          value: "info"
        - name: MALLOC_CONF
          value: "narenas:4"
        ports:
        - containerPort: 50051
          name: grpc
        startupProbe:
          grpc: {port: 50051}
          failureThreshold: 30
          periodSeconds: 2
        livenessProbe:
          grpc: {port: 50051}
          periodSeconds: 10
          timeoutSeconds: 2
        readinessProbe:
          grpc:
            port: 50051
            service: pricing.v1.Pricing
          periodSeconds: 5
          timeoutSeconds: 2
        resources:
          requests:
            memory: 256Mi
            cpu: "1"
            ephemeral-storage: 100Mi
          limits:
            memory: 512Mi
            cpu: "2"
            ephemeral-storage: 1Gi
        volumeMounts:
        - name: tmp
          mountPath: /tmp
      volumes:
      - name: tmp
        emptyDir:
          medium: Memory
          sizeLimit: 64Mi
---
apiVersion: v1
kind: Service
metadata:
  name: pricing
spec:
  selector: {app: pricing}
  ports:
  - name: grpc
    port: 50051
    targetPort: 50051
```

The `CPU_LIMIT` environment variable is sourced from the container's own `resources.limits.cpu` via the `resourceFieldRef` mechanism — the Kubernetes downward API. The application then reads `$CPU_LIMIT` at startup rather than re-deriving from cgroups. This is the cleanest production wiring for the pattern from Doc 05; the orchestrator already knows the limit, so the application should not re-derive it.

The `securityContext` block is the Kubernetes counterpart to a hardened container — read-only rootfs, non-root user, no privilege escalation, all capabilities dropped. These are independent of statelessness but worth turning on by default for any production service.

## Recommendation summary

Organize the service code around the explicit composition shown here. `main()` constructs everything by name. `RequestContext` bundles request-scoped state. Helpers throw `grpc::Status` or `std::exception` for errors; the handler catches and translates.

The build produces a single binary. The image bundles the binary, the C++ runtime, the certificates, and `grpc_health_probe`. The image runs read-only with a tmpfs `/tmp` and no other writable paths.

The deployment pulls config from environment variables, sourced from the orchestrator (compose `environment:` or Kubernetes `env:` blocks with secret refs). `CPU_LIMIT` comes from the orchestrator's own resource limits via the downward API in Kubernetes or an explicit env var in compose.

Health checks use the gRPC standard protocol via `grpc_health_probe` (or Kubernetes' native gRPC probes). Startup probe permissive, liveness narrow, readiness fine-grained.

Graceful shutdown via signal handler: flip readiness to NOT_SERVING, signal `std::stop_source` for background workers, `server->Shutdown(deadline)`, reverse-order destruction in `main()`, exit clean.

This shape extends to every gRPC service in the stack. New services replace `PricingService` with their own; replace the helpers with their own; the surrounding wiring is unchanged.

## Cross-references

Doc 01 set up the deployment-posture vocabulary that this document operationalizes.

Doc 02 covers RAII discipline and the `RequestContext` pattern shown above.

Doc 03 covers PMR and the per-request arena in the `RequestContext` member.

Doc 04 covers process-scoped state and the `main()`-owned wiring pattern shown in the example.

Doc 05 covers the threading model and the cgroup-detection helper used in `main()`.

Doc 06 covers 12-factor adaptation and the `Config` struct pattern.

Doc 07 covers backing-service pools and the `fetch_customer`/`compute_tax` helper patterns.

Doc 08 covers the ephemeral filesystem and the `read_only: true` configuration.

Doc 09 covers health checks and the graceful-shutdown sequence at the bottom of `main()`.

Doc 11 (build tooling appendix) covers the Conan recipes for grpc, protobuf, opentelemetry-cpp, libpqxx, redis-plus-plus, spdlog, and the CMake configuration that builds the whole binary.

## Annotated bibliography

This document is the integration of the patterns developed in the prior nine; the bibliography there covers the underlying material. A few references are specifically relevant to gRPC C++ service design as a whole.

**gRPC C++ documentation, "Best Practices" (`grpc.io/docs/languages/cpp/best_practices/`).** The official guidance on channel reuse, threading models, deadline propagation, and the callback API. Worth reading once cover-to-cover; this document operationalizes most of its recommendations.

**Geewax, *API Design Patterns*.** The cross-cutting concerns chapters — error handling, idempotency, pagination, batching — are directly applicable to designing the service's RPCs. The proto shape and the error-translation pattern in the handler follow Geewax's framing.

**Iglberger, *C++ Software Design*.** The dependency-injection chapter is the architectural underpinning of the `main()`-owned wiring shown here. The strategy chapter applies to how the handler uses the channel cache, the pool, and the cache as injected collaborators rather than reached-for singletons.

**"C++ High Performance" (2nd edition).** The network programming chapter and the chapter on data structures inform the pool sizing, channel argument tuning, and request-scope allocation. The book's treatment of latency under failure is useful background for the deadline-propagation pattern.

**Kubernetes documentation, "Downward API for Pod Information".** The reference for the `resourceFieldRef` mechanism used to inject `CPU_LIMIT` into the container. Worth reading once for the full set of fields exposed (memory limits, node name, namespace).

**asio-grpc documentation (`github.com/Tradias/asio-grpc`).** The reference for the coroutine bridge from gRPC's `CompletionQueue` to Boost.Asio's executor model. Recommended reading when the synchronous handler shown here is migrated to coroutines.

**OpenTelemetry C++ SDK documentation (`opentelemetry.io/docs/instrumentation/cpp/`).** Reference for the tracer construction, span lifecycle, scope handling, and metadata propagation used throughout. The Otlp exporter documentation covers the endpoint configuration referenced in `parse_config`.
