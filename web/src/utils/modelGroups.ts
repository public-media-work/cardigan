/**
 * Groups the flat model roster into optgroup-ready sections for the per-phase
 * model picker: cloud models under "Cloud", and each local server under
 * "provider · host" (e.g. "oMLX · studio.riechers.co:8000").
 *
 * A model is "local" when it carries a `host` (set by the backend roster for
 * discovered endpoints); cloud models have none.
 */
export interface RosterModel {
  id: string
  name: string
  provider: string
  host?: string | null
  backend?: string | null
}

export interface ModelGroup {
  label: string
  models: RosterModel[]
}

/**
 * A "latest in family" choice: an OpenRouter alias such as
 * `~anthropic/claude-sonnet-latest`, plus the concrete model it points at now
 * (`resolves_to`, null when the catalog can't say). Picking one makes a phase
 * follow new releases of that family (#499).
 */
export interface ModelAliasOption {
  id: string
  name: string
  family: string
  resolves_to?: string | null
}

export function isModelAlias(modelId: string | null | undefined): boolean {
  return !!modelId && modelId.startsWith('~')
}

/** "Claude Sonnet" for the sonnet alias. */
export function familyLabel(alias: ModelAliasOption): string {
  return `Claude ${alias.family.charAt(0).toUpperCase()}${alias.family.slice(1)}`
}

/** Display name for the model an alias currently runs on, or null if unknown. */
export function resolvedModelName(alias: ModelAliasOption, models: RosterModel[]): string | null {
  if (!alias.resolves_to) return null
  return models.find((m) => m.id === alias.resolves_to)?.name ?? alias.resolves_to
}

/** Dropdown label, e.g. "Latest Claude Sonnet (now Claude Sonnet 5.5)". */
export function aliasOptionLabel(alias: ModelAliasOption, models: RosterModel[]): string {
  const now = resolvedModelName(alias, models)
  return now ? `${alias.name} (now ${now})` : alias.name
}

export function groupAvailableModels(models: RosterModel[]): ModelGroup[] {
  const byName = (a: RosterModel, b: RosterModel) => a.name.localeCompare(b.name)

  const cloud = models.filter((m) => !m.host)
  const localByHost = new Map<string, RosterModel[]>()
  for (const m of models) {
    if (!m.host) continue
    const label = `${m.provider} · ${m.host}`
    const arr = localByHost.get(label) ?? []
    arr.push(m)
    localByHost.set(label, arr)
  }

  const groups: ModelGroup[] = []
  if (cloud.length) groups.push({ label: 'Cloud', models: [...cloud].sort(byName) })
  for (const label of [...localByHost.keys()].sort()) {
    groups.push({ label, models: [...localByHost.get(label)!].sort(byName) })
  }
  return groups
}
