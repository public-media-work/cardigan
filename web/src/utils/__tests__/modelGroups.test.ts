import { describe, it, expect } from 'vitest'
import {
  aliasOptionLabel,
  familyLabel,
  groupAvailableModels,
  isModelAlias,
  resolvedModelName,
} from '../modelGroups'

describe('groupAvailableModels', () => {
  it('groups cloud models under "Cloud" and locals under "provider · host"', () => {
    const groups = groupAvailableModels([
      { id: 'anthropic/claude-haiku-4.5', name: 'Claude Haiku 4.5', provider: 'Anthropic' },
      { id: 'zeta', name: 'Zeta', provider: 'oMLX', host: 'studio.riechers.co:8000', backend: 'studio.riechers.co:8000' },
      { id: 'alpha', name: 'Alpha', provider: 'oMLX', host: 'studio.riechers.co:8000', backend: 'studio.riechers.co:8000' },
    ])
    expect(groups.map((g) => g.label)).toEqual(['Cloud', 'oMLX · studio.riechers.co:8000'])
    expect(groups[0].models.map((m) => m.id)).toEqual(['anthropic/claude-haiku-4.5'])
    // sorted by name within a group
    expect(groups[1].models.map((m) => m.id)).toEqual(['alpha', 'zeta'])
  })

  it('omits the Cloud group when there are no cloud models', () => {
    const groups = groupAvailableModels([{ id: 'x', name: 'X', provider: 'oMLX', host: 'h:1' }])
    expect(groups.map((g) => g.label)).toEqual(['oMLX · h:1'])
  })

  it('separates two local servers into their own groups, ordered alphabetically by label', () => {
    const groups = groupAvailableModels([
      { id: 'b', name: 'B', provider: 'vLLM', host: 'gpu-box:8000' },
      { id: 'a', name: 'A', provider: 'oMLX', host: 'studio:8000' },
    ])
    // Assert the emitted order directly (no .sort()) so the alphabetical-by-label
    // contract is what's under test — inputs are deliberately reversed.
    expect(groups.map((g) => g.label)).toEqual(['oMLX · studio:8000', 'vLLM · gpu-box:8000'])
  })
})

describe('model aliases', () => {
  const roster = [{ id: 'anthropic/claude-sonnet-5.5', name: 'Claude Sonnet 5.5', provider: 'Anthropic' }]
  const sonnet = {
    id: '~anthropic/claude-sonnet-latest',
    name: 'Latest Claude Sonnet',
    family: 'sonnet',
    resolves_to: 'anthropic/claude-sonnet-5.5',
  }

  it('recognises OpenRouter aliases by their ~ prefix', () => {
    expect(isModelAlias('~anthropic/claude-sonnet-latest')).toBe(true)
    expect(isModelAlias('anthropic/claude-sonnet-4.6')).toBe(false)
    expect(isModelAlias('')).toBe(false)
    expect(isModelAlias(undefined)).toBe(false)
  })

  it('labels an alias with the model it runs on now', () => {
    expect(aliasOptionLabel(sonnet, roster)).toBe('Latest Claude Sonnet (now Claude Sonnet 5.5)')
    expect(resolvedModelName(sonnet, roster)).toBe('Claude Sonnet 5.5')
  })

  it('falls back to the raw id when the target is not in the roster', () => {
    expect(resolvedModelName(sonnet, [])).toBe('anthropic/claude-sonnet-5.5')
  })

  it('shows just the alias name when the target is unknown', () => {
    const unknown = { ...sonnet, resolves_to: null }
    expect(aliasOptionLabel(unknown, roster)).toBe('Latest Claude Sonnet')
    expect(resolvedModelName(unknown, roster)).toBeNull()
  })

  it('names the family for display', () => {
    expect(familyLabel(sonnet)).toBe('Claude Sonnet')
  })
})
