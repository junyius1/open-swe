import { createFileRoute } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"

import type { ModelOption } from "@/lib/api"
import {
  AppShell,
  SettingsNavRow,
  SettingsRow,
  SettingsSection,
} from "@/components/AppShell"
import { Button } from "@/components/ui/button"
import { RepoSelector } from "@/features/settings/components/RepoSelector"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  buildProfileUpdate,
  useOptions,
  useProfile,
  useRepos,
  useSaveProfile,
} from "@/lib/profile"
import { RequireLogin } from "@/lib/auth-redirect"
import { useSession } from "@/lib/session"

export const Route = createFileRoute("/cloud-agents")({
  component: CloudAgentsPage,
})

function CloudAgentsPage() {
  const session = useSession()
  const profile = useProfile()
  const options = useOptions()
  const repos = useRepos()
  const save = useSaveProfile()

  const [modelId, setModelId] = useState("")
  const [effortChoice, setEffort] = useState("")
  const [subagentModelId, setSubagentModelId] = useState("")
  const [subagentEffortChoice, setSubagentEffort] = useState("")
  const [defaultRepo, setDefaultRepo] = useState("")
  const [baseBranch, setBaseBranch] = useState("")
  const [branchPrefix, setBranchPrefix] = useState("")
  const [error, setError] = useState<string | null>(null)
  const initialized = useRef(false)

  const firstModel: ModelOption | undefined = options.data?.models[0]
  const defaultAgentModel =
    options.data?.default_agent_model ?? firstModel?.id ?? ""
  const defaultAgentEffort =
    options.data?.default_agent_reasoning_effort ??
    firstModel?.default_effort ??
    ""
  const defaultSubagentModel =
    options.data?.default_agent_subagent_model ?? defaultAgentModel
  const defaultSubagentEffort =
    options.data?.default_agent_subagent_reasoning_effort ?? defaultAgentEffort
  const currentModel: ModelOption | undefined =
    options.data?.models.find((m) => m.id === modelId) ?? firstModel
  const currentSubagentModel: ModelOption | undefined =
    options.data?.models.find((m) => m.id === subagentModelId) ?? firstModel
  const effort =
    currentModel && !currentModel.efforts.includes(effortChoice)
      ? currentModel.default_effort
      : effortChoice
  const subagentEffort =
    currentSubagentModel &&
    !currentSubagentModel.efforts.includes(subagentEffortChoice)
      ? currentSubagentModel.default_effort
      : subagentEffortChoice

  useEffect(() => {
    if (!profile.data || initialized.current) return
    const hasModel = !!profile.data.default_model || !!defaultAgentModel
    if (!hasModel) return
    initialized.current = true
    // oxlint-disable-next-line react/set-state-in-effect
    setModelId(profile.data.default_model ?? defaultAgentModel)
    setEffort(profile.data.reasoning_effort ?? defaultAgentEffort)
    setSubagentModelId(
      profile.data.default_subagent_model ??
        profile.data.default_model ??
        defaultSubagentModel
    )
    setSubagentEffort(
      profile.data.subagent_reasoning_effort ??
        profile.data.reasoning_effort ??
        defaultSubagentEffort
    )
    setDefaultRepo(profile.data.default_repo ?? "")
    setBaseBranch(profile.data.base_branch ?? "")
    setBranchPrefix(profile.data.branch_prefix ?? "")
  }, [
    profile.data,
    defaultAgentModel,
    defaultAgentEffort,
    defaultSubagentModel,
    defaultSubagentEffort,
  ])

  if (session.isLoading) {
    return (
      <main className="p-6">
        <Skeleton className="h-64 w-full" />
      </main>
    )
  }
  if (!session.data) return <RequireLogin />

  const fallbackModel = defaultAgentModel
  const fallbackEffort = defaultAgentEffort

  const persist = (patch: Parameters<typeof buildProfileUpdate>[1]) => {
    setError(null)
    save
      .mutateAsync(
        buildProfileUpdate(profile.data, patch, fallbackModel, fallbackEffort)
      )
      .catch((e: Error) => setError(e.message))
  }

  const persistDefaults = () => {
    persist({
      default_model: modelId,
      reasoning_effort: effort,
      default_subagent_model: subagentModelId,
      subagent_reasoning_effort: subagentEffort,
      default_repo: defaultRepo || null,
      base_branch: baseBranch || null,
      branch_prefix: branchPrefix || null,
    })
  }

  return (
    <AppShell
      user={session.data}
      title="Open SWE Agent"
      description="Configure how the Open SWE Agent picks a model, repository, and PR defaults."
    >
      <SettingsSection title="Defaults">
        <div className="divide-y divide-border">
          <SettingsRow
            label="Default Model"
            description="Used when no model is specified"
            control={
              <Select value={modelId} onValueChange={(v) => v && setModelId(v)}>
                <SelectTrigger className="w-40">
                  <SelectValue placeholder="Pick a model" />
                </SelectTrigger>
                <SelectContent>
                  {options.data?.models.map((m) => (
                    <SelectItem key={m.id} value={m.id}>
                      {m.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            }
          />
          <SettingsRow
            label="Reasoning Effort"
            description="How hard the model thinks before answering"
            control={
              <Select value={effort} onValueChange={(v) => v && setEffort(v)}>
                <SelectTrigger className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {currentModel?.efforts.map((e) => (
                    <SelectItem key={e} value={e}>
                      {e}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            }
          />
          <SettingsRow
            label="Default Subagent Model"
            description="Used for delegated tasks launched by your agent"
            control={
              <Select
                value={subagentModelId}
                onValueChange={(v) => v && setSubagentModelId(v)}
              >
                <SelectTrigger className="w-40">
                  <SelectValue placeholder="Pick a model" />
                </SelectTrigger>
                <SelectContent>
                  {options.data?.models.map((m) => (
                    <SelectItem key={m.id} value={m.id}>
                      {m.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            }
          />
          <SettingsRow
            label="Subagent Reasoning Effort"
            description="How hard delegated subagents think before answering"
            control={
              <Select
                value={subagentEffort}
                onValueChange={(v) => v && setSubagentEffort(v)}
              >
                <SelectTrigger className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {currentSubagentModel?.efforts.map((e) => (
                    <SelectItem key={e} value={e}>
                      {e}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            }
          />
          <SettingsRow
            label="Default Repository"
            description="Used when no repository is specified"
            control={
              repos.data?.repositories.length ? (
                <div className="w-56">
                  <RepoSelector
                    repos={repos.data.repositories}
                    selectedRepo={defaultRepo || null}
                    onRepoChange={(repo) => setDefaultRepo(repo ?? "")}
                    placeholder="Pick a repository…"
                    emptySelectionLabel="No default repository"
                    triggerClassName="h-7 w-full max-w-none rounded-md border border-input bg-input/20 px-2 py-1.5 text-xs/relaxed text-foreground transition-colors hover:opacity-100 dark:bg-input/30"
                    dropdownClassName="w-56"
                  />
                </div>
              ) : (
                <Input
                  className="w-56"
                  placeholder="owner/repo"
                  value={defaultRepo}
                  onChange={(e) => setDefaultRepo(e.target.value)}
                />
              )
            }
          />
          <SettingsRow
            label="Base Branch"
            description="When empty, Cloud Agent will use a repository's default branch (recommended)"
            htmlFor="base-branch"
            control={
              <Input
                id="base-branch"
                className="w-56"
                placeholder="Branch name…"
                value={baseBranch}
                onChange={(e) => setBaseBranch(e.target.value)}
              />
            }
          />
          <SettingsRow
            label="Branch Prefix"
            description="Prefix for branch names created by Cloud Agent"
            htmlFor="branch-prefix"
            control={
              <Input
                id="branch-prefix"
                className="w-56"
                placeholder="open-swe/"
                value={branchPrefix}
                onChange={(e) => setBranchPrefix(e.target.value)}
              />
            }
          />
          <div className="flex justify-end px-4 py-3">
            <Button
              size="sm"
              onClick={persistDefaults}
              disabled={save.isPending}
            >
              {save.isPending ? "Saving…" : "Save defaults"}
            </Button>
          </div>
        </div>
      </SettingsSection>

      <SettingsSection title="Pull Requests">
        <div className="divide-y divide-border">
          <SettingsRow
            label="Automatically fix CI failures"
            description="Agent will attempt to fix failing CI checks and resolve reviewer comments on PRs it opens."
            control={
              <Switch
                checked={profile.data?.auto_fix_ci ?? true}
                onCheckedChange={(v) => persist({ auto_fix_ci: v })}
              />
            }
          />
        </div>
      </SettingsSection>

      <ReasoningStreamSection profile={profile.data} persist={persist} />

      <SettingsSection title="Rules">
        <SettingsNavRow
          to="/agents/instructions"
          label="Repository Instructions"
          description="Per-repo custom instructions injected into the agent's system prompt."
        />
        {session.data.is_admin && (
          <SettingsNavRow
            to="/agents/snapshots"
            label="Repository Snapshots"
            description="Build a per-repo sandbox image from a custom Dockerfile. Falls back to the default image."
          />
        )}
      </SettingsSection>

      {error && <p className="text-xs text-destructive">{error}</p>}
    </AppShell>
  )
}

type ReasoningStreamMode = "inherit" | "on" | "off"

function reasoningStreamMode(value: boolean | null | undefined): ReasoningStreamMode {
  if (value === true) return "on"
  if (value === false) return "off"
  return "inherit"
}

function reasoningStreamModeValue(mode: ReasoningStreamMode): boolean | null {
  if (mode === "on") return true
  if (mode === "off") return false
  return null
}

function ReasoningStreamSection({
  profile,
  persist,
}: {
  profile: ReturnType<typeof useProfile>["data"]
  persist: (patch: Parameters<typeof buildProfileUpdate>[1]) => void
}) {
  return (
    <SettingsSection
      title="Reasoning Stream"
      description="Controls whether the local LLM's reasoning stream is pulled and shown. Off sends enable_thinking=false so the model stops generating thinking, and reasoning content is no longer streamed into the conversation."
    >
      <div className="divide-y divide-border">
        <SettingsRow
          label="Show and stream reasoning"
          description="Inherit uses the LOCAL_LLM_ENABLE_THINKING deployment default (on). Applies to local models (local:) only."
          control={
            <Select
              value={reasoningStreamMode(profile?.reasoning_stream)}
              onValueChange={(next) =>
                persist({ reasoning_stream: reasoningStreamModeValue(next as ReasoningStreamMode) })
              }
            >
              <SelectTrigger className="w-48">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="inherit">Inherit deployment default</SelectItem>
                <SelectItem value="on">On</SelectItem>
                <SelectItem value="off">Off</SelectItem>
              </SelectContent>
            </Select>
          }
        />
      </div>
    </SettingsSection>
  )
}
