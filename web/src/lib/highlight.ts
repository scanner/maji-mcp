/**
 * Split a text into the parts a fuzzy search matched and the parts it didn't, so the
 * matched characters can be marked without building markup from strings.
 */
export interface Segment {
  text: string;
  matched: boolean;
}

export function segments(text: string, ranges: number[][]): Segment[] {
  const parts: Segment[] = [];
  let at = 0;
  for (const range of [...ranges].sort((a, b) => (a[0] ?? 0) - (b[0] ?? 0))) {
    const [start = 0, end = 0] = range;
    if (start > at) parts.push({ text: text.slice(at, start), matched: false });
    if (end > start) parts.push({ text: text.slice(Math.max(start, at), end), matched: true });
    at = Math.max(at, end);
  }
  if (at < text.length) parts.push({ text: text.slice(at), matched: false });
  return parts;
}
