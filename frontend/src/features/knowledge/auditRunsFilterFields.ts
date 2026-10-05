import type { FieldDef, FieldOption } from "../../components/filterConditions";

export function auditRunsFilterFields(repoOptions: FieldOption[]): FieldDef[] {
  return [
    {
      key: "repository",
      label: "Repository",
      type: "select",
      group: "Select",
      options: repoOptions,
      operators: [
        { key: "is", label: "is", needsValue: true, toParams: (v) => ({ repo_id: v }) },
      ],
    },
  ];
}
