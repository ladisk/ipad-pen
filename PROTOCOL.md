# iPad Pen protocol, version 1

The iPad app listens on TCP port 27015 on its loopback address (127.0.0.1).
The laptop connects through usbmux (the USB channel). Only the iPad sends;
the laptop sends nothing. One client at a time; a new client replaces the old.

Every frame starts with a one-byte type. Each type has a fixed size. All
numbers are little-endian. `f32`/`f64` are IEEE 754 floats.

## Hello (type 1, 15 bytes)

Sent when a client connects, and again when the view size changes.

| Offset | Type   | Field                         |
|--------|--------|-------------------------------|
| 0      | u8     | type = 1                      |
| 1      | 4 byte | magic `IPEN`                  |
| 5      | u16    | protocol version = 1          |
| 7      | f32    | view width in points          |
| 11     | f32    | view height in points         |

Python: `struct.Struct("<B4sHff")`.

## Sample (type 2, 30 bytes)

One Apple Pencil sample. The app sends every coalesced touch (up to 240 Hz).

| Offset | Type | Field                                                     |
|--------|------|-----------------------------------------------------------|
| 0      | u8   | type = 2                                                  |
| 1      | u8   | phase: 0 down, 1 move, 2 up, 3 cancel                     |
| 2      | f32  | x, fraction of view width, 0..1 (left to right)           |
| 6      | f32  | y, fraction of view height, 0..1 (top to bottom)          |
| 10     | f32  | pressure, `force / maximumPossibleForce`, 0..1            |
| 14     | f32  | altitude in rad (`altitudeAngle`, pi/2 = upright)         |
| 18     | f32  | azimuth in rad (`azimuthAngle(in: view)`, from +x to +y)  |
| 22     | f64  | timestamp, iPad uptime in s (`UITouch.timestamp`)         |

Python: `struct.Struct("<BBfffffd")`.

## Button (type 3, 10 bytes)

A momentary button press. Button 0 is the Pencil double tap.

| Offset | Type | Field                       |
|--------|------|-----------------------------|
| 0      | u8   | type = 3                    |
| 1      | u8   | button                      |
| 2      | f64  | timestamp, iPad uptime in s |

Python: `struct.Struct("<BBd")`.

## Compatibility

A receiver drops the connection on an unknown type or an unknown version.
A new frame type or a changed layout needs a new version number.
