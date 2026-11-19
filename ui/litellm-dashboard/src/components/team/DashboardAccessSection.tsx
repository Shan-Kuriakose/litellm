"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  createTeamDashboardTokenCall,
  getTeamDashboardTokenCall,
  revokeTeamDashboardTokenCall,
  rotateTeamDashboardTokenCall,
  type DashboardTokenMetadata,
} from "@/components/networking";
import { toast } from "@/lib/toast";

import DashboardTokenGenerateModal from "./DashboardTokenGenerateModal";
import DashboardTokenRevealModal from "./DashboardTokenRevealModal";

interface DashboardAccessSectionProps {
  readonly teamId: string;
  readonly teamAlias: string | null;
  readonly accessToken: string | null;
}

/**
 * Inline "Dashboard Access" section on the team detail page.
 *
 * Visible only to proxy admins (enforced by the parent wiring). Lets an
 * admin issue / rotate / revoke the team's dashboard-scope token. See
 * ``litellm/proxy/management_endpoints/dashboard_token_management.py``
 * for the backing endpoints.
 */
export default function DashboardAccessSection({ teamId, teamAlias, accessToken }: DashboardAccessSectionProps) {
  const queryClient = useQueryClient();
  const [generateOpen, setGenerateOpen] = useState(false);
  const [rotateOpen, setRotateOpen] = useState(false);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [revealedToken, setRevealedToken] = useState<string | null>(null);

  const queryKey = ["team-dashboard-token", teamId];
  const metadataQuery = useQuery<DashboardTokenMetadata | null>({
    queryKey,
    queryFn: () => getTeamDashboardTokenCall(accessToken ?? "", teamId),
    enabled: accessToken !== null,
  });

  const createMutation = useMutation({
    mutationFn: (label: string) =>
      createTeamDashboardTokenCall(accessToken ?? "", teamId, label.trim() || undefined),
    onSuccess: (reveal) => {
      setGenerateOpen(false);
      setRevealedToken(reveal.raw_token);
      void queryClient.invalidateQueries({ queryKey });
    },
    onError: (err: unknown) => {
      const detail = (err as { body?: { detail?: { error?: string } } })?.body?.detail?.error;
      if (detail === "live_dashboard_token_exists") {
        toast.error("A live dashboard token already exists for this team. Rotate or revoke it first.");
      } else {
        toast.error("Failed to generate dashboard token.");
      }
    },
  });

  const rotateMutation = useMutation({
    mutationFn: () => rotateTeamDashboardTokenCall(accessToken ?? "", teamId),
    onSuccess: (reveal) => {
      setRotateOpen(false);
      setRevealedToken(reveal.raw_token);
      void queryClient.invalidateQueries({ queryKey });
    },
    onError: () => {
      toast.error("Failed to rotate dashboard token.");
    },
  });

  const revokeMutation = useMutation({
    mutationFn: () => revokeTeamDashboardTokenCall(accessToken ?? "", teamId),
    onSuccess: () => {
      setRevokeOpen(false);
      toast.success("Dashboard token revoked.");
      void queryClient.invalidateQueries({ queryKey });
    },
    onError: () => {
      toast.error("Failed to revoke dashboard token.");
    },
  });

  const metadata = metadataQuery.data ?? null;
  const metadataLoading = metadataQuery.isLoading;
  // Distinguish "no token yet" (query returned null) from a real transport
  // failure. Without this branch, a 500 from the proxy would silently
  // render as the "Generate token" state — then Generate also 500s and the
  // user hits a confusing double-error.
  const metadataError = metadataQuery.isError;
  // Derive a single render state so each JSX branch has a simple predicate.
  type RenderState = "loading" | "error" | "empty" | "live";
  let renderState: RenderState;
  if (metadataLoading) {
    renderState = "loading";
  } else if (metadataError) {
    renderState = "error";
  } else if (metadata === null) {
    renderState = "empty";
  } else {
    renderState = "live";
  }

  return (
    <Card className="block p-6 overflow-y-auto mt-4">
      <div className="mb-4">
        <h3 className="text-lg font-medium">Dashboard Access</h3>
        <p className="text-sm text-muted-foreground mt-1">
          Issue a scoped bearer token so this team can access their own view of the LiteLLM usage dashboard.
        </p>
      </div>

      {renderState === "loading" && <p className="text-sm text-muted-foreground">Loading…</p>}

      {renderState === "error" && (
        <div className="flex items-center justify-between gap-4">
          <p className="text-sm text-destructive">Failed to load dashboard token state.</p>
          <Button variant="outline" onClick={() => void metadataQuery.refetch()}>
            Retry
          </Button>
        </div>
      )}

      {renderState === "empty" && (
        <div className="flex items-center justify-between gap-4">
          <p className="text-sm text-muted-foreground">
            This team has no dashboard token yet.
          </p>
          <Button onClick={() => setGenerateOpen(true)}>Generate token</Button>
        </div>
      )}

      {renderState === "live" && metadata !== null && (
        <div>
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 text-sm">
            <dt className="text-muted-foreground">Label</dt>
            <dd className="font-medium">{metadata.label ?? "—"}</dd>
            <dt className="text-muted-foreground">Created</dt>
            <dd>{formatIso(metadata.created_at)}</dd>
            <dt className="text-muted-foreground">Created by</dt>
            <dd className="font-mono text-xs">{metadata.created_by ?? "—"}</dd>
            <dt className="text-muted-foreground">Hash prefix</dt>
            <dd className="font-mono text-xs">{metadata.token_hash_prefix}…</dd>
          </dl>
          <div className="flex gap-2 mt-4">
            <Button variant="outline" onClick={() => setRotateOpen(true)}>
              Rotate
            </Button>
            <Button variant="destructive" onClick={() => setRevokeOpen(true)}>
              Revoke
            </Button>
          </div>
        </div>
      )}

      <DashboardTokenGenerateModal
        open={generateOpen}
        defaultLabel={teamAlias}
        busy={createMutation.isPending}
        onCancel={() => setGenerateOpen(false)}
        onConfirm={(label) => createMutation.mutate(label)}
      />

      <DashboardTokenRevealModal rawToken={revealedToken} onClose={() => setRevealedToken(null)} />

      <AlertDialog open={rotateOpen} onOpenChange={setRotateOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Rotate dashboard token?</AlertDialogTitle>
            <AlertDialogDescription>
              This will invalidate the current token immediately. The account must install the new token to continue
              using the dashboard.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={rotateMutation.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => rotateMutation.mutate()} disabled={rotateMutation.isPending}>
              {rotateMutation.isPending ? "Rotating…" : "Rotate"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={revokeOpen} onOpenChange={setRevokeOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Revoke dashboard token?</AlertDialogTitle>
            <AlertDialogDescription>
              The account&apos;s dashboard will stop working until a new token is issued. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revokeMutation.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => revokeMutation.mutate()} disabled={revokeMutation.isPending}>
              {revokeMutation.isPending ? "Revoking…" : "Revoke"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}

function formatIso(iso: string): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toISOString().replace("T", " ").slice(0, 16) + " UTC";
  } catch {
    return iso;
  }
}
