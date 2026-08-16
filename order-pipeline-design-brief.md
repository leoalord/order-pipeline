# Consolidated design brief: single-machine order pipeline

Research-only synthesis organized around decisions, not researcher-by-researcher notes. No implementation code. Exactly-once business effects are not claimed.

The system must:

- Accept high-volume, bursty traffic.
- Preserve valid per-order lifecycle transitions.
- Handle slow, rate-limited, and unreliable downstream services.
- Avoid lost orders and harmful duplicate effects.
- Recover from worker and component failures.
- Provide real-time business and operational visibility.
- Run on one machine and remain small enough to build and explain.

---

## 1. Executive summary

The evidence converges on one achievable contract: **at-least-once attempts, unique business effects, no silent drop, and legal per-order transitions**. That is what Workato teaches, what Uber and DoorDash actually document for business paths, and what Helland, AWS, Stripe, and Google SRE formalize. It is not “exactly-once Kafka,” not Cadence, and not Workato Event Streams.

**Recommendation.** Build one durable order record as the source of truth. Accept writes that record and the next unit of work in the same commit, then returns success. A small worker pool claims work with leases, serializes each order with a version and a legal transition table, and calls restaurant and courier sims with a stable intent key. Retries are classified, bounded, and jittered. Poison or exhausted work leaves the live path. The dashboard reads the same store: occupancy and oldest age, pipeline rates, and conservation plus one-order history.

Uber, DoorDash, and Grubhub are useful for **failure shapes and contracts**. Their platforms exist because of fleet and organizational scale. Copying those platforms would make the take-home harder to explain without adding a correctness property you cannot get from a work table and a version column.

Where first-party sources disagree — the meaning of `confirmed`, who mints idempotency keys, what happens at a confirm deadline, whether in-flight work may vanish on crash — this brief picks an explicit default and labels it a recommendation. Those defaults are in section 2 and the checklist.

---

## 2. Recommended correctness model and system guarantees

### Vocabulary

Use these words when defending the design. Do not treat them as synonyms.

| Term | Meaning | Is not |
|---|---|---|
| Delivery | A message is handed to a consumer *N* times | Food delivered; handler succeeded |
| Execution | Handler code ran *N* times | The business effect happened *N* times |
| Business effect | One kitchen ticket, one courier dispatch, one legal transition | Queue delete, HTTP 200, job Completed |
| Ambiguous outcome | Sent, no trustworthy response | Known failure |
| Durable acceptance | Crash cannot erase the order | In-memory 201 |
| Claim / lease | Temporary exclusive right to work | Proof of uniqueness |
| Ack / complete | Do not re-offer this work | Downstream succeeded |
| Idempotency key | Identifier of intent, reused on retry | New UUID per HTTP attempt |
| Ingest de-dupe | Don’t create two orders for one place intent | Don’t double-call the restaurant |
| Effect de-dupe | Don’t apply the same mutation twice | Broker exactly-once |
| Compensation | Later action that semantically undoes a step | Database rollback; free platform saga |
| Pivot | Point of no return; retry forward | Cancel anytime |
| Conservation | `accepted = delivered + cancelled + failed + in_flight` | ACID across restaurant and courier |

**Evidence.** [Kafka message delivery semantics](https://kafka.apache.org/43/design/design/#message-delivery-semantics); [Helland, *Idempotence Is Not a Medical Condition*](https://queue.acm.org/detail.cfm?id=2187821) (2012); [Featonby, *Making retries safe with idempotent APIs*](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/); [RFC 9110 §9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2); [AWS REL04-BP04](https://docs.aws.amazon.com/wellarchitected/latest/framework/rel_prevent_interaction_failure_idempotent.html).

### What to claim

**Recommendation.** Claim these, and only these:

1. No accepted order ID vanishes across crash or restart.
2. Every accepted order reaches a terminal state or is visibly stuck with a next action.
3. Only legal per-order transitions apply.
4. At most one successful kitchen accept and one successful courier dispatch per order.
5. Attempts may duplicate; effects must not.
6. Timeout is treated as unknown, not as failure.
7. One slow or poison order does not stall the rest.

### What not to claim

Do not claim exactly-once delivery, execution, or business effects. Do not claim that job success, queue delete, or HTTP 200 means the restaurant cooked. Do not claim a lease is a mutex. Do not claim a durable queue processes each order once. Do not claim compensation always succeeds. Do not claim count-in = count-out means fulfillment is correct.

**Evidence that “exactly-once” is a homonym.** Kafka EOS is read–process–write *among Kafka topics* and “generally requires cooperation” with external systems. SQS FIFO “exactly-once” is a five-minute *send* dedup. Cadence guarantees one *open workflow* per ID; activities may run more than once. Workato trigger de-dupe means one *job* per seen event; [reruns can duplicate records](https://docs.workato.com/en/recipes/rerun-job.html). Uber’s [ads exactly-once post](https://www.uber.com/us/en/blog/real-time-exactly-once-ad-event-processing/) (23 Sep 2021) is analytics and still uses record UUIDs. DoorDash DashPass’s “exactly-once job execution” (18 May 2022) is single-flight per ID, not unique partner side effects.

You can *approximate* unique effects under at-least-once delivery if the key is reused, the effect is remembered atomically, and the callee honors the key or the pipeline reconciles. That is the Stripe/AWS contract, not physics.

### Default product choices

These are **recommendations**, not facts. Sources disagree on several of them.

| Decision | Recommended default | Why |
|---|---|---|
| No lost orders | No accepted ID vanishes; stuck work is visible and must progress or fail explicitly | Uber “no loss” has five incompatible validators. Count equality can pass while an order is stuck. |
| Harmful duplicate | Second kitchen ticket, second courier dispatch, or second order from one diner intent | Helland allows log/metric dupes. The assignment’s harm is downstream effects. |
| `confirmed` | Restaurant will-cook accept. Notify/insert ACK is an internal attempt | The assignment already has `being prepared`. [Grubhub `CONFIRMED`](https://developer.grubhub.com/docs/3FrsnGXiR1Yt3Olm4sDWuM/order-workflow) is insert-only. Eats webhook ACK ≠ accept. |
| Confirm timeout | Configurable deadline, then **explicit fail**. Do not auto-accept | Grubhub POS, tablet, and merchant policy disagree. Auto-accept can cook food nobody saw. |
| Sim idempotency | Treat mutations as non-idempotent unless they honor a key you send. Remember effects either way | Timeline D is unsatisfiable without one or the other. DoorDash Drive could not assume callees were idempotent. |
| Keys | Client key for place-order. Server key `(order_id, transition)` for downstream | A load-gen that mints a new UUID per retry creates duplicate orders. Workato Factor V vs Stripe is audience-dependent. |
| Cancel | Synchronous from pre-pivot states. After pivot, reject or compensate per a written table | Grubhub Care tickets exist because money moves. A take-home needs a finishable cancel demo. |
| Isolation | Per order. Separate capacity for intake, restaurant I/O, courier I/O, and load-gen | DoorDash’s shared Celery/RabbitMQ fabric took checkout, merchant transmit, and Dashers down together. |
| Shedding | Protect in-flight. Shed load-gen first, then new intake if saturated | [QALM](https://www.uber.com/us/en/blog/qalm-qos-load-management-framework/) (22 Mar 2018): never drop core trip flow. |
| Crash bar | Accepted IDs survive restart. Unacked work may redeliver; effects must not double-apply | DoorDash 2020 allowed local-queue loss. That fails conservation unless you say so. |

### Crash timelines the design must close

Any later architecture is incomplete if it cannot point to a close for each:

| ID | What happens | Required close |
|---|---|---|
| A | Crash before durable accept | Client retries same place-key; no success ACK before commit |
| B | Persist `placed`, crash before work is visible | Next work committed with the order, or a startup scanner |
| C | Worker crash after claim | Lease expiry re-offers; overlap possible; effects must be guarded |
| D | Downstream success, lost response | Same transition key or remembered effect; do not mark failed without reconcile |
| E | State persisted, ack lost | Redelivery is a no-op via version/transition guard |
| F | Cancel vs in-flight progress | OCC + legal table; pivot states reject or compensate |

---

## 3. Findings organized around the five research questions

Each finding below is one decision. **Evidence** is cited. **Interpretation** and **recommendation** are labeled. Classification is essential / optional / excessive *for this take-home*.

### Q1. How should orders be queued and processed so traffic spikes do not overwhelm the system?

**Finding: decouple accept from fulfillment; persist first.**

- **Evidence.** DoorDash checkout materialized the order, then emitted work ([Wei et al., 2 Feb 2021](https://careersatdoordash.com/blog/building-a-more-reliable-checkout-service-with-kotlin/)). Microsoft’s [transactional outbox](https://learn.microsoft.com/en-us/azure/architecture/databases/guide/transactional-out-box-cosmos) (2026) states that two independent writes are not atomic. Workato parks long actions instead of holding the request ([long actions](https://docs.workato.com/en/recipes/long-actions.html)).
- **Failure.** Sync-on-the-request-path collapses when restaurant/courier I/O is slow. Crash after “we have an order” but before “someone will work it” produces limbo (Timeline B).
- **Guarantees / does not.** Intake can return while downstreams are slow. It does not guarantee fulfillment, and it does not close Timeline B unless next work is committed with the order.
- **Assumptions / trade-offs.** Clients can tolerate “accepted, not finished.” You now need a worker and a stuck-order story.
- **Recommendation.** On place: validate, write `placed` + next work in one commit, then return the ID. A broker is not required to express this. A broker *without* the same-commit write reintroduces Timeline B.
- **Class.** Essential. Kafka/uForwarder/double fleets: excessive.

**Finding: bound in-flight work and isolate slow or poison items.**

- **Evidence.** DoorDash Kafka workers hit partition head-of-line blocking; they isolated fetch from execute with a bounded local queue, and admitted that queue “may get lost” on crash ([Khalilnaji & Kachhara, 3 Sep 2020](https://doordash.engineering/2020/09/03/eliminating-task-processing-outages-with-kafka/)). Uber’s [retry/DLQ post](https://www.uber.com/us/en/blog/reliable-reprocessing/) (Xia, 15 Feb 2018) and [Consumer Proxy](https://www.uber.com/us/en/blog/kafka-async-queuing-with-consumer-proxy/) (Chu, 30 Aug 2021) treat a blocked live cursor as the bug. Workato’s database guide uses **new orders** to argue against batch-fail-all.
- **Failure.** One 30-second restaurant call, or one bad payload, stalls unrelated orders. Shared fate took DoorDash’s checkout, merchant transmit, and Dasher work down together.
- **Guarantees / does not.** Unrelated orders continue. It does not preserve global arrival order. Parallelizing the *same* order can break happens-before.
- **Assumptions / trade-offs.** You accept cross-order reordering. DoorDash’s isolation trick created a crash-loss hole this assignment should not copy.
- **Recommendation.** Per-order claims. Global cap on outbound restaurant and courier calls. Failed or exhausted work leaves the live path (`needs_intervention`), not the hot cursor.
- **Class.** Essential as isolation. Kafka partitions and Uber’s proxy: excessive.

**Finding: treat the load generator as a reliability threat; shed it first.**

- **Evidence.** [QALM](https://www.uber.com/us/en/blog/qalm-qos-load-management-framework/) never dropped core trip flow. [Cinnamon](https://www.uber.com/us/en/blog/cinnamon-using-century-old-tech-to-build-a-mean-load-shedder/) (22 Nov 2023) exists because CoDel-style rejects caused retry herds. [SRE cascading failures](https://sre.google/sre-book/addressing-cascading-failures/) and [Brooker](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/) treat retries as extra offered load. DoorDash [Aperture](https://doordash.engineering/2023/03/14/failure-mitigation-for-microservices-an-intro-to-aperture/) (14 Mar 2023) documents retry storms and metastable overload.
- **Failure.** Dinner-rush plus naive retries melts the only machine. Accept-all is not the same as no-lost-orders.
- **Guarantees / does not.** In-flight orders stay servable. Shed is a counted reject, not a silent drop. It does not mean zero 429s.
- **Assumptions / trade-offs.** You must define “lost” so a visible shed is not graded as data loss.
- **Recommendation.** Hard in-flight and outbound caps. When saturated, shed load-gen, then new intake. Never shed by wiping state.
- **Class.** Essential as policy. Mesh GRL / PID shedders: excessive.

### Q2. How should lifecycle transitions remain ordered and valid for each order?

**Finding: per-order happens-before, not global FIFO.**

- **Evidence.** [Helland, *Life Beyond Distributed Transactions*](https://www.cidrdb.org/cidr2007/papers/cidr07p15.pdf) (2007): the entity is the serializability scope. Kafka orders only within a partition. Uber’s queueing proxy *abandons* partition order for independent events (Chu 2021) — the wrong analogue for `confirm` then `cancel` on the same ID. Workato long actions [explicitly break job sequence](https://docs.workato.com/en/recipes/long-actions.html) when waiting, even at concurrency 1. Grubhub returns **409** on illegal channel transitions.
- **Failure.** Global FIFO plus a slow courier stalls the rush. Unordered workers let cancel and confirm last-write-win.
- **Guarantees / does not.** Transitions on one ID have a defined order. It does not preserve global diner arrival order, and it does not cancel an in-flight HTTP call — only prevent its result from applying illegally.
- **Assumptions / trade-offs.** Waiting unblocks other orders. That surprises people who wanted a single kitchen line.
- **Recommendation.** One versioned order row. At most one claim per `order_id`. Legal graph in one table. Dashboard gauges may be latest-wins; the order history must not be.
- **Class.** Essential. Cadence-as-the-state-machine, or a second workflow engine: excessive.

**Finding: `confirmed` is a product word, not a universal state.**

- **Evidence.** [Grubhub](https://developer.grubhub.com/docs/3FrsnGXiR1Yt3Olm4sDWuM/order-workflow): `CONFIRMED` means the restaurant received the ticket, “even if unable to fulfill.” [Uber Eats webhooks](https://developer.uber.com/docs/eats/guides/webhooks): HTTP 200 on the notify is not accept; accept/deny is a second call, or auto-cancel in 11.5 minutes. The assignment lists both `confirmed` and `being prepared`. Third-party OpenAPI with `IN_PROGRESS` is not Grubhub’s contract.
- **Failure.** Flattening ACK and will-cook hides “confirmed but cannot cook” and “notified but never accepted.”
- **Guarantees / does not.** Splitting them makes ownership and races visible. It does not require Grubhub’s Care loop or Eats’ 11.5-minute SLA.
- **Recommendation.** Diner `confirmed` = will-cook accept. Notify/insert is an internal attempt on the history, not a lifecycle stage. Restaurant cannot write `delivered` if the platform owns the courier (409-shaped guard).
- **Class.** Essential as a definition. Dual full order/delivery machines, JIT geofence, scheduled `ANTICIPATED`: optional.

**Finding: cancel is a concurrent writer with a pivot.**

- **Evidence.** Grubhub [cancellations](https://developer.grubhub.com/docs/5OLUtSXDWplHjxEg7oTJqi/order-cancellations) are Care-approved tickets after confirm; diner self-cancel exists only while `ANTICIPATED`. DoorDash Drive ran a **separate cancellation task** after repeated failure (Lin, 14 Aug 2020). Checkout compensated to a “clean failed state.” Microsoft’s [saga page](https://learn.microsoft.com/en-us/azure/architecture/patterns/saga) defines pivot; compensations might not succeed. Helland 2007 argues against creating multiple serializability scopes for one business object.
- **Failure.** Timeline F: confirm overwrites cancel, or cancel arrives after food is already cooking, or a late retry resurrects a cancelled order.
- **Guarantees / does not.** A written table makes races gradeable. It does not make compensation reliable, and it does not require a saga runtime.
- **Recommendation.** Sync cancel from `placed` and `confirmed`. After `being_prepared`, document the pivot (reject or compensate). Token lifetime is tied to the order, not a wall-clock TTL that can recreate a cancelled order.
- **Class.** Essential as a table. Saga framework: excessive. Care-shaped PENDING ticket: optional fidelity.

### Q3. How should retries and idempotency prevent lost orders and harmful duplicate processing?

**Finding: ack-after-effect is the anti-loss switch; unique effects are an application property.**

- **Evidence.** Kafka: save position then process → at-most-once (loss); process then save → at-least-once (dupes). Azure receive-and-delete loses on crash. Xia 2018 and Chu 2021: autocommit is not tenable for billing; idempotency lives in the consumer. Workato: [jobs are not duplicated at the trigger](https://docs.workato.com/en/recipes/triggers); reruns and timed-out POSTs can still duplicate writes. [Jobs](https://docs.workato.com/en/recipes/jobs): “Completed but not processed as you expect.”
- **Failure.** Ack-before-accept loses orders (Timeline A). Ack-after-success still redelivers if the ack is lost (Timeline E).
- **Guarantees / does not.** Work is not forgotten by the accept path. It does not give exactly-once execution. Redelivery will happen.
- **Assumptions / trade-offs.** You must make effects unique. A “guaranteed queue” does not do that ([Helland 2012](https://queue.acm.org/detail.cfm?id=2187821)).
- **Recommendation.** Never return 201 before the order is durable. Never retire work before the transition or explicit fail is durable. Close Timeline E with the same transition guard, not with “the queue said deleted.”
- **Class.** Essential. Broker EOS as the correctness story: excessive.

**Finding: two keys, not one, and timeout is its own class.**

- **Evidence.** [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests) and Featonby: client key, atomic token+mutation, parameter mismatch, finite retention. [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2): do not auto-retry non-idempotent methods unless you know the original never applied. Workato [HTTP catalog](https://docs.workato.com/en/developing-connectors/http/error-message.html): retry unmodified on 408/429/5xx; not on 400/404/409. [Factor V](https://www.workato.com/7factors/factor-v-safe-retries): timeout cannot be distinguished from success. SDK: at most one POST per retried action. DoorDash Drive recorded completed non-idempotent steps because making callees idempotent was “not feasible” (Lin, 14 Aug 2020). Eats: `event_id` digest-once. Grubhub: `uuid` is the only durable order identity; `order_number` is not.
- **Failure.** New UUID per HTTP retry creates two orders (Timeline A). Retry-on-timeout without a transition key creates two tickets (Timeline D). Retrying 409 “already confirmed” as if it were a blip.
- **Guarantees / does not.** Same place-key ⇒ one order. Same `(order, confirm)` key ⇒ one kitchen accept *if* the sim honors it or the pipeline remembers. It does not make `dispatch_courier()` a set-if unless you remember the effect. Token expiry can turn a late retry into a new request (Stripe 24h).
- **Assumptions / trade-offs.** **Disagreement:** Factor V says do not trust the caller to mint keys (especially an agent). Stripe/AWS/Eats/Grubhub expect a client or partner id. **Interpretation:** the load-gen is a deterministic client; downstream workers should not invent a new intent.
- **Recommendation.** Client place-key on intake. Server-derived `(order_id, transition)` on restaurant and courier calls. Classify: transient / overloaded / permanent / unknown. Retry 429/503/timeout only with the same key. Do not retry 400/404/409/validation. Bound attempts, full jitter ([Brooker, 2015/2023](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)), **one** retry layer.
- **Class.** Essential. Copying Workato’s max-3 or Eats’ 7 attempts as laws: optional numbers. Treating trigger de-dupe or SQS FIFO as effect uniqueness: excessive.

### Q4. How should the system respond to downstream failures, rate limits, and worker crashes?

**Finding: leases recover workers; they do not make work unique.**

- **Evidence.** [SQS visibility timeout](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html): hide while working; re-offer on expiry; “no absolute guarantee” of uniqueness even while invisible. Azure Complete can fail after success. Cadence activities use timeouts, heartbeats, and task-list rate limits ([docs](https://cadenceworkflow.io/docs/concepts/activities)). DoorDash 2020: in-flight local queue may be lost on crash — an honest hole this assignment should not copy.
- **Failure.** Timelines C and E. In-memory-only claims vanish or never reclaim. Too-short leases overlap two workers.
- **Guarantees / does not.** A dead worker’s work becomes available again. It does not prevent two overlapping calls. It does not prevent loss if claims live only in memory and die with the process without a restart scan.
- **Recommendation.** `leased_until` on the work row. On startup, reclaim expired leases and scan `placed` with no work (Timeline B). Successors always use the same transition key. Do not treat the lease as a mutex.
- **Class.** Essential. SQS/Service Bus as products: optional. Cadence for recovery: excessive.

**Finding: silence is not a stable state; retries are dual-use.**

- **Evidence.** Grubhub unconfirmed orders become `STALE` and are reaped — but POS vs tablet vs merchant policy disagree on auto-confirm vs cancel ([GFR policies](https://get.grubhub.com/help-center/grubhub-restaurant-policies/)). Eats auto-cancels if accept/deny is missing. DoorDash checkout classified retryable vs fatal and compensated fatals; legacy cancel-on-blip lost ~1% of orders they later claimed to save. Aperture: retries amplified a payment-service blip; a misconfigured circuit breaker cut unrelated services. SRE: three layers × four attempts = 64×; after the trigger is gone, retries can keep the system down.
- **Failure.** Orders sit in `placed` forever, or retries become the outage, or a breaker cancels good orders.
- **Guarantees / does not.** A deadline plus a classified retry policy makes the demo gradeable. It does not pick the morally right timeout action. It does not make circuit breakers safe — DoorDash’s own breaker caused a wide outage.
- **Recommendation.** Configurable confirm deadline, default **fail the order**, optionally pause that restaurant for new intake. Cap outbound calls per dependency. Honor 429. After N classified failures, isolate to `needs_intervention`. Fail-closed on restaurant confirm; fail-open on dashboard refresh. Do not import a global circuit breaker.
- **Class.** Essential as deadline + budget + isolation. Aperture/GRL/Cinnamon: excessive.

### Q5. What dashboard and health metrics will clearly demonstrate load, failures, and recovery?

**Finding: three visibilities, not one health number.**

- **Evidence.** [Google golden signals](https://sre.google/sre-book/monitoring-distributed-systems/) (2016): latency, traffic, errors, saturation; page on symptoms. [Wilkie RED](https://grafana.com/blog/the-red-method-how-to-instrument-your-services/) (2015/2018) is request-centric and misses long-lived kitchen state. [Gregg USE](https://www.brendangregg.com/usemethod.html) is the resource checklist — here, worker slots and outbound slots matter more than disk. [Netflix SPS](https://netflixtechblog.com/sps-the-pulse-of-netflix-streaming-ae4db0e05f8a) (2 Feb 2015): one business pulse because CPU did not tell them whether members could watch. Chaperone exists *because* broker dashboards missed loss ([Li & Bansal, 7 Dec 2016](https://www.uber.com/us/en/blog/chaperone-audit-kafka-messages/)). DoorDash [StatsD died at peak](https://doordash.engineering/2023/08/01/how-doordash-migrated-from-statsd-to-prometheus/) (1 Aug 2023). [Stripe canonical log lines](https://brandur.org/canonical-log-lines) (Leach, 26 Nov 2016): one wide record per unit of work. [Tene / wrk2](https://github.com/giltene/wrk2): closed-loop load omits the samples that would have arrived during a stall.
- **Failure.** Healthy intake p99 while the kitchen is frozen. Green CPU while IDs vanish. A dinner-rush graph that only proves the generator waited.
- **Guarantees / does not.** Three panes plus conservation make load, failure, and recovery visible. They do not require Prometheus, Grafana, or Jaeger. Little’s `L = λW` is a post-window accounting identity ([Little 1961](https://doi.org/10.1287/opre.9.3.383); finite-interval restatement 2011), not a live controller during a rush.
- **Recommendation.** One auto-refresh page, TUI, or JSON endpoints. Always visible: accepts/min, stacked terminals, oldest age, conservation residual. Funnel + restaurant/courier RED + retry rate. Paste `order_id` for history — that is the trace. Load-gen is open-loop, or closed-loop is labeled.
- **Class.** Essential as questions and records. Observability product: excessive.

---

## 4. Architecture and setup implications

This is a **recommended shape**, not a vendor stack. One process and one embedded durable store can express every essential property. Extra processes are optional isolation.

### Component responsibilities

| Component | Owns | Must not own |
|---|---|---|
| Intake | Validate place/cancel; durable accept; return ID; count accepts/rejects | Blocking restaurant/courier I/O; success meaning “food is moving” |
| Order store | Current state, version, timestamps, last attempt, effect records, next work | Being replaced by queue offsets as the operator source of truth |
| Work publisher | Same-commit “this order needs X”; reclaim expired leases; deadline scans | A second source of truth that can diverge silently |
| Workers | Claim, call sims, apply guarded transitions, ack after commit, classify errors | In-memory-only claims; ack-before-effect; retrying permanent errors |
| Restaurant / courier sims | Slow, 429, 5xx, timeouts, optional key honor, classifiable errors | Being the only memory of whether a ticket or dispatch happened |
| Load generator | Open-loop burst; reusable place-keys; labeled as shed-able traffic | Closed-loop p99 as proof the rush was handled |
| Dashboard | Three visibilities from the store; one-order history; fault toggles | A frontend product; geo maps; 20 charts |

### Durable state and work publication

**Evidence.** Dual-write is the named Timeline B failure. DoorDash checkout persisted first. Drive wrote `in-progress` as work started.

**Recommendation.** The outbox is a `next_action` column or a work row committed with the order. On restart, scan for `placed` with no work. A broker plus change-data-capture is excessive here and still needs that same-commit write or you have not closed Timeline B.

### Queueing and concurrency

| Knob | Recommended setup | Class |
|---|---|---|
| Accept concurrency | Bounded; return 429/503 when full rather than silent drop | Essential |
| Workers | Small pool; at most one claim per order | Essential |
| Outbound slots | Separate caps for restaurant and courier | Essential |
| In-flight budget | Hard cap; oldest age visible when it binds | Essential |
| New vs retry share | A fairness knob; default bounded mix + jitter | Optional but valuable |
| Broker partitions | Not required to express the above | Excessive |

### Ordering and state transitions

Recommended diner machine: `placed → confirmed → being_prepared → ready → out_for_delivery → delivered`, plus `cancelled` and `failed`. Internal notify/dispatch RPCs are history, not stages.

Cancel allowed from `placed` and `confirmed`. After `being_prepared`, apply the written pivot. Illegal edges increment `invalid_transition` and do not apply. Restaurant cannot mark `delivered` if the platform owns the courier.

### Retries, idempotency, and ambiguous outcomes

| Call | On timeout | On 429/503 | On 400/404/409 |
|---|---|---|---|
| Place order | Retry same client key; lookup by key | Backoff / shed | Do not retry |
| Restaurant confirm | Retry same `(order, confirm)` key; or GET state then apply | Jittered backoff, budget | Permanent; fail or stop |
| Courier dispatch | Retry same `(order, dispatch)` key; remember effect | Jittered backoff, budget | Permanent |
| Cancel | Retry same cancel intent; guard vs pivot | Backoff | Already terminal → success-equivalent |

Timeout is unknown. Do not mark failed without reconcile. Do not retry at intake *and* worker *and* an SDK independently.

### Backpressure and downstream protection

Cap outbound calls. Honor 429. When the restaurant is down, occupancy piles in `placed` / confirming; intake continues until the in-flight cap, then load-gen is shed. Do not fail-open restaurant confirm. Dashboard refresh may fail-open.

### Recovery and dead-letter handling

| Event | Recovery |
|---|---|
| Process restart | Reload orders; reclaim expired leases; scan placed-with-no-work (Timeline B) |
| Worker death mid-call | Lease expires; successor retries with same transition key |
| Downstream outage | Backoff; after deadline, explicit fail or `needs_intervention` — never vanish |
| Poison payload | After N permanent or unclassifiable failures, isolate; other orders continue |
| Wipe state to look healthy | Forbidden. That is a lost-order demonstration |

---

## 5. Minimal business and system-health metrics

| Pane | Metric | Why |
|---|---|---|
| Business | Open orders by stage | Funnel / occupancy |
| Business | Delivered / cancelled / failed per minute | Pulse (SPS analog) |
| Business | Age of oldest open order and its stage | Stuck work; recovery proof |
| Business | p50 / p95 / max e2e latency of delivered orders | Do not average failures into this |
| Pipeline | Intake accept and reject/error rate | RED at the edge |
| Pipeline | Stage enter/leave rates | Drain vs pile-up |
| Pipeline | In-flight count (waiting + in service) | Saturation; pair with oldest age |
| Pipeline | Restaurant and courier: rate, error, latency | RED on deps; split timeout / 429 / 5xx |
| Pipeline | Retry rate (not-first attempts) | Storm visibility; easy to misread |
| Pipeline | Busy workers and outbound slots vs cap | USE on software resources |
| Correctness | `accepted = delivered + cancelled + failed + in_flight` | Conservation; live, not only at end |
| Correctness | Duplicate attempts vs duplicate successful effects | High attempts can be OK |
| Correctness | Invalid transition count | Must stay 0 |
| Correctness | Orders with no progress for > T | Silent hang |
| Correctness | Per-order event history | The trace; lookup by `order_id` |

Optional: age histogram, time-in-stage, retry-budget remaining, shed count, worker restarts, key-reuse (client vs worker), coordinated-omission-corrected versus raw accept latency. CPU is secondary on a laptop.

Dashboard: pulse strip always visible; funnel plus deps; paste `order_id`. That is enough.

---

## 6. Dinner-rush, downstream-outage, and worker-crash scenarios

Write the hypothesis first. Inject a bounded fault. Symptoms appear while correctness invariants still hold. Recover: fault off, drain rate positive, oldest age falling.

Shared preconditions: open-loop arrivals, toggleable faults, live conservation, one reserved `order_id`, load-gen reuses place-keys.

**Dinner rush.** Arrivals jump several times above baseline, then decay. Downstreams stay “normally slow,” not dead. Hypothesis: accepts are not silently dropped; oldest age rises then falls; duplicate effects stay 0; invalid transitions stay 0.

**Downstream outage.** Restaurant *or* courier goes to 100% error or infinite delay for about 60 seconds, then returns. Arrivals continue. Hypothesis: no lost IDs; retry rate rises; successful effects do not exceed unique orders; after recovery, oldest age peaks then falls.

**Worker crash.** Kill the process that advances orders while work is in flight, then restart. Hypothesis: every previously accepted ID is still accounted for; work resumes or fails closed; no duplicate effects; history shows a gap and a resume, not a second confirm.

---

## 7. Pass/fail invariants

### Always on

| Invariant | Pass | Fail |
|---|---|---|
| Conservation | Identity holds at all sample times | Unexplained drift |
| Terminal exclusivity | At most one terminal state | Delivered and cancelled |
| Transition legality | Only allowed edges | Count > 0 |
| Effect uniqueness | ≤1 successful effect per (order, type), or exact idempotent replay | Two distinct dispatches |
| No silent drop | Rejects counted; accepts have IDs | Rate mismatch without reject counters |
| Progress or explicit fail | Old open orders are on the stuck list and move or fail | Eternal `being_prepared` with no events |
| Recovery | After fault clear, oldest-age derivative becomes negative | Errors stop only because load stopped or state was wiped |
| Measurement honesty | Arrivals continue during stall, or closed-loop is admitted | Accept p99 used as proof the rush was handled |

**Dinner rush fail examples:** IDs vanish; completions keep up only because the generator stopped; oldest age still climbing long after arrivals are baseline.

**Outage fail examples:** two courier dispatches; retry storm after the sim is healthy; delivered with no successful downstream call in history.

**Crash fail examples:** empty system after restart; second confirm; replay of already-`ready` orders from `placed`.

---

## 8. Important trade-offs and credible alternatives

### Trade-offs you should be ready to defend

| Choice | You gain | You give up |
|---|---|---|
| At-least-once + idempotent effects vs ack-before-process | No silent loss | Must design for duplicates and show attempts vs effects |
| Per-order isolation vs global FIFO | Burst survives one slow restaurant | Global diner arrival order |
| Same-store `next_action` vs a broker | Closes Timeline B without dual-write; explainable | Looks less like Uber/DoorDash blogs |
| Wait-until-deadline vs fail-fast on blips | Avoid DoorDash cancel-on-blip | Oldest age grows; need a stuck list |
| Auto-fail vs auto-accept on confirm timeout | No food cooked into a dead restaurant | More diner-visible failures |
| Sync cancel vs Care-shaped ticket | Finishable demo | Less fidelity to Grubhub money-moving races |
| Treat sims as non-idempotent | Honest Timeline D demo | Must remember effects in the pipeline |
| Shed under saturation vs accept-all | Machine stays useful | Rejects must be explained as policy, not loss |

### Credible alternatives that stay honest

| Alternative | When it is still honest | When it becomes cargo |
|---|---|---|
| Single worker thread, per-order version guards | One machine, modest burst, simpler crash story | If you then claim “we don’t need isolation” and demo HOL |
| Sim honors idempotency keys; pipeline is thinner | You still retry on timeout and show key reuse in history | If you assume keys and never test a non-honoring sim |
| Operator “recheck stuck” button instead of an automatic scanner | Timeline B is still closable and visible | If the button is the only recovery and the demo never uses it |
| Two processes (intake + worker) on one machine | You want to demo crash of the worker without killing intake | If you add a network and call it microservices |
| Durable workflow library in-process | You can explain replay in five minutes and still have an order row | If the take-home becomes operating Cadence/Temporal |

### Disagreements this brief did not average away

| Disagreement | How this brief treats it |
|---|---|
| Workato default-no-retry vs SRE retries-will-happen | Retries exist; they are classified, bounded, and idempotent |
| Factor V vs Stripe client keys | Client key for place; server key for downstream transitions |
| Grubhub `CONFIRMED` vs assignment `being prepared` | Split: will-cook is confirmed; insert ACK is internal |
| Grubhub confirm-timeout action | Default fail, not auto-accept; number is a knob |
| DoorDash in-flight local-queue loss vs no lost orders | Do not copy the hole. Accepted IDs survive |
| Cadence fallback vs Cadence primary vs 2021 fulfillment rewrite | Need is durable progress. Engine is not evidence |
| Saga vs Helland one entity | One order entity. Compensation is a policy table, not a framework |
| Uber “no loss” definitions | Use fulfillment-style durability + conservation, not Chaperone counts |

---

## 9. Unresolved questions that must be decided during architecture design

Even with the defaults above, write these down when you pick the final shape:

1. The exact legal graph and pivot row. Cancel after `ready`? After `out_for_delivery`?
2. Numeric retry budget and confirm deadline for the demo. Workato’s 3, Eats’ 11.5 minutes, and Grubhub’s 15/25 are not laws.
3. Whether the sim honors keys, or only the pipeline remembers. That is where Timeline D is proven.
4. One process versus intake + worker processes.
5. Whether money (hold on confirm, capture on deliver) exists at all. If yes, cancel-after-confirm is a financial event.
6. Whether scheduled / `ANTICIPATED` orders are in the demo (diner-cancel rights; time compression).
7. How much Workato interview dialect to surface. Typed errors and “Completed ≠ correct” transfer. Agent token-fabrication may not.

Further company-architecture research is negative-ROI. These are author choices, not missing blogs.

---

## 10. Sources ranked by relevance and credibility

1. [Workato: Error handling and monitoring](https://docs.workato.com/en/recipes/best-practices-error-handling) (updated 30 Jun 2026); [HTTP error handling](https://docs.workato.com/en/developing-connectors/http/error-message.html); [Triggers](https://docs.workato.com/en/recipes/triggers) (updated 7 Jul 2026); [Rerunning jobs](https://docs.workato.com/en/recipes/rerun-job.html) (updated 17 Jun 2026); [Jobs](https://docs.workato.com/en/recipes/jobs) — classify, default-no-retry, trigger de-dupe ≠ write de-dupe, Completed ≠ correct.
2. [Featonby, Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/); [Kafka Design — delivery semantics](https://kafka.apache.org/43/design/design/#message-delivery-semantics) — client keys; at-most / at-least / EOS fine print for external systems.
3. [Helland 2012](https://queue.acm.org/detail.cfm?id=2187821); [Helland 2007](https://www.cidrdb.org/cidr2007/papers/cidr07p15.pdf) — ambiguous outcomes; entity scope. Old, still applicable.
4. [Xia, Uber, 15 Feb 2018](https://www.uber.com/us/en/blog/reliable-reprocessing/); [Chu et al., 30 Aug 2021](https://www.uber.com/us/en/blog/kafka-async-queuing-with-consumer-proxy/) — at-least-once, consumer idempotency, non-blocking retry. Business path, not analytics.
5. [DoorDash Kafka, 3 Sep 2020](https://doordash.engineering/2020/09/03/eliminating-task-processing-outages-with-kafka/); [Drive Cadence fallback, 14 Aug 2020](https://doordash.engineering/2020/08/14/workflows-cadence-event-driven-processing/); [Checkout, 2 Feb 2021](https://careersatdoordash.com/blog/building-a-more-reliable-checkout-service-with-kotlin/) — shared fate, HOL, crash-loss, persist+resume+compensate. Old as architecture snapshots; failure modes still apply.
6. [Grubhub Standard Order Workflow](https://developer.grubhub.com/docs/3FrsnGXiR1Yt3Olm4sDWuM/order-workflow); [Orders](https://grubhub-developers.zendesk.com/hc/en-us/articles/115002713846-Orders); [Cancellations](https://developer.grubhub.com/docs/5OLUtSXDWplHjxEg7oTJqi/order-cancellations) — partner contract, not internals. Living docs, retrieved 14 Aug 2026.
7. [SRE: Monitoring](https://sre.google/sre-book/monitoring-distributed-systems/); [Cascading failures](https://sre.google/sre-book/addressing-cascading-failures/) (2016) — golden signals, retry storms. Not superseded.
8. [Tene / wrk2](https://github.com/giltene/wrk2) (2013–) — coordinated omission. The dinner-rush honesty bar.
9. [Brooker jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) (2015, updated May 2023); [timeouts and retries](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/) — still current; timeout ≠ “did not happen.”
10. [Uber Eats webhooks](https://developer.uber.com/docs/eats/guides/webhooks) — public contract: `event_id`, ACK ≠ accept, 11.5-minute auto-cancel. Not Eats internals.
11. [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests); [RFC 9110 §9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2); [Workato Factor V](https://www.workato.com/7factors/factor-v-safe-retries) — key contracts and the timeout-as-unknown rule.
12. [Microsoft outbox](https://learn.microsoft.com/en-us/azure/architecture/databases/guide/transactional-out-box-cosmos); [saga](https://learn.microsoft.com/en-us/azure/architecture/patterns/saga) — dual-write; compensation caveats. Saga is for database-per-service, not a one-machine mandate.
13. [Netflix SPS](https://netflixtechblog.com/sps-the-pulse-of-netflix-streaming-ae4db0e05f8a) (2015); [canonical log lines](https://brandur.org/canonical-log-lines) (2016) — business pulse; one-order evidence without a frontend project.
14. [DoorDash Aperture](https://doordash.engineering/2023/03/14/failure-mitigation-for-microservices-an-intro-to-aperture/) (2023); [StatsD → Prometheus](https://doordash.engineering/2023/08/01/how-doordash-migrated-from-statsd-to-prometheus/) — retry storms; metrics that die at peak. Aperture itself was test-env only.
15. [Uber fulfillment rewrite](https://www.uber.com/us/en/blog/fulfillment-platform-rearchitecture/) (27 Jul 2021); [Chaperone](https://www.uber.com/us/en/blog/chaperone-audit-kafka-messages/) (2016); [ads EOS](https://www.uber.com/us/en/blog/real-time-exactly-once-ad-event-processing/) (2021) — cite with class labels. Do not transfer analytics/infra guarantees to orders.
16. [QALM](https://www.uber.com/us/en/blog/qalm-qos-load-management-framework/) (2018); [Cinnamon](https://www.uber.com/us/en/blog/cinnamon-using-century-old-tech-to-build-a-mean-load-shedder/) (2023) — priority and anti-retry-storm. Mechanisms are fleet-scale; the priority lesson transfers.
17. [USE](https://www.brendangregg.com/usemethod.html); [RED](https://grafana.com/blog/the-red-method-how-to-instrument-your-services/); [Chaos principles](https://principlesofchaos.org/); [AWS FIS planning](https://docs.aws.amazon.com/fis/latest/userguide/getting-started-planning.html) — checklists and experiment shape. Production chaos does not bind a take-home.

**Do not use as architecture evidence:** third-party Grubhub OpenAPI enums; Vetora-style “50k orders/min Cadence sagas”; Workato Enterprise MCP “built-in compensations”; Grubhub Bytes Akka posts (2017–19) as the 2026 partner contract; Iguazu/feature-bus posts as the order pipeline.

---

## Design decision checklist

Use this when selecting and defending the final architecture. If you cannot point to a close for a line, the design is incomplete.

1. **Where is durable acceptance?** Order row committed before 201. Crash before commit ⇒ client retries the same place-key.
2. **Where is next work published?** Same commit as the order. Startup scan for leftovers.
3. **When is work acked?** After the transition or explicit fail is durable. Never before accept.
4. **How is one order serialized?** Version and lease per `order_id`. Global FIFO is not the story.
5. **What is the legal graph and pivot?** Written table. Cancel and progress use optimistic concurrency. Illegal edges counted.
6. **What is the place-key and the transition-key?** Client place-key. Server `(order_id, transition)`. Load-gen reuses them.
7. **What happens on timeout?** Unknown, not failure. Retry the same key or GET-then-apply. Do not mark failed without reconcile.
8. **What is retried versus not?** 429/503/timeout yes, if idempotent. 400/404/409/validation no. Bugs go to isolation.
9. **Where is the retry budget?** One layer, capped, jittered. No hidden SDK-plus-app double retry.
10. **How do spikes not melt the box?** Bounded in-flight and outbound slots. Shed load-gen first. Counts visible.
11. **How does a slow order not stall others?** Per-order isolation. Failed work leaves the live path.
12. **What if the restaurant never answers?** Deadline, then explicit fail (default). No auto-accept.
13. **What if the worker dies?** Lease expires; successor resumes; conservation holds; no second effect.
14. **What is a harmful duplicate in the demo?** Second ticket or second dispatch. Attempts may rise; effects stay one.
15. **How do you show recovery?** Oldest age peaks then falls. Drain rate positive. State is not wiped.
16. **What do the three panes show?** Pulse and occupancy; rates, backlog, retries, deps; conservation and one-order history.
17. **Is the load test honest?** Open-loop, or closed-loop is labeled. Accept p99 is not the rush proof.
18. **What did you refuse to build?** No Kafka, Cadence, microservices, saga runtime, or Grafana-as-correctness — and you can say why.
