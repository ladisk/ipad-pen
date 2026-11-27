import Foundation

// Wire format, see PROTOCOL.md. All values little-endian.
enum Wire {
    static let port: UInt16 = 27015
    static let version: UInt16 = 1

    static let typeHello: UInt8 = 1
    static let typeSample: UInt8 = 2
    static let typeButton: UInt8 = 3

    static let phaseDown: UInt8 = 0
    static let phaseMove: UInt8 = 1
    static let phaseUp: UInt8 = 2
    static let phaseCancel: UInt8 = 3

    static func hello(width: Double, height: Double) -> Data {
        var d = Data()
        d.append(typeHello)
        d.append(contentsOf: Array("IPEN".utf8))
        d.appendLE(version)
        d.appendLE(Float(width))
        d.appendLE(Float(height))
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
