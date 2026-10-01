import { describe, expect, it } from "vitest";

import {
  buildQueryParams,
  fieldFor,
  newCondition,
  operatorFor,
  type Condition,
  type FieldDef,
} from "./filterConditions";

const FIELDS: FieldDef[] = [
  {
    key: "kind",
    label: "Kind",
    type: "select",
    group: "Select",
    options: [
      { value: "guidance", label: "Guidance" },
      { value: "do_not_suggest", label: "Do-not-suggest" },
    ],
    operators: [
      { key: "is", label: "is", needsValue: true, toParams: (v) => ({ kind: v }) },
      { key: "is_not", label: "is not", needsValue: true, toParams: (v) => ({ kind_not: v }) },
    ],
  },
  {
    key: "strength",
    label: "Strength",
    type: "number",
    group: "Number",
    unit: "%",
    operators: [
      {
        key: "gte",
        label: "≥",
        needsValue: true,
        toParams: (v) => ({ strength_min: Number(v) / 100 }),
      },
      {
        key: "no_verdicts",
        label: "has no verdicts yet",
        needsValue: false,
        toParams: () => ({ no_verdicts: true }),
      },
    ],
  },
];

function cond(field: string, operator: string, value = ""): Condition {
  return { id: "x", field, operator, value };
}

describe("buildQueryParams", () => {
  it("maps a select condition to its operator's params", () => {
    expect(buildQueryParams(FIELDS, [cond("kind", "is", "guidance")])).toEqual({
      kind: "guidance",
    });
  });

  it("maps a different operator on the same field to a different param key", () => {
    expect(buildQueryParams(FIELDS, [cond("kind", "is_not", "do_not_suggest")])).toEqual({
      kind_not: "do_not_suggest",
    });
  });

  it("merges multiple conditions into one flat AND params object", () => {
    const params = buildQueryParams(FIELDS, [
      cond("kind", "is", "guidance"),
      cond("strength", "gte", "50"),
    ]);
    expect(params).toEqual({ kind: "guidance", strength_min: 0.5 });
  });

  it("skips a value-needing condition whose value is empty", () => {
    expect(buildQueryParams(FIELDS, [cond("strength", "gte", "")])).toEqual({});
  });

  it("includes a no-value operator's params even with an empty value", () => {
    expect(buildQueryParams(FIELDS, [cond("strength", "no_verdicts", "")])).toEqual({
      no_verdicts: true,
    });
  });

  it("ignores a condition whose field no longer exists", () => {
    expect(buildQueryParams(FIELDS, [cond("gone", "is", "x")])).toEqual({});
  });

  it("returns an empty object for no conditions", () => {
    expect(buildQueryParams(FIELDS, [])).toEqual({});
  });
});

describe("newCondition", () => {
  it("defaults a select field to its first option and first operator", () => {
    const c = newCondition(FIELDS[0]);
    expect(c.field).toBe("kind");
    expect(c.operator).toBe("is");
    expect(c.value).toBe("guidance");
  });

  it("defaults a number field to an empty value", () => {
    const c = newCondition(FIELDS[1]);
    expect(c.operator).toBe("gte");
    expect(c.value).toBe("");
  });
});

describe("fieldFor / operatorFor", () => {
  it("finds a field by key and an operator by key within it", () => {
    const field = fieldFor(FIELDS, "strength");
    expect(field?.label).toBe("Strength");
    expect(operatorFor(field!, "gte")?.label).toBe("≥");
  });

  it("returns undefined for an unknown key", () => {
    expect(fieldFor(FIELDS, "nope")).toBeUndefined();
  });
});
