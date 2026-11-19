// Controller module (cinema.controller, 2026-11-14): policy over Cinema's controller input. Core (web/controller-host.mjs)
// owns arbitration, navigation and every effect; this module chooses what host input to ask for, a button preset, and
// what the trackpads and gyro do, expressed only through the bounded page actions it is given (keys Cinema already has,
// scrolling, a page-only pointer). It never sends OS input. Settings live in this module's own storage.
//
// Settings in Cinema's Settings (0.2.0, 2026-11-15): the button preset, both pads, the gyro activation, its button and its
// sensitivity are DECLARED preferences (manifest "preferences"), each one top-level key of this module's storage. That key
// is the ONE source: Cinema's Settings (Extensions › Controller) and this module's own panel both read and write it, and
// a change saved in Settings reaches this running module at once (cinema.storage.onChange). The Steam Controller choice
// and its explicit "read directly anyway" consent stay in this panel only (nested under "settings"); calibration offsets
// stay under "offsets". Settings stored by 0.1.0 (all nested under "settings") are moved once, value for value.
//
// The view (0.3.0, the approved layout): a summary (the reported device and input state, then Buttons, Trackpads, Motion
// pointer and Advanced with their current values) and one screen per area holding the same controls as before. Back on a
// screen returns to the summary (page.view onBack); nothing about input, storage or behaviour changed.
import {padDirection, PadScroll, calibrate, GyroPointer} from './pads.mjs';

export const CHOICES = Object.freeze({
  steam: ['system', 'raw'],                 // Steam Controllers: left as Windows/Steam see them, or read by Cinema
  left: ['off', 'click', 'touch'],          // left pad as directions (pressed, or touched)
  right: ['off', 'scroll', 'pointer'],      // right pad scrolls, or moves the page pointer
  gyro: ['off', 'pad', 'button', 'always'], // gyro pointer activation
  gyroButton: ['r1', 'l1', 'r4', 'l4'],
  sensitivity: [0.5, 1, 1.5, 2, 3],
  preset: ['standard', 'swap', 'nintendo'],
});
export const DEFAULTS = Object.freeze({steam: 'system', override: false, left: 'click', right: 'scroll', gyro: 'off', gyroButton: 'r1',
  sensitivity: 1, preset: 'standard'});
// Button presets (core refuses any table that would leave no Enter or no Back).
export const PRESETS = Object.freeze({standard: null, swap: Object.freeze({south: 'east', east: 'south'}),
  nintendo: Object.freeze({south: 'east', east: 'south', west: 'north', north: 'west'})});
const LABELS = {
  steam: {system: 'Leave to Windows / Steam', raw: 'Read directly in Cinema'},
  left: {off: 'Off', click: 'Directions (press)', touch: 'Directions (touch)'},
  right: {off: 'Off', scroll: 'Scroll', pointer: 'Pointer'},
  gyro: {off: 'Off', pad: 'While right pad touched', button: 'While a button is held', always: 'Always'},
  gyroButton: {r1: 'R1', l1: 'L1', r4: 'Right grip', l4: 'Left grip'},
  preset: {standard: 'Standard', swap: 'Swap Enter and Back', nintendo: 'Nintendo layout'},
};
const FAMILY = {'steam-sc1': 'Steam Controller', 'steam-2026': 'Steam Controller (2026)', 'steam-deck': 'Steam Deck', xbox: 'Xbox', playstation: 'PlayStation', generic: 'Controller'};

// Declared preferences: settings key -> [storage key, to stored value, from stored value]. Sensitivity is stored as an
// integer percent (a preference choice is an integer or a word).
export const PREFERENCES = Object.freeze({
  preset: ['buttons', v => v, v => v], left: ['left_pad', v => v, v => v], right: ['right_pad', v => v, v => v],
  gyro: ['gyro', v => v, v => v], gyroButton: ['gyro_button', v => v, v => v],
  sensitivity: ['gyro_sensitivity', v => Math.round(v * 100), v => (Number.isInteger(v) ? v / 100 : undefined)],
});
// The whole storage -> {settings, migrate}: a declared key that is present decides (an invalid value means its default,
// exactly as Settings shows it); only an ABSENT key falls back to 0.1.0's nested value, and is then listed in `migrate`.
export function resolve(values) {
  const v = values && typeof values === 'object' ? values : {}, nested = v.settings && typeof v.settings === 'object' ? v.settings : {};
  const s = {...DEFAULTS}, migrate = {};
  for (const [key, [storeKey, , from]] of Object.entries(PREFERENCES)) {
    if (Object.hasOwn(v, storeKey)) { const value = from(v[storeKey]); if (CHOICES[key].includes(value)) s[key] = value; }
    else if (CHOICES[key].includes(nested[key])) { s[key] = nested[key]; migrate[key] = nested[key]; }
  }
  if (CHOICES.steam.includes(nested.steam)) s.steam = nested.steam;
  s.override = nested.override === true && s.steam === 'raw';
  const legacy = Object.keys(PREFERENCES).some(key => Object.hasOwn(nested, key));
  return {settings: Object.freeze(s), migrate, legacy};
}
// Stored settings, checked: anything unknown falls back to its default.
export function settingsFrom(stored) {
  const s = {...DEFAULTS};
  if (stored && typeof stored === 'object') {
    for (const key of Object.keys(CHOICES)) if (CHOICES[key].includes(stored[key])) s[key] = stored[key];
    s.override = stored.override === true;
  }
  return Object.freeze(s);
}
// What to ask the host for. Raw over a mapper only after the user said so (override).
export function hostRequest(settings, {calibrating = false} = {}) {
  return {on: true, steam: settings.steam !== 'raw' ? 'system' : (settings.override ? 'raw-despite-mapper' : 'raw'),
    sensors: settings.gyro !== 'off' || calibrating};
}
export function offsetsFrom(stored) {
  const out = {};
  if (stored && typeof stored === 'object')
    for (const [family, v] of Object.entries(stored)) if (FAMILY[family] && Array.isArray(v) && v.length === 3 && v.every(Number.isFinite) && v.every(x => Math.abs(x) < 1)) out[family] = [...v];
  return out;
}
const ARROW = {up: 'ArrowUp', down: 'ArrowDown', left: 'ArrowLeft', right: 'ArrowRight'};

// Pads and gyro -> page actions, one frame at a time. `controller` is the capability handle (key, scroll, pointer).
export class Behaviour {
  constructor(controller, settings, offsets = {}, family = () => null) {
    Object.assign(this, {controller, settings, offsets, family});
    this.scroller = new PadScroll(); this.direction = null; this.repeatAt = 0; this.lastTouch = null; this.clickWas = false;
    this.gyro = null; this.gyroFamily = null; this.pointerAt = 0; this.calibration = null;
  }
  reset() {
    this.scroller = new PadScroll(); this.direction = null; this.lastTouch = null; this.clickWas = false;
    if (this.gyro) this.gyro.active = false;
    if (this.pointerAt) { this.controller.pointer.hide(); this.pointerAt = 0; }
  }
  gyroFor(state) {
    const family = this.family(state);
    if (!this.gyro || this.gyroFamily !== family) {
      this.gyroFamily = family;
      this.gyro = new GyroPointer({activation: this.settings.gyro === 'off' ? 'always' : this.settings.gyro, button: this.settings.gyroButton, pad: 1,
        sensitivity: 600 * this.settings.sensitivity, offset: this.offsets[family] || [0, 0, 0]});
    }
    return this.gyro;
  }
  moved(now) { this.pointerAt = now; }
  frame(state, now) {
    if (this.calibration) {
      if (state?.gyro) this.calibration.samples.push([...state.gyro]);
      if (now >= this.calibration.until) { const done = this.calibration; this.calibration = null; done.finish(calibrate(done.samples), this.family(state)); }
      return;
    }
    if (!state) { this.reset(); return; }
    const [left, right] = state.pads.length === 2 ? state.pads : [null, state.pads[0] || null];
    // Left pad: directions (one key per new direction, then repeating while held, as core navigation does).
    if (this.settings.left !== 'off' && left) {
      const dir = padDirection(left, {mode: this.settings.left});
      if (dir !== this.direction) { this.direction = dir; this.repeatAt = now + 400; if (dir) this.controller.key(ARROW[dir]); }
      else if (dir && now >= this.repeatAt) { this.repeatAt = now + 140; this.controller.key(ARROW[dir]); }
    }
    // Right pad: scroll, or the page pointer (relative movement of the touch; a click presses what is under it).
    if (right && this.settings.right === 'scroll') {
      const dy = this.scroller.step(right);
      if (dy) this.controller.scroll(dy * 900);
    } else if (right && this.settings.right === 'pointer') {
      const dx = right.touch && this.lastTouch ? (right.x - this.lastTouch.x) * 1400 : 0, dy = right.touch && this.lastTouch ? (right.y - this.lastTouch.y) * 900 : 0;
      if (dx || dy) { this.controller.pointer.move(dx, dy); this.moved(now); }   // a still touch does not show the pointer
      this.lastTouch = right.touch ? {x: right.x, y: right.y} : null;
    }
    // Gyro: the page pointer while active (activation, dead band, recentring and calibration offset per family).
    if (this.settings.gyro !== 'off' && state.gyro) {
      const step = this.gyroFor(state).step(state, now);
      if (step && (step.dx || step.dy)) { this.controller.pointer.move(step.dx, step.dy); this.moved(now); }
    }
    const click = Boolean(right?.click);
    if (click && !this.clickWas && this.pointerAt) this.controller.pointer.press();
    this.clickWas = click;
    if (this.pointerAt && now - this.pointerAt > 3000) { this.controller.pointer.hide(); this.pointerAt = 0; }
  }
}

// --- What the view says (0.3.0), from the settings and what the host reports: pure, so tests read the same text ----------
// The face buttons by position, never by a printed letter: the same words are true on every family.
export const ROLES = Object.freeze({standard: 'bottom button selects, right button goes back',
  swap: 'right button selects, bottom button goes back', nintendo: 'right button selects, bottom button goes back'});
const TRANSPORT = {usb: 'USB', dongle: 'wireless dongle', bluetooth: 'Bluetooth', unknown: 'connection unknown'};
const LEFT = {off: 'Left pad off', click: 'Left pad: directions when pressed', touch: 'Left pad: directions when touched'};
const RIGHT = {off: 'right pad off', scroll: 'right pad scrolls', pointer: 'right pad moves a pointer'};
export function optionLabel(key, value) { return LABELS[key]?.[value] ?? (key === 'sensitivity' ? `${value}×` : String(value)); }
export function summaries(settings, {asking = false} = {}) {
  const s = settings;
  return {
    buttons: `${LABELS.preset[s.preset]} · ${ROLES[s.preset]}`,
    trackpads: `${LEFT[s.left]} · ${RIGHT[s.right]}`,
    motion: s.gyro === 'off' ? 'Off' : `${LABELS.gyro[s.gyro]}${s.gyro === 'button' ? ` (${LABELS.gyroButton[s.gyroButton]})` : ''} · ${optionLabel('sensitivity', s.sensitivity)}`,
    advanced: `Steam Controllers: ${s.steam === 'raw' ? 'read directly by Cinema' : 'left to Windows / Steam'}${asking ? ' · needs your answer' : ''}`,
  };
}
export function deviceLine(d) {
  const family = FAMILY[d.family] || 'Controller';
  return `${d.name || family} — ${family}, ${TRANSPORT[d.transport] || d.transport}${d.has?.pads ? `, ${d.has.pads} trackpad${d.has.pads > 1 ? 's' : ''}` : ''}${d.has?.gyro ? ', gyro' : ''} (unverified)`;
}
// The card at the top: only what the host reported. No device is invented; every device is marked unverified.
export function deviceCard({available, status, devices}) {
  const first = devices[0], family = first && (FAMILY[first.family] || 'Controller');
  const name = !available ? 'Controller input not available' : first ? (first.name || family) : 'No controller reported';
  const detail = !available ? 'Only browser gamepads work in this window.'
    : first ? `${family} · ${TRANSPORT[first.transport] || first.transport}${devices.length > 1 ? ` · ${devices.length - 1} more` : ''}`
    : 'A controller Cinema reads shows here once it is connected.';
  const state = !available ? 'Not available' : !status ? 'Waiting for focus' : !status.ready ? 'Could not start' : 'Input on';
  const tone = !available || (status && !status.ready) ? 'off' : !status ? 'waiting' : 'on';
  return {name, detail, state, note: first ? 'Unverified hardware' : '', tone};
}
// --- Mount: settings view, host request, frames -----------------------------------------------------------------
let mounted = null;
export async function mount(cinema) {
  const controller = cinema.controller;
  if (!controller) return;   // another module holds Cinema's controller input
  // Settings may save a preference at any moment, also while this mounts (review of a9a6447). So: listen FIRST; a change
  // heard before this copy is ready only marks it (dirty) and is read once when ready; one heard after is read then.
  const declared = new Set(Object.values(PREFERENCES).map(([storeKey]) => storeKey));
  let ready = false, dirty = false, reading = 0, reconcile = () => {};
  cinema.storage.onChange?.(({key}) => { if (!declared.has(key)) return; if (ready) reconcile(); else dirty = true; });
  let stored = await cinema.storage.get().catch(() => null);
  let found = resolve(stored);
  let message = '';
  // 0.1.0's nested settings, moved once: storage.fill sets a declared key ONLY if the store still has none, in one step
  // under the same lock as Settings' own writes, so a value Settings saved since the read above is never replaced. The
  // settings used come from the store as it is after that step. Only then does "settings" drop its old copy (the Steam
  // choice and its consent stay). If anything fails, the old copy stays, still applies, and is tried again next start.
  if (stored && found.legacy) {
    try {
      const fill = Object.fromEntries(Object.entries(found.migrate).map(([key, value]) => [PREFERENCES[key][0], PREFERENCES[key][1](value)]));
      if (Object.keys(fill).length) { stored = (await cinema.storage.fill(fill)).values; found = resolve(stored); }
      await cinema.storage.set('settings', {steam: found.settings.steam, override: found.settings.override});
    } catch (error) { message = 'Earlier settings could not be moved; they still apply.'; }
  }
  let settings = found.settings, offsets = offsetsFrom(stored?.offsets);
  let devicesById = new Map();
  const familyOf = state => devicesById.get(state?.device)?.family || (state?.source === 'browser' ? 'generic' : null);
  let behaviour = new Behaviour(controller, settings, offsets, familyOf);
  const apply = ({calibrating = false} = {}) => {
    controller.setRemap(PRESETS[settings.preset]);
    controller.request(hostRequest(settings, {calibrating}));
  };
  // Writes only what changed: a declared preference to its own key (the one Settings shows), the Steam choice to "settings".
  const write = async (before, next) => {
    for (const [key, [storeKey, to]] of Object.entries(PREFERENCES)) if (before[key] !== next[key]) await cinema.storage.set(storeKey, to(next[key]));
    if (before.steam !== next.steam || before.override !== next.override) await cinema.storage.set('settings', {steam: next.steam, override: next.override});
  };
  const save = async next => { const before = settings; settings = settingsFrom(next); behaviour = new Behaviour(controller, settings, offsets, familyOf); apply(); render();
    try { await write(before, settings); } catch (error) { message = 'Settings could not be saved.'; render(); } };
  // A preference changed in Cinema's Settings: read the store again and apply it now (the Steam choice is not one). Reads
  // only; a newer change wins; nothing happens once this copy has gone.
  reconcile = async () => {
    const ticket = ++reading, values = await cinema.storage.get().catch(() => null);
    if (ticket !== reading || !values || mounted?.cinema !== cinema) return;
    const next = resolve(values).settings;
    settings = settingsFrom({...settings, ...Object.fromEntries(Object.keys(PREFERENCES).map(k => [k, next[k]]))});
    behaviour = new Behaviour(controller, settings, offsets, familyOf); apply(); render();
  };
  controller.onFrame((state, now) => behaviour.frame(state, now));
  apply();

  // --- The view (0.3.0 layout, a SEMANTIC surface since 0.4.0, 2026-11-18): a summary with the device card and one entry
  // per area, each opening its own screen with the existing choices. The page's skin draws it (no element or style of
  // this module's): a choice is one action per value, the selected one primary (the skin shows it pressed). Back on a
  // screen returns to the summary with focus on the entry it came from; on the summary the page closes the view.
  let screen = 'summary', cameFrom = null, asked = false, view = null;
  const TITLES = {buttons: 'Buttons', trackpads: 'Trackpads', motion: 'Motion pointer', advanced: 'Advanced'};
  // Icon HINTS (neutral names; the skin draws the ones it knows, as the panel's approved icons): no markup of this module's.
  const ICON = {buttons: 'buttons', trackpads: 'touchpad', motion: 'motion', advanced: 'settings'};
  const HELP = {
    preset: ['Button layout', 'Which face buttons select and go back. Select and Back always stay reachable.'],
    left: ['Left trackpad', 'Use the left trackpad as arrow directions.'],
    right: ['Right trackpad', 'Scroll, or move a pointer inside Cinema only (never the Windows pointer).'],
    gyro: ['Active', 'When turning the controller moves Cinema\u2019s pointer (controllers with a gyro).'],
    gyroButton: ['Hold to aim', 'The button to hold when Active is \u201cWhile a button is held\u201d.'],
    sensitivity: ['Sensitivity', 'How far the pointer moves for a turn of the controller.'],
    steam: ['Steam Controllers', 'Read directly in Cinema works without Steam. Leave them to Windows / Steam if Steam should handle them.'],
  };
  const text = (t, tone = 'muted') => ({kind: 'text', text: t, tone});
  // A setting: its title and help, then its values alone in one section (one focus row; the skin shows the selected one).
  const choice = key => [{heading: HELP[key][0], items: [text(HELP[key][1])]},
    {items: CHOICES[key].map(value => ({kind: 'action', id: `set.${key}.${value}`, label: optionLabel(key, value), selected: String(settings[key]) === String(value),
      disabled: key === 'gyroButton' && settings.gyro !== 'button'}))}];
  const selectedOf = key => `set.${key}.${settings[key]}`;
  const FIRST = {buttons: 'preset', trackpads: 'left', motion: 'gyro', advanced: 'steam'};
  function descriptor() {
    const available = controller.available(), status = controller.status(), devices = controller.devices();
    devicesById = new Map(devices.map(d => [d.id, d]));
    asked = settings.steam === 'raw' && !settings.override && status?.mapper === 'steam';
    const footer = {state: 'ok', text: message || 'Changes are saved automatically.'};
    if (screen === 'summary') {
      const card = deviceCard({available, status, devices}), lines = summaries(settings, {asking: asked});
      return {title: 'Controller', status: footer, sections: [
        {items: [{kind: 'facts', icon: 'gamepad', rows: [{label: card.name, value: [card.detail, card.state, card.note].filter(Boolean).join(' \u00b7 ')}]}]},
        {items: ['buttons', 'trackpads', 'motion'].map(name => ({kind: 'action', id: `entry.${name}`, label: TITLES[name], note: lines[name], icon: ICON[name], opens: true}))},
        {items: [{kind: 'action', id: 'entry.advanced', label: TITLES.advanced, note: lines.advanced, icon: ICON.advanced, opens: true}]}]};
    }
    const sections = [{items: [{kind: 'action', id: 'back', label: 'Back to Controller', icon: 'back'}]}];
    if (screen === 'buttons') sections.push(...choice('preset'), {items: [text(`Now: ${ROLES[settings.preset]}.`, 'normal')]});
    if (screen === 'trackpads') {
      sections.push(...choice('left'), ...choice('right'));
      if (devices.length && !devices.some(d => d.has?.pads)) sections.push({items: [text('No connected controller reports a trackpad.')]});
    }
    if (screen === 'motion') {
      const calibrated = Object.keys(offsets).filter(f => FAMILY[f]).map(f => FAMILY[f]);
      sections.push(...choice('gyro'), ...choice('gyroButton'), ...choice('sensitivity'),
        {heading: 'Calibration', items: [text('Put the controller down and keep it still for about a second. Saved for each kind of controller.'),
          {kind: 'action', id: 'calibrate', label: 'Calibrate'}, text(calibrated.length ? `Calibrated: ${calibrated.join(', ')}.` : 'Not calibrated yet.', 'normal')]});
      if (devices.length && !devices.some(d => d.has?.gyro)) sections.push({items: [text('No connected controller reports a gyro.')]});
    }
    if (screen === 'advanced') {
      sections.push(...choice('steam'));
      if (asked) sections.push({items: [text('Steam is running and may already be handling your controller. Read it directly in Cinema anyway? While Cinema has focus, Steam\u2019s own controller settings may not apply to it.', 'warning'),
        {kind: 'action', id: 'steam.raw', label: 'Read directly anyway'}, {kind: 'action', id: 'steam.system', label: 'Leave it to Steam'}]});
      sections.push({items: [text(!available ? 'Cinema\u2019s controller input is not available in this window (only browser gamepads).'
        : !status ? 'Waiting for the controller input to start (Cinema must have focus).'
        : !status.ready ? 'Controller input could not start.'
        : `Steam Controllers: ${status.steam === 'raw' ? 'read directly by Cinema' : 'left to Windows / Steam'}${status.mapper === 'steam' ? ' (Steam is running)' : ''}. Motion: ${status.sensors ? 'on' : 'off'}.`, 'normal')]},
        {heading: 'Devices', items: (devices.length ? devices.map(d => text(deviceLine(d), 'normal')) : [text('No controller reported.', 'normal')]).slice(0, 16)},
        {items: [text('Hardware not verified: no controller, transport or Steam setup has been tested with this build. A Steam Controller read directly returns to its own mouse and keyboard mode when Cinema loses focus, closes or this module is turned off; if Cinema is killed, reconnect the controller to restore it.')]});
    }
    return {title: TITLES[screen], status: footer, sections};
  }
  let drawn = '';
  function render() {
    const next = descriptor(), json = JSON.stringify(next);
    if (json === drawn) return;   // polled twice a second: redrawn only when something changed
    drawn = json;view?.update(next);
  }
  // Moving between screens. Entering a screen focuses its selected value; leaving it focuses its entry again.
  function show(name) {
    if (name !== 'summary' && !TITLES[name]) return;
    if (name !== 'summary') cameFrom = name;
    screen = name;render();
    view?.focus(name === 'summary' ? `entry.${cameFrom || 'buttons'}` : selectedOf(FIRST[name]));
  }
  function back() { if (screen === 'summary') return false; show('summary'); return true; }
  function act({id}) {
    if (id.startsWith('entry.')) return show(id.slice(6));
    if (id === 'back') return show('summary');
    if (id === 'calibrate') return startCalibration();
    if (id === 'steam.raw' || id === 'steam.system') {
      save(id === 'steam.raw' ? {...settings, override: true} : {...settings, steam: 'system', override: false});
      return view?.focus(selectedOf('steam'));
    }
    const m = /^set\.([A-Za-z]+)\.(.+)$/.exec(id);
    if (!m || !CHOICES[m[1]]) return;
    const value = CHOICES[m[1]].find(v => String(v) === m[2]);
    if (value === undefined) return;
    save({...settings, [m[1]]: value, ...(m[1] === 'steam' ? {override: false} : {})});
    view?.focus(id);
  }
  function startCalibration() {
    apply({calibrating: true});
    message = 'Calibrating: hold the controller still\u2026'; render();
    cinema.timers.setTimeout(() => {   // the host restarts its session for motion first
      behaviour.calibration = {samples: [], until: (globalThis.performance?.now?.() ?? Date.now()) + 1000, finish: async (offset, family) => {
        apply();
        if (!offset || !family) { message = family ? 'The controller moved or sent no gyro: try again.' : 'No controller with a gyro reported.'; render(); return; }
        offsets = {...offsets, [family]: offset}; behaviour = new Behaviour(controller, settings, offsets, familyOf);
        try { await cinema.storage.set('offsets', offsets); message = 'Gyro calibrated.'; } catch (error) { message = 'The calibration could not be saved.'; }
        render();
      }};
    }, 400);
  }
  // The page closed the view: the next opening starts on the summary (its first entry gets the page's focus).
  const reset = () => { message = ''; screen = 'summary'; cameFrom = null; render(); };
  // 0.3.1: opened from Settings, not the navigation. Without semantic surfaces (an older Cinema): no view.
  if (typeof cinema.surfaces?.view === 'function')
    view = cinema.surfaces.view({label: 'Controller', placement: 'settings'}, descriptor(), {onAction: act, onClose: reset, onBack: back});
  const timer = cinema.timers.setInterval(() => { if (view?.open) render(); else devicesById = new Map(controller.devices().map(d => [d.id, d])); }, 500);
  render();
  mounted = {controller, timer, cinema};
  ready = true;
  if (dirty) reconcile();   // a Settings save heard while mounting: read once now (not polling)
}
export function unmount() {
  if (!mounted) return;
  mounted.cinema.timers.clear(mounted.timer);
  mounted.controller.release();   // input off, remap and pointer gone (also done by the host's cleanup)
  mounted = null;
}
