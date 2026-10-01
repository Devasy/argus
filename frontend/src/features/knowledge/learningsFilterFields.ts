import type { FieldDef, FieldOption } from "../../components/filterConditions";

const GLOBAL_ONLY = "__global__";

export function learningsFilterFields(repoOptions: FieldOption[]): FieldDef[] {
  return [
    {
      key: "repository",
      label: "Repository",
      type: "select",
      group: "Select",
      options: [{ value: GLOBAL_ONLY, label: "— global (no repo) —" }, ...repoOptions],
      operators: [
        {
          key: "is",
          label: "is",
          needsValue: true,
          toParams: (v) =>
            v === GLOBAL_ONLY
              ? { scope: "global" }
              : ({ scope: "repo", repo_id: v } as Record<string, string>),
        },
      ],
    },
    {
      key: "kind",
      label: "Kind",
      type: "select",
      group: "Select",
      options: [
        { value: "guidance", label: "Guidance" },
        { value: "do_not_suggest", label: "Do-not-suggest" },
        { value: "missed_pattern", label: "Missed pattern" },
      ],
      operators: [
        { key: "is", label: "is", needsValue: true, toParams: (v) => ({ kind: v }) },
        { key: "is_not", label: "is not", needsValue: true, toParams: (v) => ({ kind_not: v }) },
      ],
    },
    {
      key: "status",
      label: "Status",
      type: "select",
      group: "Select",
      options: [
        { value: "active", label: "Active" },
        { value: "archived", label: "Archived" },
      ],
      operators: [
        { key: "is", label: "is", needsValue: true, toParams: (v) => ({ status: v }) },
        { key: "is_not", label: "is not", needsValue: true, toParams: (v) => ({ status_not: v }) },
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
          key: "lte",
          label: "≤",
          needsValue: true,
          toParams: (v) => ({ strength_max: Number(v) / 100 }),
        },
        {
          key: "no_verdicts",
          label: "has no verdicts yet",
          needsValue: false,
          toParams: () => ({ no_verdicts: true }),
        },
      ],
    },
    {
      key: "grounded",
      label: "Grounded",
      type: "number",
      group: "Number",
      unit: "%",
      operators: [
        {
          key: "gte",
          label: "≥",
          needsValue: true,
          toParams: (v) => ({ groundedness_min: Number(v) / 100 }),
        },
        {
          key: "lte",
          label: "≤",
          needsValue: true,
          toParams: (v) => ({ groundedness_max: Number(v) / 100 }),
        },
        {
          key: "audited",
          label: "is audited",
          needsValue: false,
          toParams: () => ({ audited: true }),
        },
        {
          key: "not_audited",
          label: "is not yet audited",
          needsValue: false,
          toParams: () => ({ audited: false }),
        },
      ],
    },
    {
      key: "hit_count",
      label: "Hit count",
      type: "number",
      group: "Number",
      operators: [
        { key: "gte", label: "≥", needsValue: true, toParams: (v) => ({ hit_count_min: Number(v) }) },
      ],
    },
    {
      key: "harmful_count",
      label: "Harmful count",
      type: "number",
      group: "Number",
      operators: [
        {
          key: "gte",
          label: "≥",
          needsValue: true,
          toParams: (v) => ({ harmful_count_min: Number(v) }),
        },
      ],
    },
    {
      key: "miss_count",
      label: "Miss count",
      type: "number",
      group: "Number",
      operators: [
        {
          key: "gte",
          label: "≥",
          needsValue: true,
          toParams: (v) => ({ miss_count_min: Number(v) }),
        },
      ],
    },
    {
      key: "created_at",
      label: "Created",
      type: "date",
      group: "Date",
      operators: [
        {
          key: "after",
          label: "is after",
          needsValue: true,
          toParams: (v) => ({ created_after: v }),
        },
        {
          key: "before",
          label: "is before",
          needsValue: true,
          toParams: (v) => ({ created_before: v }),
        },
      ],
    },
    {
      key: "learned_from",
      label: "Learnt-from author",
      type: "text",
      group: "Text",
      placeholder: "username",
      operators: [
        {
          key: "is",
          label: "is",
          needsValue: true,
          toParams: (v) => ({ learned_from_username: v }),
        },
      ],
    },
    {
      key: "mr_iid",
      label: "Merge request !IID",
      type: "number",
      group: "Text",
      operators: [
        { key: "is", label: "is", needsValue: true, toParams: (v) => ({ mr_iid: Number(v) }) },
      ],
    },
  ];
}
