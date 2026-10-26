import type { DmrRecord, LibraryFile } from './types';

export const normalize = (value: string) => value.normalize('NFKC').toLocaleLowerCase().replace(/\s+/g, ' ').trim();

// Search only records already visible to this user; include their current attachments.
export function matchesRecord(record: DmrRecord, query: string, files: LibraryFile[]) {
  const terms = normalize(query).split(' ').filter(Boolean);
  const attachments = files.filter(file => file.recordId === record.id && file.version === record.version);
  const haystack = normalize([record.no, record.name, record.product, record.registrant,
    record.project_category, record.module, record.version, record.file,
    ...attachments.map(file => file.name)].filter(Boolean).join(' '));
  return terms.every(term => haystack.includes(term));
}

export function matchesText(query: string, values: Array<string | undefined>) {
  const haystack = normalize(values.filter(Boolean).join(' '));
  return normalize(query).split(' ').filter(Boolean).every(term => haystack.includes(term));
}
