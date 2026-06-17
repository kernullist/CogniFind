export interface SearchResult {
  file_path: string;
  file_name: string;
  file_extension: string;
  file_size: number;
  last_modified: string;
  text_content: string;
  chunk_index: number;
  similarity: number;
}

export interface SearchFilters {
  date_from: string | null;
  date_to: string | null;
  extensions: string[] | null;
}

export type DateFilter = "all" | "today" | "week" | "month";
export type ExtFilter = "all" | ".pdf" | ".docx" | ".xlsx" | ".txt" | ".md";
