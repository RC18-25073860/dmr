export type ViewName = 'dashboard' | 'register' | 'files' | 'upload' | 'logs' | 'users';
export type UploadStage = 'select' | 'processing' | 'review' | 'complete';
export type UserRole = 'admin' | 'document_control' | 'reader' | 'uploader';
export type PermissionName = 'read' | 'download' | 'preview' | 'analyze' | 'review' | 'commit' | 'classify' | 'sync' | 'resolve' | 'notify' | 'manage_users' | 'history' | 'audit' | 'correct_archive';

export interface UserScope {
  registrant: string;
  project_category: string;
  product: string;
}

export interface PublicUser {
  id: string;
  username: string;
  display_name: string;
  role: UserRole;
  active: boolean;
  must_change_password?: boolean;
  edit_token?: string;
  scopes: UserScope[];
  can_download: boolean;
  can_history: boolean;
  identity_source: 'local' | string;
  nas_account_configured?: boolean;
  nas_account?: string;
  nas_check_status?: string;
}

export interface UserAuditEntry {
  id: string;
  time: string;
  actor_id: string;
  actor_name: string;
  target_id: string;
  target?: { id?: string; username?: string; display_name?: string };
  action: string;
  detail: unknown;
}

export interface DmrRecord {
  metadata?: { version_rule?: string; document_version?: string; version_date?: string };
  edit_token?: string;
  id: string;
  no: string;
  name: string;
  registrant: string;
  project_category?: string;
  project_category_source?: string;
  type: string;
  product: string;
  module: string;
  version: string;
  previous?: string;
  file: string;
  date: string;
  sourceType?: string;
  linked_bom?: string;
}

export interface LibraryFile {
  metadata?: { version_rule?: string; document_version?: string; version_date?: string };
  id: string;
  recordId: string;
  recordNo: string;
  name: string;
  version: string;
  status: string;
  date: string;
  source: string;
  size: string;
  type: string;
  storage: string;
  sha256: string;
  linked_bom?: string;
  link_status?: string;
  sync_status?: string;
  sync_error?: string;
  nas_path?: string;
  synced_at?: string;
  storage_mode?: string;
}

export interface NasAccountStatus {
  account: string;
  password_configured: boolean;
  check_status: 'unverified' | 'pending' | 'checking' | 'verified' | 'failed' | string;
  check_error?: string;
  checked_at?: string | null;
}

export interface StorageServiceStatus {
  configured: boolean;
  online: boolean;
  last_seen?: string | null;
  last_seen_at?: string | null;
  read_base_url?: string;
  message?: string;
}

export interface StorageSyncFile {
  id: string;
  file_id?: string;
  record_id?: string;
  name: string;
  target_name?: string;
  submitted_by?: string;
  owner_display_name?: string;
  status: 'pending' | 'transferring' | 'blocked' | 'synced' | string;
  error?: string;
  last_error?: string;
  attempts?: number;
  account_changed?: boolean;
  target_path?: string;
  synced_at?: string;
  updated_at?: string;
}

export interface StorageSyncOverview extends StorageServiceStatus {
  files?: StorageSyncFile[];
  jobs?: StorageSyncFile[];
}

export interface NasDirectoryMapping {
  id?: string;
  record_id?: string;
  record_no?: string;
  registrant: string;
  project_category: string;
  product: string;
  module: string;
  rule?: string;
  nas_path?: string;
  target_path: string;
  active?: boolean;
  enabled: boolean;
  updated_at?: string;
}

export interface DmrException {
  id: string;
  title: string;
  detail: string;
  type: string;
  recordId?: string;
  itemId?: string;
  batchId?: string;
  recordNo?: string;
  recommendation?: string;
  status: string;
}

export interface OperationLog {
  time: string;
  user: string;
  action: string;
  object: string;
  change: string;
  source: string;
  result: string;
}

export interface NotificationItem {
  id: string;
  time: string;
  title: string;
  detail: string;
  product: string;
  read: boolean;
}

export interface FileRuleMetadata {
  management_kind?: string; document_role?: string; software_branch?: string; trial_flag?: boolean;
  version_semantic?: string; identifiers?: Record<string, string>; dates?: Record<string, string>;
  field_evidence?: {field: string; value: string; source?: string; scope?: string}[];
}

export interface BatchItem {
  metadata?: FileRuleMetadata & { review_note?: string; review_pending?: boolean; version_rule?: string; version_date?: string; document_version?: string; email_source?: { mail_case?: 'ecn' | 'bom' | 'other'; subject?: string } };
  edit_token?: string;
  id: string;
  batch_id: string;
  fileName: string;
  recordId?: string;
  contextRecordId?: string;
  code?: string;
  registrant?: string;
  project_category?: string;
  project_category_source?: string;
  type?: string;
  product?: string;
  module?: string;
  version?: string;
  linked_bom?: string;
  notes?: string | string[];
  current?: string;
  detected?: string;
  status: string;
  recommendation?: string;
  sha256: string;
  size: number;
  result?: string;
}

export interface SyncChange {
  source?: string;
  object?: string;
  detail?: string;
  result?: string;
  status?: string;
}

export interface ServerState {
  records: DmrRecord[];
  libraryFiles: LibraryFile[];
  exceptions: DmrException[];
  operationLogs: OperationLog[];
  notifications: NotificationItem[];
  batches: Array<{ id?: string; batch_id?: string; status: string }>;
  latestSyncChanges: SyncChange[];
  latestSyncTime: string;
  latestSyncBatch: string;
  latestSyncTrigger: string;
  latestConfirmed: boolean;
  latestConfirmedBy: string;
  currentUser?: PublicUser;
  permissions?: PermissionName[];
}
