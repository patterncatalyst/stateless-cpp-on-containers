# 08 — The Ephemeral Filesystem Trap

## Thesis

The container filesystem is ephemeral. Anything written outside an explicitly-mounted volume — log files, crash dumps, cached compiled regexes, downloaded model weights, temporary parse trees — disappears when the container restarts. The kernel does not warn the application; the writes succeed, the file descriptors close cleanly, the data is simply gone on the next start. For a C++ developer coming from monolithic-server backgrounds, where `/var/log/myservice/` survived restarts and `/var/cache/` outlived process lifetimes, this is the assumption most likely to silently break in production.

The right framing is that ephemerality is a feature, not a bug. It is the operational counterpart to the architectural property the rest of this doc set has been describing: a stateless service must be killable and replaceable; its filesystem inherits the same property. The fix is not to fight the model — pinning the container to a node, mounting `/var/log` to a shared filesystem, adding handlers that snapshot to disk before exit — but to embrace it. Logs go to stdout. Temporary files go to a tmpfs that is sized and recycled correctly. Files that must persist go to explicitly-named volumes. The rootfs is read-only, enforced by `read_only: true` in compose or `readOnlyRootFilesystem: true` in Kubernetes, so accidental writes fail loudly.

This document covers the C++ patterns for getting that right: the container layer model that produces the ephemerality, the read-only-rootfs enforcement pattern, the specific C++ libraries that quietly write files in ways that will not survive a restart, the ephemeral-storage budget in Kubernetes, and the rootless Podman quirks that affect development workflows.

## The container layer model

A container image is built as a stack of read-only layers. At runtime, the container runtime — runc, crun, or similar — adds a writable layer on top, implemented via overlayfs. Reads come from the topmost layer that contains the file; writes go to the writable layer. When the container exits, the writable layer is removed. Image layers persist (they belong to the image, not the container instance); the writable layer does not.

This is true of Podman, Docker, Kubernetes' container runtimes, and any OCI-compliant implementation. The semantics are uniform. What varies is the syntax for enforcing read-only on the rootfs, for adding tmpfs mounts, and for adding persistent volumes.

For a Podman compose file:

```yaml
services:
  my-service:
    image: my-service:latest
    read_only: true              # rootfs is read-only
    tmpfs:
      - /tmp:size=64M,mode=1777  # writable /tmp with explicit size
      - /run:size=8M,mode=755    # writable /run for PID files etc.
    volumes:
      - cache-data:/var/cache/my-service   # persistent cache
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: "0.5"

volumes:
  cache-data:
```

The equivalent for Kubernetes:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: my-service
spec:
  containers:
  - name: my-service
    image: my-service:latest
    securityContext:
      readOnlyRootFilesystem: true
    volumeMounts:
    - name: tmp
      mountPath: /tmp
    - name: cache-data
      mountPath: /var/cache/my-service
    resources:
      limits:
        memory: 256Mi
        cpu: "500m"
        ephemeral-storage: 1Gi
      requests:
        ephemeral-storage: 256Mi
  volumes:
  - name: tmp
    emptyDir:
      medium: Memory             # tmpfs-backed, in memory
      sizeLimit: 64Mi
  - name: cache-data
    persistentVolumeClaim:
      claimName: my-service-cache
```

The two are semantically equivalent: rootfs read-only, `/tmp` writable as tmpfs with a size cap, a persistent volume mounted at a specific path. The Kubernetes side adds `ephemeral-storage` resource accounting, which has no direct Podman analogue.

The forcing function — turning on read-only rootfs from day one — is the most useful single configuration choice in this space. Without it, accidental writes to `/var/log/` or `/opt/cache/` succeed silently in development and disappear silently in production. With it, the same writes fail loudly with `EROFS`, which surfaces during testing and gets fixed before deploy.

> **Opinion.** `read_only: true` should be the default for every C++ service container. The cost is a small upfront audit of "what does my code actually write, and where"; the return is that the audit ever happens. Services that opt out of read-only rootfs accumulate silent dependencies on writable paths that nobody documented.

## What goes where

The clean mental model has three categories.

The rootfs is read-only. Anything baked into the image at build time lives here: the C++ binary, shared libraries, configuration templates, static assets, the protobuf-compiled descriptors. The runtime never writes to it.

A small tmpfs at `/tmp` (and possibly `/run`) is writable, in-memory, and recycled on restart. Anything genuinely temporary lives here: `std::tmpfile` output, in-flight protobuf serialization buffers when they need to spill, the gRPC channel's name-resolution scratch space, ML model deserialization intermediates. The size is bounded by the tmpfs `size=` parameter; under tight memory budgets, this counts against the container's RSS, so size it sparingly.

Persistent volumes mounted at specific paths are writable and survive restarts. Anything stateful that must outlive the container belongs here — a local SQLite database, a downloaded ML model file, a cached compiled regex set. The lifetime is decoupled from the container's; on restart, the new container sees the previous container's writes.

The lifetime contract for each category is explicit. The C++ code can rely on `/tmp` being writable and on the rootfs not being writable. Volume mount paths are part of the deployment contract — the service expects them, the deployment provides them, mismatches surface at startup as "permission denied" or "no such file."

## Logs go to stdout

The 12-factor logs-as-event-streams principle (Doc 06) is the clearest case where C++ instinct collides with container reality. The monolith pattern is to write structured logs to `/var/log/myservice/myservice.log` and let `logrotate` handle rotation. In a container with a read-only rootfs, that write fails. With a writable rootfs, the write succeeds but disappears on restart.

The fix is `stdout`. The container runtime captures the process's stdout and stderr, ships them to a logging driver (Podman's `journald` or `json-file` driver, Kubernetes' container-runtime-managed logs), which forwards to Loki or whatever aggregator the deployment uses. The application does not own log rotation; the orchestrator does.

For spdlog — the most common modern C++ logging library — the configuration looks like:

```cpp
#include <spdlog/spdlog.h>
#include <spdlog/sinks/stdout_color_sinks.h>

void configure_logging(spdlog::level::level_enum level) {
    auto sink = std::make_shared<spdlog::sinks::stdout_color_sink_mt>();
    auto logger = std::make_shared<spdlog::logger>("default", sink);
    logger->set_level(level);
    logger->set_pattern("{\"ts\":\"%Y-%m-%dT%H:%M:%S.%fZ\","
                        "\"level\":\"%l\",\"msg\":\"%v\"}");
    spdlog::set_default_logger(logger);
}
```

The stdout sink, the JSON pattern, the level from config. No file path, no rotation policy, no on-disk log directory. The orchestrator handles the rest.

A common variant in C++ services is to write to stderr for error-level logs and stdout for everything else. Most logging drivers preserve the distinction; some don't. The 12-factor canon says all logs to stdout; either is acceptable in practice, and stdout-only is simpler.

> **Opinion.** Configure structured JSON logging from day one. The Grafana stack — Loki for storage, Tempo for traces, Mimir for metrics — assumes structured input, and parsing free-form log lines after the fact is a significant time sink. spdlog with a JSON pattern, the OTel C++ SDK's log exporter, or a small custom sink against `nlohmann::json` all work. Pick one and standardize.

## The classic C++ traps

Several patterns common in C++ codebases produce writes that fail (under read-only rootfs) or vanish (without it). They are worth auditing for explicitly.

The first is **logging library defaults**. spdlog's `basic_logger_mt("name", "logs/log.txt")` writes to a file; the file path is relative to the working directory; the working directory inside a container is usually `/`. The first write fails with `EROFS`. Other logging libraries — glog, log4cplus, even custom wrappers around `std::ofstream` — have the same trap. Configure to stdout explicitly; do not rely on defaults.

The second is **crash dumps**. Code that installs a signal handler to write a stacktrace or core dump to `/var/crash/` fails under read-only rootfs. The fix is either to write to a mounted volume (acceptable but requires deployment cooperation) or to stream the stacktrace to stderr, where the logging pipeline picks it up. C++23's `<stacktrace>` makes the latter cleaner:

```cpp
void install_terminate_handler() {
    std::set_terminate([]() noexcept {
        auto st = std::stacktrace::current();
        std::cerr << "FATAL: terminating\n" << st << '\n';
        std::abort();
    });
}
```

The trace lands in the container log, gets shipped to Loki, is searchable from the same query interface as the rest of the logs. No on-disk crash directory needed.

The third is **ML model caches**. A service that loads a model from a remote bucket on first use and caches it locally typically writes to `/opt/cache/` or `~/.cache/`. Under read-only rootfs, the write fails. The fix depends on the model size: small models (under a few MB) can live in the image itself, baked at build time; medium models go in a persistent volume mounted at a known path; large models stay remote and are loaded into memory on each cold start, with the cold-start cost amortized over the long-lived process.

The fourth is **protobuf temporary files**. Some protobuf operations — large schema parses, certain reflection paths — spill to disk via `std::tmpfile` or platform-equivalent. As long as `/tmp` is mounted as tmpfs and sized adequately, this is fine; without an explicit `/tmp` tmpfs, it fails. The tmpfs is the standard mitigation.

The fifth is **coverage and profiling data**. Running tests inside a container with `--coverage` (gcov) writes `.gcda` files relative to the source directory. Inside the container, the source directory is wherever the build system put it, typically read-only because the image was built from those sources. The fix in CI is to mount a writable volume at the source path for the coverage run, or to bind-mount the host's source tree, or to override `GCOV_PREFIX`. None of this is hard, but it is the kind of thing that surprises the first time the CI run lands in a container.

The sixth is **prometheus_cpp metric snapshots**. The library can write a metric textfile to disk for the node_exporter textfile collector pattern. Useful in some deployment models, useless in container-native ones — Prometheus or the OTel collector should be scraping over the network instead. Disable the file output; expose `/metrics` over HTTP.

> **Opinion.** The audit for "where does this code write" is mechanical: `grep -rn 'ofstream\|tmpfile\|fopen\|::open' .` and review each hit. Most production C++ codebases discover one or two hits that nobody knew about. The cleanup pays back in deployment portability.

## Ephemeral storage budgets

Kubernetes treats the writable layer, `emptyDir` volumes, and container logs as a shared resource called "ephemeral storage." Pods can declare requests and limits on it like CPU and memory:

```yaml
resources:
  requests:
    ephemeral-storage: 256Mi
  limits:
    ephemeral-storage: 1Gi
```

When the limit is exceeded, the pod is evicted. The eviction is graceful — the pod gets a SIGTERM and a grace period — but it is still a restart, and persistent state in `emptyDir` is lost.

For a C++ service the common path to exceeding the budget is log volume. A service that writes 100 log lines per second at 200 bytes each produces about 1.6 GB per day of log output. Even if the orchestrator ships logs to Loki, the local copy is retained briefly (typically until it hits a per-file size limit and rotates), and rotated files accumulate against the ephemeral-storage budget until they're shipped and deleted.

The mitigations are structural: structured log levels (`info` in production, not `debug`), rate sampling on high-volume events (one in 100 cache-miss messages logged, all errors logged), and ensuring the logging driver ships aggressively enough that rotated files don't accumulate. Doc 11 covers Loki's promtail configuration for the Podman/compose side; Kubernetes uses Fluent Bit or similar.

Podman does not have a direct ephemeral-storage limit equivalent. The host filesystem is the limit, and runaway log volume on a development machine fills the disk. The mitigation is the same — structured levels and sampling — but the enforcement is host-level rather than per-container.

## Rootless Podman and the UID story

Podman supports rootless operation by default — the daemon (or daemonless equivalent) runs as the invoking user, and containers run with that user's UID by default. This has consequences for the filesystem story.

The container image typically declares a `USER` directive — `USER 1000` or `USER service`. When running rootless, Podman maps that UID inside the container to a different UID outside, via `/etc/subuid` and `/etc/subgid`. The mapping is automatic but the file ownership it produces can be surprising:

```bash
$ podman run --rm -v $PWD:/data -u 1000 alpine touch /data/file
$ ls -la file
-rw-r--r--  1 100999  100999  0 Nov  2 10:24 file
```

The file is owned by UID 100999 from the host's perspective — the rootless UID-map's offset of the container's UID 1000. From inside any future container with the same user mapping, it's owned by UID 1000. From the host shell, it looks weird.

For development workflows this affects bind mounts. Code edited on the host and bind-mounted into the container must have permissions that allow the in-container user to read or write it. Source code mounted read-only is straightforward; cache directories mounted writable need either the container UID to match the host UID (run as the developer, not as a service user) or explicit `chown` on container start.

For production this rarely matters — production images run as a fixed UID, persistent volumes are sized and permissioned at provisioning time, no host bind mounts exist. The rootless story is primarily a development concern.

Rootless Podman storage lives under `~/.local/share/containers/storage/` on the host. Image layers, container writable layers, volumes — all under there. The default volume driver creates volume directories with the appropriate UID mapping; bind mounts use the host paths directly. Knowing this is useful for cleanup (`podman system prune`) and for understanding where space goes.

## Detecting filesystem writes at startup

The cleanest detection is the operational one: run with `read_only: true` in development, watch for `EROFS` errors, fix each one. This catches everything mechanical writes can produce, and it catches it before deploy.

For an extra layer, a self-check at startup tests-writes to the paths the application uses, fails fast if any write fails unexpectedly, and logs a warning if a path that should be ephemeral is unexpectedly persistent:

```cpp
void verify_filesystem_assumptions() {
    // /tmp must be writable.
    {
        std::ofstream f{"/tmp/.startup_check"};
        if (!f) throw std::runtime_error("/tmp not writable");
        std::filesystem::remove("/tmp/.startup_check");
    }
    // The configured persistent volume must be writable.
    {
        const auto path = std::filesystem::path{config.cache_dir} /
                          ".startup_check";
        std::ofstream f{path};
        if (!f) throw std::runtime_error(config.cache_dir + " not writable");
        std::filesystem::remove(path);
    }
    // The rootfs should NOT be writable; warn if it is.
    {
        std::ofstream f{"/.rootfs_write_check"};
        if (f) {
            std::filesystem::remove("/.rootfs_write_check");
            log_warn("rootfs is writable; consider read_only: true");
        }
    }
}
```

The startup check runs after configuration parsing, before the gRPC server starts. Failures result in the process exiting with a clear error rather than discovering the misconfiguration mid-handler when a log write fails. The rootfs check is a soft warning rather than a hard error — some environments legitimately allow writable rootfs — but seeing it in the logs prompts an audit.

## A note on restart semantics

The data-loss-on-restart property is sometimes treated as a problem to mitigate. It is usually a property to embrace. A service whose state on disk is recoverable only because the same container instance was running for weeks has accumulated implicit state. A deploy, a node failure, a routine rolling restart will surface that state as missing data.

The healthy pattern is to assume restart is frequent and operationally cheap. State that matters lives in backing services (Doc 07). The container's writable storage is for genuinely transient things: in-flight scratch space, short-lived caches that can be rebuilt from the authoritative source, ephemeral diagnostics. Restart is then a non-event.

The exception is services that genuinely own local state — databases, queue brokers, cache servers. Those are run as StatefulSets in Kubernetes or with named volumes in Podman, with explicit volume management and explicit awareness that the container's identity matters across restarts. This is a different operational model; the rest of this doc set has been about stateless services that don't need it.

## Recommendation summary

Enable `read_only: true` (Podman compose) or `readOnlyRootFilesystem: true` (Kubernetes) from day one. Treat any write that fails as a bug in the application code, not a configuration to relax.

Mount `/tmp` (and `/run` if needed) as tmpfs, sized explicitly. Anything genuinely temporary goes there.

Mount named volumes only for things that must outlive the container. Be explicit about what those things are; document them.

Send all logs to stdout, structured JSON. Configure spdlog (or equivalent) at startup; never rely on default file paths. Set log levels via config, sample noisy events.

Write crash dumps and fatal stacktraces to stderr, not to files. C++23's `<stacktrace>` makes this straightforward.

In Kubernetes, set explicit `ephemeral-storage` requests and limits. Aim for sustained log volume well under the limit, with headroom for spikes. In Podman, monitor host disk usage instead.

For rootless Podman in development, plan for the UID-mapping consequences when bind-mounting host directories. In production, this rarely matters.

Run a small startup self-check that verifies the filesystem assumptions match reality. Fail fast on mismatch; warn on writable-rootfs misconfiguration.

## Cross-references

Doc 01 set up the deployment-posture vocabulary; this document operationalizes one of its implications (containers are interchangeable; their filesystem is too).

Doc 04 covers ephemeral-storage limits as a category of resource budget, including the Kubernetes accounting model.

Doc 06 covers the 12-factor logs-as-event-streams principle; this document covers the operational mechanics.

Doc 09 covers health checks and the restart semantics that depend on filesystem ephemerality being respected.

Doc 11 (build tooling appendix) covers the Conan recipes and CMake configuration for spdlog and the OTel logging exporter.

## Annotated bibliography

**The Twelve-Factor App, Factor XI (Logs).** The canonical statement that logs are event streams, not files. Short, direct, worth reading once.

**Kubernetes documentation, "Local Ephemeral Storage" (`kubernetes.io/docs/concepts/configuration/manage-resources-containers/#local-ephemeral-storage`).** The reference for the `ephemeral-storage` resource model, including how the writable layer, `emptyDir`, and container logs are accounted.

**Podman documentation on `--read-only`, `--tmpfs`, and rootless storage.** The single-host equivalent. The rootless-mode documentation under `podman.io/docs/rootless` covers the UID-mapping story in detail.

**spdlog documentation (`github.com/gabime/spdlog`).** The README and the wiki cover sink configuration, structured patterns, and the asynchronous logger options. The default examples use file sinks; the stdout sink is the right default for container deployments.

**OpenTelemetry C++ SDK documentation, "Logs" section.** The OTel logging exporter is an alternative to spdlog's stdout sink, sending logs directly to an OTLP endpoint. Tighter integration with traces and spans; slightly more complex setup. Either is acceptable.

**"Building Low Latency Applications with C++".** The chapter on persistent state covers the cases where local filesystem persistence is genuinely needed — typically database engines, queue brokers, embedded systems. Useful as the boundary case for when statelessness does not apply.

**Linux kernel documentation on overlayfs, tmpfs, and the various `mount(2)` options.** Background reading for the layer model and the tmpfs sizing.

**Iglberger, *C++ Software Design*.** The dependency-injection chapter applies to how logging and crash-handling subsystems are wired — configurable sinks rather than hardcoded file paths is the same pattern as configurable backing services in Doc 07.
