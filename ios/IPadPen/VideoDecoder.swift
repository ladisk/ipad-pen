import AVFoundation
import CoreMedia
import UIKit

// Full-screen view whose layer shows the decoded laptop screen.
final class VideoView: UIView {
    override class var layerClass: AnyClass { AVSampleBufferDisplayLayer.self }

    var displayLayer: AVSampleBufferDisplayLayer {
        // layerClass guarantees the type.
        return layer as! AVSampleBufferDisplayLayer
    }
}

// Ink style (kind 4): tool 0 default grey, 1 pen, 2 highlighter, 3 eraser;
// colour 0...255; width in video pixels.
struct InkStyle {
    var tool: UInt8
    var red: UInt8
    var green: UInt8
    var blue: UInt8
    var alpha: UInt8
    var width: Float

    static let standard = InkStyle(tool: 0, red: 0, green: 0, blue: 0, alpha: 0, width: 0)
}

// Reads the video connection (messages: u32 little-endian length, u8 kind,
// payload; see PROTOCOL.md) and shows every H.264 access unit on an
// AVSampleBufferDisplayLayer. Runs on the main queue.
final class VideoDecoder {
    static let maxFrame = 16 << 20
    static let kindSize: UInt8 = 1
    static let kindAccessUnit: UInt8 = 2
    static let kindConfig: UInt8 = 3
    static let kindInk: UInt8 = 4

    var onFirstFrame: (() -> Void)?
    // Picture size in pixels (kind 1).
    var onVideoSize: ((CGSize) -> Void)?
    // Config flags (kind 3); bit 0 = local ink.
    var onConfig: ((UInt8) -> Void)?
    // Ink style (kind 4).
    var onInkStyle: ((InkStyle) -> Void)?

    private let layer: AVSampleBufferDisplayLayer
    private var buffer = [UInt8]()
    private var sps = [UInt8]()
    private var pps = [UInt8]()
    private var format: CMVideoFormatDescription?
    private var needKeyframe = true
    private var shownFrame = false

    init(layer: AVSampleBufferDisplayLayer) {
        self.layer = layer
        // Exact pixel mapping to the view, so the pen lines up with the image.
        layer.videoGravity = .resize
        layer.backgroundColor = UIColor.black.cgColor
    }

    // Forget the stream (new connection or disconnect).
    func reset() {
        buffer.removeAll()
        sps = []
        pps = []
        format = nil
        needKeyframe = true
        shownFrame = false
        layer.flushAndRemoveImage()
    }

    // Add bytes from the connection. Returns false on a framing error; the
    // caller then drops the connection.
    func receive(_ data: Data) -> Bool {
        buffer.append(contentsOf: data)
        var offset = 0
        while buffer.count - offset >= 4 {
            let b0 = Int(buffer[offset])
            let b1 = Int(buffer[offset + 1]) << 8
            let b2 = Int(buffer[offset + 2]) << 16
            let b3 = Int(buffer[offset + 3]) << 24
            let length = b0 | b1 | b2 | b3  // kind byte + payload
            if length < 2 || length > VideoDecoder.maxFrame {
                buffer.removeAll()
                return false
            }
            let start = offset + 4
            if buffer.count - start < length { break }
            let kind = buffer[start]
            let payload = Array(buffer[(start + 1)..<(start + length)])
            offset = start + length
            if kind == VideoDecoder.kindAccessUnit {
                decode(payload)
            } else if kind == VideoDecoder.kindSize {
                if payload.count >= 4 {
                    let w = Int(payload[0]) | Int(payload[1]) << 8
                    let h = Int(payload[2]) | Int(payload[3]) << 8
                    onVideoSize?(CGSize(width: w, height: h))
                }
            } else if kind == VideoDecoder.kindConfig {
                if let flags = payload.first {
                    onConfig?(flags)
                }
            } else if kind == VideoDecoder.kindInk {
                if payload.count >= 9 {
                    let bits = UInt32(payload[5]) | UInt32(payload[6]) << 8
                        | UInt32(payload[7]) << 16 | UInt32(payload[8]) << 24
                    onInkStyle?(InkStyle(tool: payload[0], red: payload[1], green: payload[2],
                                         blue: payload[3], alpha: payload[4],
                                         width: Float(bitPattern: bits)))
                }
            }
            // Unknown kinds are skipped (the length is already consumed).
        }
        if offset > 0 {
            buffer.removeFirst(min(offset, buffer.count))
        }
        return true
    }

    // Payloads of the NAL units in an Annex-B access unit (start codes removed).
    private static func nalUnits(_ au: [UInt8]) -> [ArraySlice<UInt8>] {
        var marks: [(codeStart: Int, payload: Int)] = []
        let n = au.count
        var i = 0
        while i + 3 <= n {
            if au[i] == 0 && au[i + 1] == 0 && au[i + 2] == 1 {
                let codeStart = (i > 0 && au[i - 1] == 0) ? i - 1 : i
                marks.append((codeStart: codeStart, payload: i + 3))
                i += 3
            } else {
                i += 1
            }
        }
        var nals: [ArraySlice<UInt8>] = []
        for (k, mark) in marks.enumerated() {
            let end = k + 1 < marks.count ? marks[k + 1].codeStart : n
            if end > mark.payload {
                nals.append(au[mark.payload..<end])
            }
        }
        return nals
    }

    private func decode(_ au: [UInt8]) {
        var avcc = [UInt8]()
        var newSPS: [UInt8]?
        var newPPS: [UInt8]?
        var isIDR = false
        for nal in VideoDecoder.nalUnits(au) {
            guard let header = nal.first else { continue }
            let type = header & 0x1F
            if type == 7 {
                newSPS = Array(nal)
            } else if type == 8 {
                newPPS = Array(nal)
            } else if type != 9 {  // 9 = access unit delimiter, not needed
                if type == 5 { isIDR = true }
                let len = UInt32(nal.count)
                avcc.append(UInt8(truncatingIfNeeded: len >> 24))
                avcc.append(UInt8(truncatingIfNeeded: len >> 16))
                avcc.append(UInt8(truncatingIfNeeded: len >> 8))
                avcc.append(UInt8(truncatingIfNeeded: len))
                avcc.append(contentsOf: nal)
            }
        }
        if let s = newSPS, let p = newPPS, format == nil || s != sps || p != pps {
            // New stream or a format change (the encoder restarted).
            sps = s
            pps = p
            format = makeFormat()
            needKeyframe = true
        }
        guard let format = format, !avcc.isEmpty else { return }
        if layer.status == .failed {
            layer.flush()
            needKeyframe = true
        }
        // P frames before the first IDR cannot be decoded.
        if needKeyframe && !isIDR { return }
        guard let sample = makeSample(avcc, format: format) else { return }
        layer.enqueue(sample)
        needKeyframe = false
        if !shownFrame {
            shownFrame = true
            onFirstFrame?()
        }
    }

    private func makeFormat() -> CMVideoFormatDescription? {
        var fmt: CMVideoFormatDescription?
        let spsBytes = sps
        let ppsBytes = pps
        let status: OSStatus = spsBytes.withUnsafeBufferPointer { s -> OSStatus in
            return ppsBytes.withUnsafeBufferPointer { p -> OSStatus in
                guard let sBase = s.baseAddress, let pBase = p.baseAddress else { return -1 }
                let pointers: [UnsafePointer<UInt8>] = [sBase, pBase]
                let sizes: [Int] = [s.count, p.count]
                return CMVideoFormatDescriptionCreateFromH264ParameterSets(
                    allocator: kCFAllocatorDefault,
                    parameterSetCount: 2,
                    parameterSetPointers: pointers,
                    parameterSetSizes: sizes,
                    nalUnitHeaderLength: 4,
                    formatDescriptionOut: &fmt)
            }
        }
        return status == noErr ? fmt : nil
    }

    private func makeSample(_ avcc: [UInt8], format: CMVideoFormatDescription) -> CMSampleBuffer? {
        var block: CMBlockBuffer?
        var status = CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault,
            memoryBlock: nil,
            blockLength: avcc.count,
            blockAllocator: kCFAllocatorDefault,
            customBlockSource: nil,
            offsetToData: 0,
            dataLength: avcc.count,
            flags: 0,
            blockBufferOut: &block)
        guard status == kCMBlockBufferNoErr, let blockBuffer = block else { return nil }
        status = avcc.withUnsafeBytes { raw -> OSStatus in
            guard let base = raw.baseAddress else { return -1 }
            return CMBlockBufferReplaceDataBytes(
                with: base, blockBuffer: blockBuffer, offsetIntoDestination: 0, dataLength: avcc.count)
        }
        guard status == kCMBlockBufferNoErr else { return nil }

        var sample: CMSampleBuffer?
        let sizes: [Int] = [avcc.count]
        status = CMSampleBufferCreateReady(
            allocator: kCFAllocatorDefault,
            dataBuffer: blockBuffer,
            formatDescription: format,
            sampleCount: 1,
            sampleTimingEntryCount: 0,
            sampleTimingArray: nil,
            sampleSizeEntryCount: 1,
            sampleSizeArray: sizes,
            sampleBufferOut: &sample)
        guard status == noErr, let sampleBuffer = sample else { return nil }

        // No timestamps: show each frame as soon as it is decoded.
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: true),
           CFArrayGetCount(attachments) > 0 {
            let dict = unsafeBitCast(CFArrayGetValueAtIndex(attachments, 0), to: CFMutableDictionary.self)
            let key: CFString = kCMSampleAttachmentKey_DisplayImmediately
            let value: CFBoolean = kCFBooleanTrue
            CFDictionarySetValue(dict,
                                 Unmanaged.passUnretained(key).toOpaque(),
                                 Unmanaged.passUnretained(value).toOpaque())
        }
        return sampleBuffer
    }
}
