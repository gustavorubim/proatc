import { describe, expect, it } from 'vitest'
import type { Chart, TaxiView } from '../../shared/protocol'
import { beyondEnd, centred, fitBox, fitPoints, polyline, ringPath, segmentsPath, zoomBox, MIN_SPAN_M } from './map'
import { initialStore, reduce } from './store'

const chart: Chart = {
  ident: 'EGXX',
  name: 'Testfield',
  runways: [
    {
      name: '09/27',
      width: 45,
      ends: [
        ['09', -1000, 0],
        ['27', 1000, 0]
      ]
    }
  ],
  pavement: [],
  taxiways: [{ name: 'A', lines: [0, 0, 0, -400] }],
  labels: [{ name: 'A', at: [0, -200] }],
  stands: [{ name: '5', at: [0, -400] }],
  bounds: [-1000, -400, 1000, 0]
}

describe('airport map geometry', () => {
  it('fits points into a box of the window’s shape, with a margin', () => {
    const box = fitBox(
      [
        [0, 0],
        [1000, 100]
      ],
      2,
      0.1
    )
    expect(box.w / box.h).toBeCloseTo(2)
    expect(box.w).toBeCloseTo(1200)
    expect(box.x + box.w / 2).toBeCloseTo(500)
    expect(box.y + box.h / 2).toBeCloseTo(50)
    const tall = fitBox(
      [
        [0, 0],
        [10, 2000]
      ],
      2,
      0
    )
    expect(tall.h).toBeCloseTo(2000)
    expect(tall.w).toBeCloseTo(4000)
    expect(fitBox([[5, 5]], 1).w).toBe(300) // one point still gets a view
    expect(fitBox([], 2).w).toBe(1000)
  })

  it('shows the route when there is one, otherwise the airport', () => {
    const parked: TaxiView = { ident: 'EGXX', position: [0, -400], heading: 0 }
    expect(fitPoints(chart, parked)).toEqual([
      [-1000, 400],
      [1000, -0]
    ])
    const taxiing: TaxiView = {
      ...parked,
      route: {
        cleared: [
          [0, -400],
          [0, -75]
        ],
        beyond: [
          [0, -75],
          [0, 0]
        ],
        goal: { label: 'Holding point 27', at: [0, 0] },
        hold: { runway: '09', at: [0, -75] }
      }
    }
    const points = fitPoints(chart, taxiing)
    expect(points[0]).toEqual([0, 400]) // north is up: the engine's north becomes the screen's minus y
    expect(points).toHaveLength(6)
  })

  it('zooms about a point and stops at its limits', () => {
    const box = { x: 0, y: 0, w: 1000, h: 500 }
    const closer = zoomBox(box, 0.5, [250, 250])
    expect(closer).toEqual({ x: 125, y: 125, w: 500, h: 250 })
    // The point under the cursor stays where it was on screen.
    expect((250 - closer.x) / closer.w).toBeCloseTo((250 - box.x) / box.w)
    expect(zoomBox(box, 0.0001, [0, 0]).w).toBe(MIN_SPAN_M)
    expect(centred([100, 100], 400, 2)).toEqual({ x: -100, y: 0, w: 400, h: 200 })
  })

  it('writes paths with north up', () => {
    expect(ringPath([0, 0, 10, 0, 10, 5], true)).toBe('M0 0L10 0L10 -5Z')
    expect(ringPath([], true)).toBe('')
    expect(segmentsPath([0, 0, 0, 10, 5, 5, 6, 6])).toBe('M0 0L0 -10M5 -5L6 -6')
    expect(
      polyline([
        [1, 2],
        [3, 4]
      ])
    ).toBe('1,-2 3,-4')
    expect(beyondEnd([1000, 0], [-1000, 0], 50)).toEqual([1050, 0])
    expect(beyondEnd([0, 0], [0, 0], 50)).toEqual([0, 0])
  })

  it('keeps the map the engine sends', () => {
    const store = reduce(initialStore, { type: 'message', message: { type: 'chart', ...chart } })
    expect(store.chart?.ident).toBe('EGXX')
    expect(store.log).toHaveLength(0)
  })
})
