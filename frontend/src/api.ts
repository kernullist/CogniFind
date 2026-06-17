import type { SearchResult, SearchFilters, ModelInfo, IndexStatus } from "./types";

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

export async function getIndexStatus(): Promise<IndexStatus> {
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

export async function getModels(): Promise<{ active: string; available: ModelInfo[] }> {
  const res = await fetch(`${API_BASE}/api/model`);
  if (!res.ok) throw new Error(`Model fetch failed: ${res.statusText}`);
  return res.json();
}

export async function setModel(modelKey: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/model`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_key: modelKey }),
  });
  if (!res.ok) throw new Error(`Model switch failed: ${res.statusText}`);
}
