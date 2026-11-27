import UIKit

final class PenViewController: UIViewController, UIPencilInteractionDelegate {
    private let server = PenServer()
    private let statusLabel = UILabel()
    private let trail = CAShapeLayer()
    private let trailPath = UIBezierPath()
    private var lastSize = CGSize.zero

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        view.isMultipleTouchEnabled = true

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

        let pencil = UIPencilInteraction()
        pencil.delegate = self
        view.addInteraction(pencil)

        server.onStatus = { [weak self] text in self?.statusLabel.text = text }
        server.onConnected = { [weak self] in self?.sendHello() }

        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(didEnterBackground),
                       name: UIApplication.didEnterBackgroundNotification, object: nil)
        nc.addObserver(self, selector: #selector(willEnterForeground),
                       name: UIApplication.willEnterForegroundNotification, object: nil)
        server.start()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        UIApplication.shared.isIdleTimerDisabled = true
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        trail.frame = view.bounds
        if view.bounds.size != lastSize {
            lastSize = view.bounds.size
            sendHello()
        }
    }

    override var prefersStatusBarHidden: Bool { true }
    override var prefersHomeIndicatorAutoHidden: Bool { true }
    override var preferredScreenEdgesDeferringSystemGestures: UIRectEdge { .all }

    @objc private func didEnterBackground() {
        server.stop()
    }

    @objc private func willEnterForeground() {
        server.start()
    }

    private func sendHello() {
        let size = view.bounds.size
        guard size.width > 0, size.height > 0 else { return }
        server.send(Wire.hello(width: Double(size.width), height: Double(size.height)))
    }

    // MARK: Touches (Pencil only; fingers and palm are ignored)

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches where touch.type == .pencil {
            trailPath.removeAllPoints()
            trailPath.move(to: touch.preciseLocation(in: view))
            send(touch, phase: Wire.phaseDown)
        }
        trail.path = trailPath.cgPath
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches where touch.type == .pencil {
            let samples = event?.coalescedTouches(for: touch) ?? [touch]
            for sample in samples {
                trailPath.addLine(to: sample.preciseLocation(in: view))
                send(sample, phase: Wire.phaseMove)
            }
        }
        trail.path = trailPath.cgPath
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches where touch.type == .pencil {
            send(touch, phase: Wire.phaseUp)
        }
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches where touch.type == .pencil {
            send(touch, phase: Wire.phaseCancel)
        }
    }

    private func send(_ touch: UITouch, phase: UInt8) {
        let bounds = view.bounds
        guard bounds.width > 0, bounds.height > 0 else { return }
        let p = touch.preciseLocation(in: view)
        let maxForce = touch.maximumPossibleForce
        let pressure = maxForce > 0 ? touch.force / maxForce : 0
        server.send(Wire.sample(phase: phase,
                                x: Double(p.x / bounds.width),
                                y: Double(p.y / bounds.height),
                                pressure: Double(pressure),
                                altitude: Double(touch.altitudeAngle),
                                azimuth: Double(touch.azimuthAngle(in: view)),
                                timestamp: touch.timestamp))
    }

    // MARK: Pencil double tap -> stylus button

    func pencilInteractionDidTap(_ interaction: UIPencilInteraction) {
        server.send(Wire.button(0, timestamp: ProcessInfo.processInfo.systemUptime))
    }
}
