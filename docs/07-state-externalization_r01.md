# 07 — State Externalization Patterns in C++

## Thesis

Doc 01 set up the proposition: a stateless service cannot hold authoritative state in any single replica, because the orchestrator can kill that replica at any time. Doc 04 said: anything that needs to be replicated, persisted, or seen by other replicas lives externally. This document covers the C++ patterns for talking to that external state — connection pools (process-scoped), RAII checkout (request-scoped), idempotent operations, deadlines propagating to the backing service, retry strategy, and the patterns that get distributed-system consistency right when you need it (the Outbox pattern, in the C++ idiom).

The two main backing-service families are key-value stores (Redis, etcd, memcached) and relational databases (PostgreSQL, MySQL). Message buses (Kafka, NATS, RabbitMQ) and object stores (S3, MinIO) are also common but get brief coverage here; the patterns generalize. The C++ libraries are reasonable: redis-plus-plus for Redis, libpqxx for PostgreSQL, librdkafka for Kafka, the AWS C++ SDK for S3. None is perfect; all work.

The C++-specific concerns are: every network call may throw or hang, connection state may be unusable after failure, deadlines need to propagate from the inbound gRPC request through to the backing service, and exception safety has to span the network boundary. RAII handles most of this; the rest is discipline.

## What externalizes, and what doesn't

The State Architecture Table from Doc 04 has a third column — "external" — that this document develops. The state categories that belong there:

Authoritative session state — user sessions, login state, anything a client expects to persist across replicas or restarts. User-visible counters and rate-limit windows — a counter consistent across replicas cannot live in any one process. Durable workflow state — pending background work, scheduled jobs, multi-step transactions. Queue items between services — messages waiting to be processed, dead-letter queues, retry queues. Authoritative business data — users, orders, inventory, transactions, audit logs.

What doesn't externalize — what stays process-scoped (Doc 04) — is anything best-effort and replica-local: a small in-process cache fronting authoritative lookups, computed-once derived values, parsed configuration, JIT-compiled artifacts. The rule is the one from Doc 04's opinion callout: if the next request needs to see this, it externalizes regardless of cost.

The cost of externalization is real. A Redis lookup at the same network proximity is single-digit milliseconds; a cross-AZ database query is tens of milliseconds; a remote API call is hundreds. Caching in front of these is fine and often necessary — best-effort, with explicit invalidation, never authoritative.

## Connection pools as process-scoped infrastructure

Every backing service is reached via one or more long-lived connections. Constructing a connection is expensive — TCP handshake, TLS handshake, authentication exchange, schema negotiation. Reusing connections across many requests is essential. The pattern is a process-scoped pool that hands out RAII checkout to handlers and reclaims on destruction.

Sizing the pool is a function of the memory budget (Doc 04) and the expected concurrency. A pool of N connections supports N concurrent operations against the backing service; if the handler concurrency exceeds N, additional handlers wait at checkout. The right N is "enough that checkout wait is rare under expected load, not so large that idle connections waste memory."

A Redis pool with redis-plus-plus:

```cpp
sw::redis::ConnectionPoolOptions redis_pool_opts;
redis_pool_opts.size                = config.redis_pool_size;        // e.g. 16
redis_pool_opts.wait_timeout        = std::chrono::milliseconds{50};
redis_pool_opts.connection_lifetime = std::chrono::minutes{30};

sw::redis::ConnectionOptions redis_conn_opts;
redis_conn_opts.host            = config.redis_host;
redis_conn_opts.port            = config.redis_port;
redis_conn_opts.socket_timeout  = std::chrono::milliseconds{200};
redis_conn_opts.connect_timeout = std::chrono::milliseconds{500};

sw::redis::Redis redis{redis_conn_opts, redis_pool_opts};
```

The `Redis` object is the pool — it owns the underlying connections, hands them out on operations, returns them on completion. Operations go through the pool transparently: `redis.set(...)`, `redis.get(...)`, `redis.incr(...)` each check out a connection, run the command, return the connection. For multi-step operations (transactions, pipelines), explicit checkout is needed; the section below on the Outbox pattern shows that case.

PostgreSQL via libpqxx is less polished. libpqxx ships a `pqxx::connection` type representing a single connection; it does not ship a built-in pool. Users either write a pool wrapper or use a library that provides one. A small pool wrapper is straightforward:

```cpp
class PgPool {
public:
    PgPool(std::string conninfo, std::size_t size)
        : conninfo_{std::move(conninfo)} {
        for (std::size_t i = 0; i < size; ++i) {
            free_.push_back(std::make_unique<pqxx::connection>(conninfo_));
        }
    }

    class ScopedConn {
    public:
        pqxx::connection* operator->() noexcept { return conn_.get(); }
        pqxx::connection& operator*()  noexcept { return *conn_; }

        ~ScopedConn() noexcept {
            if (conn_) pool_->release(std::move(conn_), valid_);
        }

        // Mark this connection unusable; pool will discard on release.
        void invalidate() noexcept { valid_ = false; }

        ScopedConn(const ScopedConn&)            = delete;
        ScopedConn& operator=(const ScopedConn&) = delete;
        ScopedConn(ScopedConn&&)                 = default;
        ScopedConn& operator=(ScopedConn&&)      = default;

    private:
        friend class PgPool;
        ScopedConn(PgPool* pool, std::unique_ptr<pqxx::connection> conn)
            : pool_{pool}, conn_{std::move(conn)} {}

        PgPool*                            pool_;
        std::unique_ptr<pqxx::connection>  conn_;
        bool                               valid_{true};
    };

    ScopedConn acquire(std::chrono::milliseconds wait_timeout);

private:
    void release(std::unique_ptr<pqxx::connection> conn, bool valid) noexcept;

    std::string                                       conninfo_;
    std::mutex                                        mtx_;
    std::condition_variable                           cv_;
    std::deque<std::unique_ptr<pqxx::connection>>     free_;
};
```

The `ScopedConn` is the RAII type that Doc 02's counterexample fix referred to. Acquisition is via the pool method; release happens automatically on destruction. The `invalidate()` method lets the handler mark the connection as unusable — for example, after a connection-reset error — so the pool drops it on release rather than returning it to the free list.

The alternative to writing this is to run PgBouncer or pgcat as an external pooler: the C++ service opens a small number of "real" connections to the pooler, the pooler multiplexes them onto many "logical" connections to PostgreSQL. This works well operationally; the in-process pool is still useful for non-blocking checkout semantics and for unit testing without an external pooler.

> **Opinion.** For PostgreSQL access in production, run PgBouncer in front of the database and a small in-process pool inside the service. The combination handles both the operational concerns — connection limits on the database, transaction-mode pooling for short queries — and the C++ concerns — RAII checkout, exception safety, deadline propagation.

## `ScopedConnection` and exception safety

The mid-handler invalidation matters because network errors leave the connection in an indeterminate state. A query that timed out may have committed at the database, or not. A connection reset between the request and response may have produced a half-state at the backing service. The safe thing is to discard the connection and let the pool replace it:

```cpp
grpc::Status MyService::FetchUser(const Request* req, Response* resp) {
    auto conn = pg_pool_.acquire(std::chrono::milliseconds{50});

    try {
        pqxx::work tx{*conn};
        auto row = tx.exec_params1(
            "SELECT id, name, email FROM users WHERE id = $1",
            req->user_id());
        tx.commit();
        // ... populate resp from row ...
        return grpc::Status::OK;
    } catch (const pqxx::broken_connection&) {
        conn.invalidate();
        return grpc::Status{grpc::StatusCode::UNAVAILABLE, "db unavailable"};
    } catch (const pqxx::sql_error& e) {
        // SQL error is a logical failure; connection is still good.
        return grpc::Status{grpc::StatusCode::INTERNAL, e.what()};
    }
    // conn destructs here, returning to the pool unless invalidated.
}
```

Two different exception types, two different responses. `broken_connection` means the underlying socket is gone; invalidate. `sql_error` means the query failed but the connection is fine; let it return to the pool normally. The distinction matters: if every error invalidates, the pool churns and rebuilds connections needlessly; if no error invalidates, broken connections come back to the pool and the next handler hits the same failure.

> **Opinion.** Every pool's `ScopedConn` type should expose `invalidate()`. Every backing-service exception handler should distinguish "the connection is bad" from "the operation failed on a good connection." This is one of the patterns most likely to be wrong in a hastily-written service; getting it right pays back in stability.

## Idempotency keys

The 12-factor canon does not address it directly, but every networked operation needs an answer to "what happens if the client retries." For reads, retry is safe — same response, no side effects. For writes, retry can produce duplicates: a payment processed twice, an order created twice, a counter incremented twice.

Geewax's *API Design Patterns* develops the idempotency-key idiom: the client supplies a unique key with each write request; the server checks whether it has seen the key recently; if yes, returns the previous response; if no, processes the request and remembers the key for some TTL. The key TTL matches the client's retry window — typically minutes to hours.

The pattern in C++:

```cpp
grpc::Status MyService::CreateOrder(
    grpc::CallbackServerContext* ctx,
    const CreateOrderRequest* req,
    OrderResponse* resp) {

    const auto& key = req->idempotency_key();
    if (key.empty()) {
        return grpc::Status{grpc::StatusCode::INVALID_ARGUMENT,
                            "idempotency_key required"};
    }

    // Check whether we've seen this key.
    if (auto cached = redis_.get("idemp:" + key); cached) {
        // Already processed; return the cached response.
        resp->ParseFromString(*cached);
        return grpc::Status::OK;
    }

    // Process the request.
    auto order = create_order_in_db(req);
    resp->set_order_id(order.id);

    // Cache the response for the key, with TTL.
    std::string serialized;
    resp->SerializeToString(&serialized);
    redis_.set("idemp:" + key, serialized, std::chrono::minutes{30});

    return grpc::Status::OK;
}
```

The cache write happens after the database commit — the order of operations matters. If the cache write fails, the database state is correct and a retry will succeed: it will hit the database again, find the row exists, and either return the existing order or return a conflict depending on the schema. If the cache write happened first and the database commit failed, a retry would falsely return success for an order that doesn't exist.

The cache here is authoritative for the deduplication question. This is a legitimate use of external state — the idempotency window is shared across replicas, so the cache cannot be process-scoped.

A subtlety: the "check then process" sequence above has a race window. Two concurrent retries with the same key can both pass the cache check and both create orders. The defenses are layered: a unique constraint on `(idempotency_key, customer_id)` in the orders table catches the race at commit time; the handler treats the unique-violation error as success-with-pre-existing. For most workloads this is sufficient; for higher-stakes domains, a distributed lock or a database-level idempotency table replaces the cache check.

## Deadlines propagating to backing services

Doc 02's `RequestContext` carries a deadline from the inbound gRPC request. That deadline has to propagate to every backing-service call inside the handler, or the handler can outlive its caller's patience while waiting on a slow backend.

For Redis with redis-plus-plus, the connection-level `socket_timeout` provides a default. For per-call deadlines, the explicit pattern is to compute remaining time and apply it. For PostgreSQL with libpqxx, the deadline maps to PostgreSQL's `statement_timeout` session parameter, set per-query or per-transaction:

```cpp
pqxx::work tx{*conn};
const auto remaining_ms =
    std::chrono::duration_cast<std::chrono::milliseconds>(
        rc.deadline() - std::chrono::system_clock::now()).count();
if (remaining_ms <= 0) {
    throw grpc::Status{grpc::StatusCode::DEADLINE_EXCEEDED, "expired"};
}
tx.exec("SET LOCAL statement_timeout = " + std::to_string(remaining_ms));
auto row = tx.exec_params1(
    "SELECT id, name, email FROM users WHERE id = $1", req->user_id());
tx.commit();
```

The pattern is the same regardless of backing service: take the request's deadline, compute remaining time, apply it to the backing-service operation. If remaining time is non-positive, fail fast with `DEADLINE_EXCEEDED` rather than launching an operation that will be cancelled anyway.

For gRPC outbound calls — the service calling another gRPC service — `ClientContext::set_deadline` is the canonical mechanism. The deadline propagates automatically through the gRPC framing; the receiving service sees it on `ServerContext::deadline()`.

```cpp
grpc::ClientContext client_ctx;
client_ctx.set_deadline(rc.deadline());
// Optionally also propagate trace context.
auto status = upstream_stub_->SomeMethod(&client_ctx, req, &resp);
```

The pattern composes: deadline at the inbound RPC, into the request context, out through every backing-service call. The deadline budget shrinks as the handler does work; each downstream sees less remaining time than the one before it.

## Retry with backoff vs fail-fast

When a backing-service call fails, there are two reasonable responses: retry with backoff, or fail fast and let the caller decide. The choice depends on the failure type and the operation's idempotency.

Fail-fast is appropriate for deadline-exceeded errors (the caller wanted an answer by now), invalid-argument errors (retry will not help), authentication failures, and anything inside a request handler with a tight deadline budget.

Retry with backoff is appropriate for transient network errors (TCP reset, connection refused), 5xx-class HTTP responses, gRPC `UNAVAILABLE` or `RESOURCE_EXHAUSTED`, and any operation with an idempotency key that the receiver will dedupe correctly.

The standard retry pattern is exponential backoff with jitter — first retry after ~100 ms, then ~200 ms, then ~400 ms, each with random jitter to prevent retry storms. Cap the total retry budget at the deadline; never retry past the caller's patience.

```cpp
template <typename Op>
auto with_retries(Op op, const RequestContext& rc, int max_attempts = 3) {
    using namespace std::chrono;
    int attempt = 0;
    milliseconds delay{100};
    while (true) {
        try {
            return op();
        } catch (const Retriable&) {
            if (++attempt >= max_attempts) throw;
            const auto remaining = duration_cast<milliseconds>(
                rc.deadline() - system_clock::now());
            if (delay >= remaining) throw;  // no time left
            std::this_thread::sleep_for(delay + jitter(delay));
            delay *= 2;
        }
    }
}
```

The example uses `std::this_thread::sleep_for` for simplicity; a real implementation in a coroutine handler uses `co_await asio::steady_timer` or similar non-blocking wait (Doc 05). Either way, the retry budget is the request's remaining deadline; never sleep past it.

Circuit breakers are the next level up — if a downstream service is unhealthy, stop retrying and start failing fast for some window, then probe occasionally. The pattern is well-documented elsewhere; in C++ services, libraries like Hystrix-style implementations exist but the pattern is also easy to hand-roll with a per-upstream counter and timestamp.

> **Opinion.** Default to fail-fast inside request handlers, and retry at the *system* level — via the orchestrator's restart policies, via the client library's automatic retry, via gRPC's built-in retry policy. Handler-internal retries are sometimes right but easy to overdo; every retry inside a handler is a deadline budget being consumed in a place the caller can't see.

## The cache fix for Doc 01's counterexample

Doc 01 opened with a counterexample: a service that counted requests-per-user in a process-scoped `std::unordered_map`, which worked fine on one replica and broke on two. The fix is exactly the externalization pattern from this document — replace the in-memory map with a Redis lookup:

```cpp
// Doc 01's anti-pattern:
// static std::unordered_map<UserId, RequestCount> g_counts;
// std::lock_guard lock{mtx_};
// ++g_counts[user_id];

// Doc 07's fix:
grpc::Status MyService::Greet(
    grpc::CallbackServerContext* ctx,
    const GreetRequest* req,
    GreetResponse* resp) {

    const auto& user = req->user_id();
    auto count = redis_.incr("greet_count:" + user);
    redis_.expire("greet_count:" + user, std::chrono::hours{24});
    resp->set_message("Hello " + user + " (greeted " +
                      std::to_string(count) + " times today)");

    auto* reactor = ctx->DefaultReactor();
    reactor->Finish(grpc::Status::OK);
    return reactor;
}
```

The `INCR` is atomic at Redis. The counter is correct across replicas because every replica reads and writes the same Redis key. The TTL bounds the counter at 24 hours, which matches the business semantics ("greeted today").

This is a tiny example, but the shape is general. State that needs to be consistent across replicas lives in a backing service. The C++ code expresses the operation as a backing-service call. The pool, the connection lifecycle, the deadline, the retries — all the machinery in this document — exists to make those calls reliable and bounded in time.

## The Outbox pattern, briefly

A common consistency requirement: when handling a request, atomically (a) write to the database and (b) emit an event to a message bus. The naive approach — write the DB row, then publish the event — has a race: if the process dies between the two, the DB is updated but the event is lost. Reversing the order has the symmetric problem.

The Outbox pattern resolves this by writing both the DB row and the event to the *same* database, inside the *same* transaction. A separate process — or a background thread in the same service — reads the outbox table and publishes events from it, marking them as published once acknowledged by the message bus. The transactional guarantee of the database extends to event emission: either both the row and the outbox entry are committed, or neither is.

```cpp
grpc::Status MyService::CreateOrder(
    const CreateOrderRequest* req, OrderResponse* resp) {

    auto conn = pg_pool_.acquire(checkout_timeout_);
    pqxx::work tx{*conn};
    try {
        const auto order_id = insert_order(tx, *req);
        insert_outbox(tx, "OrderCreated", order_id, req->customer_id());
        tx.commit();
        resp->set_order_id(order_id);
        return grpc::Status::OK;
    } catch (const pqxx::broken_connection&) {
        conn.invalidate();
        return grpc::Status{grpc::StatusCode::UNAVAILABLE, "db unavailable"};
    }
}

// Elsewhere: an outbox poller that reads unprocessed rows and publishes.
void OutboxPoller::run(std::stop_token stop) {
    while (!stop.stop_requested()) {
        auto conn = pg_pool_.acquire(std::chrono::seconds{1});
        pqxx::work tx{*conn};
        auto rows = tx.exec(
            "SELECT id, event_type, payload FROM outbox "
            "WHERE published_at IS NULL "
            "ORDER BY created_at LIMIT 100 FOR UPDATE SKIP LOCKED");
        for (const auto& row : rows) {
            publish_to_kafka(row);
            tx.exec_params(
                "UPDATE outbox SET published_at = NOW() WHERE id = $1",
                row[0].as<int64_t>());
        }
        tx.commit();
        std::this_thread::sleep_for(std::chrono::milliseconds{100});
    }
}
```

The poller runs as a background `std::jthread` inside the service, or as a separate sidecar process. `FOR UPDATE SKIP LOCKED` ensures multiple poller replicas don't process the same row twice. The `published_at` timestamp marks completion.

The pattern has trade-offs — eventual consistency between DB and message bus, complexity of the poller, idempotency requirements on the consumer side — but it is the only correct answer for the "atomic DB write + event emission" requirement. The alternative — best-effort publish after commit — has a failure mode that is silent and hard to detect.

## Recommendation summary

Externalize anything that has to be consistent across replicas, durable, or authoritative. Process-scoped state is for caches and computed-once values; external state is for the rest.

Use a process-scoped connection pool per backing service, sized against the memory budget. Hand out per-handler connections via RAII checkout. Mark connections invalid on broken-connection errors; let SQL errors and other logical failures return the connection to the pool normally.

Propagate the inbound request's deadline to every backing-service call. Use `statement_timeout` for PostgreSQL, `socket_timeout` for Redis, `ClientContext::set_deadline` for outbound gRPC. Fail fast when remaining deadline is exhausted; do not launch doomed operations.

Retry with exponential backoff and jitter for transient errors. Cap the retry budget at the request deadline. Use idempotency keys on writes so retries are safe. Consider circuit breakers for unhealthy upstreams.

Use the Outbox pattern when an operation must atomically update the database and emit an event. Do not try to coordinate two separate writes without a transactional store between them.

For PostgreSQL specifically, run PgBouncer in front of the database with a small in-process pool inside the service. For Redis, redis-plus-plus's built-in pool is sufficient. For Kafka, librdkafka with the C++ wrapper is the standard.

## Cross-references

Doc 01 set up the externalization requirement with the request-count counterexample; this document closes the loop by showing the fix.

Doc 02 covers the RAII discipline that `ScopedConnection` participates in, and the exception-safety guarantees that hold across the network boundary.

Doc 04 covers the process-scoped pool as a category of state, with sizing guidance against the memory budget.

Doc 05 covers cooperative cancellation and `std::stop_token`, which compose with the deadline-propagation pattern for backing-service calls — the same machinery drives the outbox poller's graceful shutdown.

Doc 06 covers the 12-factor backing-services principle in philosophical terms; this document covers the C++ implementation.

Doc 09 covers graceful shutdown, including draining the outbox poller and closing pool connections cleanly.

Doc 10 (gRPC microservices) shows the pool, retry, and deadline-propagation patterns wired into a complete service.

Doc 11 (build tooling appendix) covers the Conan recipes for redis-plus-plus, libpqxx, librdkafka, and the AWS C++ SDK.

## Annotated bibliography

**Geewax, *API Design Patterns*.** The chapters on idempotency, on standard methods, and on long-running operations directly inform the idempotency-key and retry patterns above. The book is the strongest available reference on the cross-cutting concerns of API design that this document operationalizes in C++.

**Iglberger, *C++ Software Design*.** The Strategy chapter applies to swapping backing-service implementations (Redis vs etcd, PostgreSQL vs MySQL); the dependency-injection chapter applies to how pools are wired into handlers.

**"C++ High Performance" (2nd edition).** The chapter on network programming covers the socket-level concerns underneath the library wrappers. The book's treatment of latency under failure modes is useful background for the retry-with-backoff and circuit-breaker discussions.

**Yonts, *100 C++ Mistakes and How to Avoid Them*.** The entries on exception safety across resource boundaries, on RAII pitfalls, and on lifetime management of pooled resources are directly relevant.

**Enberg, *Latency*.** The book's framing of latency under failure, particularly the tail-latency consequences of poorly-tuned retry, applies directly to the retry-budget discussion above.

**redis-plus-plus documentation (`github.com/sewenew/redis-plus-plus`).** The library is well-documented; the pool configuration and transaction sections are worth reading once before designing the Redis access layer.

**libpqxx documentation (`pqxx.org`).** The connection and transaction documentation is essential; the lack of a built-in pool is mentioned explicitly. The `pqxx::work` transaction wrapper is the right default for most cases.

**Vaughn Vernon, *Implementing Domain-Driven Design*.** The Outbox pattern is covered in detail in the chapter on integrating bounded contexts. Not C++-specific but a strong reference for the pattern.

**PgBouncer documentation (`pgbouncer.org`).** Useful background for the operational concerns mentioned in the PostgreSQL section, particularly transaction-mode pooling.
