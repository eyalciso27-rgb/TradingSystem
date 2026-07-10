/** Server-sent-events encoding for the live status stream (IMP-1101). */

export function encodeSseEvent(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}
