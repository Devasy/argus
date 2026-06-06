import { useMemo, useRef, useState } from "react";
import { Plus, X } from "lucide-react";

import {
  fieldFor,
  newCondition,
  operatorFor,
  type Condition,
  type FieldDef,
} from "./filterConditions";
import { useClickOutside } from "./useClickOutside";

export default function FilterBuilder({
  fields,
  conditions,
  onChange,
}: {
  fields: FieldDef[];
  conditions: Condition[];
  onChange: (conditions: Condition[]) => void;
}) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerQuery, setPickerQuery] = useState("");
  const pickerRef = useRef<HTMLDivElement>(null);
  useClickOutside(pickerRef, () => setPickerOpen(false));

  const grouped = useMemo(() => {
    const q = pickerQuery.trim().toLowerCase();
    const matching = q ? fields.filter((f) => f.label.toLowerCase().includes(q)) : fields;
    const byGroup = new Map<string, FieldDef[]>();
    for (const f of matching) {
      const list = byGroup.get(f.group) ?? [];
      list.push(f);
      byGroup.set(f.group, list);
    }
    return byGroup;
  }, [fields, pickerQuery]);

  function addCondition(field: FieldDef) {
    onChange([...conditions, newCondition(field)]);
    setPickerOpen(false);
    setPickerQuery("");
  }

  function removeCondition(id: string) {
    onChange(conditions.filter((c) => c.id !== id));
  }

  function updateCondition(id: string, patch: Partial<Condition>) {
    onChange(conditions.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  }

  return (
    <div className="filter-builder">
      {conditions.length > 0 && (
        <div className="filter-rows">
          {conditions.map((c) => {
            const field = fieldFor(fields, c.field);
            if (!field) return null;
            const op = operatorFor(field, c.operator) ?? field.operators[0];
            return (
              <div className="filter-row" key={c.id}>
                <span className="filter-row-field">{field.label}</span>
                <select
                  value={op.key}
                  onChange={(e) => updateCondition(c.id, { operator: e.target.value })}
                >
                  {field.operators.map((o) => (
                    <option key={o.key} value={o.key}>
                      {o.label}
                    </option>
                  ))}
                </select>
                {op.needsValue ? (
                  <FilterValueInput
                    field={field}
                    value={c.value}
                    onChange={(value) => updateCondition(c.id, { value })}
                  />
                ) : (
                  <span className="muted filter-row-novalue">no value needed</span>
                )}
                <span style={{ flexGrow: 1 }} />
                <button
                  type="button"
                  className="filter-row-remove"
                  aria-label="Remove condition"
                  onClick={() => removeCondition(c.id)}
                >
                  <X size={14} />
                </button>
              </div>
            );
          })}
        </div>
      )}

      <div className="filter-add" ref={pickerRef}>
        <button
          type="button"
          className="filter-add-btn"
          onClick={() => setPickerOpen((v) => !v)}
        >
          <Plus size={13} />
          Add condition
        </button>

        {pickerOpen && (
          <div className="filter-field-menu">
            <input
              autoFocus
              type="text"
              placeholder="Search fields…"
              value={pickerQuery}
              onChange={(e) => setPickerQuery(e.target.value)}
            />
            {[...grouped.entries()].map(([group, groupFields]) => (
              <div key={group}>
                <div className="filter-field-group-label">{group}</div>
                {groupFields.map((f) => (
                  <button
                    type="button"
                    key={f.key}
                    className="filter-field-option"
                    onClick={() => addCondition(f)}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            ))}
            {grouped.size === 0 && <div className="muted filter-field-empty">No matching fields</div>}
          </div>
        )}
      </div>
    </div>
  );
}

function FilterValueInput({
  field,
  value,
  onChange,
}: {
  field: FieldDef;
  value: string;
  onChange: (value: string) => void;
}) {
  if (field.type === "select") {
    return (
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {field.options?.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );
  }
  if (field.type === "date") {
    return <input type="date" value={value} onChange={(e) => onChange(e.target.value)} />;
  }
  if (field.type === "number") {
    return (
      <div className="filter-row-number">
        <input
          type="number"
          value={value}
          placeholder={field.placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
        {field.unit && <span className="muted">{field.unit}</span>}
      </div>
    );
  }
  return (
    <input
      type="text"
      value={value}
      placeholder={field.placeholder}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
