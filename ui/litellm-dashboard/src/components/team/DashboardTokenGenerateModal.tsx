import React, { useRef } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";

interface DashboardTokenGenerateModalProps {
  readonly open: boolean;
  readonly defaultLabel: string | null;
  readonly busy: boolean;
  readonly onCancel: () => void;
  readonly onConfirm: (label: string) => void;
}

/**
 * Modal for issuing a new dashboard token for the team.
 *
 * Label is pre-filled with the team's ``team_alias`` and editable. Empty
 * label is still accepted (the server defaults it to the alias if null).
 *
 * The Input is uncontrolled and keyed on ``open`` so it resets to the
 * default whenever the modal transitions from closed to open — avoids a
 * state-sync useEffect.
 */
export default function DashboardTokenGenerateModal({
  open,
  defaultLabel,
  busy,
  onCancel,
  onConfirm,
}: DashboardTokenGenerateModalProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen && !busy) onCancel();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Generate dashboard token</DialogTitle>
        </DialogHeader>
        <p className="text-sm text-muted-foreground mb-4">
          Generate a token for this team to access a scoped view of their LiteLLM usage dashboard. The token will be
          shown to you exactly once.
        </p>
        <Field>
          <FieldLabel htmlFor="dashboard-token-label">Label</FieldLabel>
          <Input
            // Keyed on open so the uncontrolled input resets to defaultLabel
            // every time the modal transitions closed → open.
            key={open ? `open-${defaultLabel ?? ""}` : "closed"}
            id="dashboard-token-label"
            ref={inputRef}
            defaultValue={defaultLabel ?? ""}
            placeholder={defaultLabel ?? "Team label"}
            disabled={busy}
            autoFocus
          />
          <FieldDescription>Human-readable name for the token. Shown in the admin UI only.</FieldDescription>
        </Field>
        <DialogFooter>
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => onConfirm(inputRef.current?.value ?? "")} disabled={busy}>
            {busy ? "Generating…" : "Generate"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
