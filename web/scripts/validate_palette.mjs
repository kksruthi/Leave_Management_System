/* Palette validator — the theme's claims, checked.
   ==========================================================================

   The stylesheet says the palette is safe for colour-vision deficiency, has
   enough contrast, and keeps the brand orange distinct from the Casual Leave
   orange. Those are testable claims, so this tests them rather than asserting
   them in a comment.

   Run: node scripts/validate_palette.mjs

   ## What is checked

   1. **Text contrast** — any colour used for text must clear WCAG AA (4.5:1)
      against the surface it sits on.
   2. **Non-text contrast** — a chart series must clear 3:1 against its
      surface to carry meaning by hue alone. A series that fails is not an
      error; it is flagged as REQUIRING the *relief rule* (a label, pattern
      or border alongside the colour). The team calendar labels every bar, so
      it satisfies this — but the validator names which series depend on it,
      so removing a label later is a visible regression.
   3. **CVD separation** — each pair of categorical series is simulated for
      protanopia, deuteranopia and tritanopia, and must stay ≥ 12 ΔE
      (CIE76 in Lab) apart under every simulation. Twelve is chosen because
      below about 10 the two read as "the same colour, slightly off" at the
      12px sizes these appear at in the calendar.
   4. **Brand vs data separation** — the accent orange and the CL orange must
      stay ≥ 12 ΔE apart. They are near neighbours by design; the check is
      what stops a future tweak merging them.

   No dependencies. The colour maths is a few dozen lines and importing a
   library for it would be a heavier commitment than writing it.
*/

// --- colour maths ----------------------------------------------------------
const hex = (h) => {
  const s = h.replace('#', '')
  const n = s.length === 3 ? s.split('').map(c => c + c).join('') : s
  return [0, 2, 4].map(i => parseInt(n.slice(i, i + 2), 16))
}

const srgbToLinear = (c) => {
  const v = c / 255
  return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)
}

const relLuminance = (rgb) => {
  const [r, g, b] = rgb.map(srgbToLinear)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

const contrast = (a, b) => {
  const [l1, l2] = [relLuminance(hex(a)), relLuminance(hex(b))].sort((x, y) => y - x)
  return (l1 + 0.05) / (l2 + 0.05)
}

const toLab = (rgbHex) => {
  const [r, g, b] = hex(rgbHex).map(srgbToLinear)
  // sRGB -> XYZ (D65)
  const x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
  const y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 1.0
  const z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
  const f = (t) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116)
  const [fx, fy, fz] = [f(x), f(y), f(z)]
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)]
}

const deltaE = (a, b) => {
  const [l1, a1, b1] = toLab(a)
  const [l2, a2, b2] = toLab(b)
  return Math.hypot(l1 - l2, a1 - a2, b1 - b2)
}

/* Brettel/Viénot-style LMS simulation, matrix form. Approximate but standard;
   good enough to catch "these two are indistinguishable", which is the only
   question being asked. */
const CVD = {
  protanopia: [[0.567, 0.433, 0], [0.558, 0.442, 0], [0, 0.242, 0.758]],
  deuteranopia: [[0.625, 0.375, 0], [0.7, 0.3, 0], [0, 0.3, 0.7]],
  tritanopia: [[0.95, 0.05, 0], [0, 0.433, 0.567], [0, 0.475, 0.525]],
}

const simulate = (h, kind) => {
  const m = CVD[kind]
  const [r, g, b] = hex(h)
  const out = m.map(row => Math.round(
    Math.max(0, Math.min(255, row[0] * r + row[1] * g + row[2] * b))
  ))
  return '#' + out.map(v => v.toString(16).padStart(2, '0')).join('')
}

// --- the palette under test ------------------------------------------------
const LIGHT = {
  surface: '#ffffff',
  page: '#f6f4f1',
  text: { ink: '#0e1a2b', ink2: '#43536b', muted: '#5f6f87', accent: '#c2521c',
          good: '#0a7d43', warning: '#8a5b00', critical: '#c0342f' },
  series: { EL: '#1a5aa0', CL: '#9c3a14', SL: '#0d6b53', Unpaid: '#5b2f9e',
            'series-5': '#8a2f6c' },
  onFill: '#ffffff',
  accentSolid: '#c2521c',
  accentBright: '#e2652b',
  navy: '#071426',
}

const DARK = {
  surface: '#10161d',
  page: '#0a0e13',
  text: { ink: '#f2f6fb', ink2: '#b3c2d6', muted: '#93a6bd', accent: '#ff8b4d',
          good: '#35c47a', warning: '#e8b055', critical: '#e5605a' },
  series: { EL: '#4f9ce8', CL: '#e8703e', SL: '#2ab392', Unpaid: '#9186e8',
            'series-5': '#d072ac' },
  onFill: '#0b1420',
  accentSolid: '#c2521c',
  accentBright: '#ff8b4d',
  navy: '#070f1b',
}

const AA_TEXT = 4.5
const NON_TEXT = 3.0
const MIN_DE = 12

let failures = 0
let reliefRequired = []

function check(ok, label, detail) {
  const mark = ok ? 'PASS' : 'FAIL'
  if (!ok) failures++
  console.log(`  ${mark}  ${label}${detail ? '  ' + detail : ''}`)
}

function audit(name, P) {
  console.log(`\n${name}`)
  console.log('  --- text contrast (AA 4.5:1 on the card surface) ---')
  for (const [key, colour] of Object.entries(P.text)) {
    const ratio = contrast(colour, P.surface)
    check(ratio >= AA_TEXT, `text/${key}`, `${ratio.toFixed(2)}:1`)
  }

  /* Only fills that CARRY WHITE TEXT are held to AA. `accentBright` is
     decorative — the nav rail, a badge dot, a 3px card edge — and never has
     text on it, so holding it to a text ratio would be measuring the wrong
     thing. Any fill added to this object is a promise that white text sits
     on it. */
  /* Dark mode flips the ink on a fill. The light series are dark colours and
     take white text; the dark series are light colours and take near-black.
     Checking both against white would fail the dark palette for a rule it
     does not actually break. */
  console.log(`  --- ${P.onFill} text on solid fills (buttons, calendar bars) ---`)
  for (const [key, colour] of Object.entries(P.series)) {
    const ratio = contrast(P.onFill, colour)
    check(ratio >= AA_TEXT, `${P.onFill} on ${key}`, `${ratio.toFixed(2)}:1`)
  }
  // The primary button keeps WHITE text in both themes: the accent fill is
  // the same colour either way, so flipping its ink with the theme would make
  // the one button that matters most read differently for no reason.
  check(contrast('#ffffff', P.accentSolid) >= AA_TEXT, '#fff on accent button',
        `${contrast('#ffffff', P.accentSolid).toFixed(2)}:1`)
  check(contrast('#ffffff', P.navy) >= AA_TEXT, '#fff on navy sidebar',
        `${contrast('#ffffff', P.navy).toFixed(2)}:1`)

  console.log('  --- series vs surface (3:1, or the relief rule applies) ---')
  for (const [key, colour] of Object.entries(P.series)) {
    const ratio = contrast(colour, P.surface)
    if (ratio >= NON_TEXT) {
      console.log(`  PASS  series/${key}  ${ratio.toFixed(2)}:1`)
    } else {
      reliefRequired.push(`${name}/${key}`)
      console.log(`  NOTE  series/${key}  ${ratio.toFixed(2)}:1 — relief rule required`)
    }
  }

  console.log('  --- CVD separation between series (>= 12 ΔE) ---')
  const keys = Object.keys(P.series)
  for (let i = 0; i < keys.length; i++) {
    for (let j = i + 1; j < keys.length; j++) {
      for (const kind of Object.keys(CVD)) {
        const d = deltaE(simulate(P.series[keys[i]], kind), simulate(P.series[keys[j]], kind))
        check(d >= MIN_DE, `${keys[i]} vs ${keys[j]} (${kind})`, `ΔE ${d.toFixed(1)}`)
      }
    }
  }

  console.log('  --- brand orange vs Casual Leave orange (>= 12 ΔE) ---')
  check(deltaE(P.accentSolid, P.series.CL) >= MIN_DE, 'accent vs CL (normal vision)',
        `ΔE ${deltaE(P.accentSolid, P.series.CL).toFixed(1)}`)
  for (const kind of Object.keys(CVD)) {
    const d = deltaE(simulate(P.accentSolid, kind), simulate(P.series.CL, kind))
    check(d >= MIN_DE, `accent vs CL (${kind})`, `ΔE ${d.toFixed(1)}`)
  }
}

audit('LIGHT', LIGHT)
audit('DARK', DARK)

if (reliefRequired.length) {
  console.log('\nRelief rule REQUIRED for: ' + reliefRequired.join(', '))
  console.log('These must carry a visible label, pattern or border — never hue alone.')
}

console.log(failures ? `\n${failures} failure(s).` : '\nAll checks passed.')
process.exit(failures ? 1 : 0)
