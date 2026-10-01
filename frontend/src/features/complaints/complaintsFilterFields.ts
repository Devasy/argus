import type { FieldDef } from "../../components/filterConditions";

const CATEGORY_LABEL: Record<string, string> = {
  tool_broken: "Tool broken",
  tool_missing: "Tool missing",
  context_insufficient: "Context insufficient",
  context_wrong: "Context wrong",
  prompt_irrelevant: "Prompt irrelevant",
  prompt_unclear: "Prompt unclear",
  instructions_conflict: "Instructions conflict",
  task_impossible: "Task impossible",
};

export function complaintsFilterFields(categories: readonly string[]): FieldDef[] {
  return [
    {
      key: "category",
      label: "Category",
      type: "select",
      group: "Select",
      options: categories.map((c) => ({ value: c, label: CATEGORY_LABEL[c] ?? c })),
      operators: [{ key: "is", label: "is", needsValue: true, toParams: (v) => ({ category: v }) }],
    },
    {
      key: "impact",
      label: "Impact",
      type: "select",
      group: "Select",
      options: [
        { value: "true", label: "Blocked the work" },
        { value: "false", label: "Worked around" },
      ],
      operators: [
        {
          key: "is",
          label: "is",
          needsValue: true,
          toParams: (v) => ({ blocked: v === "true" }),
        },
      ],
    },
  ];
}
