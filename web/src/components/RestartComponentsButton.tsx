import { useState } from 'react'

interface Props {
  onConfirm: () => void | Promise<void>
  restarting: boolean
}

export default function RestartComponentsButton({ onConfirm, restarting }: Props) {
  const [confirming, setConfirming] = useState(false)

  if (restarting) {
    return (
      <span role="status" className="text-sm text-pbs-300">
        Restarting… reconnecting to the dashboard.
      </span>
    )
  }

  if (!confirming) {
    return (
      <button
        type="button"
        onClick={() => setConfirming(true)}
        className="px-4 py-2 text-sm font-medium bg-pbs-600 hover:bg-pbs-500 text-white rounded focus:outline-none focus:ring-2 focus:ring-pbs-400"
      >
        Restart Components
      </button>
    )
  }

  return (
    <div className="flex items-center gap-3">
      <span className="text-sm text-surface-300">
        Restart the API and worker? The dashboard will briefly disconnect.
      </span>
      <button
        type="button"
        onClick={() => {
          setConfirming(false)
          onConfirm()
        }}
        className="px-3 py-2 text-sm font-medium bg-red-600 hover:bg-red-500 text-white rounded focus:outline-none focus:ring-2 focus:ring-red-400"
      >
        Confirm
      </button>
      <button
        type="button"
        onClick={() => setConfirming(false)}
        className="px-3 py-2 text-sm text-surface-300 hover:text-white"
      >
        Cancel
      </button>
    </div>
  )
}
