import { ImagePlus, X } from 'lucide-react';

import type { PendingAttachment } from '../types';

export function AttachmentPicker({ onFiles, compact = false }: { onFiles: (files: FileList | null) => void; compact?: boolean }) {
  return (
    <label className={`attachment-picker ${compact ? 'is-compact' : ''}`} title="上传图片">
      <ImagePlus size={15} />
      {!compact ? <span>图片</span> : null}
      <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple onChange={(event) => { onFiles(event.target.files); event.currentTarget.value = ''; }} />
    </label>
  );
}

export function AttachmentTray({ attachments, onRemove, compact = false }: { attachments: PendingAttachment[]; onRemove: (id: string) => void; compact?: boolean }) {
  if (!attachments.length) return null;
  return (
    <div className={`attachment-tray ${compact ? 'is-compact' : ''}`} aria-label="待发送图片">
      {attachments.map((attachment) => (
        <div className="attachment-chip" key={attachment.id}>
          <img src={attachment.previewUrl} alt={attachment.filename} />
          <span title={attachment.filename}>{attachment.filename}</span>
          <small>{formatBytes(attachment.size)}</small>
          <button type="button" onClick={() => onRemove(attachment.id)} title="移除图片" aria-label={`移除 ${attachment.filename}`}><X size={12} /></button>
        </div>
      ))}
    </div>
  );
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
