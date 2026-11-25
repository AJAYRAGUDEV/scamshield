// charts.js - the risk gauge and the Chart.js radar chart.
//
// The radar chart is created once; initRadarChart() destroys any existing chart on the canvas first, so calling
// it twice can never leave two instances (and a leaked animation loop) behind.

import { el } from './dom.js';

const GAUGE_CIRCUMFERENCE = 2 * Math.PI * 66; // 414.69: the radius of the gauge arc in index.html
let radar = null;
const counterTokens = new WeakMap();

export function initRadarChart() {
  const canvas = el.signalRadarChart;
  if (!canvas || typeof Chart === 'undefined') return null; // Chart.js (CDN) did not load: the page still works

  const existing = Chart.getChart(canvas);
  if (existing) existing.destroy();

  radar = new Chart(canvas, {
    type: 'radar',
    data: {
      labels: ['Authority', 'Urgency', 'Payment Trap', 'Synthetic'],
      datasets: [
        {
          label: 'Threat Signal',
          data: [0, 0, 0, 0],
          backgroundColor: 'rgba(6, 182, 212, 0.22)',
          borderColor: '#06b6d4',
          borderWidth: 2,
          pointBackgroundColor: '#ffffff',
          pointBorderColor: '#06b6d4',
          pointBorderWidth: 2,
          pointRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 750, easing: 'easeOutQuart' },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(15, 23, 42, 0.95)',
          borderColor: 'rgba(148, 163, 184, 0.25)',
          borderWidth: 1,
          titleColor: '#f8fafc',
          bodyColor: '#cbd5e1',
          callbacks: { label: (context) => ` Score: ${context.raw} / 100` },
        },
      },
      scales: {
        r: {
          min: 0,
          max: 100,
          ticks: { stepSize: 25, color: 'rgba(148, 163, 184, 0.6)', backdropColor: 'transparent', font: { size: 10, family: 'JetBrains Mono' } },
          grid: { color: 'rgba(148, 163, 184, 0.14)' },
          angleLines: { color: 'rgba(148, 163, 184, 0.14)' },
          pointLabels: { color: '#cbd5e1', font: { size: 11, weight: '600', family: 'Inter' } },
        },
      },
    },
  });
  return radar;
}

/** values: four numbers 0-100, or nulls when nothing was assessed. */
export function updateRadar(values, meta) {
  if (!radar) return;
  const dataset = radar.data.datasets[0];
  dataset.data = values;
  dataset.borderColor = meta.chartColor;
  dataset.backgroundColor = meta.chartFill;
  dataset.pointBorderColor = meta.chartColor;
  radar.update();

  const described = values.every((value) => typeof value === 'number')
    ? `Threat vector radar chart. Authority ${values[0]}, urgency ${values[1]}, payment trap ${values[2]}, synthetic ${values[3]}, each out of 100.`
    : 'Threat vector radar chart. No scores: nothing was assessed.';
  el.signalRadarChart.setAttribute('aria-label', described);
}

export function destroyCharts() {
  if (radar) radar.destroy();
  radar = null;
}

export function chartCount() {
  return radar ? 1 : 0;
}

function animateCounter(element, target) {
  const start = parseInt(element.textContent, 10) || 0;
  const duration = 750;
  const startTime = performance.now();
  const token = (counterTokens.get(element) || 0) + 1;
  counterTokens.set(element, token);

  function tick(now) {
    if (counterTokens.get(element) !== token) return; // superseded (a newer score, or a missing score)
    // A frame timestamp can be slightly EARLIER than performance.now() at the start; without the lower clamp the counter
    // flashed a negative number for one frame.
    const progress = Math.max(0, Math.min(1, (now - startTime) / duration));
    const eased = 1 - Math.pow(1 - progress, 3);
    element.textContent = Math.round(start + (target - start) * eased);
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

/** score: a number 0-100, or null. A missing score is an empty gauge and a dash, never 0. */
export function setGauge(score) {
  const hasScore = typeof score === 'number';
  const offset = hasScore ? GAUGE_CIRCUMFERENCE * (1 - Math.max(0, Math.min(100, score)) / 100) : GAUGE_CIRCUMFERENCE;
  el.gaugeArc.style.strokeDashoffset = offset.toFixed(2);
  if (hasScore) {
    animateCounter(el.riskScoreValue, score);
  } else {
    counterTokens.set(el.riskScoreValue, (counterTokens.get(el.riskScoreValue) || 0) + 1);
    el.riskScoreValue.textContent = '—';
  }
}
