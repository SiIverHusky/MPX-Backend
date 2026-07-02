# Lua Robot Bindings — MPX-Dog Robot HAL

Registered as the global `robot` table. All functions are called as `robot.<function>(...)`.

Source: `lua_bindings.cc` (ESP32 firmware)

---

## Gait Control

### `robot.gait(name)`
Start a gait by string name. See [Gait Names](#gait-names) below.

### `robot.get_mode() → string`
Get the current gait command name.

---

## Configuration

### `robot.set_config(period?, height?, up_height?, stride?, tilt?)`
Set gait parameters. Pass `nil` for any parameter to keep its current value.

### `robot.get_config() → {period, height, up_height, stride, tilt}`
Get current gait parameters as a table.

---

## Low-level Servo Control

Servo IDs range **1–12**.

| Function | Description |
|----------|-------------|
| `robot.set_servo_angle(id, deg)` | Set servo angle in degrees |
| `robot.set_servo_speed(id, speed)` | Set servo speed (0 = max, larger = slower) |
| `robot.set_all_servo_speed(speed)` | Set speed for all 12 servos |
| `robot.flush()` | Commit buffered servo positions (SyncWrite) |

---

## Servo Feedback

| Function | Returns |
|----------|---------|
| `robot.read_position(id)` | Raw position (0–1023), or -1 on error |
| `robot.read_speed(id)` | Signed speed, or -1 on error |
| `robot.read_load(id)` | Signed load value, or -1 on error |
| `robot.read_voltage(id)` | Voltage in 0.1V units, or -1 on error |
| `robot.read_temperature(id)` | Temperature in °C, or -1 on error |
| `robot.read_moving(id)` | 0 (stopped) or 1 (moving), or -1 on error |
| `robot.read_current(id)` | Current in mA, or -1 on error |
| `robot.ping(id)` | Servo model number, or ≤0 on failure |

---

## Calibration

| Function | Description |
|----------|-------------|
| `robot.set_offset(id, deg)` | Set calibration offset in degrees |
| `robot.get_offset(id) → float` | Get offset in degrees |
| `robot.reset_offsets()` | Zero all calibration offsets |

---

## Inverse Kinematics

IK functions set servo positions but **do NOT flush** — call `robot.flush()` to commit.

| Function | Leg |
|----------|-----|
| `robot.ik_fr(x, th0, z)` | Front-right |
| `robot.ik_fl(x, th0, z)` | Front-left |
| `robot.ik_rr(x, th0, z)` | Rear-right |
| `robot.ik_rl(x, th0, z)` | Rear-left |

---

## IMU

| Function | Description |
|----------|-------------|
| `robot.imu_read() → {ax, ay, az, gx, gy, gz}` | Accelerometer (g) + gyroscope (dps) |
| `robot.imu_print()` | Log latest IMU data to console |

---

## Utility

| Function | Description |
|----------|-------------|
| `robot.delay_ms(ms)` | Blocking delay. Breaks into 50ms chunks. Returns immediately if ≤0. |

---

## Gait Names

| Name | Description |
|------|-------------|
| `none` | No gait / stop |
| `init` | Initialize / home position |
| `step` | Single step |
| `advance` | Walk forward |
| `back` | Walk backward |
| `left` | Strafe left |
| `right` | Strafe right |
| `turnL` | Turn left |
| `turnR` | Turn right |
| `jump` | Jump up |
| `jumpfwd` | Jump forward |
| `twerk` | Twerk |
| `lookup` | Look up |
| `lookdown` | Look down |
| `lookleft` | Look left |
| `lookright` | Look right |
| `lookul` | Look upper-left |
| `lookur` | Look upper-right |
| `lookll` | Look lower-left |
| `looklr` | Look lower-right |
| `flegL` | Foreleg lift left |
| `flegR` | Foreleg lift right |
| `blegL` | Back leg lift left |
| `blegR` | Back leg lift right |
| `heightup` | Raise body height |
| `heightdown` | Lower body height |
| `balance` | Balance pose |
| `bowback` | Bow back |
| `bodycycle` | Body cycle |
| `headellipse` | Head ellipse motion |
| `moveLF` | Move left front leg |
| `moveRF` | Move right front leg |
| `moveLB` | Move left back leg |
| `moveRB` | Move right back leg |
| `testspeed` | Speed test |
| `roll` | Roll motion |
| `pitch` | Pitch motion |
| `stretch` | Stretch pose |
