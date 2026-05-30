import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { KeyRound } from "lucide-react";
import { checkHealth, setToken } from "../api/client";

export default function TokenGate() {
  const [value, setValue] = useState("");
  const [backendUp, setBackendUp] = useState<boolean | null>(null);
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? "/";

  useEffect(() => {
    checkHealth().then(setBackendUp);
  }, []);

  const save = () => {
    if (!value.trim()) return;
    setToken(value.trim());
    navigate(from, { replace: true });
  };

  return (
    <div className="gate">
      <div className="nw-card form-grid">
        <div className="row">
          <KeyRound size={18} />
          <h2>argus API token</h2>
        </div>
        {backendUp === false && (
          <div className="error-box">Backend unreachable — is the API running?</div>
        )}
        <div>
          <label htmlFor="token">Token (sent as Authorization: Bearer)</label>
          <input
            id="token"
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && save()}
            placeholder="paste ARGUS_API_TOKEN"
            autoFocus
          />
        </div>
        <div className="form-actions">
          <button className="btn-p" onClick={save} disabled={!value.trim()}>
            Save & continue
          </button>
        </div>
      </div>
    </div>
  );
}
