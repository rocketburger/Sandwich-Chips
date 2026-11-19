# cinema.controller (optional module, off by default)

This module holds the controller policy for Cinema. Cinema's core holds the mechanism:
- `web/controller-state.mjs`: arbitration;
- `web/gamepad.mjs`: navigation;
- `web/controller-host.mjs`: bounded page actions;
- `engine/ControllerInput.cpp`: native SDL3 input, in the composition DLL.

## Files
- `manifest.json`: capabilities `input.controller`, `storage` and `page.views`. Cinema's module validation accepts it.
- `ui/main.mjs`: the module.
  - **The view (0.3.0 layout)**: a summary, then one screen per area. Since 0.3.1 it opens from **Settings ›
    Extensions › Cinema extensions › Controller › Open Controller** (`page.view` placement `settings`), not from Cinema's navigation;
    closing it returns focus to that row. A Cinema without placement still shows it in the navigation, as before.
    - **Summary:** the first reported device and the input state (only what the host reports, each device marked
      unverified; honest text when there is no device, the input is not available, waiting or could not start), then
      Buttons, Trackpads and Motion pointer, each with its current value, and Advanced apart.
    - **Buttons:** the preset (Standard, Swap Enter and Back, Nintendo layout), described by button position.
    - **Trackpads:** left pad (directions on press, on touch, or off); right pad (scroll, pointer, or off).
    - **Motion pointer:** when it is active (off, while the right pad is touched, while a button is held, always), the
      button to hold, the sensitivity, and calibration.
    - **Advanced:** Steam Controllers (left to Windows/Steam, or read directly by Cinema, which needs no Steam), the
      Steam question, the status line, the device list and the hardware note.
    - **Back** (Esc, Backspace, the controller's Back, or the screen's own Back) returns from a screen to the summary,
      with focus on the entry it came from; on the summary it closes the view. This uses the page's `page.view` `onBack`.
    - **Focus and selection:** focus is the page's filled control; the selected value has a ring and a check mark.
  - **When Steam is running.** If you chose "read directly", Advanced asks you explicitly before reading the controller anyway (`raw-despite-mapper`).
  - **Storage (0.2.0).** The button preset, both pads, the gyro activation, its button and its sensitivity are declared
    preferences (`buttons`, `left_pad`, `right_pad`, `gyro`, `gyro_button`, `gyro_sensitivity` in percent):
    - each is one top-level key of this module's storage, shown and set in Cinema's Settings › Extensions › Cinema extensions › Controller
      and in this view;
    - a change saved in Settings reaches the running module at once (`cinema.storage.onChange`), and while the module is
      off it is used when it is turned on.
  - **Kept under `settings`:** the Steam choice and its explicit consent, which stay in this view only.
  - **Kept under `offsets`:** the gyro offsets, one per device family.
  - **0.1.0 settings** (all under `settings`) are moved once to the declared keys, value for value, when 0.2.0 first
    runs. Until then, Settings shows defaults for them.
  - **0.2.1:** the move only fills keys that are still absent, atomically against Settings' own writes (storage `fill`), so a
    value saved in Settings meanwhile is kept; a Settings change made while the module is starting is read once it is
    ready.
- `ui/pads.mjs`: pure maths for the trackpads and the gyro (C1).
- `ui/controller.css`: the view's layout, from skin tokens and this module's own `--m-controller-*` tokens only.

## Guarantees
- **Input is read only while it is needed.** Cinema's host reads controllers only while this module is on, the page asks, and Cinema's window has the foreground.
- **Steam Controllers are always handed back.** SDL's close path returns a Steam Controller to its own mouse and keyboard mode on each of these:
  - blur;
  - module off;
  - pagehide;
  - device removal;
  - exit.

  If the process is killed, reconnect the controller instead.
- **No effects outside the page.** There is no OS input, no Windows cursor and no virtual driver. Steam is not required.
- **Enter and Back are always reachable.** Core refuses a remap that would remove them.
- **One owner.** One module at a time holds the controller input.

## Evidence
- The 0.3.0 layout: offline only, in `engine/offline_controller` (TEST ONLY; headless browser, scripted host messages, synthetic devices), `research/CONTROLLER-LAYOUT-2026-11-15.md`.
- Synthetic only: an SDL3 virtual-joystick replay through the real native code, `research/controller-native-replay.jsonl`, plus the page tests.
- No real controller, transport or Steam setup has been tested.
- Full write-up: `research/CONTROLLER-C2-C3-2026-11-14.md`.
