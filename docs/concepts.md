# Atlas Concepts & Reference / นิยามและเอกสารอ้างอิง

This is the single reference for every term Atlas actually uses. The values
shown (node types, modes, condition types, kinds, policy keys, states) are the
exact literals the API accepts and the engine checks.

เอกสารนี้เป็นจุดอ้างอิงเดียวสำหรับนิยามทุกตัวที่ระบบ Atlas ใช้จริง ค่าที่แสดง
(ชนิด node, mode, condition, kind, policy, state) คือค่าตรงตัวที่ API รับและ
engine ตรวจสอบ ทุกค่าดึงมาจากซอร์สโค้ดจริง ไม่ใช่การคาดเดา

> See also / ดูเพิ่ม: [Workflow Examples](workflow-examples.md) ·
> [Architecture](architecture.md) ·
> [Web Guide (EN)](guides/web-user-guide-en.md) ·
> [คู่มือเว็บ (TH)](guides/web-user-guide-th.md)

## Contents / สารบัญ

1. [Core objects / วัตถุหลัก](#1-core-objects--วัตถุหลัก)
2. [Routing order / ลำดับการ route](#2-routing-order--ลำดับการ-route)
3. [Job states / สถานะ job](#3-job-states--สถานะ-job)
4. [Workflow run states / สถานะ workflow run](#4-workflow-run-states--สถานะ-workflow-run)
5. [Node types / ชนิด node](#5-node-types--ชนิด-node)
6. [Join modes / โหมด join](#6-join-modes--โหมด-join)
7. [Edge conditions / เงื่อนไข edge](#7-edge-conditions--เงื่อนไข-edge)
8. [Prompt variables / ตัวแปรใน prompt](#8-prompt-variables--ตัวแปรใน-prompt)
9. [Artifact kinds / ชนิด artifact](#9-artifact-kinds--ชนิด-artifact)
10. [Policy / นโยบายและ guard](#10-policy--นโยบายและ-guard)
11. [Manager decision / การตัดสินใจของ manager](#11-manager-decision-manager_decision_v1--การตัดสินใจของ-manager)
12. [Triggers / ทริกเกอร์](#12-triggers--ทริกเกอร์)
13. [Human gates & approvals / ประตูคนและการอนุมัติ](#13-human-gates--approvals--ประตูคนและการอนุมัติ)

---

## 1. Core objects / วัตถุหลัก

Atlas is the control plane; thClaws workers do the work. These are the records
Atlas persists in SQLite.

Atlas เป็น control plane ส่วน thClaws worker เป็นตัวทำงานจริง ด้านล่างคือเรกคอร์ด
ที่ Atlas เก็บใน SQLite

| Object | Meaning (EN) | ความหมาย (TH) |
| --- | --- | --- |
| **worker** | One thClaws API endpoint per machine/runtime | thClaws API หนึ่ง endpoint ต่อหนึ่งเครื่อง/รันไทม์ |
| **workspace** | A concrete project directory bound to a worker | directory ของโปรเจกต์จริงที่ผูกกับ worker |
| **conversation** | Atlas-level conversation identity | ตัวระบุบทสนทนาในระดับ Atlas |
| **session binding** | Maps an Atlas conversation to a thClaws `session_id` | จับคู่ conversation ของ Atlas กับ `session_id` ของ thClaws |
| **job** | One routed execution on a worker | การรันหนึ่งครั้งที่ถูก route ไปยัง worker |
| **job event** | Append-only event persisted from a worker's SSE stream | event แบบเขียนต่อท้าย เก็บจากสตรีม SSE ของ worker |
| **workflow definition** | Versioned graph + policy JSON | graph + policy JSON ที่มีเวอร์ชัน |
| **workflow run** | One execution of a definition | การรัน definition หนึ่งครั้ง |
| **workflow node / edge** | Persisted runtime state of graph nodes and edges | สถานะรันไทม์ของ node และ edge ในกราฟ |
| **workflow event** | Append-only run lifecycle timeline | ไทม์ไลน์ lifecycle ของ run แบบเขียนต่อท้าย |
| **artifact** | A typed entry on the workflow blackboard | ข้อมูลผลลัพธ์แบบมี kind บนกระดาน (blackboard) ของ workflow |
| **trigger / trigger event** | Automation source and its dedupe/run history | แหล่ง automation และประวัติการ dedupe/รัน |
| **audit log** | Operator and system actions | บันทึก action ของผู้ใช้และระบบ |

---

## 2. Routing order / ลำดับการ route

When a job is submitted, Atlas picks a worker/workspace in this strict order.
The first rule that resolves wins.

เมื่อมีการส่ง job, Atlas เลือก worker/workspace ตามลำดับนี้ กฎข้อแรกที่ตอบได้คือผู้ชนะ

1. Explicit `workspace_id` / ระบุ `workspace_id` ตรง ๆ
2. Explicit `worker_id` / ระบุ `worker_id` ตรง ๆ
3. Existing conversation's session binding / ใช้ session binding ของ conversation เดิม
4. Auto-route by online state, workspace key, company, tags, role, and prompt
   hints / route อัตโนมัติจากสถานะ online, workspace key, company, tags, role และคำใบ้ใน prompt

A flowchart of this is in the [web guide §4](guides/web-user-guide-en.md#4-command-jobs-and-handoffs).

---

## 3. Job states / สถานะ job

| State | Meaning (EN) | ความหมาย (TH) |
| --- | --- | --- |
| `queued` | Waiting to start | รอเริ่ม |
| `running` | Worker is executing | worker กำลังทำงาน |
| `cancel_requested` | Atlas accepted a cancellation request | Atlas รับคำขอยกเลิกแล้ว |
| `succeeded` | Completed successfully | สำเร็จ |
| `failed` | Failed; inspect events/error | ล้มเหลว; ดู event/error |
| `cancelled` | Cancelled | ยกเลิกแล้ว |

Cancellation is best effort: a job becomes `cancel_requested` first, and the
worker may already have produced side effects.

การยกเลิกเป็น best effort: job จะเป็น `cancel_requested` ก่อน และ worker อาจทำ
side effect ไปแล้ว

---

## 4. Workflow run states / สถานะ workflow run

| State | Meaning (EN) | ความหมาย (TH) |
| --- | --- | --- |
| `running` | Executing nodes | กำลังรัน node |
| `paused` | Paused by an operator; resume continues without repeating done nodes | ผู้ใช้สั่งหยุด; resume ทำต่อโดยไม่ทำ node ที่เสร็จแล้วซ้ำ |
| `waiting_for_human` | Blocked on a human gate | ติดรอที่ human gate |
| `recovery_required` | Atlas restarted mid-run; interrupted nodes need manual retry | Atlas restart กลางคัน; node ที่ค้างต้อง retry เอง |
| `succeeded` | All reachable nodes finished | node ที่ไปถึงได้เสร็จทั้งหมด |
| `failed` | A node failed or a guard tripped | node ล้มเหลวหรือ guard ทำงาน |
| `cancelled` | Cancelled by an operator | ผู้ใช้ยกเลิก |

After a restart, interrupted worker/manager nodes are **not** retried
automatically — use **Retry interrupted** only after reviewing duplicate
side-effect risk. A run lifecycle diagram is in the
[web guide §7](guides/web-user-guide-en.md#7-monitor-workflow-operations).

หลัง restart, node ที่ค้างจะ **ไม่** ถูก retry อัตโนมัติ — ใช้ **Retry interrupted**
หลังตรวจความเสี่ยง side effect ซ้ำเท่านั้น

---

## 5. Node types / ชนิด node

Every node has an `id` and a `type`. Only `worker` and `manager` nodes create
thClaws jobs and consume budget; `join` and `human_gate` run in the control
plane only.

ทุก node มี `id` และ `type` เฉพาะ `worker` และ `manager` ที่สร้าง job บน thClaws
และใช้ budget ส่วน `join` และ `human_gate` ทำงานในชั้น control plane เท่านั้น

### `worker`

Creates a thClaws job. / สร้าง job บน thClaws worker

| Field | Meaning (EN) | ความหมาย (TH) |
| --- | --- | --- |
| `role` | Routing role, e.g. `reporter` (auto-route) | role สำหรับ route เช่น `reporter` |
| `worker_id` / `workspace_id` | Pin to a specific worker/workspace | ระบุ worker/workspace ตรง ๆ |
| `prompt` | The task; supports prompt variables | งานที่สั่ง; ใช้ตัวแปร prompt ได้ |
| `outputs` | Artifact keys this node writes | คีย์ artifact ที่ node นี้เขียน |
| `output_format` | `json` parses the reply as JSON (node fails if unparseable) | `json` จะ parse คำตอบเป็น JSON (parse ไม่ได้ = node fail) |
| `budget_units` | Cost against `max_budget_units` (default `1`) | ต้นทุนเทียบ `max_budget_units` (ค่าเริ่มต้น `1`) |

The reply is stored under the **first** declared `outputs` key (parsed JSON when
`output_format: json`). / คำตอบถูกเก็บไว้ที่คีย์ `outputs` **ตัวแรก**

### `manager`

Proposes the allowed next node(s); Atlas validates and decides. / เสนอ node
ถัดไปภายใต้ข้อจำกัด; Atlas ตรวจสอบและตัดสิน

| Field | Meaning (EN) | ความหมาย (TH) |
| --- | --- | --- |
| `worker_id` | The worker that runs the manager | worker ที่รัน manager |
| `schema` | Output contract — `manager_decision_v1` | สัญญา output — `manager_decision_v1` |
| `prompt` / `budget_units` | As for worker nodes | เหมือน node แบบ worker |

See [§11](#11-manager-decision-manager_decision_v1--การตัดสินใจของ-manager).

### `join`

Joins fan-out branches. Creates no job. Has a `mode` (see [§6](#6-join-modes--โหมด-join))
and, for quorum, a `quorum` integer. Duplicate incoming edges count an upstream
once.

รวม branch ที่แตกออก ไม่สร้าง job มี `mode` (ดู [§6](#6-join-modes--โหมด-join)) และถ้าเป็น
quorum ต้องมี `quorum` (จำนวนเต็ม) edge ขาเข้าซ้ำนับ upstream เพียงครั้งเดียว

### `human_gate`

Pauses for a person. Creates no job. / หยุดรอคน ไม่สร้าง job

| Field | Meaning (EN) | ความหมาย (TH) |
| --- | --- | --- |
| `label` | Button/title shown to the operator | ข้อความที่แสดงให้ผู้ใช้ |
| `reason` | Why approval is needed | เหตุผลที่ต้องอนุมัติ |
| `choices` | For a choice gate: list of `{id, label}` | สำหรับ choice gate: รายการ `{id, label}` |

See [§13](#13-human-gates--approvals--ประตูคนและการอนุมัติ).

---

## 6. Join modes / โหมด join

A join node continues downstream only when its upstream branches satisfy the
mode. Downstream is scheduled exactly once.

join จะไปต่อ downstream เมื่อ branch ขาเข้าครบตามโหมด และ downstream ถูก schedule
เพียงครั้งเดียว

| Mode | Ready when… (EN) | พร้อมเมื่อ… (TH) |
| --- | --- | --- |
| `all` *(default)* | **every** declared upstream node has completed | upstream ที่ประกาศไว้ **ทุกตัว** complete |
| `any` | **at least one** upstream has completed | upstream **อย่างน้อยหนึ่งตัว** complete |
| `quorum` | the number of completed upstreams is **≥ `quorum`** | จำนวน upstream ที่ complete **≥ `quorum`** |

With `any` and `quorum`, the other branches keep running, but the join and its
downstream are never scheduled twice. Failed nodes do not traverse their
outgoing edges.

โหมด `any` และ `quorum`: branch ที่เหลือยังรันต่อ แต่ join และ downstream ไม่ถูก
schedule ซ้ำ node ที่ fail จะไม่เดินผ่าน edge ขาออกของตัวเอง

---

## 7. Edge conditions / เงื่อนไข edge

Each edge has a `condition`. If omitted it defaults to `always`. **Conditions
are independent** — two edges out of the same node are an OR, not an AND. There
is no expression engine; only these six types exist.

ทุก edge มี `condition` ถ้าไม่ใส่จะเป็น `always` **เงื่อนไขเป็นอิสระต่อกัน** — สอง edge
ที่ออกจาก node เดียวกันคือ OR ไม่ใช่ AND ไม่มี expression engine มีแค่หกชนิดนี้

| Type | Matches when… (EN) | Required fields |
| --- | --- | --- |
| `always` | always | — |
| `artifact_equals` | `artifact[path]` **equals** `value` | `artifact`, `value` (optional `path`) |
| `artifact_in` | `artifact[path]` **is in** `values[]` | `artifact`, `values` (optional `path`) |
| `manager_selected` | the manager selected `target` | `target` (a node id) |
| `human_selected` | the operator chose `choice` | `choice` (a string) |
| `max_iterations_below` | `node` has run **fewer than** `max` times | `node` (a node id), `max` (positive int) |

`path` walks a dot-path into a JSON artifact (e.g. `verdict`, or `items.0.id`
for lists). / `path` เดินเข้า JSON ของ artifact แบบ dot-path เช่น `verdict` หรือ
`items.0.id` สำหรับ list

`max_iterations_below` reads the per-node execution count and is the building
block for bounded loops; do not confuse it with the global `max_iterations`
policy guard ([§10](#10-policy--นโยบายและ-guard)).

`max_iterations_below` อ่านจำนวนครั้งที่ node ถูกรัน ใช้ทำลูปแบบมีขอบเขต อย่าสับสนกับ
`max_iterations` ที่เป็น guard ระดับ policy

---

## 8. Prompt variables / ตัวแปรใน prompt

Worker and manager `prompt` strings interpolate `{...}` placeholders from two
roots:

prompt ของ worker/manager แทนค่า `{...}` จากสองราก:

| Placeholder | Source (EN) | แหล่งข้อมูล (TH) |
| --- | --- | --- |
| `{input.X}` | The run input JSON | JSON ของ run input |
| `{artifact.KEY}` | An artifact's content by key | เนื้อหา artifact ตามคีย์ |

- Dot-paths walk into nested JSON, e.g. `{artifact.fact_check.verdict}`.
- Dict/list values are inserted as compact JSON.
- An unknown root raises `unknown prompt variable`; a missing path raises
  `missing prompt variable`.

- dot-path เข้า JSON ซ้อนได้ เช่น `{artifact.fact_check.verdict}`
- ค่า dict/list จะถูกใส่เป็น JSON แบบกระชับ
- รากที่ไม่รู้จัก → `unknown prompt variable`; path ที่ไม่มี → `missing prompt variable`

A `manager` node additionally reasons over the run state (`graph`,
`current_node`, `artifacts`, `counters`, `policy`) and must reply with
`manager_decision_v1` JSON.

node แบบ `manager` ยังพิจารณาสถานะ run (`graph`, `current_node`, `artifacts`,
`counters`, `policy`) และต้องตอบเป็น JSON `manager_decision_v1`

---

## 9. Artifact kinds / ชนิด artifact

Artifacts are the typed blackboard shared across the nodes of one run. Each
artifact is `{key, kind, content, metadata}`. Nodes write them; prompts
(`{artifact.KEY}`), edge conditions, and `artifact_created` triggers read them.

artifact คือกระดานข้อมูลที่ node ทุกตัวใน run เดียวกันใช้ร่วมกัน แต่ละชิ้นคือ
`{key, kind, content, metadata}` — node เขียนลงไป ส่วน prompt (`{artifact.KEY}`),
edge condition และ trigger `artifact_created` อ่านออกมา

| Kind | Behaviour (EN) | พฤติกรรม (TH) |
| --- | --- | --- |
| `json` | **Parsed on load** — enables dot-paths in conditions/prompts | **ถูก parse ตอนโหลด** — ใช้ dot-path ใน condition/prompt ได้ |
| `file_ref` | **A pointer to an uploaded file** (not the bytes) | **ตัวชี้ไปไฟล์ที่อัปโหลด** (ไม่ใช่ตัวไฟล์) |
| `text` | Plain string; the default for worker output | string ล้วน; ค่าเริ่มต้นของ output |
| `markdown` | Plain string, labelled markdown | string ล้วน ติดป้ายว่า markdown |
| `summary` | Plain string, labelled a summary | string ล้วน ติดป้ายว่าบทสรุป |
| `decision` | Plain string, labelled a decision | string ล้วน ติดป้ายว่าการตัดสินใจ |

Only two kinds change engine behaviour: **`json`** (content is `json.loads`-ed,
so `{artifact.fact_check.verdict}` and `path` conditions work) and **`file_ref`**
(a binary file Atlas stores and serves on demand). `text`, `markdown`,
`summary`, and `decision` are **semantic labels only** — the engine treats their
content as a plain string, though a label is still useful as an
`artifact_created` trigger filter.

มีแค่สอง kind ที่เปลี่ยนพฤติกรรมของ engine: **`json`** (content ถูก `json.loads`
จึงใช้ `{artifact.fact_check.verdict}` และ condition แบบ `path` ได้) และ
**`file_ref`** (ไฟล์ binary ที่ Atlas เก็บและส่งให้เมื่อขอ) ส่วน `text`, `markdown`,
`summary`, `decision` เป็น **ป้ายเชิงความหมายเท่านั้น** — engine มองเป็น string
ทั้งก้อน แต่ป้ายยังใช้เป็น filter ของ trigger `artifact_created` ได้

### Setting the kind / การตั้ง kind

- **Worker output** defaults to `text`; set the node's `output_format: "json"` to
  store `json`. / output ของ worker เป็น `text` โดยปริยาย; ตั้ง `output_format: "json"` เพื่อเก็บเป็น `json`
- **Manual** — `POST /api/artifacts` with any `kind` and inline `content`. /
  สร้างเองด้วย `POST /api/artifacts` ระบุ `kind` และ `content` ได้เลย
- **File upload** always produces a `file_ref` (below). / การอัปโหลดไฟล์ได้ `file_ref` เสมอ

### File upload (`file_ref`) / การอัปโหลดไฟล์

`POST /api/workflow-runs/{run_id}/files?key=...` (or **Monitor → select a run →
Upload file**) attaches a binary file to an existing run as a `file_ref`,
recording its filename, size, and SHA-256. Download it with
`GET /api/artifacts/{id}/content`. Default limit 10 MiB (`ATLAS_MAX_UPLOAD_BYTES`).

`POST /api/workflow-runs/{run_id}/files?key=...` (หรือ **Monitor → เลือก run →
Upload file**) แนบไฟล์ binary เข้ากับ run ที่มีอยู่ในรูป `file_ref` พร้อมบันทึก
filename, ขนาด, SHA-256 ดาวน์โหลดด้วย `GET /api/artifacts/{id}/content` ดีฟอลต์ 10 MiB

> **Important / สำคัญ:** a worker does **not** read an uploaded file
> automatically — `{artifact.KEY}` for a `file_ref` yields the pointer, not the
> file content. An upload is for **people** (a reviewer downloading it at a human
> gate) or for an **external system** that calls the content API itself, with an
> integrity hash tying the file to the run. /
> worker จะ **ไม่** อ่านไฟล์ที่อัปโหลดให้อัตโนมัติ — `{artifact.KEY}` ของ `file_ref`
> ได้แค่ตัวชี้ ไม่ใช่เนื้อไฟล์ การอัปโหลดมีไว้ให้ **คน** (รีวิวเวอร์ดาวน์โหลดดูตอน human
> gate) หรือ **ระบบภายนอก** ที่เรียก content API เอง โดยมี hash ผูกไฟล์กับ run ไว้ตรวจสอบ

### Example 1 — a `json` artifact drives a branch / ตัวอย่าง 1 — artifact `json` ใช้ตัดสินเส้นทาง

A `fact_checker` node sets `output_format: "json"` and replies
`{"verdict":"approved"}`. Atlas stores it as a `json` artifact, so the outgoing
edge can read the field by `path`:

node `fact_checker` ตั้ง `output_format: "json"` แล้วตอบ `{"verdict":"approved"}`
Atlas เก็บเป็น artifact `json` ทำให้ edge ขาออกอ่าน field ด้วย `path` ได้:

```json
{"from":"fact_checker","to":"anchor","condition":{"type":"artifact_equals","artifact":"fact_check","path":"verdict","value":"approved"}}
```

Without `output_format: "json"` the content would be a plain string and
`path: "verdict"` would resolve to nothing. Full graph:
[Fact Checker Approved Branch](workflow-examples.md#fact-checker-approved-branch).

ถ้าไม่ตั้ง `output_format: "json"` content จะเป็น string ทั้งก้อน และ `path: "verdict"`
จะ resolve ไม่ได้

### Example 2 — a `file_ref` upload for human review / ตัวอย่าง 2 — อัปโหลด `file_ref` ให้คนรีวิว

A contract-approval run pauses at a human gate. A person uploads the contract,
the reviewer downloads it to decide, then approves — the worker never reads the
PDF, the human does.

run อนุมัติสัญญาหยุดที่ human gate มีคนอัปโหลดไฟล์สัญญา รีวิวเวอร์ดาวน์โหลดไปดูแล้ว
กดอนุมัติ — worker ไม่ได้อ่าน PDF เลย คนเป็นคนอ่าน

```bash
# 1) run is waiting_for_human at the gate / run ค้างที่ human gate
curl -sS -X POST 'http://127.0.0.1:8787/api/workflow-runs/wfr_xxx/files?key=contract' \
  -H 'content-type: application/pdf' \
  -H 'x-filename: contract.pdf' \
  --data-binary @contract.pdf
# 2) reviewer downloads it to read / รีวิวเวอร์ดาวน์โหลดไปอ่าน
curl -sS http://127.0.0.1:8787/api/artifacts/art_xxx/content -o contract.pdf
# 3) Approve in Monitor → the run continues / กด Approve ใน Monitor แล้ว run ไปต่อ
```

The file and its SHA-256 stay tied to the run for audit. / ไฟล์พร้อม SHA-256 ผูกกับ
run ไว้ตรวจสอบย้อนหลัง

---

## 10. Policy / นโยบายและ guard

Policy bounds a run. When a guard trips, Atlas pauses or fails the run loudly
instead of continuing.

policy กำหนดขอบเขตของ run เมื่อ guard ทำงาน Atlas จะ pause หรือ fail แบบชัดเจน

| Key | Meaning (EN) | ความหมาย (TH) |
| --- | --- | --- |
| `max_jobs` | Max jobs per run | จำนวน job สูงสุดต่อ run |
| `max_iterations` | Max total iterations | จำนวนรอบรวมสูงสุด |
| `max_attempts_per_node` | Max executions of any one node | จำนวนครั้งสูงสุดต่อ node |
| `max_minutes` | Overall wall-clock limit | เวลารวมสูงสุด (นาที) |
| `requires_human_after_iterations` | Require one human approval once this many jobs have started | บังคับอนุมัติหนึ่งครั้งเมื่อ job เริ่มครบจำนวนนี้ |
| `max_budget_units` | Total budget; an abstract unit, **not** money or tokens | budget รวม; เป็นหน่วยนามธรรม **ไม่ใช่** เงินหรือ token |
| `allowed_worker_ids` | Allowlist of worker ids | allowlist ของ worker id |
| `allowed_workspace_ids` | Allowlist of workspace ids | allowlist ของ workspace id |
| `stop_on_first_failure` | Stop the run on the first failed branch; **default `true`** | หยุด run เมื่อ branch แรก fail; **ค่าเริ่มต้น `true`** |

With `stop_on_first_failure: false`, independent ready branches keep running, but
the run still finishes `failed` if any node failed. Each worker/manager node
costs `budget_units` (default `1`) against `max_budget_units`.

ถ้า `stop_on_first_failure: false` branch อิสระยังรันต่อ แต่ run ยังจบเป็น `failed`
ถ้ามี node ใด fail แต่ละ node แบบ worker/manager ใช้ `budget_units` (ค่าเริ่มต้น `1`)

---

## 11. Manager decision (`manager_decision_v1`) / การตัดสินใจของ manager

A manager node must return only this JSON. The manager proposes; Atlas
validates (allowed worker/workspace, iteration and budget guards, required
artifacts exist, no forbidden edge) and then decides.

node แบบ manager ต้องตอบเป็น JSON นี้เท่านั้น manager เป็นผู้เสนอ; Atlas ตรวจสอบ
(worker/workspace ที่อนุญาต, guard เรื่อง iteration/budget, artifact ที่ต้องมี,
ห้ามใช้ edge ต้องห้าม) แล้วจึงตัดสิน

| Field | Meaning (EN) | ความหมาย (TH) |
| --- | --- | --- |
| `stop` | `true` ends the run; `next` must be empty | `true` = จบ run; `next` ต้องว่าง |
| `reason` | Why this decision | เหตุผลของการตัดสินใจ |
| `next[]` | Selected actions, each `{node, input_artifacts[], instructions}` | action ที่เลือก แต่ละตัว `{node, input_artifacts[], instructions}` |

Only nodes reachable by a `manager_selected` edge from the manager may be
chosen. / เลือกได้เฉพาะ node ที่มี edge `manager_selected` ออกจาก manager เท่านั้น

```json
{
  "stop": false,
  "reason": "Research artifact is ready.",
  "next": [
    {"node": "writer", "input_artifacts": ["research"], "instructions": "Produce one concise draft."}
  ]
}
```

---

## 12. Triggers / ทริกเกอร์

A trigger starts a workflow run. `manual`, `schedule`, and `webhook` are fired
externally; the three internal-event types are fired only by Atlas. Atlas blocks
unguarded self-triggering to prevent infinite loops.

trigger ใช้เริ่ม workflow run ชนิด `manual`, `schedule`, `webhook` ยิงจากภายนอก
ส่วน internal event สามชนิดยิงโดย Atlas เท่านั้น Atlas กันการ self-trigger ที่ไม่มี
guard เพื่อไม่ให้เกิดลูปไม่รู้จบ

| Type | Fires on… (EN) | Config / filter |
| --- | --- | --- |
| `manual` | A manual Fire | `{}` |
| `schedule` | An interval or daily local time | `{"interval_minutes": N}` or `{"daily_time": "HH:MM"}` |
| `webhook` | An external POST to the trigger | `{}`; reuse a stable `dedupe_key` per event |
| `workflow_run_completed` | Another run finishing | filter: `source_workflow_definition_id`, `state` |
| `artifact_created` | An artifact being created | filter: `source_workflow_definition_id`, `key`, `kind` |
| `worker_status_changed` | A worker changing status | filter: `worker_id`, `status` |

Trigger events progress through `received` → `started`, or `ignored` (e.g.
duplicate `dedupe_key`) or `failed`.

trigger event ไล่สถานะ `received` → `started` หรือ `ignored` (เช่น `dedupe_key`
ซ้ำ) หรือ `failed`

API examples are in [Workflow Examples](workflow-examples.md).

---

## 13. Human gates & approvals / ประตูคนและการอนุมัติ

A `human_gate` node pauses the run as `waiting_for_human` and creates no job. A
gate can be decided **once**.

node `human_gate` ทำให้ run เป็น `waiting_for_human` และไม่สร้าง job ตัดสินใจได้
**ครั้งเดียว**

- **Normal gate** → **Approve** (state `approved`, continue) or **Reject**
  (state `rejected`, the run fails). / gate ปกติ → Approve (ไปต่อ) หรือ Reject (run fail)
- **Choice gate** → one button per declared `choice`; the chosen id is matched by
  `human_selected` edges, plus **Reject**. / choice gate → ปุ่มตามแต่ละ `choice`;
  id ที่เลือกจับคู่กับ edge `human_selected` พร้อมปุ่ม Reject
- The `requires_human_after_iterations` policy adds one approval pause after the
  given number of jobs have started, independent of explicit gate nodes. /
  policy `requires_human_after_iterations` เพิ่มจุดรออนุมัติหนึ่งครั้งหลัง job เริ่มครบจำนวน
  ที่กำหนด แยกจาก node gate ที่ประกาศไว้

Approval state literals: `pending` → `approved` / `rejected` (or a selected
choice). A cancelled run cancels pending approvals.

สถานะ approval: `pending` → `approved` / `rejected` (หรือ choice ที่เลือก) ถ้า run
ถูกยกเลิก approval ที่ค้างจะถูกยกเลิกด้วย
