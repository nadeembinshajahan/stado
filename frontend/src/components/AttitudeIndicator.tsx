interface Props {
  roll: number | null; // radians
  pitch: number | null; // radians
  size?: number;
}

/** Compact artificial horizon. Sky/ground tilt with roll, slide with pitch. */
export default function AttitudeIndicator({ roll, pitch, size = 150 }: Props) {
  const r = roll ?? 0;
  const p = pitch ?? 0;
  const rollDeg = (r * 180) / Math.PI;
  const pitchPx = Math.max(-60, Math.min(60, (p * 180) / Math.PI)) * 1.6;
  const cx = size / 2;
  const cy = size / 2;

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <defs>
        <clipPath id="adi-clip">
          <circle cx={cx} cy={cy} r={cx - 4} />
        </clipPath>
        <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#2b6cff" />
          <stop offset="100%" stopColor="#0d3aa0" />
        </linearGradient>
        <linearGradient id="ground" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#7a4a1f" />
          <stop offset="100%" stopColor="#3a2410" />
        </linearGradient>
      </defs>

      <g clipPath="url(#adi-clip)">
        <g transform={`rotate(${-rollDeg} ${cx} ${cy})`}>
          <g transform={`translate(0 ${pitchPx})`}>
            <rect x={-size} y={-size} width={size * 3} height={size * 2} fill="url(#sky)" />
            <rect x={-size} y={cy} width={size * 3} height={size * 2} fill="url(#ground)" />
            <line x1={-size} y1={cy} x2={size * 2} y2={cy} stroke="#e7edf5" strokeWidth={2} />
            {[-40, -20, 20, 40].map((d) => {
              const y = cy + d * 1.6;
              const w = Math.abs(d) === 20 ? 22 : 14;
              return (
                <g key={d}>
                  <line x1={cx - w} y1={y} x2={cx + w} y2={y} stroke="#cfe0f5" strokeWidth={1.4} />
                  <text x={cx + w + 4} y={y + 3} fill="#cfe0f5" fontSize={8} className="tnum">
                    {Math.abs(d)}
                  </text>
                </g>
              );
            })}
          </g>
        </g>
      </g>

      {/* fixed aircraft reference */}
      <g stroke="#22e3c4" strokeWidth={3} fill="none">
        <line x1={cx - 30} y1={cy} x2={cx - 10} y2={cy} />
        <line x1={cx + 10} y1={cy} x2={cx + 30} y2={cy} />
        <circle cx={cx} cy={cy} r={2.5} fill="#22e3c4" />
      </g>

      {/* roll pointer */}
      <g transform={`rotate(${-rollDeg} ${cx} ${cy})`}>
        <polygon
          points={`${cx},6 ${cx - 6},16 ${cx + 6},16`}
          fill="#22e3c4"
        />
      </g>
      <circle cx={cx} cy={cy} r={cx - 4} fill="none" stroke="rgba(120,150,190,0.4)" strokeWidth={2} />
    </svg>
  );
}
