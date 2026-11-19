# Assist workflows

This document summarizes the behavior currently implemented and lists a small number of follow-up items.

## Current implementation

### Travel Assist

1. Starts from the system route and/or local target selected in Elite Dangerous. If a system target remains, it
   follows the Galaxy Map route; otherwise, it starts with the local target. The local destination must be reported
   by `Status.json`; compass-only local targets are not inferred. Before flight, it confirms
   **SYS 2 / ENG 4 / WEP 0** through Status.json. If already correct, no power inputs are sent; otherwise,
   it confirms a **Balance Power Distribution** reset to 2/2/2 before setting and confirming 2/4/0.
   If the distribution cannot be confirmed, Travel Assist displays a user message and continues the flight.
2. During system travel, it avoids a bright star in front of the ship, aligns to the navigation compass, aligns to
   the target using screen detection, and starts the FSD jump.
3. After each jump, it starts a discovery scan in a background thread, attempts to refuel, then accelerates to move
   away from the star. The scan is attempted only in Analysis Mode.
4. When the system route ends, it continues to a remaining local target automatically. If there is no local target,
   it stops at the destination.
5. For a local target, it undocks or enters supercruise if needed, aligns using the navigation compass, and repeatedly
   aligns using screen detection while watching for the supercruise disengage prompt.
   When the target is occluded, it attempts a repositioning maneuver; it also checks for interdiction, submits,
   and attempts to escape back into supercruise. Recovery waits for both the supercruise status flag and the journal's
   `in_supercruise` state before realigning; the jump-transition flag alone is not sufficient.
   If arrival is not confirmed within 60 seconds of requesting supercruise after cooldown, the assist stops with an
   error. This recovery also applies to Waypoint Assist.
6. On disengagement, it checks whether docking is possible and whether the destination supports docking. When
   applicable, it sets zero throttle, waits briefly for deceleration and normal-space confirmation, and checks
   that the compass target is ahead and centered. It turns only when needed and confirms the result before
   approaching. Missing or unconfirmed alignment stops the approach with zero throttle and a user message.
   This also applies to Waypoint station approaches and is repeated before moving closer after a docking denial
   due to distance. Forward approach segments remain time-based; station distance is not measured.
   It requests docking through the Navigation panel and waits up to five minutes by default for the docking computer
   (configurable in Settings > Wait For Autodock).
   It confirms panel focus and the selected Contacts tab before sending the docking inputs. Tab changes are sent
   individually and checked again; unreadable highlights do not trigger blind switching. If confirmation times out,
   the docking attempt stops with an error instead of retrying selection in an unknown tab.
7. Once docked, the docking sequence selects refuel, repair, and ammunition services. The local-target phase does not
   run a waypoint trade sequence.

Supercruise entry requested by `sc_engage()` or during a planetary/Fleet Carrier departure must be confirmed by
both Status.json and the journal's `in_supercruise` state before subsequent flight steps.
The FSD has up to 30 seconds to start charging/jumping; after the first start indication, entry has a separate
60-second deadline that is not extended by repeated charging indications.
Planetary departure additionally waits up to 60 seconds for the latitude/longitude flag to clear, indicating that
the ship has left the surface/orbital-cruise HUD region. A timeout stops the assist with a user message and zero
throttle instead of continuing with alignment, SCO toggles or the configured carrier departure flight.
Zero throttle in supercruise does not bring the ship to a standstill. These checks also apply to Waypoint Assist;
they are departure checks, not collision avoidance during travel.

### Waypoint Assist

1. Loads the route selected in the Waypoints tab and visits its stops in order, skipping inactive and completed stops.
   Older route files must first be imported through **Import... > Legacy JSON...** and saved.
   At startup it uses the same cockpit check and **SYS 2 / ENG 4 / WEP 0** power setup as Travel Assist.
   If the PIP distribution cannot be confirmed, it shows a message and continues.
2. For another system, it first tries the configured Galaxy Map bookmark. If there is none or it does not produce
   the expected route, it searches by system name. A blank system name means the current system.
3. After entering supercruise, it selects and confirms the next route system before each jump alignment.
   If the active target cannot be confirmed, it sets throttle to zero and stops with a message.
   Zero throttle in supercruise does not bring the ship to a standstill.
4. If the requested station is already selected after arrival, it reuses that target. Otherwise, it tries configured
   local bookmarks, then reads station names from the Navigation panel and scrolls to the first exact match.
   The station must be visible under the game's current filters. An unconfirmed target stops the assist before local travel.
5. It runs the local-target flight and docking sequence for the selected destination. If already docked at the
   destination, it can perform configured trades or cargo transfers, then marks the waypoint complete and saves the
   progress separately from the route. Ordinary market trades require confirmation from the game before remaining
   quantities are reduced.
   Trade requests use commodity IDs; their editor order is preserved and the current market supplies the localized
   name used for dialog confirmation. A change of display language does not change the requested goods.
6. **Repeat route** restores the original quantities and makes all stops pending after the last active stop.
   The route repeats until stopped when **Runs** is `0` (unlimited), or stops after the configured total
   number of complete runs. Waypoint Assist remains docked at the end of a non-repeating or completed finite route
   and undocks when the next travel operation requires it.

For route editing, imports, trade options and troubleshooting, see the [Waypoint guide](Waypoint.md).
The editor's **Plot to System/Station** buttons only select targets; they do not start travel or trading.

### Interface and status overlay

The main window is dark and has a fixed size. The sidebar arrow collapses it to a narrow icon strip, the pin keeps
it on top, and the power button restarts or exits the program. Changes on the Ship, Overlay, Settings and Debug tabs
are saved automatically; each tab's lower right corner shows the save state. Window position, collapsed state and
pin state are remembered in `user_data/configs/gui.json`.

**Settings > Activate Elite window for each key** is enabled by default for new configurations and when the setting
is missing. Existing saved choices are preserved. It attempts to bring Elite Dangerous to the front before each
key or group of keys, but Windows can reject the focus change; keep the game focused while an assist is active.

The status overlay shows assist mode and status, ship status, fuel, current system, route destination, next star,
jumps and route distance, with progress bars and an optional learned ETA for system travel. Its AP status matches
the main window footer; the footer also shows jumps and route distance during route assists.

Use the **Overlay** tab to set position, appearance, visible lines and their order. The overlay is excluded from
screenshots and recordings by default. Enabling **Show in screenshots and recordings** also allows it to appear
in the autopilot's own captures, where it can interfere with screen detection.

### Assist statistics and ETA

The Info tab shows lifetime assisted jumps, lightyears and active time for the current account and individual ship,
plus the ship's average jump-cycle duration and lifetime count of valid time measurements. Accounts are separated
by the journal's commander ID (`FID`); ships use the combination of `ShipID` and the journal ship type,
not their editable name or registration. The type is an additional safeguard against a different model
ever receiving the same ID; it does not imply that the game normally reuses IDs.
Two ships of the same type therefore have independent histories. Switching account or ship ends the active
measurement chain. There is no reset button.

Travel, Waypoint and Single Waypoint flight contribute active assist time. Target-only plotting, Honk Assist and AFK
Combat do not contribute. System travel time covers the FSD route phase, including alignment, charging, hyperspace,
intermediate refueling and positioning. It ends on arrival at the plotted destination, before final-system refueling,
station approach, docking or trading. Local actions contribute only to overall active assist time.

Only autopilot-requested hyperspace jumps confirmed by new journal events count toward jump and distance totals.
Past journal records are not imported. The first confirmed jump in each FSD phase counts but establishes only a
timing baseline. Each following arrival provides one jump-cycle measurement while that phase remains uninterrupted.
Stopping, switching ships/accounts or entering a new FSD phase breaks the chain. A partial or interrupted cycle
never becomes a valid time measurement. Ordinary refueling delays remain in the measured duration.
Durations use a monotonic clock; the measurement timestamp is stored in UTC. Journal polling can introduce small
timing differences. Batched arrivals without a reliable interval are not used as time measurements.

**Overlay > Estimated arrival** controls one sortable line, for example `ETA: ~1h 12m (00:58)`.
The footer also shows the ETA while the system route is active. It estimates arrival at the final system of the
current plotted route, not completion of a whole waypoint list or its station/trade actions. The clock in parentheses
uses the PC's local timezone. Without three valid measurements for the ship, it shows `Calculating...`; with
sufficient stored history it is available immediately on a new route. ETA is hidden during local travel,
when off route or after system arrival.

The estimate is based on remaining jumps, not lightyears. Each valid measurement adjusts a persisted
ship-specific estimate with a 5% weight for the new cycle; older observations gradually lose influence.
The lifetime arithmetic average remains separate. Between jumps, elapsed cycle time reduces the estimate.
An overdue cycle does not consume the estimated time of later jumps or falsely indicate arrival.
The prediction is approximate, particularly after outfitting changes, during retries or on unusual refueling routes.
It never changes assist controls, fuel policy or navigation. A completed system route adds one concise summary
to the GUI log without replacing the AP status.

#### Storage and backups

Statistics are local and unversioned in `user_data/stats/statistics.sqlite3`; Python's built-in SQLite requires no
additional dependency. The database stores account/ship identifiers, numeric counters and UTC-timestamped duration
measurements, not commander names, ship names, system names or complete journals.
Only the latest **1,000 valid time measurements per ship** are retained. Age alone never deletes a measurement.
Lifetime totals and measurement counts are not reduced when old details are removed.

Writes use transactions. Active time is checkpointed at each confirmed jump and when a phase/assist ends;
an abrupt process termination can lose active time since the last checkpoint, but not previously committed totals.
After changed data is committed, a consistent SQLite backup is made if none exists or the previous successful
regular backup is at least **24 hours** old. Restarting or remaining idle does not itself produce another backup.
The latest **ten** regular `statistics-backup-*.sqlite3` files are retained in the same folder.
A new backup is checked before replacing its temporary file and rotating older backups.
Future schema migrations must first create a separate `statistics-migration-*.sqlite3` backup, outside regular
rotation. The original statistics format without ship types is migrated automatically after a verified backup.
Entries without a confirmed type remain intact in account totals, but are not guessed into a ship history or ETA.
Unknown database schema versions are rejected without rewriting the data.

Storage errors disable statistics/ETA and explicitly notify the user; backup failures warn without stopping flight.
A damaged database is never silently replaced with empty statistics or automatically restored from an older backup.
To restore manually, close ED Flight Control, preserve the damaged/current file, then copy a known-good backup to
`statistics.sqlite3`. Restoration recovers only the data present in that backup.
Same-folder backups protect against individual file damage, not disk failure or deletion of the whole folder;
include `user_data/stats/` in an external backup for that protection.

### Vision diagnostics

Cockpit tab and navigation-row recognition first checks the existing bright filled highlights, including
thin-line removal. If neither check finds a highlight, a bounded adaptive brightness threshold separates
dim selected backgrounds from dark inactive tabs. The first fallback uses HSV value (the brightest color channel),
so shaded default-orange highlights are not rejected merely because their grayscale luminance is low.
Only if it finds no highlight, a separate adaptive grayscale check can isolate a dim selection from saturated
scenery that merges with it in the value mask. Existing results, including multiple ambiguous highlights, retain
priority. Each adaptive threshold stays between 140 and 210; the normal bright-highlight threshold is unchanged.
If a left- or right-panel tab still has no candidate, an intermediate grayscale threshold can recover a dim filled tab
that blends into the surrounding HUD at the lower threshold. Navigation destination rows also have a narrowly scoped
value threshold of 205 for bright orange fills that fall just below the adaptive value cutoff.
The same brightness measure and threshold used for each mask also validate its rectified highlight's
filled area; quadrilateral, size and fill checks still reject text-only areas and hollow outlines.
Tab navigation still requires one uniquely recognized, OCR-confirmed tab before sending a switch or action.
The left tab capture includes the System icon when the panel appears slightly farther left.
Destination-name preprocessing separates leading icons from the rectified row border and removes their soft edges
when there is a clear gap before the name. Shallow highlights use a smaller vertical inset so the name's lower
strokes survive before the crop is padded for OCR. The shortest crops are converted to luminance so colored
selection chevrons are less likely to be read as letters in the name.

Occluded-target detection checks white and beige dashed rings with their existing color-specific match thresholds.
If the normal-size template does not match, it also checks templates 5% smaller and larger to tolerate slight
ring-size variation at the same screen resolution. Bright surfaces alone do not qualify as a ring.

Visible-target detection compares the native template with variants 5% smaller and larger on every reading,
even when the native score barely passes the existing threshold. All variants reuse the same filtered capture.
The best variant is used; an already accepted native match retains priority unless a variant improves its score
by at least 0.03. This avoids switching between a weak native match and a much stronger resized match merely
because the native score crosses the threshold. Matching the extra variants adds processing time but no captures.
Alignment uses the circle center adjusted to the actual matched template dimensions; the hidden-target check uses the same recognition
path so a recognized visible ring is not treated as missing. Targets obscured by cockpit structure or other
HUD elements can still require compass realignment. Visible and occluded target searches start higher in the HUD
to include rings near the previous upper boundary.

The compass search extends left to keep the sphere in view when the cockpit HUD shifts. Compass marker fallback
uses sphere-relative pixel areas for 1080p and 4K and checks both bright and dim,
low-saturation markers. Hollow markers are preferred over filled charging glow; small antialiased ring centers
are checked separately so a behind marker is not mistaken for an ahead dot. A rejected filled-template match
cannot validate a weak behind-template match. A filled-blob fallback that disagrees with the template direction
is treated as unknown, so alignment retries rather than steering from a guess. A morphologically recognized hollow
marker can override a weak filled-template vote, including when the marker is visible near the compass rim.
Unknown markers still stop compass alignment rather than providing a guessed direction. This is one shared
recognition path for SDR/HDR and both HUD color schemes, not a
resolution-specific mode. Real monitor/HDR and HUD-brightness combinations still require in-game validation.
If a dim hollow marker merges with the compass rim or charging glow, an additional fallback checks for one
compact dark hole with a brighter surrounding ring. It requires contrast in at least seven of eight directions
and a compact hole regardless of its orientation; diagonal ship-hull texture gaps must not turn a filled marker
into a behind reading. Hollow markers can be up to 0.25 of the compass radius in their longest dimension, covering
slightly wider bottom-rim markers while retaining the shape and contrast checks. It rejects multiple candidate holes
rather than guessing a position.

For filled markers, a separate local-contrast pass also accepts slightly saturated bright cores. A morphological
background estimate removes broad reflections; candidates must be compact, sphere-relative in size and brighter
than their surroundings in at least seven of eight directions. A single such core takes priority over an ordinary
low-saturation blob; multiple cores are treated as unknown. The existing white-marker path remains available for
glare and rim cases where the marker touches other bright surfaces; its saturation limits are unchanged.

Compass alignment requires repeated centered readings, including after the last steering attempt. If confirmation
fails or the marker disappears, it can hand off to target-ring alignment only when the visible-ring detector
independently finds a target. Otherwise it sets zero throttle, notifies the user and stops the assist.
The return value of `nav_align()` still reports a requested supercruise exit, not alignment success.
Normal-space station approaches retain their stricter compass confirmation before moving closer.

Set **Debug > Log level > Debug** before reproducing a recognition problem. Normal technical messages remain in
`user_data/logs/autopilot.log`; the complete technical log, including those messages and debug details, is written to
`user_data/debug/debug.log` while Debug is enabled. Both logs rotate separately in their respective folders on the first use of each log in a
program run; up to ten older files are kept for each. The GUI's log-file button opens `autopilot.log`.
For custom output locations, set `EDFC_LOG_DIR` for normal logs and `EDFC_DEBUG_DIR` for the detailed log and
diagnostic images before starting the program.
Recognition scores, message-gate measurements, OCR text and fallback attempts are Debug-only diagnostics.
Short user-facing events, such as sending the supercruise exit, remain at Info level.
The supercruise message pre-check requires at least nine compact, glyph-sized cyan components overlapping the same
horizontal scanline, at most 80 compact components overall, and the existing color-area and line checks.
The existing bright-cyan mask is checked first. If it does not pass, a second mask on the same capture includes dim
cyan-to-blue lettering (OpenCV HSV hue 88-110, saturation at least 60 and value at least 140), not just the brighter
frame. Each mask must independently satisfy the same area, line and glyph-geometry requirements; measurements from
different masks are not combined. Keeping the original mask first preserves prompts partly obscured by bright radar
elements, where including dimmer pixels can merge otherwise separate letters.
There is no separate 16-glyph minimum: short English prompts and letters merged by antialiasing can contain fewer
components. This also prevents fluctuations between 13-16 otherwise aligned glyphs from canceling an in-flight OCR
job. Sparse cockpit decorations, scattered HUD readouts and dense cyan noise remain rejected. If the message
pre-check no longer passes, its job is still invalidated immediately; this is not a time-based grace period.
In Debug mode, a passing-to-failing pre-check transition saves a `hud-disengage` case with reason `gate-lost`:
the unmodified rejected crop (`evaluated`) and its `mask-bright` and `mask-dim` images. All three come from the
same evaluated frame, without an additional capture. Only one case is saved per transition, not on every
subsequent rejected check; the existing 50-case category limit applies. These images help distinguish an actually
disappearing safe-exit prompt from a pre-check failure. A saved case does not itself prove that OCR had completed
or that the safe-exit window remained open. Dropout decisions and OCR invalidation are unchanged.

During a supercruise approach, one background OCR worker reads copied message crops while the flight loop continues
target alignment. There is no pending-frame queue, and only the flight loop can send the supercruise-exit input.
A recognized prompt must still have an active message pre-check and a supercruise journal state when consumed;
matches more than five seconds old are discarded. Stop requests, target changes, approach completion and a new
approach invalidate earlier jobs. An already-running inference cannot be interrupted, but its result is discarded
and no second job starts until it finishes. Calls using the same OCR engine are serialized, including lazy result
iteration. This avoids concurrent engine access but does not reduce OCR CPU cost; responsiveness under game load
still requires in-game validation. Negative results remain available for Debug diagnostics even after five seconds.
The optional CV view draws match boxes on display copies; it does not alter the recognition images or saved context.
Raw BGRA-backed OCR crops are copied into writable display images before annotations are drawn.
Its windows are managed by the main GUI thread and open within the work area of the monitor containing the main window.
Large images are scaled down to fit. You can move the windows; subsequent frames do not reset their positions.
Resizing a CV window scales its images up or down while preserving their aspect ratios, including the last frame
when recognition is idle. Only the display is resized; captures, matching and saved diagnostic images are unchanged.
The Debug tab has four **CV Views** buttons: **Compass**, **Target**, **Occluded Target** and **HUD Messages**.

In the right column below **Single Waypoint Assist**, **Capture Screenshot Set** records the forward cockpit
view once at the start, then cycles through the left, right and lower role panels. It returns to the forward
view without taking another screenshot. Start in the main ship cockpit with no assist running. For target-ring and
compass screenshots, select a target ahead before starting. The capture does not select a target or start flight.
Panel focus comes from the game status; the sequence does not require OCR to recognize the current tab.
The capture uses the same game-window activation as **Activate Elite window for each key** without changing that
setting. If Windows leaves EDFC in the foreground, switch to Elite Dangerous within five seconds of starting the set.
While the set is running, the enabled overlay shows **Screenshot Capture** on the mode line and
**Capturing screenshot set** on the status line if those lines are enabled. The previous status returns when the
set finishes or stops. Keep **Overlay visible in captures** disabled for clean fixture images.
The lower role panel briefly holds **UI Up** to return to the top before taking four shots (Suit, Ship, SRV and Crew).
The left panel omits the fifth shot when its tab bar matches the starting view, recording the duplicate
in the manifest without relying on OCR. Missing panels
or interrupted sets are noted in the report.

The PNGs and `manifest.json` stay under `user_data/debug/screenshot-sets/` by default. Display mode, HUD
brightness and HUD color scheme are optional per-set inputs; unknown values are left unset. File names include
the ship type and game language from the journal, when available, and the actual image resolution.
Files prefixed `RENAME-BEFORE-FIXTURE__` are raw captures: review the actual view, complete any known metadata
and rename to the fixture convention before copying useful images to the local test portfolio.
If the local Copilot workspace skill is installed, `/import-screenshot-set` reviews the newest set by default;
provide a set directory to review an older one. The import keeps the source set intact and reports which new
fixtures have automated assertions and which are diagnostic context only. For verified recognition failures,
the import offers a handoff to `/cv-tuning` to fix and rerun the failing assertions.
A button opens or focuses its window; the title-bar X closes it, and the button opens it again.
HUD Messages combines the message pre-check and OCR result in two separate display areas.
Windows initially show "Waiting for recognition..." until the corresponding recognition step runs. There are no
extra background captures; the last displayed frame remains visible between recognition steps.
Only opened views prepare display images, and none open automatically at startup. The old global CV switch is no
longer used. The CV view is independent of Debug logging and diagnostic image recording.
Display images may be filtered, cropped, annotated or scaled; they are not always unmodified raw search captures.

**Debug Overlay** takes effect without restarting. It draws search rectangles and OCR results over the game only during
supported HUD-message and panel/OCR checks, not continuously for compass or target matching. Switching it off removes
diagnostic markings without clearing normal AP status information. CV windows can overlap screen captures, and so can
debug markings when **Show in screenshots and recordings** is enabled on the Overlay tab; disable them for unobstructed
diagnostic runs or move CV windows to another monitor.

Diagnostic images are saved directly in `user_data/debug/<category>/`. Images from the same case share a timestamp
and identifier in their filenames, followed by their role, such as `_evaluated.png` or `_context.png`. The debug log
identifies the category, reason and filename prefix; related scores, OCR text and decisions are logged by the recognition
code. New case prefixes are relative to the program folder, such as `user_data/debug/compass/<case>`, rather than exposing
the absolute installation path. For a debug-directory override on another drive, prefixes are relative to that debug
directory instead. Existing logs are not rewritten. At most 50 cases are retained per category across runs: after successfully saving a new case, all images belonging
to the oldest excess cases in that category are removed. Unmanaged files and older case directories are not rotated.
Images and logs rotate independently, so their retention periods can differ. Existing logs at the old
`user_data/logs/debug.log` location are not moved or deleted.

To share a run, review and ZIP `user_data/debug/`; it can also contain earlier cases and rotated logs.
For maintainers, `user_data/incoming-debug/` is a separate local inbox for debug dumps received from other users.
The application creates it but does not process or automatically clean its contents. It is not part of the debug ZIP
and is excluded from Git along with the rest of `user_data/`.

- A failed target alignment saves the original search region used for the last failed match and a same-frame context
  with 1.5 times its width and height (clipped at screen edges). The search region and matching behavior stay unchanged.
  While Debug is enabled, each target search captures the larger region; images are saved only for a diagnostic case.
  The target can still lie outside the context; this is not a full screenshot.
- Missing compasses and weak nav-point matches save the evaluated image and a larger same-frame context. Matching
  uses the same search region with or without Debug; the extra context is captured only when Debug is enabled.
- Unconfirmed navigation and internal-panel highlights save the evaluated region and a later panel-context image,
  before the next navigation input. The later image is not necessarily identical to the evaluated frame.
- For a close but non-exact destination OCR read, Debug saves the evaluated list region and the exact OCR name crop
  before advancing, at most once per scan attempt. Similarity triggers diagnostics only; selection still requires
  an exact match and the existing confirmation checks.
- Odyssey Galaxy Map name searches save full game-window snapshots in `user_data/debug/galaxy-map/` only for
  failed checks, lost map focus or unconfirmed routes. Successful steps do not capture or save diagnostic images.
  The debug log records the stage, requested system, attempt, game GUI focus and window-activation setting.
  Opening the map waits up to 30 seconds for CARTOGRAPHICS using OCR. The keyboard search then uses Enter,
  menu directions, select and camera zoom without reading the search name, result or plot prompt.
  Map-focus checks remain active, but do not prove text-field focus. Plotting can start while the camera is moving.
  The requested final destination in NavRoute.json confirms success.
  An unconfirmed keyboard route switches to the OCR-checked mouse fallback. Its clicks use resolution-relative
  regions and the captured monitor's desktop offset. Name comparisons ignore case and extra whitespace.
  Mouse search-field entry has up to three attempts; the first result and the selected system card must match
  before the plot button is clicked. There are at most three selection/plot attempts in total: one keyboard
  attempt followed by up to two mouse attempts.
  Failed mouse OCR checks save one image per expired wait, not for individual polling misses.
  Each unconfirmed plot attempt saves `route-unconfirmed`; unavailable route data saves `route-unavailable`.
  The failed check's image also covers a fallback or final failure; no extra image duplicates it.
  Lost focus saves `focus-lost` and stops selection. The map is closed after success or exhausted attempts
  if it is still active. The same 50-case category limit applies; technical text logging remains available.
- An unconfirmed trade dialog saves its separately captured title and commodity regions and a later dialog context.
  OCR wait timeouts, unreadable or unmatched disengage OCR results, and ambiguous alignment or line-of-sight text
  can also save cases. Disengage mismatches save one case per continuous message-gate event, including when OCR
  returns readable but unrelated text. Canceled or superseded jobs are not reported as completed OCR failures.

The program cannot automatically detect every wrong recognition: an apparently successful match can still be wrong.
Not every absence of HUD text is an error. Images and logs can contain player information; review and remove sensitive
details before sharing them. Debug images are not captured or saved with Debug disabled. Image encoding and the larger
compass and target captures can add overhead while debugging.

Navigation destination selection accepts long, narrow filled row highlights without collapsing their corners during
contour simplification. If the usual approximation fails the corner, rectified-size or fill checks, a single finer
approximation is tried before rejecting the contour. This preserves perspective-slanted selections without changing
already valid highlights. Thin lines and hollow outlines do not count as selected rows; the destination still requires an exact
name match and confirmation from game state.
Tall leading and trailing symbols are removed before the name-column crop, so a partially clipped station icon
does not become a spurious OCR prefix. Compact leading icons are also removed when a gap wider than the text
height separates them from the name within the narrow leading-symbol area. Only these newly cleaned crops
receive an additional background margin, which helps OCR preserve spaced numeric suffixes; established crops
retain their previous framing. No recognized letters or digits are silently discarded or substituted.

Before searching, the Navigation panel holds up for five seconds to move toward the start of the list; reaching
the first entry is not confirmed. Destination names are not unique row identifiers: several resource extraction
sites can have the same title.
After a down input, a changed highlight position or movement of a uniquely detected scrollbar thumb can confirm
progress even if the name is unchanged. Both observations use the same capture as the name read; background
animation alone does not count as progress. The scan does not stop merely because a name has appeared before.
Unreadable or unconfirmed movement does not trigger blind row skipping. The existing per-input wait, 200-row
limit per attempt, two-attempt search window and exact-name re-read/game-target confirmation remain in effect.

### Other assists

- **Discovery Scanner Assist** is labeled **Honk Assist** in the sidebar. While in supercruise, it attempts the
  Discovery Scanner (D-Scanner/Honk) after entering a different system; Analysis Mode is required. Only a
  journal-confirmed scan counts as completed. If a scan fails, the same system is not fired on repeatedly; the assist
  can retry after leaving supercruise, entering another system, or restarting the assist.
- **Single Waypoint Assist** selects a system entered on the Debug tab and travels there; it can also be given a
  local station target. It uses the same next-jump target checks as Waypoint Assist.
- **AFK Combat Assist** monitors shield and fighter status, reports its monitoring, evasion and fighter-deployment
  phases, evades when shields are down, and attempts to redeploy a fighter when one is destroyed.
  Evasion and fighter-deployment phases use the central status route with voice output when enabled.
  Technical intermediate steps are debug-only; extra warnings use the GUI-log/voice notification route.
  - Start in the main ship in normal space, pointing away from the resource extraction site. The existing fighter
    menu sequence assumes two hangar rows and an available hired NPC pilot; it still alternates rows without
    checking hangar readiness. Single-hangar and readiness recognition remain unsupported.
  - Evasion retracts deployed hardpoints, landing gear and cargo scoop using Status.json. It uses the dedicated
    **Supercruise** binding, never the combined FSD binding, so a selected system target does not redirect the
    escape into a hyperspace jump. Bind **Cargo Scoop** (`ToggleCargoScoop`); run `fix_keybinds.bat` if needed.
  - The 120-second entry timeout includes preparation, boosts and retries. No new supercruise command is sent
    while charging, jumping, mass locked, cooling down or while the deployed items still block entry.
    Entry is confirmed through the Status.json Supercruise flag, not merely a charging journal event.
    After travelling away for 20 seconds and slowing for 10 seconds, the assist requests one exit and waits up
    to 30 seconds for confirmed normal-space flight. Unreadable Status.json retries are also bounded for AFK.
  - A fresh `LaunchFighter` event with `PlayerControlled=false` must confirm each NPC deployment within 30
    seconds after the menu sequence. Old launch events do not count, and an unconfirmed launch is not retried
    blindly. After confirmed redeployment it resumes monitoring; after evasion and confirmed deployment it ends.
    A recorded fighter launch clears the earlier loss event, so restarting does not redeploy for that old loss.
    A destruction event after the launch remains pending.
    The held UI Focus binding is released even if opening the fighter panel is interrupted or fails.
  - Failure stops the assist with a GUI-log/voice warning; take control of the ship. Entry failure does not
    automatically stop the ship's forward escape. Debug logging records attempts, observed states and confirmations.
    Each supercruise attempt logs whether FSD charging/jumping was observed or remained unobserved when the next
    retry window was reached. These diagnostics do not identify the cause of a failed start, such as a temporary
    damaged-module malfunction; they use the existing status polls without additional captures or reads.
  - Before changing power distribution, the assist uses **Balance Power Distribution** (`ResetPowerDistribution`)
    and confirms the 2/2/2 reset through Status.json. Escape then uses **SYS 2 / ENG 4 / WEP 0**; after confirmed
    dropout, recovery uses **SYS 4 / ENG 2 / WEP 0**. The final distribution must also be confirmed.
    Weapons remain retracted after escape. No weapons power or automatic return to combat is requested.
    Bind the reset action manually or add it with `fix_keybinds.bat`. SYS power supplies the systems capacitor;
    actual shield regeneration also depends on the shield and its current state.

## Issue reporting

The README directs bug reports and feature requests to the repository's GitHub issue tracker.
**Info > Report a bug** opens GitHub's issue submission/template chooser page in the browser; it does not
automatically attach logs, images or other local files. The existing **Open GitHub Page** button opens the repository
homepage.

Choose **Bug report** or **Feature request**. Both forms require confirmation that you searched open and closed
issues first; add relevant details to an existing matching issue instead of creating a duplicate.
Blank issues are disabled in the template chooser.

Bug reports ask for the application version (shown on the Info tab), actual and expected behavior, and steps or
context leading to the problem. Include the normal `user_data/logs/autopilot.log` from the affected run if available,
even without Debug output. Save a copy before restarting, which rotates the previous log to a timestamped file.
GitHub's text area supports file attachments as well as pasted excerpts; environment details and attachments remain
optional. For recognition problems,
the form links to the Debug-mode instructions below; reproducing the issue or supplying diagnostic images is not
a prerequisite for reporting it. Review all attachments for player information before posting them publicly.
Feature requests ask for the underlying need and desired behavior; alternatives and extra context are optional.

## Follow-up items

These are deferred improvements for future consideration.

- Design planetary-approach protection during travel, including compass-only alignment. Evaluate journal
  `ApproachBody`/`LeaveBody` events and Status.json altitude trends to detect hazardous approaches without rejecting
  legitimate travel to planetary destinations. Define when to reduce throttle or stop with a warning; add automatic
  avoidance only once a reliable direction away from the planet and suitable in-game validation are available.
- Assess HUD distance OCR for feedback-controlled station approaches instead of timed forward flight. Deferred
  until its reliability across resolutions, HUD colors and languages has been evaluated; not used by current approaches.
- Evaluate using the game's built-in Supercruise Assist for eligible in-system destinations when the ship has the
  module fitted. Verify how EDFC can confirm activation and arrival, and define when to use EDFC's own approach
  instead if the module is unavailable or cannot be engaged.
- Defer automatic recovery from the supercruise "loop of shame" until a naturally occurring case is captured in
  the debug log. After an overshoot, look for a target that stays off-center across several turn corrections
  despite reliable compass readings, rather than assuming a slow turn or a momentary ring loss is a loop.
  Evaluate whether temporarily reducing throttle to 25% breaks the loop without disrupting normal recovery or
  missing the disengage prompt.
- Refine station-name screen reading for additional destination icons and HUD layouts as examples become available.
  Bookmarks remain an alternative when a destination cannot be read reliably.
- Avoid changing hyperspace-interdiction recovery until there is a reliable way to validate it. Interdiction attempts
  are too unpredictable for routine testing: they may not occur for days, then happen several times within an hour.
- Verify the screen-capture channel order and simplify the related color conversions only if vision regression tests
  confirm the expected colors in both captured-screen and supplied-image modes.
- Assess explicit state confirmation for Orbital Construction Services instead of relying on a fixed menu-render delay.
- Review waypoint arrival matching for Orbital Construction Sites; an older candidate name match was commented out, so confirm the intended journal name and current route behavior before adding support.
- Defer changes to Thargoid shutdown recovery during hyperspace until reliable validation is possible.
  Encounters are even rarer than ordinary hyperspace interdictions and cannot be reproduced reliably for routine testing.
