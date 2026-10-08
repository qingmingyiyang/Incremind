import "./atoms.css";

export function ProgressDots({ total = 4, done = 0, running = false, title = "进度", className = "" }) {
  const count = Math.max(0, Math.floor(total));
  const completed = Math.min(count, Math.max(0, Math.floor(done)));
  const current = typeof running === "number" ? running : running ? completed : -1;
  return <span role="img" aria-label={title} title={title} className={`ui-progress-dots ${className}`}>
    {Array.from({ length: count }, (_, i) => <span key={i} aria-hidden="true" data-progress={i < completed ? "done" : i === current ? "running" : "waiting"} />)}
  </span>;
}

export default ProgressDots;
