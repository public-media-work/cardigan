import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, test, expect, vi } from 'vitest'
import RestartComponentsButton from '../RestartComponentsButton'

describe('RestartComponentsButton', () => {
  test('requires confirmation before calling onConfirm', async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined)
    render(<RestartComponentsButton onConfirm={onConfirm} restarting={false} />)

    fireEvent.click(screen.getByRole('button', { name: /restart components/i }))
    expect(onConfirm).not.toHaveBeenCalled() // shows confirm step, does not fire yet

    fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }))
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
  })

  test('cancel aborts without calling onConfirm', () => {
    const onConfirm = vi.fn()
    render(<RestartComponentsButton onConfirm={onConfirm} restarting={false} />)
    fireEvent.click(screen.getByRole('button', { name: /restart components/i }))
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onConfirm).not.toHaveBeenCalled()
    // back to the initial state
    expect(screen.getByRole('button', { name: /restart components/i })).toBeTruthy()
  })

  test('shows a status message while restarting', () => {
    render(<RestartComponentsButton onConfirm={vi.fn()} restarting={true} />)
    expect(screen.getByRole('status').textContent).toMatch(/restarting/i)
  })
})
