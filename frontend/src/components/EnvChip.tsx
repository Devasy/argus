export default function EnvChip({ envVar }: { envVar: string }) {
  return (
    <span
      className="env-chip"
      title="Secrets are managed in the environment and never editable here"
    >
      🔒 set via {envVar}
    </span>
  );
}
