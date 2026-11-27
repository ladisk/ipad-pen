# iPad Pen protocol, version 2

The iPad app listens on two TCP ports on its loopback address (127.0.0.1).
The laptop connects to both through usbmux (the USB channel). Each port
accepts one client at a time; a new client replaces the old one.

- Port 27015, pen: only the iPad sends (hello, samples, buttons).
- Port 27016, video (new in version 2): only the laptop sends (the laptop
  screen as H.264). A separate connection, so video never delays pen events.

All numbers are little-endian. `f32`/`f64` are IEEE 754 floats.

## Pen connection (port 27015)

Every frame starts with a one-byte type. The size of a sample and a button
frame is fixed; the size of a hello depends on its version field.

## Hello (type 1)

Sent when a client connects, and again when the view size changes.

| Offset | Type   | Field                                        |
|--------|--------|----------------------------------------------|
| 0      | u8     | type = 1                                     |
| 1      | 4 byte | magic `IPEN`                                 |
| 5      | u16    | protocol version (1 or 2)                    |
| 7      | f32    | view width in points                         |
| 11     | f32    | view height in points                        |
| 15     | u16    | view width in display pixels (version 2)     |
| 17     | u16    | view height in display pixels (version 2)    |
| 19     | f32    | scale, display pixels per point (version 2)  |

Version 1 is 15 bytes (`struct.Struct("<B4sHff")`), version 2 is 23 bytes
(`struct.Struct("<B4sHffHHf")`). The app is landscape only, so width > height
(2732 x 2048 px, scale 2 on the 12.9" iPad Pro).

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

## Video connection (port 27016)

The laptop sends one frame per H.264 access unit (one encoded picture):

| Offset | Type      | Field                                    |
|--------|-----------|------------------------------------------|
| 0      | u32       | length N of the access unit in bytes     |
| 4      | N bytes   | access unit, Annex-B (start code before every NAL unit) |

Python: `struct.Struct("<I")` + data. N is 1 to 16 MiB; the iPad drops the
connection on any other value.

The stream is H.264 High profile, no B-frames, in decode order with no
timestamps: the iPad shows every picture as soon as it is decoded. The
picture size is the pixel size from the hello. Every keyframe (IDR) has SPS
and PPS in front of it. The first frame after the connection opens is a
keyframe; there is one about every 60 pictures. The laptop restarts the
encoder for every connection, and the iPad drops pictures until the first
keyframe. A new SPS/PPS (other size) can come at any keyframe.

## Compatibility

A receiver drops the connection on an unknown type or an unknown version.
A new frame type or a changed layout needs a new version number.

The laptop accepts version 1 and 2 hellos. With a version 1 hello (an old
app) it does not open the video connection and works as a pen tablet only.
