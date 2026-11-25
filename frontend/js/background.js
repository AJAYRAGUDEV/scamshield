// background.js - the interactive neural-grid background canvas (decorative, aria-hidden).
//
// Reads state.isScanning (faster while scanning) and the theme colour state.bgThemeRgb, which it fades towards
// state.targetThemeRgb. requestAnimationFrame pauses by itself in a hidden tab. initBackgroundCanvas() is safe to
// call once; it returns a function that stops the animation and removes its listeners.

import { el } from './dom.js';
import { state } from './state.js';

let started = false;

export function initBackgroundCanvas() {
  const canvas = el.bgCanvas;
  const ctx = canvas && canvas.getContext('2d');
  if (!ctx || started) return () => {};
  started = true;

  let width = 0;
  let height = 0;
  let particles = [];
  const mouse = { x: -9999, y: -9999 };
  let sweepAngle = 0;
  let frame = 0;
  const listeners = new AbortController();

  function resize() {
    width = canvas.width = window.innerWidth;
    height = canvas.height = window.innerHeight;
    const count = Math.min(72, Math.max(28, Math.floor((width * height) / 22000)));
    particles = Array.from({ length: count }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.45,
      vy: (Math.random() - 0.5) * 0.45,
      r: Math.random() * 1.8 + 0.9,
    }));
  }

  window.addEventListener('resize', resize, { signal: listeners.signal });
  window.addEventListener('mousemove', (event) => {
    mouse.x = event.clientX;
    mouse.y = event.clientY;
  }, { signal: listeners.signal });
  window.addEventListener('mouseleave', () => {
    mouse.x = -9999;
    mouse.y = -9999;
  }, { signal: listeners.signal });

  resize();

  function draw() {
    // Smoothly interpolate theme RGB
    for (let i = 0; i < 3; i++) {
      state.bgThemeRgb[i] += (state.targetThemeRgb[i] - state.bgThemeRgb[i]) * 0.04;
    }
    const [r, g, b] = state.bgThemeRgb.map((value) => Math.round(value));

    ctx.clearRect(0, 0, width, height);

    // Subtle radar rings near top-right
    const cx = width * 0.78;
    const cy = height * 0.28;
    sweepAngle += state.isScanning ? 0.035 : 0.008;
    ctx.save();
    ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, 0.06)`;
    ctx.lineWidth = 1;
    [110, 210, 320].forEach((radius) => {
      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.stroke();
    });
    // Sweep beam
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, 320, sweepAngle, sweepAngle + 0.35);
    ctx.closePath();
    const gradient = ctx.createRadialGradient(cx, cy, 0, cx, cy, 320);
    gradient.addColorStop(0, `rgba(${r}, ${g}, ${b}, 0.12)`);
    gradient.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0)`);
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.restore();

    // Update & draw nodes
    const speedBoost = state.isScanning ? 2.4 : 1;
    for (let i = 0; i < particles.length; i++) {
      const p = particles[i];
      p.x += p.vx * speedBoost;
      p.y += p.vy * speedBoost;
      if (p.x < 0 || p.x > width) p.vx *= -1;
      if (p.y < 0 || p.y > height) p.vy *= -1;

      // Connect nearby nodes
      for (let j = i + 1; j < particles.length; j++) {
        const q = particles[j];
        const dist = Math.hypot(p.x - q.x, p.y - q.y);
        if (dist < 140) {
          ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${(1 - dist / 140) * 0.18})`;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(p.x, p.y);
          ctx.lineTo(q.x, q.y);
          ctx.stroke();
        }
      }

      // Connect to pointer
      const pointerDist = Math.hypot(p.x - mouse.x, p.y - mouse.y);
      if (pointerDist < 170) {
        ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${(1 - pointerDist / 170) * 0.35})`;
        ctx.lineWidth = 1.1;
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(mouse.x, mouse.y);
        ctx.stroke();
      }

      ctx.fillStyle = `rgba(${r}, ${g}, ${b}, 0.65)`;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }

    frame = requestAnimationFrame(draw);
  }

  frame = requestAnimationFrame(draw);

  return function stopBackground() {
    cancelAnimationFrame(frame);
    listeners.abort();
    started = false;
  };
}
