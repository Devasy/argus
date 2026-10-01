export type FieldType = "select" | "number" | "date" | "text";

export interface FieldOption {
  value: string;
  label: string;
}

export interface OperatorDef {
  key: string;
  label: string;
  needsValue: boolean;
  toParams: (value: string) => Record<string, string | number | boolean>;
}

export interface FieldDef {
  key: string;
  label: string;
  type: FieldType;
  group: string;
  options?: FieldOption[];
  operators: OperatorDef[];
  unit?: string;
  placeholder?: string;
}

export interface Condition {
  id: string;
  field: string;
  operator: string;
  value: string;
}

export function newConditionId(): string {
  return Math.random().toString(36).slice(2);
}

export function newCondition(field: FieldDef): Condition {
  const operator = field.operators[0];
  const value = field.type === "select" ? (field.options?.[0]?.value ?? "") : "";
  return { id: newConditionId(), field: field.key, operator: operator.key, value };
}

export function fieldFor(fields: FieldDef[], key: string): FieldDef | undefined {
  return fields.find((f) => f.key === key);
}

export function operatorFor(field: FieldDef, key: string): OperatorDef | undefined {
  return field.operators.find((o) => o.key === key);
}

/** Flat AND of every condition's params -- a later condition on the same
 * query key overwrites an earlier one, since the field pickers only ever
 * expose one condition per field/operator pair worth adding twice. */
export function buildQueryParams(
  fields: FieldDef[],
  conditions: Condition[],
): Record<string, string | number | boolean> {
  const params: Record<string, string | number | boolean> = {};
  for (const c of conditions) {
    const field = fieldFor(fields, c.field);
    if (!field) continue;
    const op = operatorFor(field, c.operator);
    if (!op) continue;
    if (op.needsValue && c.value === "") continue;
    Object.assign(params, op.toParams(c.value));
  }
  return params;
}
