export default function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-box">
      <span>{message}</span>
      {onRetry && (
        <button className="btn-g" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}
