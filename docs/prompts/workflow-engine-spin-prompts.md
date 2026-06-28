# Workflow Engine Coding Spin Prompts

ใช้ไฟล์นี้เป็นชุด prompt สำหรับเปิด new chat แล้วสั่ง coding ต่อทีละ spin.

> Historical: spins ชุดนี้ถูกทำครบและถูกแทนที่ด้วย milestone checklist ใน
> `docs/plans/workflow-engine-coding-plan.md` แล้ว อย่าใช้ Recommended Order ด้านล่าง
> เป็นสถานะปัจจุบันของระบบ.

## Prompt ตั้งต้นทุก Spin

```text
Repo: /Users/seal/Documents/GitHub/atlas-control-plane

อ่าน README.md, docs/plans/workflow-engine-plan.md, atlas/db.py, atlas/app.py,
atlas/jobs.py ก่อนแก้

ห้ามแก้ thClaws repo
ใช้ stdlib เท่านั้น ห้ามเพิ่ม dependency ถ้าไม่จำเป็น
ทำ diff ให้น้อยที่สุด แต่ต้องครบและ test/check ได้
หลังทำเสร็จให้สรุปไฟล์ที่แก้ + วิธีทดสอบ
ยังไม่ต้อง commit/push เว้นแต่ผมสั่ง
```

## Spin 1: Workflow DB + CRUD

```text
ทำ Phase 1 เริ่มจาก database layer ก่อน

เพิ่มตารางตาม docs/plans/workflow-engine-plan.md:
- workflow_definitions
- workflow_runs
- workflow_nodes
- workflow_edges
- artifacts
- workflow_triggers
- workflow_trigger_events

เพิ่ม method ใน atlas/db.py สำหรับ:
- create/list/get/update/delete workflow definitions
- create/get/list workflow runs
- create/list workflow triggers
- append/list trigger events
- create/list artifacts

ยังไม่ต้องทำ runner จริง
เพิ่ม check เล็ก ๆ ถ้ามี pattern test/self-check ใน repo
```

## Spin 2: Graph Validation + Prompt Rendering

```text
เพิ่ม atlas/workflows.py แบบ minimal

ทำ:
- validate_workflow_graph(graph, policy)
- validate node/edge/start
- reject missing nodes, bad edge target, duplicate node id
- detect cycle แล้วบังคับต้องมี loop guard ใน policy
- render_prompt(template, input, artifacts, run/node/job metadata)
- missing variable ให้ error ชัดเจน

รองรับ condition แค่ {"type":"always"} ก่อน
เพิ่ม runnable check/test สำหรับ validation และ prompt rendering
ยังไม่ต้อง dispatch job จริง
```

## Spin 3: Workflow Runner Minimal

```text
ต่อ workflow runner ใน atlas/workflows.py ให้รัน workflow แบบ Phase 1

ทำ:
- create workflow_run
- enqueue start node
- execute linear graph และ fan-out
- worker node ใช้ JobService เดิมใน atlas/jobs.py
- เก็บ node runtime state
- เก็บ artifact จาก assistant text ลง artifacts table
- ใช้ output key ตัวแรกของ node.outputs
- mark run succeeded/failed

ยังไม่ต้องทำ condition DSL อื่นนอกจาก always
ยังไม่ต้อง schedule
เพิ่ม fake/minimal check ให้รัน linear workflow ได้
```

## Spin 4: Workflow API

```text
เพิ่ม API ใน atlas/app.py สำหรับ workflow Phase 1

Endpoints:
- GET /api/workflows
- POST /api/workflows
- GET /api/workflows/{id}
- PUT /api/workflows/{id}
- DELETE /api/workflows/{id}
- POST /api/workflows/{id}/validate
- POST /api/workflow-runs
- GET /api/workflow-runs
- GET /api/workflow-runs/{id}
- GET /api/workflow-runs/{id}/artifacts

ใช้ db/workflows helpers ที่มีอยู่
error response ต้องชัดเจน
ยังไม่ต้อง UI
เพิ่ม curl examples ใน README สั้น ๆ
```

## Spin 5: Manual + Schedule Triggers

```text
ทำ workflow triggers ตามแผนแบบ minimal

เพิ่ม API:
- GET /api/workflow-triggers
- POST /api/workflow-triggers
- PUT /api/workflow-triggers/{id}
- DELETE /api/workflow-triggers/{id}
- POST /api/workflow-triggers/{id}/fire
- GET /api/workflow-triggers/{id}/events

รองรับ trigger:
- manual
- schedule แบบ interval_minutes
- daily local time ถ้าทำได้โดยไม่เยอะ

เพิ่ม scheduler thread ใน Atlas process เดิม poll ทุก 30-60 วินาที
ใช้ dedupe_key/last_fired_at/next_fire_at กันยิงซ้ำ
ยังไม่ต้อง cron เต็ม
```

## Spin 6: AI-Assisted Workflow Builder

```text
เพิ่ม workflow builder แบบไม่เพิ่ม provider ใหม่

แนวทาง:
- ใช้ worker ปกติที่ role/tag เป็น workflow_builder
- POST /api/workflows/draft รับ plain_language_prompt
- Atlas ส่ง context ให้ worker: workers, workspaces, templates, node types,
  condition DSL, trigger types
- worker ต้องตอบ JSON draft: name, description, graph, policy, triggers,
  explanation, warnings
- Atlas validate draft ก่อนส่งกลับ
- ถ้าไม่มี workflow_builder worker ให้ return error ที่บอกวิธีตั้งค่า

เพิ่ม:
- POST /api/workflows/{id}/explain
- POST /api/workflows/{id}/repair แบบ minimal ถ้าทำได้ไม่เยอะ

ยังไม่ต้อง runtime manager
```

## Spin 7: Workflow UI

```text
เพิ่ม UI ใน atlas/static/index.html, app.js, styles.css

ทำแบบ form/table ไม่ต้อง drag-drop:
- Workflow Definitions list
- create/edit workflow JSON
- validate button
- plain-language draft card เรียก /api/workflows/draft
- Workflow Runs list/detail
- run workflow button
- artifacts view
- Triggers list/create/fire

ระวัง UX เดิม worker/workspace อย่าให้รกเพิ่ม
ไม่ต้องทำ canvas editor
ทดสอบเปิดเว็บแล้วใช้งาน manual workflow ได้
```

## Spin 8: Conditional Workflow Phase 2

```text
ทำ Phase 2 เฉพาะ condition DSL และ loop guard

เพิ่ม condition types:
- artifact_equals
- artifact_in
- max_iterations_below

ทำ:
- JSON artifact parsing เมื่อ node.output_format = "json"
- evaluate outgoing edges ตาม condition
- loop counters
- pause/fail เมื่อ guard trip
- trigger event history แสดงใน API/UI ถ้ายังไม่ได้ทำ

เพิ่ม test/check:
- fact_checker approved -> anchor
- needs_more_sources -> reporter
- loop หยุดที่ max iteration
```

## Recommended Order

```text
Spin 1-4: manual workflow ใช้งานได้จริง
Spin 5: schedule/trigger
Spin 6: AI builder
Spin 7: UI
Spin 8: condition/loop
```
