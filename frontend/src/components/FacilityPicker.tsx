import type { Facility } from '../lib/types'

export function FacilityPicker({
  facilities, value, onChange,
}: { facilities: Facility[]; value: number | null; onChange: (id: number) => void }) {
  if (facilities.length <= 1) return null
  return (
    <select
      className="input w-auto py-1.5 text-sm"
      value={value ?? ''}
      onChange={(event) => onChange(Number(event.target.value))}
    >
      {facilities.map((facility) => (
        <option key={facility.id} value={facility.id}>
          {facility.name}
        </option>
      ))}
    </select>
  )
}
