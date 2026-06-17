import type { SearchResult, SearchFilters } from "./types";

const API_BASE = "http://127.0.0.1:8765";

export async function searchDocuments(
  query: string,
  filters: SearchFilters,
  limit: number = 5
): Promise<SearchResult[]> {
  const res = await fetch(`${API_BASE}/api/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      date_from: filters.date_from,
      date_to: filters.date_to,
      extensions: filters.extensions,
      limit,
    }),
  });
  if (!res.ok) throw new Error(`Search failed: ${res.statusText}`);
  return res.json();
}

export async function getIndexStatus(): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/api/status`);
  if (!res.ok) throw new Error(`Status fetch failed: ${res.statusText}`);
  return res.json();
}

export async function openFile(path: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/api/open-file?path=${encodeURIComponent(path)}`,
    { method: "POST" }
  );
  if (!res.ok) throw new Error(`File open failed: ${res.statusText}`);
}

export async function triggerRescan(): Promise<void> {
  await fetch(`${API_BASE}/api/index/scan`, { method: "POST" });
}
