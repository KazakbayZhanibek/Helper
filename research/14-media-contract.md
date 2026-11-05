# Real media implementation contract

All endpoints are tenant scoped under `/api/v1/orgs/{organization_id}` and require an active writer membership and session CSRF. Original files stay under PRIVATE_MEDIA_ROOT and have no public URL.

* `POST /instructions/{id}/uploads`, JSON `{filename,size_bytes,language,rights_confirmed:true}`, Idempotency-Key required. Returns 201 `{id,upload_id,state,expires_at,max_size_bytes,max_duration_ms}`. A new instruction must have no source. Size limit 500 MiB, reserved under an organization lock; quota defaults to 20 GiB.
* `POST /uploads/{id}/content`, `application/octet-stream`, raw file body. Returns 202 `{upload_id,asset_id,job_id,state:"queued"}`. Completion is idempotent: repeating it returns the same identifiers. Concurrent receiving returns 409. The server reads bounded chunks after authorization, verifies actual bytes and SHA-256, and stores only a UUID private path. Aborted uploads must be restarted with a new session.
* `DELETE /uploads/{id}` cancels an incomplete session and releases its reservation once; returns 204. Completed sessions cancel their active preparation job.
* `GET /instructions/{id}/jobs` returns `{items:[Job]}` newest first. `GET /jobs/{id}` returns Job. `POST /jobs/{id}/retry` requires Idempotency-Key and returns 202 Job. `POST /jobs/{id}/cancel` returns 202 Job.
* Job: `{id,instruction_id,asset_id,kind,state,stage,attempts,error_code,error_summary,retry_allowed,manual_allowed,cancel_requested,result}` plus timestamps. States queued/running/done/failed/cancelled. Kinds prepare_media/transcribe/draft_steps. Processing does not change working-copy workflow state.

Integration: install `media_pipeline`, include `media_pipeline.urls` under the org prefix. `media_pipeline.jobs.enqueue_job(member,instruction,kind,payload=None,input_hash='')` and `job_data(job)` are shared with AI. `ProcessingJob` owns durable queue state. AI dispatch is `ai_drafts.worker.process_job(job,context)` returning a result dictionary. Context provides `check()`, `stage(label)`, `run(argv,timeout)`, `lease_token`, and `commit_guard()` (transaction-safe lease verification).

MediaAsset fields: `original_key`, `original_size_bytes`, `sha256`, `audio_key`, `thumbnail_key`, `has_audio`, `width`, `height`; duration nullable while quarantined; `storage_key` is the derived playback only. Status quarantined/validating/ready/rejected/deleted. No original URL is serialized.

## Local queue decision

Redis is not installed in this runnable local slice. A database-backed durable queue plus `python backend/manage.py processing_worker` provides one sequential worker, leases, heartbeat, retries and cancellation. It keeps the same job contract planned for a dedicated production queue. Run one worker in local SQLite mode; PostgreSQL is required for concurrent workers and production. This is an explicit deployment deviation, not a claim that an in-process thread is durable.

## Upload transport decision

The content endpoint uses a raw binary body rather than multipart. All metadata is validated at initialization. This lets authorization and CSRF complete before the body is consumed, and avoids multipart pre-parsing or in-memory buffering. Reverse proxies should impose a 500 MiB body cap and an upload timeout. Private UUID files have no user-controlled filenames.

Validation uses FFprobe with local-file-only protocols and the MOV/MP4 demuxer. Limits: 10 seconds–10 minutes, one H.264/HEVC video stream, optional AAC/PCM audio, up to 3840×2160 in either orientation. FFmpeg produces H.264/AAC playback bounded by 1280×720 (or portrait 720×1280), JPEG thumbnail, and mono PCM 16 kHz WAV. Child processes are invoked without a shell, with bounded time/output and two encoding threads. A failed/cancelled/stale attempt cannot publish derived paths.

## Verification and remaining deployment work

Implemented tenant guards for upload/job relationships at the database level, actual upload byte/hash verification, reservation expiry, private file permissions, CSRF-protected binary transport, stale-attempt fencing, process-group cancellation, finite subprocess timeouts, and a heartbeat around external provider calls. Ready media is preserved if cancellation races with a committed result and the job is retried. Disabled users, revoked memberships, suspended organizations and deleted sources stop processing. Source integrity is rechecked before decoding. The worker expires upload reservations every minute and removes unreferenced attempt directories only after 24 hours, retaining live leases and published paths.

Tests in `backend/tests/test_uploads.py` and `backend/tests/test_media_pipeline.py` exercise a real 10.2-second synthetic H.264/AAC file through HTTP upload → FFprobe → FFmpeg → protected Range playback; remaining cases cover quotas, invalid data, real CSRF cookie/header, roles/tenants, raw database guard violations, disabled users, leases, cancellation, retry, and subprocess timeouts. This fixture validates processing, not real speech recognition or cloud quality; those belong to the independent AI checks.

Current local limits: quota accounting measures original bytes; derivatives are separately bounded by process output limits but not yet charged to the organization quota. Temporary provider failures expose an explicit bounded retry action; timed automatic retry/backoff and a production queue broker remain deployment work. A reverse proxy must bound body size and upload/read time because a WSGI read can block before application code regains control. Completed originals are retained privately; complete instruction deletion/tombstones and backup retention remain outside this upload slice. Original source replacement remains prohibited, including rejected video; the UI directs users to create another instruction.
