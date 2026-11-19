import React, { useState } from "react";
import { CopyToClipboard } from "react-copy-to-clipboard";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { toast } from "@/lib/toast";

interface DashboardTokenRevealModalProps {
  readonly rawToken: string | null;
  readonly onClose: () => void;
}

/**
 * One-time reveal for a newly-issued/rotated dashboard token. Patterned on
 * the existing ``CreatedKeyDisplay`` reveal used for virtual keys.
 *
 * The raw ``ldash_…`` token is shown here and nowhere else in the system —
 * subsequent GETs return metadata only, and the DB persists only
 * sha256(raw). The modal is intentionally blocking until the admin
 * acknowledges with "Close".
 */
export default function DashboardTokenRevealModal({ rawToken, onClose }: DashboardTokenRevealModalProps) {
  const [copied, setCopied] = useState(false);
  const open = rawToken !== null;

  const handleCopy = () => {
    setCopied(true);
    toast.success("Dashboard token copied to clipboard");
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Dashboard token generated</DialogTitle>
        </DialogHeader>
        <p className="mb-2 text-sm">
          Store this token now. You will <b>not</b> be able to view it again through the admin UI. If it is lost, rotate
          to issue a new one.
        </p>
        <p className="text-sm text-muted-foreground mt-3 mb-1">Dashboard token:</p>
        <div className="bg-muted rounded-md p-2.5 mb-2.5">
          <pre
            aria-label="dashboard token"
            className="m-0 whitespace-normal break-words text-foreground text-xs"
          >
            {rawToken ?? ""}
          </pre>
        </div>
        <DialogFooter>
          <CopyToClipboard text={rawToken ?? ""} onCopy={handleCopy}>
            <Button>{copied ? "Copied!" : "Copy token"}</Button>
          </CopyToClipboard>
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
