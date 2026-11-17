// Geometry for the airport map, kept apart from the drawing so it can be tested.
// The engine sends metres east and north; on screen, y runs down, so north is negated here and nowhere else.

import type { Chart, TaxiView } from '../../shared/protocol'

export interface Box {
  x: number
  y: number
  w: number
  h: number
}

export type XY = [number, number]

export const MIN_SPAN_M = 150
export const MAX_SPAN_M = 30000

/** A point from the engine, as screen coordinates. */
export function screen(point: XY): XY {
  return [point[0], -point[1]]
}

/** The smallest box of the given shape (width / height) that holds every point, with a margin around them. */
export function fitBox(points: XY[], aspect: number, margin = 0.12, least = 300): Box {
  if (points.length === 0) return { x: -500, y: -500 / aspect, w: 1000, h: 1000 / aspect }
  const xs = points.map((point) => point[0])
  const ys = points.map((point) => point[1])
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2
  const cy = (Math.min(...ys) + Math.max(...ys)) / 2
  let w = Math.max((Math.max(...xs) - Math.min(...xs)) * (1 + 2 * margin), least)
  let h = Math.max((Math.max(...ys) - Math.min(...ys)) * (1 + 2 * margin), least)
  if (w / h < aspect) w = h * aspect
  else h = w / aspect
  return { x: cx - w / 2, y: cy - h / 2, w, h }
}

/** What "Fit" shows: the taxi route with the aircraft when there is one, otherwise the whole airport. */
export function fitPoints(chart: Chart, view: TaxiView): XY[] {
  if (view.route) {
    return [view.position, ...view.route.cleared, ...view.route.beyond, view.route.goal.at].map(screen)
  }
  const [west, south, east, north] = chart.bounds
  return [screen([west, south]), screen([east, north])]
}

export function centred(at: XY, span: number, aspect: number): Box {
  return { x: at[0] - span / 2, y: at[1] - span / aspect / 2, w: span, h: span / aspect }
}

/** Zoom by `factor` (below 1 is closer), keeping the point `about` where it is on screen. */
export function zoomBox(box: Box, factor: number, about: XY): Box {
  const w = Math.min(Math.max(box.w * factor, MIN_SPAN_M), MAX_SPAN_M)
  const applied = w / box.w
  return {
    x: about[0] - (about[0] - box.x) * applied,
    y: about[1] - (about[1] - box.y) * applied,
    w,
    h: box.h * applied
  }
}

/** An SVG path through points given as x, y, x, y… in engine coordinates; closed for a pavement ring. */
export function ringPath(flat: number[], close: boolean): string {
  let d = ''
  for (let index = 0; index + 1 < flat.length; index += 2) {
    d += `${index === 0 ? 'M' : 'L'}${flat[index]} ${-flat[index + 1]}`
  }
  return close && d ? `${d}Z` : d
}

/** An SVG path of separate segments given as x1, y1, x2, y2… */
export function segmentsPath(flat: number[]): string {
  let d = ''
  for (let index = 0; index + 3 < flat.length; index += 4) {
    d += `M${flat[index]} ${-flat[index + 1]}L${flat[index + 2]} ${-flat[index + 3]}`
  }
  return d
}

export function polyline(points: XY[]): string {
  return points.map((point) => `${point[0]},${-point[1]}`).join(' ')
}

/** Where a runway's number is written: just beyond its end, on the extended centreline. */
export function beyondEnd(end: XY, other: XY, distance: number): XY {
  const dx = end[0] - other[0]
  const dy = end[1] - other[1]
  const length = Math.hypot(dx, dy) || 1
  return [end[0] + (dx / length) * distance, end[1] + (dy / length) * distance]
}
