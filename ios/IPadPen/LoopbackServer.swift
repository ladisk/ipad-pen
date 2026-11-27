import Foundation
import Network

// TCP server on the iPad's loopback interface. The laptop reaches it through
// usbmux (USB cable); it is not reachable over Wi-Fi. One client at a time.
final class PenServer {
    var onStatus: ((String) -> Void)?
    var onConnected: (() -> Void)?

    private var listener: NWListener?
    private var connection: NWConnection?
    private var running = false

    var isConnected: Bool { connection != nil }

    func start() {
        guard !running else { return }
        running = true
        startListener()
    }

    func stop() {
        running = false
        dropConnection()
        listener?.cancel()
        listener = nil
        onStatus?("Paused")
    }

    func send(_ data: Data) {
        guard let conn = connection else { return }
        conn.send(content: data, completion: .contentProcessed { [weak self] error in
            if error != nil {
                DispatchQueue.main.async { self?.dropConnection(conn) }
            }
        })
    }

    private func startListener() {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        let params = NWParameters(tls: nil, tcp: tcp)
        params.allowLocalEndpointReuse = true
        params.requiredLocalEndpoint = NWEndpoint.hostPort(host: .ipv4(.loopback),
                                                           port: NWEndpoint.Port(rawValue: Wire.port)!)
        let listener: NWListener
        do {
            listener = try NWListener(using: params)
        } catch {
            onStatus?("Listener error: \(error)")
            retryLater()
            return
        }
        listener.stateUpdateHandler = { [weak self] state in
            guard let self = self else { return }
            switch state {
            case .ready:
                if !self.isConnected { self.onStatus?("Waiting for laptop (USB, port \(Wire.port))") }
            case .failed(let error):
                self.onStatus?("Listener failed: \(error)")
                listener.cancel()
                if self.listener === listener { self.listener = nil }
                self.retryLater()
            default:
                break
            }
        }
        listener.newConnectionHandler = { [weak self] conn in
            self?.accept(conn)
        }
        self.listener = listener
        listener.start(queue: .main)
    }

    private func retryLater() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self = self, self.running, self.listener == nil else { return }
            self.startListener()
        }
    }

    private func accept(_ conn: NWConnection) {
        guard Self.isLoopback(conn.endpoint) else {
            conn.cancel()
            return
        }
        // A new client replaces the old one (e.g. the daemon restarted).
        dropConnection()
        connection = conn
        conn.stateUpdateHandler = { [weak self] state in
            guard let self = self else { return }
            switch state {
            case .ready:
                self.onStatus?("Connected")
                self.onConnected?()
                self.receiveLoop(conn)
            case .failed, .cancelled:
                self.dropConnection(conn)
            default:
                break
            }
        }
        conn.start(queue: .main)
    }

    // The laptop sends nothing; reading only detects when it goes away.
    private func receiveLoop(_ conn: NWConnection) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 1024) { [weak self] _, _, isComplete, error in
            if isComplete || error != nil {
                self?.dropConnection(conn)
            } else {
                self?.receiveLoop(conn)
            }
        }
    }

    private func dropConnection(_ conn: NWConnection? = nil) {
        guard let current = connection else { return }
        if let conn = conn, conn !== current { return }
        connection = nil
        current.stateUpdateHandler = nil
        current.cancel()
        if running { onStatus?("Waiting for laptop (USB, port \(Wire.port))") }
    }

    private static func isLoopback(_ endpoint: NWEndpoint) -> Bool {
        guard case let .hostPort(host, _) = endpoint else { return false }
        switch host {
        case .ipv4(let addr): return addr.isLoopback
        case .ipv6(let addr): return addr.isLoopback
        default: return false
        }
    }
}
