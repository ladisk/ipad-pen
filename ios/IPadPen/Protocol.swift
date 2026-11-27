import Foundation

// Wire format, see PROTOCOL.md. All values little-endian.
enum Wire {
    static let port: UInt16 = 27015
    static let videoPort: UInt16 = 27016
    static let version: UInt16 = 3

    static let typeHello: UInt8 = 1
    static let typeSample: UInt8 = 2
    static let typeButton: UInt8 = 3
    static let typeGesture: UInt8 = 4
    static let typeScroll: UInt8 = 5
    static let typeZoom: UInt8 = 6

    static let gestureScroll: UInt8 = 1
    static let gestureZoom: UInt8 = 2
    static let gestureBegin: UInt8 = 0
    static let gestureEnd: UInt8 = 1

    static let phaseDown: UInt8 = 0
    static let phaseMove: UInt8 = 1
    static let phaseUp: UInt8 = 2
    static let phaseCancel: UInt8 = 3

    // Version 2 hello: view size in points, then in display pixels, and the
    // scale (pixels per point).
    static func hello(width: Double, height: Double, widthPx: UInt16, heightPx: UInt16, scale: Double) -> Data {
        var d = Data()
        d.append(typeHello)
        d.append(contentsOf: Array("IPEN".utf8))
        d.appendLE(version)
        d.appendLE(Float(width))
        d.appendLE(Float(height))
        d.appendLE(widthPx)
        d.appendLE(heightPx)
        d.appendLE(Float(scale))
        return d
    }

    static func sample(phase: UInt8, x: Double, y: Double, pressure: Double,
                       altitude: Double, azimuth: Double, timestamp: Double) -> Data {
        var d = Data()
        d.append(typeSample)
        d.append(phase)
        d.appendLE(Float(x))
        d.appendLE(Float(y))
        d.appendLE(Float(pressure))
        d.appendLE(Float(altitude))
        d.appendLE(Float(azimuth))
        d.appendLE(timestamp)
        return d
    }

    // Two-finger gesture start or end (version 3).
    static func gesture(_ gesture: UInt8, phase: UInt8) -> Data {
        var d = Data()
        d.append(typeGesture)
        d.append(gesture)
        d.append(phase)
        return d
    }

    // Two-finger pan since the previous scroll frame, in points, y down.
    static func scroll(dx: Double, dy: Double) -> Data {
        var d = Data()
        d.append(typeScroll)
        d.appendLE(Float(dx))
        d.appendLE(Float(dy))
        return d
    }

    // Pinch factor since the previous zoom frame (1 = no change).
    static func zoom(scale: Double) -> Data {
        var d = Data()
        d.append(typeZoom)
        d.appendLE(Float(scale))
        return d
    }

    static func button(_ button: UInt8, timestamp: Double) -> Data {
        var d = Data()
        d.append(typeButton)
        d.append(button)
        d.appendLE(timestamp)
        return d
    }
}

extension Data {
    mutating func appendLE(_ v: UInt16) {
        let bytes = Swift.withUnsafeBytes(of: v.littleEndian) { Array($0) }
        append(contentsOf: bytes)
    }

    mutating func appendLE(_ v: Float) {
        let bytes = Swift.withUnsafeBytes(of: v.bitPattern.littleEndian) { Array($0) }
        append(contentsOf: bytes)
    }

    mutating func appendLE(_ v: Double) {
        let bytes = Swift.withUnsafeBytes(of: v.bitPattern.littleEndian) { Array($0) }
        append(contentsOf: bytes)
    }
}
