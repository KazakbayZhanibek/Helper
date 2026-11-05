# Первый работающий API — локальный ручной срез

Base `/api/v1`, JSON snake_case, cookie session + X-CSRFToken (cookie `csrftoken`). Vite proxy `/api` → `127.0.0.1:8000`. Fetch credentials same-origin. GET `/auth/csrf` returns `{csrf_token}`; POST login `{email,password}` returns same as GET me:

```ts
{user:{id,email,display_name}, organizations:[{id,name,role}], demo_mode:boolean}
```

POST logout → 204. Ошибки `{error:{code,message,field_errors,request_id,retryable,details}}`.

GET `/orgs/{org}/instructions?q=&status=&language=` → `{items:[InstructionSummary],next_cursor:null,total:number}`.

```ts
type InstructionSummary = {id:string,title:string,purpose:string,language:'ru'|'kk'|'mixed',status:'draft'|'in_review'|'published',step_count:number,duration_ms:number|null,current_version_id:string|null,version_number:number|null,updated_at:string,owner_name:string};
type Step = {id:string,ordinal:number,title:string,description:string,start_ms:number,end_ms:number,origin:'manual',checked:boolean,checked_by:string|null,evidence_segment_ids:[]};
type WorkingCopy = {id:string,instruction_id:string,revision:number,etag:string,editable:boolean,is_open:boolean,workflow:'draft'|'in_review',title:string,purpose:string,language:string,steps:Step[],source:{id:string,duration_ms:number,status:string,playback_url:string}|null,return_reason:string};
type Version = {id:string,version_number:number,published_at:string,published_by:string,snapshot:{title:string,purpose:string,language:string,steps:Step[],source:{id:string,duration_ms:number,status:string,playback_url:string}}};
```

GET `/instructions/{id}` → `{...InstructionSummary,publication:Version|null,working_copy:WorkingCopy|null}` (reader gets null working_copy; draft returns 404).
GET `/instructions/{id}/working-copy` → WorkingCopy with ETag header.
POST `/instructions` `{title,purpose,language,demo_source?:true}` → 201 WorkingCopy + `Idempotency-Key` required. `demo_source` works only DEMO_MODE=true and attaches a new protected asset referencing synthetic fixture (no real video upload).
PUT `/instructions/{id}/working-copy` `{title,purpose,language,steps:[{id:string|null,title,description,start_ms,end_ms,evidence_segment_ids:[]}]}` → WorkingCopy. If-Match required, 428 missing, 412 stale. No server-authored fields sent.
POST `/submit-review` `{}` → WorkingCopy (If-Match).
POST `/withdraw-review` `{}` → WorkingCopy (If-Match).
POST `/return-to-draft` `{reason:string}` (10–2000 chars) → WorkingCopy (If-Match).
POST `/review/check-step` `{step_id:string,checked:boolean}` → WorkingCopy (If-Match), reviewer/admin only; in_review only.
POST `/publish` `{confirmations:{order:true,terminology:true,completeness:true,relevance:true,privacy:true}}` → 201 Version, requires If-Match + Idempotency-Key. All steps checked; source ready; in_review. Closes draft.
POST `/create-draft` `{source_version_id:string}` → 201 WorkingCopy. Existing open draft →409.
GET `/versions` → `{items:Version[]}`. GET `/versions/{version_id}` → Version (author/reviewer/admin only).
POST `/withdraw-publication` `{reason:string,current_version_id:string}` → InstructionSummary (reviewer/admin).
GET `/orgs/{org}/assets/{asset}/playback` → protected MP4, Range supported for local fixture, membership and publication checked on every request.

Seed users: admin@demo.local, author@demo.local, reviewer@demo.local, reader@demo.local. Password supplied by operator seed command/env. Do not hardcode credential in UI. Real uploads, invitations/reset, deletion, external AI and async worker remain unimplemented and must not be presented as available.
