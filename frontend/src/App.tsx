import { useState, useEffect, useRef, useCallback } from "react";
import { searchDocuments, openFile, getIndexStatus } from "./api";
import type { SearchResult, DateFilter, ExtFilter, SearchFilters } from "./types";
import "./App.css";

// Format a Date as a local-time naive ISO string (no timezone / no "Z").
// The backend stores last_modified as datetime.fromtimestamp().isoformat(),
// which is local time without a timezone. Using toISOString() here would emit
// a UTC value and shift the filter boundary by the local UTC offset.
function toLocalNaiveISO(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  );
}

function getDateRange(filter: DateFilter): string | null {
  const now = new Date();
  if (filter === "today") {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return toLocalNaiveISO(d);
  }
  if (filter === "week") {
    const day = now.getDay();
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - day);
    return toLocalNaiveISO(d);
  }
  if (filter === "month") {
    const d = new Date(now.getFullYear(), now.getMonth(), 1);
    return toLocalNaiveISO(d);
  }
  return null;
}

function getExtensions(filter: ExtFilter): string[] | null {
  if (filter === "all") return null;
  return [filter];
}

function getBadgeColor(ext: string): string {
  const upper = ext.toUpperCase().replace(".", "");
  if (upper === "PDF") return "#ef4444";
  if (upper === "DOCX") return "#3b82f6";
  if (upper === "XLSX") return "#10b981";
  if (upper === "TXT" || upper === "MD") return "#6b7280";
  return "#8b5cf6";
}

function formatSize(bytes: number): string {
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString("ko-KR", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function ResultItem({
  item,
  selected,
  onOpen,
}: {
  item: SearchResult;
  selected: boolean;
  onOpen: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (selected && ref.current) {
      ref.current.scrollIntoView({ block: "nearest" });
    }
  }, [selected]);

  const ext = item.file_extension.toUpperCase().replace(".", "");
  const simPct = Math.round(item.similarity * 100);
  const snippet = item.text_content.replace(/\n/g, " ").slice(0, 150);

  return (
    <div
      ref={ref}
      className={`result-item ${selected ? "selected" : ""}`}
      onClick={onOpen}
      onDoubleClick={onOpen}
    >
      <div className="result-row1">
        <span className="badge" style={{ backgroundColor: getBadgeColor(item.file_extension) }}>
          {ext}
        </span>
        <span className="filename">{item.file_name}</span>
        <span className={`similarity ${simPct >= 80 ? "high" : simPct >= 60 ? "mid" : "low"}`}>
          {simPct}% Match
        </span>
      </div>
      <div className="result-path">{item.file_path}</div>
      <div className="result-snippet">{snippet}...</div>
      <div className="result-meta">
        Size: {formatSize(item.file_size)} | Modified: {formatDate(item.last_modified)}
      </div>
    </div>
  );
}

export default function App() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [dateFilter, setDateFilter] = useState<DateFilter>("all");
  const [extFilter, setExtFilter] = useState<ExtFilter>("all");
  const [status, setStatus] = useState("Ready");
  const [loading, setLoading] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const interval = setInterval(() => {
      getIndexStatus()
        .then((s) => setStatus(s.status))
        .catch(() => setStatus("Backend offline"));
    }, 3000);
    return () => clearInterval(interval);
  }, []);

  const doSearch = useCallback(
    async (q: string, df: DateFilter, ef: ExtFilter) => {
      if (!q.trim()) {
        setResults([]);
        return;
      }
      setLoading(true);
      try {
        const filters: SearchFilters = {
          date_from: getDateRange(df),
          date_to: null,
          extensions: getExtensions(ef),
        };
        const data = await searchDocuments(q, filters);
        setResults(data);
        setSelectedIdx(0);
      } catch (e) {
        console.error("Search error:", e);
        setResults([]);
      } finally {
        setLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      doSearch(query, dateFilter, extFilter);
    }, 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query, dateFilter, extFilter, doSearch]);

  const handleOpen = useCallback(async (path: string) => {
    try {
      await openFile(path);
    } catch (e) {
      console.error("Open error:", e);
    }
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIdx((prev) => Math.min(prev + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIdx((prev) => Math.max(prev - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (results[selectedIdx]) {
        handleOpen(results[selectedIdx].file_path);
      }
    } else if (e.key === "Escape") {
      (window as any).__TAURI__?.window?.getCurrentWindow()?.hide();
    }
  };

  return (
    <div className="app-container">
      <div className="search-bar">
        <input
          ref={inputRef}
          type="text"
          className="search-input"
          placeholder="Search documents by context..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          autoFocus
        />
      </div>

      <div className="filter-bar">
        <select
          value={dateFilter}
          onChange={(e) => setDateFilter(e.target.value as DateFilter)}
          className="filter-select"
        >
          <option value="all">All Time</option>
          <option value="today">Today</option>
          <option value="week">This Week</option>
          <option value="month">This Month</option>
        </select>

        <select
          value={extFilter}
          onChange={(e) => setExtFilter(e.target.value as ExtFilter)}
          className="filter-select"
        >
          <option value="all">All Types</option>
          <option value=".pdf">PDF</option>
          <option value=".docx">DOCX</option>
          <option value=".xlsx">XLSX</option>
          <option value=".txt">TXT</option>
          <option value=".md">MD</option>
        </select>
      </div>

      <div className="separator" />

      <div className="results-list">
        {loading && <div className="loading">Searching...</div>}
        {!loading && results.length === 0 && query.trim() && (
          <div className="no-results">No results found</div>
        )}
        {results.map((item, idx) => (
          <ResultItem
            key={`${item.file_path}-${item.chunk_index}`}
            item={item}
            selected={idx === selectedIdx}
            onOpen={() => handleOpen(item.file_path)}
          />
        ))}
      </div>

      <div className="status-bar">
        <span className="status-text">{status}</span>
        <span className="shortcut-hint">Win + Alt + F | Esc to Close</span>
      </div>
    </div>
  );
}
