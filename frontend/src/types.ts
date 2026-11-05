export type Role = "admin" | "author" | "reviewer" | "reader";
export type Language = "ru" | "kk" | "mixed";
export type Organization = { id: string; name: string; role: Role };
export type Session = {
  user: { id: string; email: string; display_name: string };
  organizations: Organization[];
  demo_mode: boolean;
};
export type Source = {
  id: string;
  duration_ms: number | null;
  status: string;
  playback_url: string | null;
  is_demo?: boolean;
  has_audio?: boolean | null;
  thumbnail_url?: string | null;
};
export type Step = {
  id: string | null;
  ordinal: number;
  title: string;
  description: string;
  start_ms: number;
  end_ms: number;
  origin: "manual" | "ai" | "ai_edited";
  checked: boolean;
  checked_by: string | null;
  evidence_segment_ids: string[];
  evidence_frame_ids?: string[];
  evidence_frames?: VisualFrame[];
  evidence_visual_session_id?: string | null;
  evidence_transcript_revision?: number | null;
  warning_codes?: string[];
};
export type WorkingCopy = {
  id: string;
  instruction_id: string;
  revision: number;
  etag: string;
  editable: boolean;
  is_open: boolean;
  workflow: "draft" | "in_review";
  title: string;
  purpose: string;
  language: Language;
  steps: Step[];
  source: Source | null;
  return_reason: string;
};
export type Version = {
  id: string;
  version_number: number;
  published_at: string;
  published_by: string;
  snapshot: {
    title: string;
    purpose: string;
    language: Language;
    steps: Step[];
    source: Source;
  };
};
export type InstructionSummary = {
  id: string;
  title: string;
  purpose: string;
  language: Language;
  status: "draft" | "in_review" | "published";
  step_count: number;
  duration_ms: number | null;
  current_version_id: string | null;
  version_number: number | null;
  updated_at: string;
  owner_name: string;
  source_is_demo?: boolean;
  thumbnail_url?: string | null;
};
export type Instruction = InstructionSummary & {
  publication: Version | null;
  working_copy: WorkingCopy | null;
};
export type Navigate = (path: string) => void;

export type ProcessingJob = {
  id: string;
  instruction_id: string;
  asset_id: string;
  kind: string;
  state: string;
  stage: string;
  attempts: number;
  error_code: string;
  error_summary: string;
  retry_allowed: boolean;
  manual_allowed: boolean;
  cancel_requested: boolean;
  result: {
    transcript_id?: string;
    transcript_revision?: number;
    proposal_id?: string;
    visual_session_id?: string;
    visual_proposal_id?: string;
    analysis?: VisualAnalysisProgress;
  };
};
export type AiCapability = {
  available: boolean;
  configured: boolean;
  provider: string;
  model: string;
  reason: string;
  external?: boolean;
};
export type AiCapabilities = {
  asr: AiCapability;
  step_drafts: AiCapability;
  experimental_languages: string[];
};
export type AiWarning = { code: string; message: string };
export type Transcript = {
  id: string;
  asset_id: string;
  revision: number;
  language: Language;
  provider: string;
  model: string;
  duration_ms: number;
  created_at: string;
  warnings: AiWarning[];
  segments: {
    id: string;
    ordinal: number;
    start_ms: number;
    end_ms: number;
    raw_text: string;
    text: string;
  }[];
};
export type DraftProposal = {
  id: string;
  instruction_id: string;
  transcript_id: string;
  transcript_revision: number;
  base_working_revision: number;
  provider: string;
  model: string;
  created_at: string;
  applied_at: string | null;
  stale: boolean;
  language: Language;
  warnings: AiWarning[];
  steps: {
    title: string;
    description: string;
    start_ms: number;
    end_ms: number;
    evidence_segment_ids: string[];
  }[];
};
export type VisualCapabilities = AiCapability & {
  live_supported: boolean;
  uploaded_video_supported: boolean;
  automatic_refinements?: boolean;
  budget?: VisualBudgetCapability;
  limits: {
    session_duration_ms: number;
    max_frames: number;
    max_frame_bytes: number;
    max_dimension: number;
    min_frame_interval_ms: number;
    max_analysis_frames: number;
    analysis_interval_ms: number;
    max_analysis_calls?: number;
    max_window_ms?: number;
  };
};
export type VisualBudgetCapability = {
  mode:
    "subscription_calls" | "local_calls" | "free_calls" | "usd" | "unavailable";
  currency: "USD" | null;
  default_max_calls: number;
  max_calls: number;
  default_max_cost_microusd: number | null;
  max_cost_microusd: number | null;
  call_upper_bound_microusd: number | null;
  pricing_version: string | null;
};
export type VisualAnalysisProgress = {
  scope: "full" | "window";
  windows_total: number;
  windows_completed: number;
  refinement_windows: number;
  refinements_completed: number;
  coverage_complete: boolean;
  sampling: "discrete_frames";
  unprocessed_ranges: { start_ms: number; end_ms: number }[];
  windows?: {
    id: string;
    start_ms: number;
    end_ms: number;
    kind: "overview" | "refinement";
    state: "pending" | "running" | "completed" | "failed";
    error_code: string | null;
  }[];
  budget?: VisualBudgetSnapshot;
};
export type VisualBudgetSnapshot = {
  mode:
    "subscription_calls" | "local_calls" | "free_calls" | "usd" | "unavailable";
  currency: "USD" | null;
  max_calls: number;
  calls_used: number;
  max_cost_microusd: number | null;
  spent_microusd: number | null;
  reserved_microusd: number | null;
  unknown_calls: number;
};
export type VisualFrame = {
  id: string;
  sequence: number;
  timestamp_ms: number;
  width: number;
  height: number;
  url: string;
};
export type VisualObservation = {
  id: string;
  kind: "action" | "state";
  subject: string;
  title: string;
  before: string | null;
  before_frame_id: string | null;
  movement: string | null;
  action_frame_ids: string[];
  after: string | null;
  after_frame_id: string | null;
  evidence_frame_ids: string[];
  start_ms: number;
  end_ms: number;
};
export type VisualGap = {
  id: string;
  reason:
    | "occluded"
    | "sampling_gap"
    | "cut"
    | "outside_recording"
    | "unclear_action";
  description: string;
  evidence_frame_ids: string[];
  start_ms: number;
  end_ms: number;
};
export type VisualProposalStep = {
  title: string;
  description: string;
  start_ms: number;
  end_ms: number;
  evidence_frame_ids: string[];
  observation_ids?: string[];
};
export type VisualProposal = {
  id: string;
  instruction_id: string;
  session_id: string;
  source?: Source | null;
  base_working_revision: number;
  provider: string;
  model: string;
  created_at: string;
  applied_at: string | null;
  window_start_ms?: number;
  window_end_ms?: number;
  preliminary: boolean;
  can_apply: boolean;
  stale: boolean;
  schema_version: "2" | "3";
  step_format?: "teaching_v1";
  presentation_format?: "process_story_v1";
  language: Language;
  steps: VisualProposalStep[];
  observations?: VisualObservation[];
  gaps?: VisualGap[];
  warnings: AiWarning[];
  frames: VisualFrame[];
  analysis?: VisualAnalysisProgress;
};
export type VisualSession = {
  id: string;
  instruction_id: string;
  asset_id: string | null;
  mode: "live" | "upload";
  state: "active" | "completed" | "cancelled" | "expired";
  base_working_revision: number;
  created_at: string;
  expires_at: string;
  frame_count: number;
  frames: VisualFrame[];
  observations: VisualProposal[];
  analysis?: VisualAnalysisProgress;
};
