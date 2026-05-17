#!/usr/bin/env python3
"""
generate.py — produce all per-section diagrams.

Run from this directory:
    python3 generate.py

Writes paired NN-title.excalidraw and NN-title.svg files in the current dir.
"""

from __future__ import annotations

import os
import sys

from diagram_lib import (
    Diagram, FONT_SANS, FONT_MONO,
    STROKE_DARK, WHITE,
    PROCESS_FILL, PROCESS_LINE,
    REQUEST_FILL, REQUEST_LINE,
    EXTERNAL_FILL, EXTERNAL_LINE,
    WARN_FILL, WARN_LINE,
    ACCENT_FILL, ACCENT_LINE,
)


# -----------------------------------------------------------------------------
# 01 — Stateless vs stateful as deployment posture
# -----------------------------------------------------------------------------

def diagram_01_deployment_posture() -> Diagram:
    d = Diagram(width=920, height=560,
                title="01 — Same Binary, Two Deployment Postures")

    # Common binary box at top center
    d.rect(380, 70, 160, 50, label="my-service\n(C++ binary)",
           fill=ACCENT_FILL, stroke=ACCENT_LINE, label_font_size=14)

    # Arrows down to two deployments
    d.arrow(440, 120, 230, 200, end="arrow")
    d.arrow(480, 120, 690, 200, end="arrow")

    # ---- STATEFUL (left) ----
    d.rect(60, 200, 340, 320, fill=WARN_FILL, stroke=WARN_LINE,
           stroke_width=2, dashed=True)
    d.text(230, 215, "STATEFUL DEPLOYMENT", font_size=14,
           align="center", color=WARN_LINE)
    d.text(230, 240, "(unsafe to kill)", font_size=12,
           align="center", color=WARN_LINE)

    d.rect(100, 270, 260, 40, label="static unordered_map cache",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)
    d.rect(100, 320, 260, 40, label="logs to /var/log/svc.log",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)
    d.rect(100, 370, 260, 40, label="session table in-process",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)
    d.rect(100, 420, 260, 40, label="persistent volume mounted",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)

    d.text(230, 490, "kill → data loss", font_size=14,
           align="center", color=WARN_LINE)

    # ---- STATELESS (right) ----
    d.rect(520, 200, 340, 320, fill=REQUEST_FILL, stroke=REQUEST_LINE,
           stroke_width=2, dashed=True)
    d.text(690, 215, "STATELESS DEPLOYMENT", font_size=14,
           align="center", color=REQUEST_LINE)
    d.text(690, 240, "(safe to kill, replace, scale)", font_size=12,
           align="center", color=REQUEST_LINE)

    d.rect(560, 270, 260, 40, label="Redis cache (external)",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)
    d.rect(560, 320, 260, 40, label="logs to stdout",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)
    d.rect(560, 370, 260, 40, label="sessions in PostgreSQL",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)
    d.rect(560, 420, 260, 40, label="read_only rootfs",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)

    d.text(690, 490, "kill → orchestrator restarts", font_size=14,
           align="center", color=REQUEST_LINE)
    return d


# -----------------------------------------------------------------------------
# 02 — RAII as the foundation: RequestContext lifetime
# -----------------------------------------------------------------------------

def diagram_02_raii() -> Diagram:
    d = Diagram(width=920, height=560,
                title="02 — RequestContext as Request-Scope RAII")

    # Handler stack frame outer
    d.rect(60, 70, 800, 440, fill=WHITE, stroke=STROKE_DARK,
           stroke_width=2, dashed=True, rounded=False)
    d.text(80, 95, "Handler stack frame", font_size=14, font=FONT_MONO,
           color=STROKE_DARK)

    # RequestContext box (request-scope green)
    d.rect(110, 130, 700, 320, fill=REQUEST_FILL, stroke=REQUEST_LINE,
           stroke_width=3)
    d.text(460, 155, "RequestContext  (request scope)", font_size=18,
           align="center", color=REQUEST_LINE)

    # Members shown as ordered boxes — construction order top-to-bottom,
    # destruction order is reverse (annotated with arrow).
    members = [
        ("grpc::CallbackServerContext& grpc_ctx", "stream/RPC handle"),
        ("std::string correlation_id", "request ID"),
        ("system_clock::time_point deadline", "from caller"),
        ("monotonic_buffer_resource arena", "64KB inline buffer"),
        ("nostd::shared_ptr<Span> span", "OTel span"),
        ("trace::Scope scope", "TLS guard — restores prior on dtor"),
    ]
    y = 190
    for name, note in members:
        d.rect(140, y, 360, 32, label=name, fill=WHITE,
               label_font_size=12, label_font=FONT_MONO)
        d.text(515, y + 16, note, font_size=11, valign="middle",
               color=STROKE_DARK)
        y += 38

    # Destruction order arrow (right side, pointing up)
    d.arrow(770, 415, 770, 200, end="arrow")
    d.text(780, 310, "destruction\norder\n(reverse)",
           font_size=12, color=REQUEST_LINE)

    # Below the box: what happens on scope exit
    d.text(460, 485, "scope exit  →  members destruct in reverse  →  arena releases all allocations in one shot",
           font_size=12, align="center", color=STROKE_DARK)
    return d


# -----------------------------------------------------------------------------
# 03 — PMR layered memory resources
# -----------------------------------------------------------------------------

def diagram_03_pmr() -> Diagram:
    d = Diagram(width=900, height=560,
                title="03 — PMR Layered Resources for a Request Arena")

    # Layers, top = closest to consumer, bottom = upstream.
    layers = [
        ("std::pmr::vector / pmr::string / pmr::unordered_map", "PMR-aware containers", REQUEST_FILL, REQUEST_LINE),
        ("std::pmr::polymorphic_allocator<T>", "type-erased allocator (no template param)", ACCENT_FILL, ACCENT_LINE),
        ("std::pmr::unsynchronized_pool_resource", "size-class freelists for reuse within request", REQUEST_FILL, REQUEST_LINE),
        ("std::pmr::monotonic_buffer_resource", "bump pointer; do_deallocate = no-op", REQUEST_FILL, REQUEST_LINE),
        ("std::array<std::byte, 64 * 1024>  (inline)", "stack-allocated initial buffer", WHITE, STROKE_DARK),
        ("upstream: pool_resource or global heap", "process-scoped fallback when arena overflows", PROCESS_FILL, PROCESS_LINE),
    ]

    x = 200
    w = 500
    y = 90
    for name, note, fill, stroke in layers:
        d.rect(x, y, w, 55, label=name, fill=fill, stroke=stroke,
               stroke_width=2, label_font_size=13, label_font=FONT_MONO)
        d.text(x + w + 20, y + 28, note, font_size=11, valign="middle")
        y += 70

    # Arrows on left showing data flow (allocation downward)
    d.arrow(x - 30, 115, x - 30, 510, end="arrow", stroke_width=2)
    d.text(x - 80, 300, "allocate\n(downward)", font_size=12,
           align="center", color=STROKE_DARK)

    # Arrows on right showing destruction (one-shot release upward at end)
    d.arrow(x + w + 280, 510, x + w + 280, 115,
            end="arrow", stroke_width=2, stroke=REQUEST_LINE)
    d.text(x + w + 305, 300, "release\n(O(1) on\narena dtor)",
           font_size=12, color=REQUEST_LINE)
    return d


# -----------------------------------------------------------------------------
# 04 — State Architecture Table (three columns)
# -----------------------------------------------------------------------------

def diagram_04_state_architecture() -> Diagram:
    d = Diagram(width=960, height=560,
                title="04 — State Architecture: Three Columns")

    col_w = 290
    col_h = 430
    y = 80

    # Column headers
    cols = [
        ("PROCESS-SCOPED", "lives for the process; rebuildable from config",
         30, PROCESS_FILL, PROCESS_LINE,
         ["TracerProvider", "gRPC Channel cache", "Redis / PG pool",
          "Prepared statements", "In-process caches", "Parsed config",
          "Thread pools", "PMR upstream"]),
        ("REQUEST-SCOPED", "lives for the handler; RAII",
         335, REQUEST_FILL, REQUEST_LINE,
         ["CallbackServerContext", "ClientContext (outbound)",
          "Active span / OTel scope", "Checked-out connection",
          "PMR monotonic arena", "Parsed req/resp",
          "Intermediate computations"]),
        ("EXTERNAL", "lives outside any process; authoritative",
         640, EXTERNAL_FILL, EXTERNAL_LINE,
         ["Session / user state", "Counters & rate-limit windows",
          "Durable workflow state", "Inter-service queue items",
          "Business data", "Audit logs"]),
    ]

    for header, sub, x, fill, line, items in cols:
        d.rect(x, y, col_w, col_h, fill=fill, stroke=line, stroke_width=2)
        d.text(x + col_w / 2, y + 28, header, font_size=15,
               align="center", color=line)
        d.text(x + col_w / 2, y + 52, sub, font_size=11,
               align="center", color=STROKE_DARK)

        iy = y + 80
        for item in items:
            d.rect(x + 20, iy, col_w - 40, 32, label=item,
                   fill=WHITE, label_font_size=12, label_font=FONT_MONO)
            iy += 40

    # Bottom rule
    d.text(480, 530,
           "Anywhere a piece of state could go in two columns is a design smell. "
           "Pick one.",
           font_size=12, align="center", color=STROKE_DARK)
    return d


# -----------------------------------------------------------------------------
# 05 — Threading: TLS, concurrency families, the co_await trap
# -----------------------------------------------------------------------------

def diagram_05_threading() -> Diagram:
    d = Diagram(width=940, height=580,
                title="05 — Threading: TLS Is Process-Scoped, "
                      "Coroutines Migrate")

    # Two OS threads in a pool
    d.rect(60, 100, 380, 200, fill=PROCESS_FILL, stroke=PROCESS_LINE,
           stroke_width=2)
    d.text(250, 122, "OS Thread A  (gRPC pool)", font_size=14,
           align="center", color=PROCESS_LINE)

    d.rect(500, 100, 380, 200, fill=PROCESS_FILL, stroke=PROCESS_LINE,
           stroke_width=2)
    d.text(690, 122, "OS Thread B  (gRPC pool)", font_size=14,
           align="center", color=PROCESS_LINE)

    # TLS slots on each thread
    d.rect(90, 155, 320, 38, label="thread_local g_correlation_id",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)
    d.text(250, 215, "TLS is process-scoped\n— persists across requests on this thread",
           font_size=11, align="center", color=STROKE_DARK)

    d.rect(530, 155, 320, 38, label="thread_local g_correlation_id",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)
    d.text(690, 215, "Different TLS slot here —\nresume after co_await sees this",
           font_size=11, align="center", color=STROKE_DARK)

    # Coroutine that suspends on one thread, resumes on the other
    d.rect(330, 360, 280, 110, fill=REQUEST_FILL, stroke=REQUEST_LINE,
           stroke_width=2)
    d.text(470, 382, "Coroutine frame  (request scope)",
           font_size=13, align="center", color=REQUEST_LINE)
    d.text(470, 410, "auto rows = co_await db.query();", font_size=12,
           font=FONT_MONO, align="center")
    d.text(470, 440, "log_info(g_correlation_id, ...);", font_size=12,
           font=FONT_MONO, align="center", color=WARN_LINE)

    # Suspend / resume arrows
    d.arrow(250, 300, 380, 360, dashed=True, stroke=WARN_LINE)
    d.text(280, 333, "suspend", font_size=11, color=WARN_LINE)

    d.arrow(560, 360, 690, 300, dashed=True, stroke=WARN_LINE)
    d.text(620, 333, "resume (different thread!)", font_size=11,
           color=WARN_LINE)

    # Bottom warning
    d.rect(60, 500, 820, 60, fill=WARN_FILL, stroke=WARN_LINE,
           stroke_width=2)
    d.text(470, 520, "Trap: TLS read after co_await may differ from "
           "before. Capture into the coroutine frame.",
           font_size=13, align="center", color=WARN_LINE)
    d.text(470, 543, "Mitigation: const auto correlation_id = "
           "req.correlation_id();  // before any co_await",
           font_size=12, align="center", font=FONT_MONO,
           color=STROKE_DARK)
    return d


# -----------------------------------------------------------------------------
# 06 — 12-Factor adapted: four config dimensions
# -----------------------------------------------------------------------------

def diagram_06_twelve_factor() -> Diagram:
    d = Diagram(width=920, height=560,
                title="06 — 12-Factor for C++: Where Config Lives")

    # Four dimensions as horizontal layers
    dims = [
        ("Compile-time",
         "if constexpr, templates, concepts",
         "Doesn't vary across deploys at all",
         "#e0e7ff", "#4f46e5"),
        ("Link-time",
         "static linking, LTO, allocator choice",
         "Baked at link, not parameterizable at run",
         "#e0e7ff", "#4f46e5"),
        ("Env-time (12-factor)",
         "std::getenv read once in main()",
         "Deploy-varying config — THIS is what 12-factor means",
         ACCENT_FILL, ACCENT_LINE),
        ("Runtime-fetched",
         "control plane / config service / Consul",
         "Changes mid-process; rare but real",
         "#fce7f3", "#be185d"),
    ]
    x = 60
    w = 800
    y = 100
    for name, examples, note, fill, stroke in dims:
        d.rect(x, y, w, 90, fill=fill, stroke=stroke, stroke_width=2)
        d.text(x + 30, y + 28, name, font_size=18, color=stroke)
        d.text(x + 30, y + 54, examples, font_size=13,
               font=FONT_MONO, color=STROKE_DARK)
        d.text(x + 30, y + 76, note, font_size=11, color=STROKE_DARK)
        y += 105

    # Side arrow showing "deploy variation" axis
    d.arrow(40, 95, 40, 530, end="arrow", stroke_width=2)
    d.text(15, 310, "deploy-time\nvariation\n(more →)",
           font_size=11, align="center")
    return d


# -----------------------------------------------------------------------------
# 07 — Connection pool with RAII checkout
# -----------------------------------------------------------------------------

def diagram_07_state_externalization() -> Diagram:
    d = Diagram(width=960, height=580,
                title="07 — Connection Pool with RAII Checkout")

    # Process-scoped pool at top
    d.rect(60, 80, 360, 160, fill=PROCESS_FILL, stroke=PROCESS_LINE,
           stroke_width=2)
    d.text(240, 102, "PgPool  (process-scoped)",
           font_size=14, align="center", color=PROCESS_LINE)

    # Free list inside pool
    for i, label in enumerate(["conn 1", "conn 2", "conn 3", "conn N"]):
        d.rect(90 + i * 75, 145, 60, 60, label=label,
               fill=WHITE, label_font_size=12, label_font=FONT_MONO)

    d.text(240, 220, "free list (mutex + condvar)",
           font_size=11, align="center")

    # Handler box
    d.rect(60, 320, 360, 200, fill=REQUEST_FILL, stroke=REQUEST_LINE,
           stroke_width=2)
    d.text(240, 342, "Handler  (request scope)",
           font_size=14, align="center", color=REQUEST_LINE)
    d.rect(85, 375, 310, 32, label="auto conn = pool.acquire(50ms);",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)
    d.rect(85, 413, 310, 32, label="conn->query(...)  // throws on error",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)
    d.rect(85, 451, 310, 32, label="// conn destructs → release",
           fill=WHITE, label_font_size=12, label_font=FONT_MONO)

    # Arrows: acquire / release
    d.arrow(290, 240, 240, 320, end="arrow", stroke=PROCESS_LINE)
    d.text(295, 280, "acquire", font_size=12, color=PROCESS_LINE)

    d.arrow(180, 510, 130, 240, dashed=True, end="arrow",
            stroke=REQUEST_LINE)
    d.text(75, 380, "release\n(RAII)", font_size=12, color=REQUEST_LINE)

    # External backing service
    d.rect(620, 230, 280, 100, fill=EXTERNAL_FILL, stroke=EXTERNAL_LINE,
           stroke_width=2)
    d.text(760, 252, "PostgreSQL  (external)",
           font_size=14, align="center", color=EXTERNAL_LINE)
    d.text(760, 280, "authoritative state", font_size=12, align="center")
    d.text(760, 300, "lives across replica restarts",
           font_size=11, align="center")

    # Query arrow to backing service
    d.arrow(420, 280, 620, 280, stroke=EXTERNAL_LINE)
    d.text(520, 268, "SQL", font_size=11, align="center",
           color=EXTERNAL_LINE)

    # Invalidate path (warning red)
    d.rect(440, 380, 220, 110, fill=WARN_FILL, stroke=WARN_LINE,
           stroke_width=2, dashed=True)
    d.text(550, 405, "broken_connection ?",
           font_size=12, align="center", color=WARN_LINE)
    d.text(550, 430, "→ conn.invalidate()",
           font_size=12, align="center", font=FONT_MONO, color=WARN_LINE)
    d.text(550, 460, "pool discards on release",
           font_size=11, align="center", color=WARN_LINE)
    d.arrow(420, 440, 440, 440, stroke=WARN_LINE)
    return d


# -----------------------------------------------------------------------------
# 08 — Container layer model
# -----------------------------------------------------------------------------

def diagram_08_ephemeral_filesystem() -> Diagram:
    d = Diagram(width=900, height=560,
                title="08 — Container Filesystem: Layers and Lifetimes")

    # Stack of layers (image = bottom, writable = top, tmpfs/volume side-by-side)
    base_x = 200
    base_w = 500
    y = 470

    # Image layers (read-only, persistent with image)
    image_layers = [
        ("Base layer (Fedora/Alpine)", PROCESS_FILL, PROCESS_LINE),
        ("System libraries", PROCESS_FILL, PROCESS_LINE),
        ("Application binary + assets", PROCESS_FILL, PROCESS_LINE),
    ]
    for name, fill, stroke in image_layers:
        d.rect(base_x, y, base_w, 50, label=name + "   (read-only)",
               fill=fill, stroke=stroke, label_font_size=13)
        y -= 55

    # Writable layer on top (ephemeral — dies on restart)
    d.rect(base_x, y, base_w, 50,
           label="Writable layer (overlayfs)  —  ephemeral",
           fill=WARN_FILL, stroke=WARN_LINE, stroke_width=2,
           label_font_size=13, dashed=True)

    # tmpfs to the side
    d.rect(70, 300, 110, 80, label="tmpfs\n/tmp",
           fill=ACCENT_FILL, stroke=ACCENT_LINE, stroke_width=2,
           label_font_size=14)
    d.text(125, 395, "RAM-backed,\nrecycled on restart",
           font_size=10, align="center")

    # Persistent volume to the side
    d.rect(720, 300, 110, 80, label="Persistent\nVolume",
           fill=REQUEST_FILL, stroke=REQUEST_LINE, stroke_width=2,
           label_font_size=14)
    d.text(775, 395, "Survives restart,\nexplicitly mounted",
           font_size=10, align="center")

    # Labels
    d.text(450, 100, "What dies on restart:  writable layer + tmpfs contents",
           font_size=13, align="center", color=WARN_LINE)
    d.text(450, 125, "What survives:  image layers, persistent volumes, "
           "external state",
           font_size=13, align="center", color=REQUEST_LINE)

    # Common traps
    d.text(450, 165, "C++ traps  →  /var/log writes (use stdout), "
           "/var/crash dumps (use stderr),",
           font_size=11, align="center", color=STROKE_DARK)
    d.text(450, 183, "ML model caches (use volume), spdlog file sinks "
           "(use stdout sink)",
           font_size=11, align="center", color=STROKE_DARK)

    # Read-only rootfs note
    d.rect(280, 220, 340, 40, fill=ACCENT_FILL, stroke=ACCENT_LINE,
           stroke_width=2,
           label="read_only: true makes accidental writes fail loudly",
           label_font_size=12, label_font=FONT_MONO)
    return d


# -----------------------------------------------------------------------------
# 09 — Health checks and graceful shutdown sequence
# -----------------------------------------------------------------------------

def diagram_09_health_checks() -> Diagram:
    d = Diagram(width=960, height=600,
                title="09 — Health Checks and Graceful Shutdown")

    # Three probes (top)
    probe_y = 90
    probes = [
        ("startupProbe", "has it initialized?",
         "until success: liveness/readiness are not run",
         50, ACCENT_FILL, ACCENT_LINE),
        ("livenessProbe", "is it deadlocked?",
         "fail → restart container",
         350, WARN_FILL, WARN_LINE),
        ("readinessProbe", "should I route traffic?",
         "fail → remove from Service endpoints",
         650, REQUEST_FILL, REQUEST_LINE),
    ]
    for name, sub, action, x, fill, stroke in probes:
        d.rect(x, probe_y, 260, 130, fill=fill, stroke=stroke, stroke_width=2)
        d.text(x + 130, probe_y + 30, name, font_size=15,
               align="center", color=stroke, font=FONT_MONO)
        d.text(x + 130, probe_y + 60, sub, font_size=12, align="center")
        d.text(x + 130, probe_y + 100, action,
               font_size=11, align="center", color=stroke)

    # Section header for shutdown sequence
    d.text(480, 260, "Graceful Shutdown Sequence (SIGTERM → SIGKILL within terminationGracePeriodSeconds)",
           font_size=13, align="center", color=STROKE_DARK)

    # Numbered steps
    steps = [
        "1. SIGTERM",
        "2. flip readiness → NOT_SERVING\n(keep server-wide SERVING)",
        "3. stop_source.request_stop()\n(stops outbox / async workers)",
        "4. server->Shutdown(deadline)\n(drains in-flight RPCs)",
        "5. main() returns:\nreverse-order destructors",
        "6. provider->ForceFlush()\n(export buffered spans)",
        "7. process exits clean",
    ]

    step_w = 124
    gap = 8
    start_x = 30
    sy = 320
    for i, label in enumerate(steps):
        sx = start_x + i * (step_w + gap)
        d.rect(sx, sy, step_w, 110, label=label, fill=PROCESS_FILL,
               stroke=PROCESS_LINE, stroke_width=2, label_font_size=10,
               label_font=FONT_MONO)
        # Arrow to next
        if i < len(steps) - 1:
            d.arrow(sx + step_w, sy + 55, sx + step_w + gap, sy + 55,
                    stroke=PROCESS_LINE)

    # Final note
    d.text(480, 470,
           "Liveness rarely fails (deadlocks only).  Readiness fails for "
           "transient conditions and during drain.",
           font_size=12, align="center")
    d.text(480, 490,
           "Probes use the gRPC standard health protocol "
           "(grpc.health.v1.Health) via grpc_health_probe.",
           font_size=11, align="center", color=STROKE_DARK)
    return d


# -----------------------------------------------------------------------------
# 10 — gRPC microservice architecture (capstone)
# -----------------------------------------------------------------------------

def diagram_10_grpc_microservices() -> Diagram:
    d = Diagram(width=980, height=600,
                title="10 — Pricing Service: Composition End-to-End")

    # Process-scoped state row (top)
    d.rect(40, 70, 900, 100, fill=PROCESS_FILL, stroke=PROCESS_LINE,
           stroke_width=2, dashed=True)
    d.text(490, 90, "Process-scoped state  (constructed in main(), "
           "passed by reference)",
           font_size=13, align="center", color=PROCESS_LINE)

    proc_items = [
        ("TracerProvider", 60),
        ("ChannelCache", 240),
        ("PgPool", 420),
        ("redis::Redis", 600),
        ("Config (parsed once)", 780),
    ]
    for label, x in proc_items:
        d.rect(x, 120, 160, 40, label=label, fill=WHITE,
               label_font_size=12, label_font=FONT_MONO)

    # gRPC server in the middle
    d.rect(330, 220, 320, 80, fill=ACCENT_FILL, stroke=ACCENT_LINE,
           stroke_width=2)
    d.text(490, 245, "grpc::Server  +  callback API",
           font_size=14, align="center", color=ACCENT_LINE)
    d.text(490, 270, "+ HealthCheckService", font_size=12, align="center")

    # Inbound RPC
    d.arrow(60, 260, 330, 260, stroke=REQUEST_LINE)
    d.text(195, 245, "PriceOrder RPC", font_size=11, align="center",
           color=REQUEST_LINE)

    # PricingService handler
    d.rect(330, 330, 320, 130, fill=REQUEST_FILL, stroke=REQUEST_LINE,
           stroke_width=2)
    d.text(490, 352, "PricingService::PriceOrder",
           font_size=13, align="center", color=REQUEST_LINE, font=FONT_MONO)
    d.text(490, 380, "RequestContext rc (arena, span, deadline)",
           font_size=11, align="center", font=FONT_MONO)
    d.text(490, 405, "fetch_customer · fetch_price · compute_tax",
           font_size=11, align="center")
    d.text(490, 425, "idempotency check + store",
           font_size=11, align="center")
    d.text(490, 445, "RAII destructors → arena released",
           font_size=11, align="center", color=REQUEST_LINE)

    # Arrow from server to handler
    d.arrow(490, 300, 490, 330, stroke=ACCENT_LINE)

    # Backing services (right side)
    backing = [
        ("PostgreSQL",  720, 330, "customers, idempotency"),
        ("Redis",       720, 400, "best-effort cache"),
        ("Tax Service\n(gRPC)", 720, 470, "deadline propagated"),
    ]
    for label, x, y, note in backing:
        d.rect(x, y, 200, 50, label=label, fill=EXTERNAL_FILL,
               stroke=EXTERNAL_LINE, stroke_width=2, label_font_size=12)
        d.text(x + 100, y + 65, note, font_size=10, align="center")
        # Arrow from handler to backing
        d.arrow(650, 395, x, y + 25, stroke=EXTERNAL_LINE)

    # Health probe path
    d.arrow(660, 260, 940, 260, stroke=ACCENT_LINE, dashed=True, end="arrow")
    d.text(800, 245, "grpc_health_probe / k8s probes",
           font_size=11, align="center", color=ACCENT_LINE)

    # Bottom note: same port
    d.text(490, 575, "Same port (50051) carries application RPCs and "
           "the gRPC standard health service.",
           font_size=11, align="center", color=STROKE_DARK)
    return d


# -----------------------------------------------------------------------------
# 11 — Build / container pipeline
# -----------------------------------------------------------------------------

def diagram_11_build_tooling() -> Diagram:
    d = Diagram(width=960, height=540,
                title="11 — Build Pipeline: Conan → CMake → Container")

    # Sources (left)
    d.rect(40, 100, 180, 320, fill=WHITE, stroke=STROKE_DARK,
           stroke_width=2)
    d.text(130, 120, "Sources", font_size=14, align="center")

    src_items = [
        "conanfile.py",
        "CMakeLists.txt",
        "src/  (C++)",
        "proto/  (.proto)",
        "vendor/  (helpers)",
        "tests/",
        "profiles/{dev,release}",
        "Containerfile",
    ]
    sy = 150
    for s in src_items:
        d.rect(60, sy, 140, 30, label=s, fill=WHITE,
               label_font_size=11, label_font=FONT_MONO)
        sy += 35

    # Conan stage
    d.rect(260, 130, 160, 100, fill=ACCENT_FILL, stroke=ACCENT_LINE,
           stroke_width=2)
    d.text(340, 152, "Conan 2.x", font_size=14,
           align="center", color=ACCENT_LINE)
    d.text(340, 180, "fetch + build", font_size=11, align="center")
    d.text(340, 200, "transitive deps", font_size=11, align="center")
    d.text(340, 218, "(grpc, OTel, pqxx, ...)",
           font_size=10, align="center", font=FONT_MONO)

    d.arrow(220, 200, 260, 200, stroke=STROKE_DARK)

    # CMake + Ninja
    d.rect(260, 280, 160, 100, fill=ACCENT_FILL, stroke=ACCENT_LINE,
           stroke_width=2)
    d.text(340, 302, "CMake + Ninja", font_size=14,
           align="center", color=ACCENT_LINE)
    d.text(340, 330, "configure + build", font_size=11, align="center")
    d.text(340, 350, "compile + link", font_size=11, align="center")
    d.text(340, 368, "(GCC 14 + libstdc++)",
           font_size=10, align="center", font=FONT_MONO)

    d.arrow(220, 320, 260, 320, stroke=STROKE_DARK)
    d.arrow(340, 230, 340, 280, stroke=ACCENT_LINE)

    # Strip + binary
    d.rect(470, 240, 140, 70, fill=WHITE, stroke=STROKE_DARK,
           stroke_width=2)
    d.text(540, 262, "strip", font_size=14, align="center")
    d.text(540, 285, "pricing_service", font_size=11,
           align="center", font=FONT_MONO)

    d.arrow(420, 280, 470, 280, stroke=STROKE_DARK)

    # Multi-stage container build
    d.rect(660, 100, 260, 240, fill=PROCESS_FILL, stroke=PROCESS_LINE,
           stroke_width=2)
    d.text(790, 122, "Multi-stage Containerfile",
           font_size=13, align="center", color=PROCESS_LINE)

    d.rect(680, 150, 220, 70, fill=WHITE, stroke=STROKE_DARK,
           stroke_width=2)
    d.text(790, 170, "build stage", font_size=12,
           align="center", color=STROKE_DARK)
    d.text(790, 192, "fedora + toolchain", font_size=11,
           align="center", font=FONT_MONO)
    d.text(790, 210, "(discarded after build)",
           font_size=10, align="center")

    d.rect(680, 240, 220, 90, fill=WHITE, stroke=STROKE_DARK,
           stroke_width=2)
    d.text(790, 260, "runtime stage", font_size=12,
           align="center", color=STROKE_DARK)
    d.text(790, 282, "fedora-minimal", font_size=11,
           align="center", font=FONT_MONO)
    d.text(790, 300, "+ pricing_service", font_size=11,
           align="center", font=FONT_MONO)
    d.text(790, 318, "+ grpc_health_probe",
           font_size=11, align="center", font=FONT_MONO)

    d.arrow(610, 280, 660, 280, stroke=STROKE_DARK)

    # Final output
    d.rect(660, 380, 260, 60, fill=REQUEST_FILL, stroke=REQUEST_LINE,
           stroke_width=2)
    d.text(790, 400, "OCI image", font_size=14,
           align="center", color=REQUEST_LINE)
    d.text(790, 425, "→ registry → Podman / k8s",
           font_size=12, align="center")

    d.arrow(790, 340, 790, 380, stroke=PROCESS_LINE)

    return d


# -----------------------------------------------------------------------------
# Main: generate all
# -----------------------------------------------------------------------------

DIAGRAMS = [
    ("01-deployment-posture",     diagram_01_deployment_posture),
    ("02-raii-request-scope",     diagram_02_raii),
    ("03-pmr-layered-resources",  diagram_03_pmr),
    ("04-state-architecture",     diagram_04_state_architecture),
    ("05-threading-tls-coroutine", diagram_05_threading),
    ("06-twelve-factor-config",   diagram_06_twelve_factor),
    ("07-pool-raii-checkout",     diagram_07_state_externalization),
    ("08-container-layers",       diagram_08_ephemeral_filesystem),
    ("09-probes-and-shutdown",    diagram_09_health_checks),
    ("10-pricing-service",        diagram_10_grpc_microservices),
    ("11-build-pipeline",         diagram_11_build_tooling),
]


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    for stem, fn in DIAGRAMS:
        d = fn()
        excalidraw_path, svg_path = d.write(os.path.join(here, stem))
        print(f"  wrote {os.path.basename(excalidraw_path)} + "
              f"{os.path.basename(svg_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
