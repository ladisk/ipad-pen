# iPad Pen protocol, version 3

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
| 5      | u16    | protocol version (1, 2 or 3)                 |
| 7      | f32    | view width in points                         |
| 11     | f32    | view height in points                        |
| 15     | u16    | view width in display pixels (version 2)     |
| 17     | u16    | view height in display pixels (version 2)    |
| 19     | f32    | scale, display pixels per point (version 2)  |

Version 3 has the layout of version 2 and adds the finger gesture frames
(types 4 to 6, below). Version 1 is 15 bytes (`struct.Struct("<B4sHff")`), version 2 is 23 bytes
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

## Finger gestures (version 3)

One finger and palms are ignored. Two fingers on the glass are a gesture:
pan = scroll, pinch = zoom (the two can run at the same time). The app does
not start a gesture while the Pencil is down or within 0.4 s after it lifts.
Frames of types 4 to 6 are sent on the pen connection, only after a version 3
hello.

Gesture (type 4, 3 bytes, `struct.Struct("<BBB")`): `u8` type = 4, `u8`
gesture (1 scroll, 2 zoom), `u8` phase (0 begin, 1 end; end also covers a
cancelled gesture). Begin and end are sent once per gesture.

Scroll (type 5, 9 bytes, `struct.Struct("<Bff")`): `f32` dx, `f32` dy, the
finger movement in iPad points since the previous scroll frame, y down. The
content follows the fingers (natural direction).

Zoom (type 6, 5 bytes, `struct.Struct("<Bf")`): `f32` scale, the factor since
the previous zoom frame. 1 = no change, above 1 = the fingers move apart.
Factors multiply, so `ln(scale)` adds up.

The laptop turns scroll into wheel events on a second virtual device ("iPad
Pen Scroll") and a pinch into Ctrl + wheel. While a zoom is active it ignores
scroll frames. It releases Ctrl at the end of the zoom gesture, when the
session ends and when it stops.

## Video connection (port 27016)

The laptop sends messages. Each message:

| Offset | Type    | Field                                      |
|--------|---------|--------------------------------------------|
| 0      | u32     | length N = 1 + payload size                |
| 4      | u8      | kind                                       |
| 5      | N - 1   | payload                                    |

Python: `struct.Struct("<IB")` + payload. N is 2 to 16 MiB; the iPad drops
the connection on any other value. The iPad ignores a message of an unknown kind.

| Kind | Name        | Payload                                                        |
|------|-------------|----------------------------------------------------------------|
| 3    | config      | `u8` flags; bit 0 = local ink on. Sent first, once.             |
| 1    | size        | `u16` width, `u16` height of the picture in pixels. Sent before the first access unit. |
| 4    | ink style   | `u8` tool, `u8` r, `u8` g, `u8` b, `u8` a, `f32` width. Sent after the size, then at every change. |
| 2    | access unit | one H.264 access unit (one picture), Annex-B (start code before every NAL unit) |

The picture is H.264 High profile, no B-frames, in decode order with no
timestamps: the iPad shows every picture as soon as it is decoded. Every
keyframe (IDR) has SPS and PPS in front of it. The first picture after the
connection opens is a keyframe; there is one about every 60 pictures. The
laptop restarts the encoder for every connection, and the iPad drops
pictures until the first keyframe.

The iPad shows the picture aspect-fit (black bars where the aspect ratio of
the picture and the screen differ). While the picture is shown, the pen
position in a sample is a fraction of the picture rectangle, not of the view:
(0, 0) is the top left pixel of the picture. A stroke that starts in a black
bar is not sent; a stroke that leaves the picture is clamped to its edge.
Without video the position is a fraction of the whole view.

Local ink: the iPad draws the current stroke itself (thin grey line, clipped
to the picture) and fades it out about 300 ms after the pen goes up, when the
real stroke has arrived in the video. The laptop turns it off with flags = 0.

Ink style (kind 4, payload 9 bytes, `struct.Struct("<BBBBBf")`): the colour and
width of the local ink. `tool`: 0 default (thin dark grey, as without this
message), 1 pen, 2 highlighter, 3 eraser (no local ink). `r, g, b, a`: colour,
0 to 255 (the alpha of a highlighter is in `a`). `width`: stroke width in
video pixels; the iPad draws it as `width * (picture width in points / picture
width in pixels)` points. The laptop reads the style from the note app Saber
(`$XDG_RUNTIME_DIR/saber-ink.json`: `v` 1, `tool` pen, highlighter, eraser or
other, `color` `#RRGGBBAA`, `width` in logical pixels of its monitor, `pid`).
It sends tool 0 when the file is missing, malformed or its `pid` is not alive,
and only when local ink is on. The width is `logical * monitor scale *
picture width / output width in pixels`.

## Compatibility

A receiver drops the connection on an unknown type or an unknown version.
A new frame type or a changed layout needs a new version number.

The laptop accepts version 1, 2 and 3 hellos. Daemon and app are updated
together: an old daemon drops a version 3 hello, and a version 2 app never
sends gesture frames. The pen frames (types 1 to 3) did not change.

With a version 1 hello (an old app) the laptop does not open the video
connection and works as a pen tablet only.
