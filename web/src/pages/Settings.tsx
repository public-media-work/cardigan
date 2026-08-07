import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import { Link } from 'react-router-dom'
import { usePreferences, TextSize } from '../context/PreferencesContext'
import { AGENT_INFO } from '../constants/agents'
import ModelStatsWidget from '../components/ModelStatsWidget'
import PhaseStatsWidget from '../components/PhaseStatsWidget'
import RestartComponentsButton from '../components/RestartComponentsButton'
import { groupAvailableModels } from '../utils/modelGroups'
import { isRemotePreview } from '../utils/preview'

// Upper bound on the "restarting…" banner. Recovery is normally detected from
// the status poll; this only stops the banner sticking forever if a component
// never comes back. Generous enough to cover the worker's 60s drain timeout.
const RESTART_BANNER_MAX_MS = 90_000

interface AvailableModel {
  id: string
  name: string
  provider: string
  host?: string | null
  backend?: string | null
  tier?: number | null
  context_len?: number | null
}

interface PhaseModelsConfig {
  phase_models: Record<string, string>
  available_models: AvailableModel[]
  available_phases: string[]
}

interface BackendInfo {
  name: string
  type: string
  endpoint?: string | null
  enabled: boolean
  discover: boolean
}

interface WorkerConfig {
  max_concurrent_jobs: number
  poll_interval_seconds: number
  heartbeat_interval_seconds: number
}

interface IngestConfig {
  enabled: boolean
  scan_interval_hours: number
  scan_time: string  // "HH:MM" format
  last_scan_at: string | null
  last_scan_success: boolean | null
  server_url: string
  directories: string[]
  ignore_directories: string[]
  next_scan_at: string | null
}

interface IngestConfigUpdate {
  enabled?: boolean
  scan_interval_hours?: number  // 1-168
  scan_time?: string  // "HH:MM" format
}

type TabId = 'agents' | 'models' | 'worker' | 'ingest' | 'system' | 'export' | 'accessibility'

const TABS: { id: TabId; label: string; icon: string }[] = [
  { id: 'agents', label: 'Agents', icon: '🤖' },
  { id: 'models', label: 'Models', icon: '🧠' },
  { id: 'worker', label: 'Worker', icon: '⚙️' },
  { id: 'ingest', label: 'Ingest', icon: '📥' },
  { id: 'system', label: 'System', icon: '🖥️' },
  { id: 'export', label: 'Export', icon: '📤' },
  { id: 'accessibility', label: 'Accessibility', icon: '♿' },
]

interface ComponentStatus {
  name: string
  running: boolean
  pid: number | null
  container?: string | null
}

interface SystemStatus {
  api: ComponentStatus
  worker: ComponentStatus
  watcher: ComponentStatus
}

export default function Settings() {
  const { preferences, updatePreferences } = usePreferences()
  const [phaseModels, setPhaseModels] = useState<PhaseModelsConfig | null>(null)
  const [worker, setWorker] = useState<WorkerConfig | null>(null)
  const [ingestConfig, setIngestConfig] = useState<IngestConfig | null>(null)
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null)
  const [restarting, setRestarting] = useState(false)
  const [apiReachable, setApiReachable] = useState(true)
  // Whether we've observed the stack actually go down since the restart request.
  const restartDipSeen = useRef(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<TabId>('agents')
  const [exportStatus, setExportStatus] = useState<{ google_drive: { configured: boolean } } | null>(null)
  const [driveFolderId, setDriveFolderId] = useState(() => localStorage.getItem('cardigan_drive_folder_id') || '')

  // Track unsaved changes
  const [pendingPhaseModels, setPendingPhaseModels] = useState<Record<string, string> | null>(null)
  const [pendingWorker, setPendingWorker] = useState<Partial<WorkerConfig> | null>(null)
  const [pendingIngest, setPendingIngest] = useState<Partial<IngestConfigUpdate> | null>(null)
  const [refreshingModels, setRefreshingModels] = useState(false)

  // Local-endpoint management (Models tab)
  const [backends, setBackends] = useState<BackendInfo[] | null>(null)
  const [newEndpoint, setNewEndpoint] = useState('')
  const [newApiKeyEnv, setNewApiKeyEnv] = useState('')
  const [backendBusy, setBackendBusy] = useState(false)

  const fetchConfig = useCallback(async () => {
    try {
      setLoading(true)
      const [modelsRes, workerRes] = await Promise.all([
        fetch('/api/config/models'),
        fetch('/api/config/worker')
      ])

      if (!workerRes.ok) {
        throw new Error('Failed to fetch worker configuration')
      }

      const workerData = await workerRes.json()
      if (modelsRes.ok) {
        setPhaseModels(await modelsRes.json())
      }
      setWorker(workerData)
      setPendingPhaseModels(null)
      setPendingWorker(null)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchIngestConfig = useCallback(async () => {
    try {
      const response = await fetch('/api/ingest/config')
      if (response.ok) {
        setIngestConfig(await response.json())
      }
    } catch {
      // Ingest config may not be available — non-critical
    }
  }, [])

  const fetchSystemStatus = useCallback(async () => {
    try {
      const res = await fetch('/api/system/status')
      if (res.ok) {
        const data = await res.json()
        setSystemStatus(data)
        setApiReachable(true)
        return
      }
      setApiReachable(false)
    } catch {
      // System status may not be available — non-critical. During a restart
      // this is the expected "blink", so it must not surface as an error.
      setApiReachable(false)
    }
  }, [])

  const restartComponents = useCallback(async () => {
    restartDipSeen.current = false
    setRestarting(true)
    try {
      await fetch('/api/system/restart', { method: 'POST' })
    } catch {
      // Expected: the API restarts itself and the socket may drop mid-request.
    }
  }, [])

  // Clear the "restarting…" banner on *observed* recovery rather than a fixed
  // timer: wait until the stack has actually dipped (API unreachable, or the
  // worker's heartbeat gone stale) and then come back fresh. A fixed timeout
  // would clear the banner while components were still down on a slow cycle,
  // and make the user wait on a fast one.
  useEffect(() => {
    if (!restarting) return

    if (!apiReachable || systemStatus?.worker.running === false) {
      restartDipSeen.current = true
      return
    }
    if (restartDipSeen.current && apiReachable && systemStatus?.worker.running) {
      setRestarting(false)
    }
  }, [restarting, apiReachable, systemStatus])

  // Backstop: never let the banner stick if a container doesn't come back.
  useEffect(() => {
    if (!restarting) return
    const cap = setTimeout(() => setRestarting(false), RESTART_BANNER_MAX_MS)
    return () => clearTimeout(cap)
  }, [restarting])


  const fetchBackends = useCallback(async () => {
    try {
      const res = await fetch('/api/config/backends')
      if (res.ok) {
        const data = await res.json()
        setBackends(data.backends)
      }
    } catch {
      // Non-critical — the Models tab shows an empty list if this fails.
    }
  }, [])

  useEffect(() => {
    fetchConfig()
    fetchIngestConfig()
    fetchSystemStatus()
    fetchBackends()
  }, [fetchConfig, fetchIngestConfig, fetchSystemStatus, fetchBackends])

  // Poll system status when on System tab
  useEffect(() => {
    if (activeTab !== 'system') return

    const interval = setInterval(fetchSystemStatus, 5000)
    return () => clearInterval(interval)
  }, [activeTab, fetchSystemStatus])

  useEffect(() => {
    if (activeTab === 'export') {
      fetch('/api/export/status')
        .then(res => res.json())
        .then(setExportStatus)
        .catch(() => {})
    }
  }, [activeTab])

  useEffect(() => {
    if (driveFolderId) {
      localStorage.setItem('cardigan_drive_folder_id', driveFolderId)
    } else {
      localStorage.removeItem('cardigan_drive_folder_id')
    }
  }, [driveFolderId])

  const handleRefreshModels = async () => {
    try {
      setRefreshingModels(true)
      const res = await fetch('/api/config/models/refresh', { method: 'POST' })
      if (!res.ok) throw new Error('Failed to refresh')
      const data = await res.json()
      setPhaseModels(data)
      setSuccess('Model roster refreshed from OpenRouter')
      setTimeout(() => setSuccess(null), 3000)
    } catch {
      setError('Could not refresh models from OpenRouter')
      setTimeout(() => setError(null), 5000)
    } finally {
      setRefreshingModels(false)
    }
  }

  const handlePhaseModelChange = (phase: string, modelId: string) => {
    const current = pendingPhaseModels || phaseModels?.phase_models || {}
    setPendingPhaseModels({ ...current, [phase]: modelId })
  }

  // After any endpoint change, rebuild the roster so discovered models appear,
  // then reload the endpoints list and the model picker together.
  const refreshRosterAndLists = useCallback(async () => {
    const res = await fetch('/api/config/models/refresh', { method: 'POST' })
    if (!res.ok) {
      setError('Could not rebuild the model roster — the model list may be out of date.')
    }
    await Promise.all([fetchBackends(), fetchConfig()])
  }, [fetchBackends, fetchConfig])

  // The roster is identical for every agent phase, so group it once per render
  // rather than recomputing inside the AGENT_INFO map (once per agent).
  const modelGroups = useMemo(
    () => groupAvailableModels(phaseModels?.available_models || []),
    [phaseModels?.available_models],
  )

  const handleAddEndpoint = async () => {
    setBackendBusy(true)
    setError(null)
    setSuccess(null)
    try {
      const res = await fetch('/api/config/backends', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          endpoint: newEndpoint.trim(),
          api_key_env: newApiKeyEnv.trim() || undefined,
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Could not add endpoint')
      }
      const created = await res.json()
      setNewEndpoint('')
      setNewApiKeyEnv('')
      await refreshRosterAndLists()
      setSuccess(`Added ${created.name}`)
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not add endpoint')
      setTimeout(() => setError(null), 5000)
    } finally {
      setBackendBusy(false)
    }
  }

  const handleToggleEndpoint = async (name: string, enabled: boolean) => {
    setBackendBusy(true)
    setError(null)
    try {
      const res = await fetch(`/api/config/backends/${encodeURIComponent(name)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Could not update endpoint')
      }
      await refreshRosterAndLists()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not update endpoint')
      setTimeout(() => setError(null), 5000)
    } finally {
      setBackendBusy(false)
    }
  }

  const handleDeleteEndpoint = async (name: string) => {
    setBackendBusy(true)
    setError(null)
    try {
      const res = await fetch(`/api/config/backends/${encodeURIComponent(name)}`, { method: 'DELETE' })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Could not remove endpoint')
      }
      await refreshRosterAndLists()
      setSuccess(`Removed ${name}`)
      setTimeout(() => setSuccess(null), 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove endpoint')
      setTimeout(() => setError(null), 5000)
    } finally {
      setBackendBusy(false)
    }
  }

  const handleWorkerChange = (key: keyof WorkerConfig, value: number) => {
    setPendingWorker({
      ...pendingWorker,
      [key]: value
    })
  }

  const handleIngestChange = (key: keyof IngestConfigUpdate, value: boolean | number | string) => {
    setPendingIngest({
      ...pendingIngest,
      [key]: value
    })
  }

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    setSuccess(null)

    try {
      // Save phase model assignments if changed
      if (pendingPhaseModels) {
        const res = await fetch('/api/config/models', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ phase_models: pendingPhaseModels })
        })
        if (!res.ok) {
          let detail = 'Failed to save model assignments'
          try {
            const data = await res.json()
            detail = data.detail || detail
          } catch {
            // Response wasn't JSON
          }
          throw new Error(detail)
        }
      }

      // Save worker config if changed
      if (pendingWorker) {
        const res = await fetch('/api/config/worker', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(pendingWorker)
        })
        if (!res.ok) {
          let detail = 'Failed to save worker config'
          try {
            const data = await res.json()
            detail = data.detail || detail
          } catch {
            // Response wasn't JSON
          }
          throw new Error(detail)
        }
      }

      // Save ingest config if changed
      if (pendingIngest) {
        const res = await fetch('/api/ingest/config', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(pendingIngest)
        })
        if (!res.ok) throw new Error('Failed to save ingest config')
      }

      setSuccess('Settings saved successfully. Restart workers to apply changes.')
      await fetchConfig()
      await fetchIngestConfig()
      setPendingIngest(null)
      setTimeout(() => setSuccess(null), 5000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    setPendingPhaseModels(null)
    setPendingWorker(null)
    setPendingIngest(null)
  }

  const hasChanges = pendingPhaseModels !== null || pendingWorker !== null || pendingIngest !== null

  const getCurrentWorker = (): WorkerConfig => {
    return {
      max_concurrent_jobs: pendingWorker?.max_concurrent_jobs ?? worker?.max_concurrent_jobs ?? 3,
      poll_interval_seconds: pendingWorker?.poll_interval_seconds ?? worker?.poll_interval_seconds ?? 5,
      heartbeat_interval_seconds: pendingWorker?.heartbeat_interval_seconds ?? worker?.heartbeat_interval_seconds ?? 60
    }
  }

  const getCurrentIngest = (): IngestConfig => {
    return {
      enabled: pendingIngest?.enabled ?? ingestConfig?.enabled ?? false,
      scan_interval_hours: pendingIngest?.scan_interval_hours ?? ingestConfig?.scan_interval_hours ?? 24,
      scan_time: pendingIngest?.scan_time ?? ingestConfig?.scan_time ?? '02:00',
      last_scan_at: ingestConfig?.last_scan_at ?? null,
      last_scan_success: ingestConfig?.last_scan_success ?? null,
      server_url: ingestConfig?.server_url ?? '',
      directories: ingestConfig?.directories ?? [],
      ignore_directories: ingestConfig?.ignore_directories ?? [],
      next_scan_at: ingestConfig?.next_scan_at ?? null
    }
  }

  const formatDateTime = (isoString: string | null): string => {
    if (!isoString) return 'Never'
    const date = new Date(isoString)
    return date.toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true
    })
  }

  // Remote preview hides the config surface (nginx blocks the config writes);
  // show a short note instead of a visibly-broken Settings page.
  if (isRemotePreview()) {
    return (
      <div className="flex items-center justify-center py-24 px-4">
        <p role="status" className="text-surface-300 text-center text-sm">
          Configuration unavailable in remote application.
        </p>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold text-white">Settings</h1>
        <div className="bg-surface-800 rounded-lg border border-surface-700 p-6">
          <p className="text-surface-400 animate-pulse">Loading configuration...</p>
        </div>
      </div>
    )
  }

  // Toggle component for cleaner code
  const Toggle = ({ checked, onChange, label }: { checked: boolean; onChange: () => void; label: string }) => (
    <button
      onClick={onChange}
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
        checked ? 'bg-pbs-500' : 'bg-surface-600'
      }`}
    >
      <span
        aria-hidden="true"
        className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
          checked ? 'translate-x-6' : 'translate-x-1'
        }`}
      />
    </button>
  )

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Settings</h1>
        {hasChanges && (
          <div className="flex items-center space-x-3">
            <button
              onClick={handleReset}
              className="px-4 py-2 bg-surface-700 hover:bg-surface-600 text-white rounded-md text-sm transition-colors"
            >
              Reset
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="px-4 py-2 bg-pbs-500 hover:bg-pbs-400 disabled:opacity-50 text-white rounded-md text-sm transition-colors"
            >
              {saving ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        )}
      </div>

      {/* Status Messages */}
      {error && (
        <div role="alert" aria-live="assertive" className="bg-status-failed/15 border border-status-failed/30 rounded-lg p-4">
          <p className="text-status-failed">{error}</p>
        </div>
      )}
      {success && (
        <div role="status" aria-live="polite" className="bg-status-completed/15 border border-status-completed/30 rounded-lg p-4">
          <p className="text-status-completed">{success}</p>
        </div>
      )}

      {/* Tab Navigation */}
      <div className="border-b border-surface-700">
        <nav className="flex space-x-1" role="tablist" aria-label="Settings sections">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              id={`tab-${tab.id}`}
              role="tab"
              aria-selected={activeTab === tab.id}
              aria-controls={`panel-${tab.id}`}
              onClick={() => setActiveTab(tab.id)}
              className={`px-4 py-3 text-sm font-medium rounded-t-lg transition-colors ${
                activeTab === tab.id
                  ? 'bg-surface-800 text-white border-b-2 border-pbs-500'
                  : 'text-surface-400 hover:text-surface-200 hover:bg-surface-800/50'
              }`}
            >
              <span className="mr-2">{tab.icon}</span>
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Panels */}
      <div role="tabpanel" id={`panel-${activeTab}`} aria-labelledby={`tab-${activeTab}`}>
        {/* AGENTS TAB */}
        {activeTab === 'agents' && (
          <div className="space-y-6">
            {/* Agent Model Assignment */}
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-6">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold text-white">Agent Models</h2>
                <button
                  onClick={handleRefreshModels}
                  disabled={refreshingModels}
                  className="text-xs text-surface-400 hover:text-white transition-colors disabled:opacity-50"
                  title="Fetch latest models from OpenRouter"
                  aria-label="Refresh available models from OpenRouter"
                >
                  {refreshingModels ? 'Refreshing…' : '↻ Refresh models'}
                </button>
              </div>
              <p className="text-sm text-surface-400 mb-6">
                Choose which model runs each agent phase.
              </p>

              <div className="space-y-4">
                {AGENT_INFO.map((agent) => {
                  const currentModel = pendingPhaseModels?.[agent.id]
                    || phaseModels?.phase_models?.[agent.id]
                    || ''

                  return (
                    <div key={agent.id} className="p-4 bg-surface-900 rounded-lg space-y-2">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center space-x-3">
                          <span className="text-lg">{agent.icon}</span>
                          <span className="font-medium text-white">{agent.name}</span>
                        </div>

                        <label htmlFor={`model-${agent.id}`} className="sr-only">
                          Model for {agent.name}
                        </label>
                        <select
                          id={`model-${agent.id}`}
                          value={currentModel}
                          onChange={(e) => handlePhaseModelChange(agent.id, e.target.value)}
                          className="pl-3 pr-8 py-2 rounded-md border text-sm font-medium bg-surface-800 border-surface-600 text-surface-200"
                          aria-label={`Select model for ${agent.name} agent`}
                        >
                          {modelGroups.map((group) => (
                            <optgroup key={group.label} label={group.label}>
                              {group.models.map((m) => (
                                <option key={m.id} value={m.id} className="bg-surface-800 text-white">
                                  {m.name}
                                </option>
                              ))}
                            </optgroup>
                          ))}
                        </select>
                      </div>
                      <p className="text-sm text-surface-400 pl-8 max-w-prose">{agent.description}</p>
                    </div>
                  )
                })}
              </div>
            </div>

            {/* Model Usage Stats (from Langfuse) */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <ModelStatsWidget />
              <PhaseStatsWidget />
            </div>
          </div>
        )}

        {/* MODELS TAB */}
        {activeTab === 'models' && (
          <div className="space-y-6">
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-6">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold text-white">Local model endpoints</h2>
                <button
                  onClick={refreshRosterAndLists}
                  disabled={backendBusy}
                  className="text-xs text-surface-400 hover:text-white transition-colors disabled:opacity-50"
                  aria-label="Re-check endpoints for available models"
                >
                  ↻ Rediscover
                </button>
              </div>
              <p className="text-sm text-surface-400 mb-6">
                Connect an OpenAI-compatible server (oMLX, vLLM, LM Studio). Its models join
                the cloud ones in the Agents tab, free to run.
              </p>

              <div className="space-y-3">
                {(backends || []).filter((b) => b.type === 'openai' && b.discover).length === 0 && (
                  <p className="text-sm text-surface-500">
                    No endpoints yet. Add one below to run models on your own hardware.
                  </p>
                )}
                {(backends || [])
                  .filter((b) => b.type === 'openai' && b.discover)
                  .map((b) => {
                    const count = (phaseModels?.available_models || []).filter((m) => m.backend === b.name).length
                    return (
                      <div key={b.name} className="flex items-center justify-between p-4 bg-surface-900 rounded-lg">
                        <div className="min-w-0">
                          <div className="font-medium text-white truncate">{b.name}</div>
                          <div className="text-xs text-surface-400">
                            {b.enabled ? `${count} model${count === 1 ? '' : 's'} available` : 'disabled'}
                          </div>
                        </div>
                        <div className="flex items-center gap-4 shrink-0">
                          <button
                            onClick={() => handleToggleEndpoint(b.name, !b.enabled)}
                            disabled={backendBusy}
                            className="text-xs text-surface-400 hover:text-white transition-colors disabled:opacity-50"
                            aria-label={`${b.enabled ? 'Disable' : 'Enable'} endpoint ${b.name}`}
                          >
                            {b.enabled ? 'Disable' : 'Enable'}
                          </button>
                          <button
                            onClick={() => handleDeleteEndpoint(b.name)}
                            disabled={backendBusy}
                            className="text-xs text-red-400 hover:text-red-300 transition-colors disabled:opacity-50"
                            aria-label={`Remove endpoint ${b.name}`}
                          >
                            Remove
                          </button>
                        </div>
                      </div>
                    )
                  })}
              </div>

              <div className="mt-6 pt-6 border-t border-surface-700 space-y-3">
                <h3 className="text-sm font-semibold text-white">Add an endpoint</h3>
                <div className="flex flex-col sm:flex-row gap-3">
                  <input
                    value={newEndpoint}
                    onChange={(e) => setNewEndpoint(e.target.value)}
                    placeholder="http://host:8000/v1"
                    aria-label="Endpoint base URL"
                    className="flex-1 px-3 py-2 rounded-md bg-surface-900 border border-surface-600 text-surface-200 text-sm placeholder:text-surface-500"
                  />
                  <input
                    value={newApiKeyEnv}
                    onChange={(e) => setNewApiKeyEnv(e.target.value)}
                    placeholder="API key env var (optional)"
                    aria-label="API key environment variable name (optional)"
                    className="flex-1 px-3 py-2 rounded-md bg-surface-900 border border-surface-600 text-surface-200 text-sm placeholder:text-surface-500"
                  />
                  <button
                    onClick={handleAddEndpoint}
                    disabled={backendBusy || !newEndpoint.trim()}
                    className="px-4 py-2 bg-pbs-500 hover:bg-pbs-400 disabled:opacity-50 text-white rounded-md text-sm transition-colors whitespace-nowrap"
                  >
                    {backendBusy ? 'Working…' : 'Add & discover'}
                  </button>
                </div>
                <p className="text-xs text-surface-500">
                  The key is read from that named environment variable on the server — it isn't stored here.
                </p>
              </div>
            </div>
          </div>
        )}

        {/* WORKER TAB */}
        {activeTab === 'worker' && (
          <div className="space-y-6">
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-6">
              <h2 className="text-lg font-semibold text-white mb-4">Worker Settings</h2>
              <p className="text-sm text-surface-400 mb-6">
                Configure job processing concurrency. Changes require worker restart.
              </p>

              <div className="space-y-4">
                {/* Concurrent Jobs */}
                <div className="p-4 bg-surface-900 rounded-lg">
                  <div className="flex items-center justify-between mb-2">
                    <div>
                      <label htmlFor="concurrent-jobs" className="font-medium text-white">Concurrent Jobs</label>
                      <div className="text-sm text-surface-400">Process multiple jobs simultaneously</div>
                    </div>
                    <span className="text-2xl font-bold text-pbs-300">{getCurrentWorker().max_concurrent_jobs}</span>
                  </div>
                  <input
                    id="concurrent-jobs"
                    type="range"
                    min="1"
                    max="5"
                    step="1"
                    value={getCurrentWorker().max_concurrent_jobs}
                    onChange={(e) => handleWorkerChange('max_concurrent_jobs', parseInt(e.target.value))}
                    className="w-full h-2 bg-surface-700 rounded-lg appearance-none cursor-pointer"
                    aria-valuemin={1}
                    aria-valuemax={5}
                    aria-valuenow={getCurrentWorker().max_concurrent_jobs}
                  />
                  <div className="flex justify-between text-xs text-surface-400 mt-1">
                    <span>1 (safe)</span>
                    <span>3 (default)</span>
                    <span>5 (max)</span>
                  </div>
                </div>

                {/* Poll Interval */}
                <div className="p-4 bg-surface-900 rounded-lg">
                  <div className="flex items-center justify-between mb-2">
                    <div>
                      <label htmlFor="poll-interval" className="font-medium text-white">Poll Interval</label>
                      <div className="text-sm text-surface-400">Seconds between queue checks</div>
                    </div>
                    <span className="text-surface-300">{getCurrentWorker().poll_interval_seconds}s</span>
                  </div>
                  <input
                    id="poll-interval"
                    type="range"
                    min="1"
                    max="30"
                    step="1"
                    value={getCurrentWorker().poll_interval_seconds}
                    onChange={(e) => handleWorkerChange('poll_interval_seconds', parseInt(e.target.value))}
                    className="w-full h-2 bg-surface-700 rounded-lg appearance-none cursor-pointer"
                    aria-valuemin={1}
                    aria-valuemax={30}
                    aria-valuenow={getCurrentWorker().poll_interval_seconds}
                  />
                  <div className="flex justify-between text-xs text-surface-400 mt-1">
                    <span>1s</span>
                    <span>15s</span>
                    <span>30s</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* INGEST TAB */}
        {activeTab === 'ingest' && (
          <div className="space-y-6">
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-6">
              <h2 className="text-lg font-semibold text-white mb-4">Ingest Scanner</h2>
              <p className="text-sm text-surface-400 mb-6">
                Automatically scan network locations for new transcript files and add them to the processing queue.
              </p>

              <div className="space-y-4">
                {/* Enable Toggle */}
                <div className="flex items-center justify-between p-4 bg-surface-900 rounded-lg">
                  <div>
                    <div className="font-medium text-white">Enable Scanner</div>
                    <div className="text-sm text-surface-400">Automatically discover and ingest new transcripts</div>
                  </div>
                  <Toggle
                    checked={getCurrentIngest().enabled}
                    onChange={() => handleIngestChange('enabled', !getCurrentIngest().enabled)}
                    label="Enable ingest scanner"
                  />
                </div>

                {/* Scan Interval */}
                <div className="p-4 bg-surface-900 rounded-lg">
                  <div className="flex items-center justify-between mb-2">
                    <div>
                      <label htmlFor="scan-interval" className="font-medium text-white">Scan Interval</label>
                      <div className="text-sm text-surface-400">Hours between automatic scans</div>
                    </div>
                    <span className="text-surface-300">{getCurrentIngest().scan_interval_hours}h</span>
                  </div>
                  <input
                    id="scan-interval"
                    type="range"
                    min="1"
                    max="168"
                    step="1"
                    value={getCurrentIngest().scan_interval_hours}
                    onChange={(e) => handleIngestChange('scan_interval_hours', parseInt(e.target.value))}
                    className="w-full h-2 bg-surface-700 rounded-lg appearance-none cursor-pointer"
                    aria-valuemin={1}
                    aria-valuemax={168}
                    aria-valuenow={getCurrentIngest().scan_interval_hours}
                  />
                  <div className="flex justify-between text-xs text-surface-400 mt-1">
                    <span>1h</span>
                    <span>24h (daily)</span>
                    <span>168h (weekly)</span>
                  </div>
                </div>

                {/* Scan Time */}
                <div className="p-4 bg-surface-900 rounded-lg">
                  <div className="mb-2">
                    <label htmlFor="scan-time" className="font-medium text-white block">Preferred Scan Time</label>
                    <div className="text-sm text-surface-400">Daily time to run scheduled scans (24-hour format)</div>
                  </div>
                  <input
                    id="scan-time"
                    type="time"
                    value={getCurrentIngest().scan_time}
                    onChange={(e) => handleIngestChange('scan_time', e.target.value)}
                    className="px-3 py-2 bg-surface-800 border border-surface-600 rounded-md text-white focus:border-pbs-500 focus:outline-none"
                  />
                </div>

                {/* Server URL (read-only) */}
                <div className="p-4 bg-surface-900 rounded-lg">
                  <div className="mb-2">
                    <div className="font-medium text-white">Server URL</div>
                    <div className="text-sm text-surface-400">Remote file server location</div>
                  </div>
                  <code className="block p-2 bg-surface-800 rounded text-pbs-300 text-sm">
                    {getCurrentIngest().server_url || 'Not configured'}
                  </code>
                </div>

                {/* Last Scan Info */}
                {getCurrentIngest().last_scan_at && (
                  <div className="p-4 bg-surface-900 rounded-lg">
                    <div className="flex items-center justify-between mb-2">
                      <div className="font-medium text-white">Last Scan</div>
                      <span className={`px-2 py-1 rounded text-xs font-medium ${
                        getCurrentIngest().last_scan_success
                          ? 'bg-status-completed/15 text-status-completed'
                          : 'bg-status-failed/15 text-status-failed'
                      }`}>
                        {getCurrentIngest().last_scan_success ? 'Success' : 'Failed'}
                      </span>
                    </div>
                    <div className="text-sm text-surface-400">
                      {formatDateTime(getCurrentIngest().last_scan_at)}
                    </div>
                  </div>
                )}

                {/* Next Scheduled Scan */}
                {getCurrentIngest().next_scan_at && (
                  <div className="p-4 bg-surface-900 rounded-lg">
                    <div className="font-medium text-white mb-2">Next Scheduled Scan</div>
                    <div className="text-sm text-surface-400">
                      {formatDateTime(getCurrentIngest().next_scan_at)}
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* Link to Ready for Work page */}
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-4">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-medium text-white">Ready for Work</h3>
                  <p className="text-xs text-surface-400 mt-1">
                    View and queue transcripts from the ingest server.
                  </p>
                </div>
                <Link
                  to="/ready"
                  className="text-sm text-pbs-400 hover:text-pbs-300 transition-colors"
                >
                  Open →
                </Link>
              </div>
            </div>

            {/* Screengrab Info */}
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-4">
              <div className="flex items-start space-x-3">
                <div className="flex-shrink-0 w-10 h-10 bg-pbs-500/20 rounded-full flex items-center justify-center">
                  <svg className="w-5 h-5 text-pbs-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
                  </svg>
                </div>
                <div>
                  <h3 className="text-sm font-medium text-white">Screengrab Attachments</h3>
                  <p className="text-xs text-surface-400 mt-1">
                    Screengrabs are attached from the job detail page. Completed jobs with matching
                    screengrabs show an "Attach Screengrabs" button in the job header.
                  </p>
                </div>
              </div>
            </div>

            {/* Configuration Note */}
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-4">
              <div className="flex items-start space-x-3">
                <span className="text-yellow-400 text-xl">💡</span>
                <div>
                  <h3 className="text-sm font-medium text-white">Server Configuration</h3>
                  <p className="text-xs text-surface-400 mt-1">
                    Network paths and credentials are managed in server environment variables.
                    Contact your system administrator to modify the server URL or monitored directories.
                  </p>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* SYSTEM TAB */}
        {activeTab === 'system' && (
          <div className="space-y-6">
            {/* Component Status */}
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-6">
              <h2 className="text-lg font-semibold text-white mb-4">System Components</h2>
              <p className="text-sm text-surface-400 mb-6">
                Monitor the containerized services that power The Metadata Neighborhood. Components are managed by Docker Compose.
              </p>

              <div className="space-y-4">
                {/* API Server */}
                <div className="p-4 bg-surface-900 rounded-lg">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center space-x-3">
                      <div className={`w-3 h-3 rounded-full ${systemStatus?.api.running !== false ? 'bg-status-completed' : 'bg-status-pending'}`} />
                      <div>
                        <div className="font-medium text-white">API Server</div>
                        <div className="text-sm text-surface-400">
                          Running - Managed by Docker
                        </div>
                      </div>
                    </div>
                    <div className="px-3 py-1 text-xs bg-pbs-500/20 text-pbs-400 border border-pbs-500/30 rounded">
                      Container: {systemStatus?.api.container ?? 'cardigan-api'}
                    </div>
                  </div>
                </div>

                {/* Worker */}
                <div className="p-4 bg-surface-900 rounded-lg">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center space-x-3">
                      <div className={`w-3 h-3 rounded-full ${systemStatus?.worker.running ? 'bg-status-completed' : 'bg-status-pending'}`} />
                      <div>
                        <div className="font-medium text-white">Worker</div>
                        <div className="text-sm text-surface-400">
                          {systemStatus?.worker.running
                            ? 'Running - Managed by Docker'
                            : 'Managed by Docker'}
                        </div>
                      </div>
                    </div>
                    <div className="px-3 py-1 text-xs bg-pbs-500/20 text-pbs-400 border border-pbs-500/30 rounded">
                      Container: {systemStatus?.worker.container ?? 'cardigan-worker'}
                    </div>
                  </div>
                </div>

                {/* Watcher */}
                <div className="p-4 bg-surface-900 rounded-lg">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center space-x-3">
                      <div className={`w-3 h-3 rounded-full ${systemStatus?.watcher.running ? 'bg-status-completed' : 'bg-status-pending'}`} />
                      <div>
                        <div className="font-medium text-white">Transcript Watcher</div>
                        <div className="text-sm text-surface-400">
                          {systemStatus?.watcher.running ? 'Running - Managed by Docker' : 'Not deployed'}
                        </div>
                      </div>
                    </div>
                    <div className="px-3 py-1 text-xs bg-pbs-500/20 text-pbs-400 border border-pbs-500/30 rounded">
                      Container: {systemStatus?.watcher.container ?? '—'}
                    </div>
                  </div>
                </div>
              </div>

              <div className="mt-6">
                <RestartComponentsButton onConfirm={restartComponents} restarting={restarting} />
              </div>
            </div>

            {/* Folder Paths */}
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-6">
              <h2 className="text-lg font-semibold text-white mb-4">Docker Volume Mounts</h2>
              <p className="text-sm text-surface-400 mb-4">
                Persistent data volumes mounted in the container
              </p>
              <div className="space-y-3 text-sm">
                <div className="flex justify-between p-3 bg-surface-900 rounded">
                  <span className="text-surface-400">Transcripts (input)</span>
                  <code className="text-pbs-300">/data/transcripts</code>
                </div>
                <div className="flex justify-between p-3 bg-surface-900 rounded">
                  <span className="text-surface-400">Output (processed)</span>
                  <code className="text-pbs-300">/data/output</code>
                </div>
                <div className="flex justify-between p-3 bg-surface-900 rounded">
                  <span className="text-surface-400">Database</span>
                  <code className="text-pbs-300">/data/db/dashboard.db</code>
                </div>
                <div className="flex justify-between p-3 bg-surface-900 rounded">
                  <span className="text-surface-400">Uploads</span>
                  <code className="text-pbs-300">/data/uploads</code>
                </div>
              </div>
            </div>

            {/* Terminal Commands */}
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-4">
              <div className="flex items-start space-x-3">
                <span className="text-pbs-400 text-xl">🐳</span>
                <div className="w-full">
                  <h3 className="text-sm font-medium text-white">Docker Commands</h3>
                  <p className="text-xs text-surface-400 mt-1 mb-3">
                    Manage the containerized system via Docker Compose
                  </p>
                  <div className="space-y-2 text-xs">
                    <div className="p-2 bg-surface-900 rounded">
                      <div className="text-surface-400 mb-1">View logs:</div>
                      <code className="text-green-400">docker compose logs -f</code>
                    </div>
                    <div className="p-2 bg-surface-900 rounded">
                      <div className="text-surface-400 mb-1">Stop system:</div>
                      <code className="text-green-400">docker compose down</code>
                    </div>
                    <div className="p-2 bg-surface-900 rounded">
                      <div className="text-surface-400 mb-1">Rebuild and restart:</div>
                      <code className="text-green-400">docker compose up --build -d</code>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* EXPORT TAB */}
        {activeTab === 'export' && (
          <div className="space-y-6">
            <div>
              <h2 className="text-xl font-semibold text-white mb-4">Export Settings</h2>

              <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 space-y-4">
                <div className="flex items-center justify-between">
                  <h3 className="text-lg font-medium text-white">Google Drive</h3>
                  {exportStatus ? (
                    <span className={`px-2 py-1 rounded text-xs font-medium ${
                      exportStatus.google_drive.configured
                        ? 'bg-green-900/50 text-green-400 border border-green-800'
                        : 'bg-gray-700 text-gray-400'
                    }`}>
                      {exportStatus.google_drive.configured ? 'Connected' : 'Not Configured'}
                    </span>
                  ) : (
                    <span className="text-gray-500 text-sm">Loading...</span>
                  )}
                </div>

                {exportStatus?.google_drive.configured ? (
                  <div className="space-y-3">
                    <p className="text-sm text-gray-400">
                      Google Drive export is active. Output files can be uploaded directly from job detail pages.
                    </p>
                    <div>
                      <label htmlFor="drive-folder-id" className="block text-sm text-gray-300 mb-1">
                        Default folder ID <span className="text-gray-500">(optional)</span>
                      </label>
                      <input
                        id="drive-folder-id"
                        type="text"
                        value={driveFolderId}
                        onChange={(e) => setDriveFolderId(e.target.value)}
                        placeholder="e.g., 1a2b3c4d5e6f..."
                        className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 font-mono"
                      />
                      <p className="mt-1 text-xs text-gray-500">
                        Find this in the Drive folder URL after /folders/. Leave empty to upload to the service account's root.
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <p className="text-sm text-gray-400">
                      To enable Google Drive export, add a service account credentials JSON file as the <code className="text-blue-300 bg-gray-900 px-1 rounded text-xs">GOOGLE_DRIVE_CREDENTIALS</code> secret.
                    </p>
                    <p className="text-sm text-gray-500">
                      See the project documentation for setup instructions.
                    </p>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {/* ACCESSIBILITY TAB */}
        {activeTab === 'accessibility' && (
          <div className="space-y-6">
            {/* Reduce Motion */}
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-6">
              <h2 className="text-lg font-semibold text-white mb-4">Motion Preferences</h2>
              <p className="text-sm text-surface-400 mb-6">
                Control animations and transitions throughout the dashboard.
              </p>

              <div className="flex items-center justify-between p-4 bg-surface-900 rounded-lg">
                <div>
                  <div className="font-medium text-white">Reduce Motion</div>
                  <div className="text-sm text-surface-400">Minimize or disable animations</div>
                </div>
                <Toggle
                  checked={preferences.reduceMotion}
                  onChange={() => updatePreferences({ reduceMotion: !preferences.reduceMotion })}
                  label="Reduce motion"
                />
              </div>
            </div>

            {/* Text Size */}
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-6">
              <h2 className="text-lg font-semibold text-white mb-4">Text Size</h2>
              <p className="text-sm text-surface-400 mb-6">
                Adjust the base text size for improved readability.
              </p>

              <div className="space-y-3">
                {(['default', 'large', 'larger'] as TextSize[]).map((size) => (
                  <button
                    key={size}
                    onClick={() => updatePreferences({ textSize: size })}
                    className={`w-full p-4 rounded-lg border text-left transition-colors ${
                      preferences.textSize === size
                        ? 'bg-pbs-900/20 border-pbs-500/30 text-pbs-400'
                        : 'bg-surface-900 border-surface-700 text-surface-300 hover:bg-surface-800'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <div>
                        <div className="font-medium capitalize">{size}</div>
                        <div className="text-sm text-surface-400">
                          {size === 'default' && 'Standard text size (16px base)'}
                          {size === 'large' && 'Larger text size (18px base)'}
                          {size === 'larger' && 'Largest text size (20px base)'}
                        </div>
                      </div>
                      {preferences.textSize === size && (
                        <span className="text-pbs-400">✓</span>
                      )}
                    </div>
                    <div className="mt-2 text-surface-400" style={{
                      fontSize: size === 'default' ? '16px' : size === 'large' ? '18px' : '20px'
                    }}>
                      Sample preview text
                    </div>
                  </button>
                ))}
              </div>
            </div>

            {/* High Contrast */}
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-6">
              <h2 className="text-lg font-semibold text-white mb-4">Contrast</h2>
              <p className="text-sm text-surface-400 mb-6">
                Increase contrast between text and backgrounds for better visibility.
              </p>

              <div className="flex items-center justify-between p-4 bg-surface-900 rounded-lg">
                <div>
                  <div className="font-medium text-white">High Contrast Mode</div>
                  <div className="text-sm text-surface-400">Enhance text and UI element contrast</div>
                </div>
                <Toggle
                  checked={preferences.highContrast}
                  onChange={() => updatePreferences({ highContrast: !preferences.highContrast })}
                  label="High contrast mode"
                />
              </div>
            </div>

            {/* Preview Note */}
            <div className="bg-surface-800 rounded-lg border border-surface-700 p-4">
              <div className="flex items-start space-x-3">
                <span className="text-pbs-400 text-xl">💡</span>
                <div>
                  <h3 className="text-sm font-medium text-white">Live Preview</h3>
                  <p className="text-xs text-surface-400 mt-1">
                    Changes are applied immediately and saved automatically. Navigate to other pages
                    to see how preferences affect the entire dashboard.
                  </p>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
