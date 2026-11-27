import AVFoundation
import UIKit

final class PenViewController: UIViewController, UIPencilInteractionDelegate, UIGestureRecognizerDelegate {
    // Local ink: how long a finished stroke stays before it fades out (the
    // real stroke has arrived in the video by then), and the fade time.
    static let localInkHoldMs = 300
    static let localInkFadeMs = 150
    static let localInkWidth: CGFloat = 2
    static let localInkColor = UIColor(white: 0.25, alpha: 0.8)

    private let server = LoopbackServer(port: Wire.port)
    private let videoServer = LoopbackServer(port: Wire.videoPort)
    private let videoView = VideoView()
    private lazy var decoder = VideoDecoder(layer: videoView.displayLayer)
    private let statusLabel = UILabel()
    private let trail = CAShapeLayer()
    private let trailPath = UIBezierPath()
    private var lastSize = CGSize.zero

    // Video state
    private var videoShown = false
    private var videoSize: CGSize?
    private var localInkEnabled = true

    // Local ink: one layer per stroke, inside a container clipped to the video.
    private let inkContainer = CALayer()
    private var inkStroke: CAShapeLayer?
    private let inkPath = UIBezierPath()

    // Two-finger gestures. They stay off while the Pencil is down and for a
    // moment after, because a resting palm can look like two fingers.
    static let palmGuardSeconds: TimeInterval = 0.4
    private var pencilDown = false
    private var pencilUpTime: TimeInterval = 0
    // A two-finger gesture is either a scroll or a zoom, never both: whichever
    // passes its threshold first owns the gesture until the fingers lift.
    static let scrollLockPoints: CGFloat = 12
    static let zoomLockLogScale: Double = 0.08
    private enum FingerMode { case undecided, scroll, zoom }
    private var fingerMode = FingerMode.undecided
    private var panLive = false
    private var pinchLive = false
    private var pendingPan = CGPoint.zero
    private var pendingLogScale = 0.0

    // False while a stroke that started in the black bars goes on: it is not sent.
    private var strokeInside = true

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        view.isMultipleTouchEnabled = true

        // Laptop screen, under the ink, the trail and the status line. Touches
        // go through to this controller's view.
        videoView.frame = view.bounds
        videoView.isUserInteractionEnabled = false
        videoView.isHidden = true
        view.addSubview(videoView)

        inkContainer.masksToBounds = true
        inkContainer.isHidden = true
        view.layer.addSublayer(inkContainer)

        trail.strokeColor = UIColor(white: 1.0, alpha: 0.25).cgColor
        trail.fillColor = nil
        trail.lineWidth = 2
        trail.lineCap = .round
        trail.lineJoin = .round
        trail.actions = ["path": NSNull()]  // no implicit animation
        view.layer.addSublayer(trail)

        statusLabel.textColor = UIColor(white: 1.0, alpha: 0.5)
        statusLabel.font = .systemFont(ofSize: 15)
        statusLabel.text = "Starting"
        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(statusLabel)
        NSLayoutConstraint.activate([
            statusLabel.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            statusLabel.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -24),
        ])

        // Fingers: two-finger pan = scroll, pinch = zoom. Direct touches only, so
        // the Pencil never reaches the recognizers. One finger does nothing.
        let direct = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
        let pan = UIPanGestureRecognizer(target: self, action: #selector(handlePan(_:)))
        pan.minimumNumberOfTouches = 2
        pan.maximumNumberOfTouches = 2
        pan.allowedTouchTypes = direct
        pan.cancelsTouchesInView = false
        pan.delegate = self
        view.addGestureRecognizer(pan)
        let pinch = UIPinchGestureRecognizer(target: self, action: #selector(handlePinch(_:)))
        pinch.allowedTouchTypes = direct
        pinch.cancelsTouchesInView = false
        pinch.delegate = self
        view.addGestureRecognizer(pinch)

        let pencil = UIPencilInteraction()
        pencil.delegate = self
        view.addInteraction(pencil)

        server.onStatus = { [weak self] text in self?.statusLabel.text = text }
        server.onConnected = { [weak self] in self?.sendHello() }

        decoder.onFirstFrame = { [weak self] in self?.showVideo(true) }
        decoder.onVideoSize = { [weak self] size in
            self?.videoSize = size
            self?.layoutVideo()
        }
        decoder.onConfig = { [weak self] flags in
            self?.localInkEnabled = (flags & 0x01) != 0
        }
        videoServer.onConnected = { [weak self] in
            guard let self = self else { return }
            self.decoder.reset()
            self.videoSize = nil
            self.localInkEnabled = true
        }
        videoServer.onDisconnected = { [weak self] in
            guard let self = self else { return }
            self.decoder.reset()
            self.videoSize = nil
            self.showVideo(false)
        }
        videoServer.onReceive = { [weak self] data in
            guard let self = self else { return }
            if !self.decoder.receive(data) {
                self.videoServer.disconnect()
            }
        }

        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(didEnterBackground),
                       name: UIApplication.didEnterBackgroundNotification, object: nil)
        nc.addObserver(self, selector: #selector(willEnterForeground),
                       name: UIApplication.willEnterForegroundNotification, object: nil)
        server.start()
        videoServer.start()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        UIApplication.shared.isIdleTimerDisabled = true
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        trail.frame = view.bounds
        layoutVideo()
        if view.bounds.size != lastSize {
            lastSize = view.bounds.size
            sendHello()
        }
    }

    override var supportedInterfaceOrientations: UIInterfaceOrientationMask { .landscape }
    override var prefersStatusBarHidden: Bool { true }
    override var prefersHomeIndicatorAutoHidden: Bool { true }
    override var preferredScreenEdgesDeferringSystemGestures: UIRectEdge { .all }

    @objc private func didEnterBackground() {
        server.stop()
        videoServer.stop()
    }

    @objc private func willEnterForeground() {
        server.start()
        videoServer.start()
    }

    // MARK: Video

    // Where the picture is: aspect fit (black bars) inside the view while the
    // video is shown, else the whole view. Pen positions are relative to it.
    private func videoRect() -> CGRect {
        let bounds = view.bounds
        guard videoShown, let size = videoSize, size.width > 0, size.height > 0 else { return bounds }
        return AVMakeRect(aspectRatio: size, insideRect: bounds)
    }

    private func layoutVideo() {
        let rect = videoRect()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        videoView.frame = rect  // the layer stretches to it (.resize), so the rect is exact
        inkContainer.frame = rect
        CATransaction.commit()
    }

    // While the laptop screen is shown, it replaces the local trail and the
    // status line.
    private func showVideo(_ on: Bool) {
        videoShown = on
        videoView.isHidden = !on
        inkContainer.isHidden = !on
        trail.isHidden = on
        statusLabel.isHidden = on
        if on {
            trailPath.removeAllPoints()
            trail.path = nil
        } else {
            removeInk()
        }
        layoutVideo()
    }

    // The hello gives the view in points and in pixels (landscape only: w > h).
    private func sendHello() {
        let size = view.bounds.size
        guard size.width > 0, size.height > 0 else { return }
        var scale = traitCollection.displayScale
        if scale <= 0 { scale = 2 }
        let widthPx = UInt16(clamping: Int((size.width * scale).rounded()))
        let heightPx = UInt16(clamping: Int((size.height * scale).rounded()))
        server.send(Wire.hello(width: Double(size.width), height: Double(size.height),
                               widthPx: widthPx, heightPx: heightPx, scale: Double(scale)))
    }

    // MARK: Local ink (hides the video round trip while drawing)

    private var inkActive: Bool { videoShown && localInkEnabled }

    // Point in inkContainer coordinates.
    private func inkPoint(_ touch: UITouch) -> CGPoint {
        let p = touch.preciseLocation(in: view)
        let rect = videoRect()
        return CGPoint(x: p.x - rect.minX, y: p.y - rect.minY)
    }

    private func inkBegin(_ touch: UITouch) {
        guard inkActive else { return }
        let stroke = CAShapeLayer()
        stroke.frame = inkContainer.bounds
        stroke.strokeColor = PenViewController.localInkColor.cgColor
        stroke.fillColor = nil
        stroke.lineWidth = PenViewController.localInkWidth
        stroke.lineCap = .round
        stroke.lineJoin = .round
        stroke.actions = ["path": NSNull(), "position": NSNull(), "bounds": NSNull()]
        inkContainer.addSublayer(stroke)
        inkStroke = stroke
        inkPath.removeAllPoints()
        inkPath.move(to: inkPoint(touch))
        stroke.path = inkPath.cgPath
    }

    private func inkMove(_ samples: [UITouch], predicted: [UITouch]) {
        guard let stroke = inkStroke else { return }
        for sample in samples {
            inkPath.addLine(to: inkPoint(sample))
        }
        // Predicted points (about one frame ahead) are shown but not kept.
        if predicted.isEmpty {
            stroke.path = inkPath.cgPath
        } else {
            let shown = UIBezierPath(cgPath: inkPath.cgPath)
            for p in predicted {
                shown.addLine(to: inkPoint(p))
            }
            stroke.path = shown.cgPath
        }
    }

    // Keep the finished stroke for localInkHoldMs, then fade it out.
    private func inkEnd(_ touch: UITouch, cancelled: Bool) {
        guard let stroke = inkStroke else { return }
        inkStroke = nil
        if cancelled {
            stroke.removeFromSuperlayer()
            return
        }
        inkPath.addLine(to: inkPoint(touch))
        stroke.path = inkPath.cgPath
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(PenViewController.localInkHoldMs)) {
            CATransaction.begin()
            CATransaction.setAnimationDuration(Double(PenViewController.localInkFadeMs) / 1000)
            CATransaction.setCompletionBlock { stroke.removeFromSuperlayer() }
            stroke.opacity = 0
            CATransaction.commit()
        }
    }

    private func removeInk() {
        inkStroke = nil
        for layer in inkContainer.sublayers ?? [] {
            layer.removeFromSuperlayer()
        }
    }

    // MARK: Touches (Pencil only; fingers and palm are ignored here, two fingers are gestures below)

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches where touch.type == .pencil {
            pencilDown = true
            send(touch, phase: Wire.phaseDown)
            if videoShown {
                if strokeInside { inkBegin(touch) }
            } else {
                trailPath.removeAllPoints()
                trailPath.move(to: touch.preciseLocation(in: view))
                trail.path = trailPath.cgPath
            }
        }
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches where touch.type == .pencil {
            let samples = event?.coalescedTouches(for: touch) ?? [touch]
            for sample in samples {
                send(sample, phase: Wire.phaseMove)
            }
            if videoShown {
                let predicted = event?.predictedTouches(for: touch) ?? []
                inkMove(samples, predicted: predicted)
            } else {
                for sample in samples {
                    trailPath.addLine(to: sample.preciseLocation(in: view))
                }
                trail.path = trailPath.cgPath
            }
        }
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches where touch.type == .pencil {
            pencilDown = false
            pencilUpTime = ProcessInfo.processInfo.systemUptime
            send(touch, phase: Wire.phaseUp)
            inkEnd(touch, cancelled: false)
        }
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches where touch.type == .pencil {
            pencilDown = false
            pencilUpTime = ProcessInfo.processInfo.systemUptime
            send(touch, phase: Wire.phaseCancel)
            inkEnd(touch, cancelled: true)
        }
    }

    // Positions are fractions of the picture rect (the whole view without
    // video). A stroke that starts in the black bars is not sent; a stroke
    // that leaves the picture is clamped to its edge.
    private func send(_ touch: UITouch, phase: UInt8) {
        let rect = videoRect()
        guard rect.width > 0, rect.height > 0 else { return }
        let p = touch.preciseLocation(in: view)
        let x = Double((p.x - rect.minX) / rect.width)
        let y = Double((p.y - rect.minY) / rect.height)
        if phase == Wire.phaseDown {
            strokeInside = x >= 0 && x <= 1 && y >= 0 && y <= 1
        }
        guard strokeInside else { return }
        let maxForce = touch.maximumPossibleForce
        let pressure = maxForce > 0 ? touch.force / maxForce : 0
        server.send(Wire.sample(phase: phase,
                                x: min(max(x, 0), 1),
                                y: min(max(y, 0), 1),
                                pressure: Double(pressure),
                                altitude: Double(touch.altitudeAngle),
                                azimuth: Double(touch.azimuthAngle(in: view)),
                                timestamp: touch.timestamp))
    }

    // MARK: Two-finger gestures

    func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool {
        let sincePencil = ProcessInfo.processInfo.systemUptime - pencilUpTime
        return !pencilDown && sincePencil > PenViewController.palmGuardSeconds
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                           shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool {
        return true
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        return touch.type != .pencil
    }

    @objc private func handlePan(_ pan: UIPanGestureRecognizer) {
        switch pan.state {
        case .began, .changed:
            panLive = true
            let t = pan.translation(in: view)
            pan.setTranslation(.zero, in: view)
            switch fingerMode {
            case .scroll:
                server.send(Wire.scroll(dx: Double(t.x), dy: Double(t.y)))
            case .zoom:
                break
            case .undecided:
                pendingPan.x += t.x
                pendingPan.y += t.y
                if hypot(pendingPan.x, pendingPan.y) >= PenViewController.scrollLockPoints {
                    fingerMode = .scroll
                    server.send(Wire.gesture(Wire.gestureScroll, phase: Wire.gestureBegin))
                    server.send(Wire.scroll(dx: Double(pendingPan.x), dy: Double(pendingPan.y)))
                }
            }
        case .ended, .cancelled, .failed:
            panLive = false
            fingersMaybeDone()
        default:
            break
        }
    }

    @objc private func handlePinch(_ pinch: UIPinchGestureRecognizer) {
        switch pinch.state {
        case .began, .changed:
            pinchLive = true
            let s = pinch.scale
            pinch.scale = 1
            guard s > 0 else { return }
            switch fingerMode {
            case .zoom:
                server.send(Wire.zoom(scale: Double(s)))
            case .scroll:
                break
            case .undecided:
                pendingLogScale += log(Double(s))
                if abs(pendingLogScale) >= PenViewController.zoomLockLogScale {
                    fingerMode = .zoom
                    server.send(Wire.gesture(Wire.gestureZoom, phase: Wire.gestureBegin))
                    server.send(Wire.zoom(scale: exp(pendingLogScale)))
                }
            }
        case .ended, .cancelled, .failed:
            pinchLive = false
            fingersMaybeDone()
        default:
            break
        }
    }

    // Ends the gesture once both recognizers have ended.
    private func fingersMaybeDone() {
        guard !panLive && !pinchLive else { return }
        switch fingerMode {
        case .scroll:
            server.send(Wire.gesture(Wire.gestureScroll, phase: Wire.gestureEnd))
        case .zoom:
            server.send(Wire.gesture(Wire.gestureZoom, phase: Wire.gestureEnd))
        case .undecided:
            break
        }
        fingerMode = .undecided
        pendingPan = .zero
        pendingLogScale = 0
    }

    // MARK: Pencil double tap -> stylus button

    func pencilInteractionDidTap(_ interaction: UIPencilInteraction) {
        server.send(Wire.button(0, timestamp: ProcessInfo.processInfo.systemUptime))
    }
}
