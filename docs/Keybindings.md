# Keybindings

ED Flight Control reads keyboard bindings from Elite Dangerous' active custom preset. Assign a keyboard key to each action below
in **Options > Controls**. The action names in the first column are the names EDFC reads from the `.binds` file.

## Required actions

| Action | In-game binding | Controls section |
|---|---|---|
| `UI_Up` | UI Panel Up | General Controls > Interface Mode |
| `UI_Down` | UI Panel Down | General Controls > Interface Mode |
| `UI_Left` | UI Panel Left | General Controls > Interface Mode |
| `UI_Right` | UI Panel Right | General Controls > Interface Mode |
| `UI_Select` | UI Panel Select | General Controls > Interface Mode |
| `UI_Back` | UI Back | General Controls > Interface Mode |
| `CycleNextPanel` | Next Panel Tab | General Controls > Interface Mode |
| `CamZoomIn` | Camera Zoom In | General Controls |
| `YawLeftButton` | Yaw Left | Ship Controls > Flight Rotation |
| `YawRightButton` | Yaw Right | Ship Controls > Flight Rotation |
| `RollLeftButton` | Roll Left | Ship Controls > Flight Rotation |
| `RollRightButton` | Roll Right | Ship Controls > Flight Rotation |
| `PitchUpButton` | Pitch Up | Ship Controls > Flight Rotation |
| `PitchDownButton` | Pitch Down | Ship Controls > Flight Rotation |
| `UpThrustButton` | Thrust Up | Ship Controls > Flight Thrust |
| `SetSpeedZero` | Set Speed to 0% | Ship Controls > Flight Throttle |
| `SetSpeed50` | Set Speed to 50% | Ship Controls > Flight Throttle |
| `SetSpeed75` | Set Speed to 75% | Ship Controls > Flight Throttle |
| `SetSpeed100` | Set Speed to 100% | Ship Controls > Flight Throttle |
| `HyperSuperCombination` | Toggle Frame Shift Drive | Ship Controls > Flight Miscellaneous |
| `Supercruise` | Supercruise | Ship Controls > Flight Miscellaneous |
| `UseBoostJuice` | Engine Boost | Ship Controls > Flight Miscellaneous |
| `TargetNextRouteSystem` | Target Next Route System | Ship Controls |
| `LandingGearToggle` | Landing Gear | Ship Controls > Miscellaneous |
| `ToggleCargoScoop` | Cargo Scoop | Ship Controls > Miscellaneous |
| `PrimaryFire` | Primary Fire | Ship Controls > Weapons |
| `SecondaryFire` | Secondary Fire | Ship Controls > Weapons |
| `DeployHardpointToggle` | Deploy Hardpoints | Ship Controls > Weapons |
| `ResetPowerDistribution` | Balance Power Distribution | Ship Controls > Miscellaneous |
| `IncreaseEnginesPower` | Divert Power to Engines | Ship Controls > Miscellaneous |
| `IncreaseWeaponsPower` | Divert Power to Weapons | Ship Controls > Miscellaneous |
| `IncreaseSystemsPower` | Divert Power to Systems | Ship Controls > Miscellaneous |
| `HeadLookReset` | Reset Headlook | Ship Controls > Headlook Mode |
| `UIFocus` | UI Focus | Ship Controls > Mode Switches |
| `GalaxyMapOpen` | Open Galaxy Map | Ship Controls > Mode Switches |
| `SystemMapOpen` | Open System Map | Ship Controls > Mode Switches |

EDFC's own global hotkeys are separate from these game bindings. Their defaults are **Home** (start Travel
Assist) and **End** (stop assists). Configure them in the ED Flight Control Settings tab and avoid assigning those same keys
to Elite Dangerous actions.

### Galaxy Map menu conflicts

The keyboard bindings EDFC uses for `UI_Up`, `UI_Down`, `UI_Left` and `UI_Right` should not use the same
key and modifiers as Galaxy Map horizontal or forward/backward camera translation. Otherwise menu-navigation
inputs can move the map instead, disrupting system-name searches and bookmark selection. EDFC warns at startup
when it finds these overlaps in the active General Controls preset, including when a shipped preset shares the keys.
For example, unmodified `W` for the menu and `Shift+W` for map movement are separate combinations.
The warning concerns the keyboard bindings EDFC uses; it does not check gamepad axes or gamepad button conflicts.

## Fill missing bindings

Run `fix_keybinds.bat` with Elite Dangerous closed. The tool reads the active preset named in
`StartPreset.4.start`, checks its General and Ship controls, and proposes missing keys from Frontier's default
presets before using fallback keys. It only changes existing keyboard bindings when resolving a real conflict and
asks before applying those changes. German and English keyboard layouts are supported.
Galaxy Map menu/translation conflicts are proposed as changes to the EDFC menu binding; review the proposal
before applying it. You can instead change the map movement binding manually in the game.

- `fix_keybinds.bat --dry-run` previews the proposed changes without writing the preset.
- A timestamped backup is made before a preset is changed.
- Built-in presets have no editable `.binds` file. Switch to a custom preset before running the fixer.
- Run the fixer again after changing in-game bindings so EDFC can read the active preset.
