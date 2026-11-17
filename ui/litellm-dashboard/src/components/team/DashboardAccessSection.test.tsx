import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderWithProviders } from "../../../tests/test-utils";
import DashboardAccessSection from "./DashboardAccessSection";

vi.mock("@/components/networking", () => ({
  getTeamDashboardTokenCall: vi.fn(),
  createTeamDashboardTokenCall: vi.fn(),
  rotateTeamDashboardTokenCall: vi.fn(),
  revokeTeamDashboardTokenCall: vi.fn(),
}));

vi.mock("@/lib/toast", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

import {
  createTeamDashboardTokenCall,
  getTeamDashboardTokenCall,
  revokeTeamDashboardTokenCall,
  rotateTeamDashboardTokenCall,
} from "@/components/networking";

const LIVE_METADATA = {
  label: "Account A",
  scope_type: "team",
  team_id: "team_01",
  created_at: "2026-11-17T10:04:33Z",
  created_by: "user_admin",
  token_hash_prefix: "a3f1b800",
};

const REVEAL = {
  raw_token: "ldash_abcdefghijklmn",
  label: "Account A",
  scope_type: "team",
  team_id: "team_01",
  created_at: "2026-11-17T10:04:33Z",
  warning: "Store this token now. It will not be shown again.",
};

const ROTATED_REVEAL = {
  ...REVEAL,
  raw_token: "ldash_rotated123",
  warning: "",
};

const TEAM_ID_PATTERN = /^team_\d+$/;

describe("DashboardAccessSection", () => {
  // Unique teamId per test so the shared TanStack Query cache in testQueryClient
  // (gcTime: Infinity) doesn't leak one test's result into another.
  let nextTeamId = 0;
  const makeProps = () => ({
    teamId: `team_${++nextTeamId}`,
    teamAlias: "Account A",
    accessToken: "sk-admin",
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders Generate button when no live token exists", async () => {
    vi.mocked(getTeamDashboardTokenCall).mockResolvedValue(null);

    renderWithProviders(<DashboardAccessSection {...makeProps()} />);

    expect(await screen.findByText(/This team has no dashboard token yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Generate token/i })).toBeInTheDocument();
  });

  it("renders error state with Retry when the metadata fetch fails", async () => {
    // A real transport error (e.g. 500 from the proxy) must NOT silently
    // render as the no-token state. Otherwise clicking Generate also
    // 500s and the user hits a confusing double-error.
    vi.mocked(getTeamDashboardTokenCall).mockRejectedValue(new Error("boom"));

    renderWithProviders(<DashboardAccessSection {...makeProps()} />);

    expect(await screen.findByText(/Failed to load dashboard token state/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Retry/i })).toBeInTheDocument();
    // The Generate button must NOT appear in the error state — that would
    // mislead the admin into thinking there's no token yet.
    expect(screen.queryByRole("button", { name: /Generate token/i })).not.toBeInTheDocument();
  });

  it("renders metadata and Rotate/Revoke when a live token exists", async () => {
    vi.mocked(getTeamDashboardTokenCall).mockResolvedValue(LIVE_METADATA);

    renderWithProviders(<DashboardAccessSection {...makeProps()} />);

    expect(await screen.findByText("Account A")).toBeInTheDocument();
    expect(screen.getByText(/user_admin/)).toBeInTheDocument();
    expect(screen.getByText(/a3f1b800…/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Rotate/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Revoke/i })).toBeInTheDocument();
  });

  it("opens the generate modal and submits a new token request", async () => {
    vi.mocked(getTeamDashboardTokenCall).mockResolvedValue(null);
    vi.mocked(createTeamDashboardTokenCall).mockResolvedValue(REVEAL);

    renderWithProviders(<DashboardAccessSection {...makeProps()} />);

    const openButton = await screen.findByRole("button", { name: /Generate token/i });
    fireEvent.click(openButton);

    // Modal appears with pre-filled label.
    const labelInput = (await screen.findByLabelText(/Label/i)) as HTMLInputElement;
    expect(labelInput.value).toBe("Account A");

    fireEvent.click(screen.getByRole("button", { name: /^Generate$/i }));

    await waitFor(() =>
      expect(createTeamDashboardTokenCall).toHaveBeenCalledWith(
        "sk-admin",
        expect.stringMatching(TEAM_ID_PATTERN),
        "Account A",
      ),
    );

    expect(await screen.findByText("ldash_abcdefghijklmn")).toBeInTheDocument();
  });

  it("confirms before rotating", async () => {
    vi.mocked(getTeamDashboardTokenCall).mockResolvedValue(LIVE_METADATA);
    vi.mocked(rotateTeamDashboardTokenCall).mockResolvedValue(ROTATED_REVEAL);

    renderWithProviders(<DashboardAccessSection {...makeProps()} />);

    const openButton = await screen.findByRole("button", { name: /Rotate/i });
    fireEvent.click(openButton);

    expect(await screen.findByText(/invalidate the current token immediately/i)).toBeInTheDocument();
    // Rotate not called until we confirm.
    expect(rotateTeamDashboardTokenCall).not.toHaveBeenCalled();

    const confirmButtons = screen.getAllByRole("button", { name: /^Rotate$/i });
    fireEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() =>
      expect(rotateTeamDashboardTokenCall).toHaveBeenCalledWith(
        "sk-admin",
        expect.stringMatching(TEAM_ID_PATTERN),
      ),
    );
  });

  it("confirms before revoking", async () => {
    vi.mocked(getTeamDashboardTokenCall).mockResolvedValue(LIVE_METADATA);
    vi.mocked(revokeTeamDashboardTokenCall).mockResolvedValue();

    renderWithProviders(<DashboardAccessSection {...makeProps()} />);

    const openButton = await screen.findByRole("button", { name: /Revoke/i });
    fireEvent.click(openButton);

    expect(await screen.findByText(/dashboard will stop working/i)).toBeInTheDocument();
    expect(revokeTeamDashboardTokenCall).not.toHaveBeenCalled();

    const confirmButtons = screen.getAllByRole("button", { name: /^Revoke$/i });
    fireEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() =>
      expect(revokeTeamDashboardTokenCall).toHaveBeenCalledWith(
        "sk-admin",
        expect.stringMatching(TEAM_ID_PATTERN),
      ),
    );
  });
});
