// Trackpad and gyro maths for the optional controller module (not active: see ../README.md). Pure functions over
// normalized controller state (web/controller-state.mjs): pads [{touch, x, y, click, pressure}] with x/y in 0..1,
// gyro [x, y, z] in rad/s (x pitch, y yaw, z roll). These are proposals of behaviour, not the user's decisions.

const finite = v => typeof v === 'number' && Number.isFinite(v);

// A pad as a direction: the region of the touch point outside a centre dead zone, by the larger offset (no
// diagonals). mode 'click' (default) needs the pad pressed; 'touch' needs only a touch.
export function padDirection(pad, {mode = 'click', deadzone = 0.25} = {}) {
  if (!pad || !pad.touch || (mode === 'click' && !pad.click) || !finite(pad.x) || !finite(pad.y)) return null;
  const dx = pad.x - 0.5, dy = pad.y - 0.5;
  if (Math.hypot(dx, dy) < deadzone / 2) return null;
  return Math.abs(dx) >= Math.abs(dy) ? (dx < 0 ? 'left' : 'right') : (dy < 0 ? 'up' : 'down');
}

// Pad scrolling: the vertical movement of a held touch, in pad heights. The first touch only sets the origin (no
// jump), lifting resets it, and one step is bounded.
export class PadScroll {
  constructor({limit = 0.25} = {}) { this.limit = limit; this.last = null; }
  step(pad) {
    if (!pad || !pad.touch || !finite(pad.y)) { this.last = null; return 0; }
    const previous = this.last; this.last = pad.y;
    if (previous === null) return 0;
    return Math.max(-this.limit, Math.min(this.limit, pad.y - previous));
  }
}

// Calibration: the gyro's resting offset, from samples taken while the controller is held still. Refused (null) when
// there are too few samples, any is invalid, or it moved (spread over `still` rad/s).
export function calibrate(samples, {minimum = 30, still = 0.05} = {}) {
  if (!Array.isArray(samples) || samples.length < minimum) return null;
  if (!samples.every(s => Array.isArray(s) && s.length === 3 && s.every(finite))) return null;
  const offset = [0, 1, 2].map(i => samples.reduce((sum, s) => sum + s[i], 0) / samples.length);
  if (samples.some(s => s.some((v, i) => Math.abs(v - offset[i]) > still))) return null;
  return offset;
}

// A gyro pointer: movement in page pixels while active. Activation: 'pad' (right pad touched), 'button' (a held
// button), or 'always'. On every activation start it recentres (the first active step moves nothing), so drift or a
// held pose never causes a jump; below the dead band nothing moves; a stalled frame counts as at most 50 ms (no
// catch-up jump).
export class GyroPointer {
  constructor({activation = 'pad', button = 'r1', pad = 1, sensitivity = 600, deadBand = 0.0087, offset = [0, 0, 0]} = {}) {
    Object.assign(this, {activation, button, pad, sensitivity, deadBand, offset: [...offset]});
    this.active = false; this.last = null;
  }
  activated(state) {
    if (this.activation === 'always') return true;
    if (this.activation === 'button') return state.buttons.has(this.button);
    return Boolean(state.pads?.[this.pad]?.touch);
  }
  step(state, now) {
    const gyro = state?.gyro;
    if (!state || !gyro || !this.activated(state) || !gyro.every(finite)) { this.active = false; this.last = null; return null; }
    if (!this.active || this.last === null) { this.active = true; this.last = now; return {dx: 0, dy: 0}; }
    const dt = Math.min(0.05, Math.max(0, (now - this.last) / 1000)); this.last = now;
    const rate = i => { const v = gyro[i] - this.offset[i]; return Math.abs(v) < this.deadBand ? 0 : v; };
    return {dx: -rate(1) * this.sensitivity * dt, dy: -rate(0) * this.sensitivity * dt};
  }
}
