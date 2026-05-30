import { ChevronLeft, ChevronRight } from "lucide-react";

const PAGE_SIZES = [20, 50, 100] as const;

export default function PaginationBar({
  page,
  perPage,
  total,
  onPageChange,
  onPerPageChange,
}: {
  page: number;
  perPage: number;
  total: number;
  onPageChange: (page: number) => void;
  onPerPageChange: (perPage: number) => void;
}) {
  const lastPage = Math.max(1, Math.ceil(total / perPage));
  const from = total === 0 ? 0 : (page - 1) * perPage + 1;
  const to = Math.min(total, page * perPage);

  return (
    <div className="pagination-bar">
      <span className="muted">
        {total === 0 ? "No results" : `Showing ${from}–${to} of ${total}`}
      </span>
      <div className="row" style={{ gap: 8 }}>
        <span className="muted">Rows per page</span>
        {PAGE_SIZES.map((size) => (
          <button
            key={size}
            type="button"
            className={perPage === size ? "btn-p" : "btn-o"}
            onClick={() => onPerPageChange(size)}
          >
            {size}
          </button>
        ))}
      </div>
      <div className="row" style={{ gap: 8 }}>
        <button
          type="button"
          className="btn-o"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
          aria-label="Previous page"
        >
          <ChevronLeft size={14} />
        </button>
        <span className="muted">
          Page {page} of {lastPage}
        </span>
        <button
          type="button"
          className="btn-o"
          disabled={page >= lastPage}
          onClick={() => onPageChange(page + 1)}
          aria-label="Next page"
        >
          <ChevronRight size={14} />
        </button>
      </div>
    </div>
  );
}
