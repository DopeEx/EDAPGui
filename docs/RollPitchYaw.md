# Ship roll, pitch, and yaw tuning

ED Flight Control uses each ship's roll, pitch, and yaw rates to estimate how long to hold its controls. The game
status determines which profile is used: the configurable supercruise rates or separate, built-in normal-space rates.
Normal-space rates are based on the [ship agility measurements at 50% throttle](https://docs.google.com/spreadsheets/d/1mGyE-MgS9IIDyJvaeVBBXLYMt8teI7WMwhlOIJsm948/edit?gid=1124267452#gid=1124267452).
They are a baseline at other throttle settings too; selecting the profile does not change the throttle. The Cobra Mk IV
uses the Cobra Mk III rates because the source has no separate measurement. If a ship has no normal-space profile,
EDFC stops the maneuver rather than using supercruise rates.

The **SHIP** panel on the **Main** tab displays and tunes only the active ship's supercruise rates.

Outfitting lists rates in **Pitch, Roll, Yaw** order; the EDFC fields are **Roll, Pitch, Yaw**. Outfitting values
are a starting point: ship rotation is slower in supercruise, so check and tune the values in-game.

## Tune the rates

1. Use the ship's outfitting rates as a starting point, keeping in mind the different order in the game and EDFC.
   The currently active ship profile is loaded automatically.
2. Enter supercruise and select a system as the target. For **Roll**, place the navigation dot at the top of the
   compass. For **Pitch** and **Yaw**, place it in the center.
3. Select the matching **Test Roll Rate**, **Test Pitch Rate**, or **Test Yaw Rate** button in the **SHIP** panel.
   EDFC checks that the ship is in supercruise and has a target, sets speed to 50%, then performs a 360-degree
   maneuver (roll left, pitch up, or yaw left, respectively).
4. Compare the navigation dot with its starting position. If the maneuver overshoots, increase that rate; if it
   undershoots, decrease it. Repeat until the maneuver finishes close to its starting position. A slight undershoot
   is preferable to a substantial overshoot.
5. Open **Settings** and select **Save All Settings** to save the current ship's rates.

The rates are stored per ship in `user_data/configs/ship_configs.json` and loaded when the active ship changes. The
optional **SunPitchUp +/- Time** field can adjust pitch time for ships that need more clearance from a star.

## Compass recognition during alignment

The hollow compass marker means the target is behind the ship. When cockpit glare hides the marker in the
brightness-based search, EDFC can recover it from a single compact cyan rim with a clear center. An unconfirmed
compass reading still does not provide a steering direction.
