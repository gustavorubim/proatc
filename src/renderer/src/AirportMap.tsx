import { type PointerEvent, type WheelEvent, useEffect, useMemo, useRef, useState } from 'react'
import type { Chart, TaxiView } from '../../shared/protocol'
import {
  type Box,
  type XY,
  beyondEnd,
  centred,
  fitBox,
  fitPoints,
  polyline,
  ringPath,
  screen,
  segmentsPath,
  zoomBox
} from './map'

type Mode = 'fit' | 'follow' | 'free'

const FOLLOW_SPAN_M = 700

/** The airport from above, north up, with the taxi route the controller gave drawn on it. */
export function AirportMap({ chart, view }: { chart: Chart; view: TaxiView }) {
  const frame = useRef<HTMLDivElement | null>(null)
  const [size, setSize] = useState({ w: 800, h: 320 })
  const [mode, setMode] = useState<Mode>('fit')
  const [span, setSpan] = useState(FOLLOW_SPAN_M)
  const [free, setFree] = useState<Box | null>(null)
  const drag = useRef<{ x: number; y: number; box: Box } | null>(null)

  useEffect(() => {
    const element = frame.current
    if (!element) return
    const measure = (): void => setSize({ w: Math.max(element.clientWidth, 50), h: Math.max(element.clientHeight, 50) })
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  // A new airport or a new route: back to showing all of it.
  const routeKey = view.route ? `${view.route.goal.label}` : ''
  useEffect(() => {
    setMode((current) => (current === 'free' ? 'fit' : current))
  }, [chart.ident, routeKey])

  const aspect = size.w / size.h
  const box: Box =
    mode === 'free' && free
      ? free
      : mode === 'follow'
        ? centred(screen(view.position), span, aspect)
        : fitBox(fitPoints(chart, view), aspect)
  const unit = box.w / size.w // metres in one pixel

  const drawn = useMemo(
    () => ({
      pavement: chart.pavement.map((rings) => rings.map((ring) => ringPath(ring, true)).join('')).join(''),
      taxiways: chart.taxiways.map((taxiway) => segmentsPath(taxiway.lines)).join('')
    }),
    [chart]
  )

  const zoom = (factor: number, about?: XY): void => {
    if (mode === 'follow' && !about) {
      setSpan((current) => Math.min(Math.max(current * factor, 150), 30000))
      return
    }
    setFree(zoomBox(box, factor, about ?? [box.x + box.w / 2, box.y + box.h / 2]))
    setMode('free')
  }
  const wheel = (event: WheelEvent<SVGSVGElement>): void => {
    const rect = event.currentTarget.getBoundingClientRect()
    const about: XY = [box.x + (event.clientX - rect.left) * unit, box.y + (event.clientY - rect.top) * unit]
    zoom(event.deltaY > 0 ? 1.25 : 0.8, about)
  }
  const down = (event: PointerEvent<SVGSVGElement>): void => {
    event.currentTarget.setPointerCapture(event.pointerId)
    drag.current = { x: event.clientX, y: event.clientY, box }
  }
  const move = (event: PointerEvent<SVGSVGElement>): void => {
    const start = drag.current
    if (!start) return
    const dx = event.clientX - start.x
    const dy = event.clientY - start.y
    if (mode !== 'free' && Math.hypot(dx, dy) < 4) return // a click is not a drag
    setFree({ ...start.box, x: start.box.x - dx * unit, y: start.box.y - dy * unit })
    setMode('free')
  }
  const up = (): void => {
    drag.current = null
  }

  const text = { fontSize: 11 * unit, strokeWidth: 3 * unit }
  const small = { fontSize: 9.5 * unit, strokeWidth: 2.5 * unit }
  const detail = 1 / unit // pixels in one metre
  const [px, py] = screen(view.position)
  const route = view.route

  return (
    <div className="map" ref={frame}>
      <svg
        viewBox={`${box.x} ${box.y} ${box.w} ${box.h}`}
        preserveAspectRatio="xMidYMid meet"
        onWheel={wheel}
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={up}
      >
        <path className="map-pavement" d={drawn.pavement} fillRule="evenodd" />
        <path className="map-taxiway" d={drawn.taxiways} />
        {chart.runways.map((runway) => {
          const [a, b] = runway.ends.map((end) => screen([end[1], end[2]]))
          return (
            <g key={runway.name}>
              <line
                className="map-runway-edge"
                x1={a[0]}
                y1={a[1]}
                x2={b[0]}
                y2={b[1]}
                strokeWidth={Math.max(runway.width, 5 * unit) + 2 * unit}
              />
              <line
                className="map-runway"
                x1={a[0]}
                y1={a[1]}
                x2={b[0]}
                y2={b[1]}
                strokeWidth={Math.max(runway.width, 5 * unit)}
              />
            </g>
          )
        })}
        {detail > 0.15 &&
          chart.stands.map((stand, index) => {
            const [x, y] = screen(stand.at)
            return <circle key={index} className="map-stand" cx={x} cy={y} r={2 * unit} />
          })}
        {detail > 0.9 &&
          chart.stands.map((stand, index) => {
            const [x, y] = screen(stand.at)
            return stand.name.length <= 8 ? (
              <text key={index} className="map-label map-stand-name" x={x} y={y - 5 * unit} style={small}>
                {stand.name}
              </text>
            ) : null
          })}
        {detail > 0.1 &&
          chart.labels.map((label, index) => {
            const [x, y] = screen(label.at)
            return (
              <text key={index} className="map-label map-taxiway-name" x={x} y={y + 3.5 * unit} style={small}>
                {label.name}
              </text>
            )
          })}
        {chart.runways.map((runway) =>
          runway.ends.map((end, index) => {
            const here = screen([end[1], end[2]])
            const other = runway.ends[1 - index]
            const [x, y] = beyondEnd(here, screen([other[1], other[2]]), 14 * unit)
            return (
              <text
                key={`${runway.name}${end[0]}`}
                className="map-label map-runway-name"
                x={x}
                y={y + 4 * unit}
                style={text}
              >
                {end[0]}
              </text>
            )
          })
        )}

        {route && route.beyond.length > 1 && (
          <polyline
            className="map-route-beyond"
            points={polyline(route.beyond)}
            strokeDasharray={`${6 * unit} ${6 * unit}`}
          />
        )}
        {route && route.cleared.length > 1 && (
          <>
            <polyline className="map-route-glow" points={polyline(route.cleared)} />
            <polyline className="map-route" points={polyline(route.cleared)} />
          </>
        )}
        {route && (
          <g>
            <circle
              className="map-goal"
              cx={route.goal.at[0]}
              cy={-route.goal.at[1]}
              r={7 * unit}
              strokeWidth={2.5 * unit}
            />
            <text
              className="map-label map-goal-name"
              x={route.goal.at[0]}
              y={-route.goal.at[1] - 12 * unit}
              style={text}
            >
              {route.goal.label}
            </text>
          </g>
        )}
        {route?.hold && (
          <g>
            <circle className="map-hold" cx={route.hold.at[0]} cy={-route.hold.at[1]} r={6 * unit} />
            <text
              className="map-label map-hold-name"
              x={route.hold.at[0]}
              y={-route.hold.at[1] + 19 * unit}
              style={text}
            >
              HOLD SHORT {route.hold.runway}
            </text>
          </g>
        )}
        <polygon
          className="map-aircraft"
          points="0,-11 7,9 0,5 -7,9"
          strokeWidth={1.5}
          transform={`translate(${px} ${py}) rotate(${view.heading}) scale(${unit})`}
        />
      </svg>
      <div className="map-title">
        <span className="mono">{chart.ident}</span> {chart.name}
      </div>
      <div className="map-tools">
        <button className={mode === 'fit' ? 'on' : ''} title="Show the whole taxi route" onClick={() => setMode('fit')}>
          {route ? 'Route' : 'Airport'}
        </button>
        <button
          className={mode === 'follow' ? 'on' : ''}
          title="Keep the aircraft in the middle"
          onClick={() => setMode('follow')}
        >
          Follow
        </button>
        <button title="Closer" onClick={() => zoom(0.7)}>
          +
        </button>
        <button title="Further" onClick={() => zoom(1.4)}>
          −
        </button>
      </div>
      {chart.taxiways.length === 0 && <div className="map-note">This airport's scenery has no taxiway names.</div>}
    </div>
  )
}
