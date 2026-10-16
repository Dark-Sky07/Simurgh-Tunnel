package node

import (
	"log/slog"
	"net"
	"strings"
	"sync"
	"time"
)

// The exit side of `simurgh speedtest`.  The relay opens a stream straight to
// 127.0.0.1:<speedtest_port> on this side, so the measurement includes the
// carrier, the multiplexer and flow control -- the things an operator is
// actually testing.
//
// One port, three behaviours, exactly as the Python engine's speedtest.py:
//
//   - a connection that says nothing gets a firehose (throughput);
//   - a connection starting with echoMagic gets every byte echoed back (the
//     latency-under-load probe);
//   - a UDP datagram gets the same datagram back (loss and jitter).
const echoMagic = "SIMURGH-ECHO\n"

const (
	speedBlock     = 64 * 1024
	udpEchoTimeout = 30 * time.Second
)

// speedServer is the localhost-only endpoint behind `simurgh speedtest`.
type speedServer struct {
	ln   net.Listener
	udp  *net.UDPConn
	stop chan struct{}
	once sync.Once
	wg   sync.WaitGroup
}

// startSpeedServer binds the TCP firehose and the UDP echo on 127.0.0.1.  A
// port that is already taken is not fatal: the tunnel does not depend on it.
func startSpeedServer(port int) *speedServer {
	if port <= 0 {
		return nil
	}
	addr := &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port}
	ln, err := net.ListenTCP("tcp", addr)
	if err != nil {
		slog.Warn("speed test endpoint is not available", "addr", addr.String(), "err", err)
		return nil
	}
	srv := &speedServer{ln: ln, stop: make(chan struct{})}
	slog.Info("speed test endpoint", "address", ln.Addr().String(), "scope", "localhost only")

	if conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port}); err == nil {
		srv.udp = conn
		srv.wg.Add(1)
		go srv.echoUDP()
	} else {
		slog.Warn("udp speed test endpoint is not available", "err", err)
	}

	srv.wg.Add(1)
	go srv.accept()
	return srv
}

func (s *speedServer) accept() {
	defer s.wg.Done()
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			select {
			case <-s.stop:
				return
			default:
			}
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			return
		}
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			s.serve(conn)
		}()
	}
}

// serve decides what this connection is: an echo probe or a throughput blast.
func (s *speedServer) serve(conn net.Conn) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(30 * time.Minute))

	head := make([]byte, 0, len(echoMagic))
	buf := make([]byte, speedBlock)
	echo := false
	for {
		got := string(head)
		if strings.HasPrefix(got, echoMagic) {
			echo = true // the probe may send the magic and its first sample at once
			break
		}
		if len(got) > 0 && !strings.HasPrefix(echoMagic, got) {
			break // not the magic: this is the throughput test
		}
		if len(got) == len(echoMagic) {
			break
		}
		n, err := conn.Read(buf)
		if n > 0 {
			head = append(head, buf[:n]...)
			continue
		}
		if err != nil {
			return
		}
	}
	// the magic has to come back even in throughput mode: a client that asked
	// for the echo must not sit and wait for an answer that never comes
	if len(head) > 0 {
		if _, err := conn.Write(head); err != nil {
			return
		}
	}

	if echo {
		for {
			n, err := conn.Read(buf)
			if n > 0 {
				if _, werr := conn.Write(buf[:n]); werr != nil {
					return
				}
			}
			if err != nil {
				return
			}
		}
	}

	// throughput: pour data at the peer while draining everything it sends, so
	// the peer's window never stalls on our side.  Blocking writes pace the
	// loop for free -- when the socket buffer is full the kernel waits, which
	// is exactly the back-pressure a saturated link should show.
	block := make([]byte, speedBlock)
	randFill(block)
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			if _, err := conn.Read(buf); err != nil {
				return
			}
		}
	}()
	for {
		select {
		case <-done:
			return
		case <-s.stop:
			return
		default:
		}
		if _, err := conn.Write(block); err != nil {
			return
		}
	}
}

// echoUDP answers every datagram with itself until the server stops.
func (s *speedServer) echoUDP() {
	defer s.wg.Done()
	buf := make([]byte, 64*1024)
	for {
		_ = s.udp.SetReadDeadline(time.Now().Add(udpEchoTimeout))
		n, addr, err := s.udp.ReadFromUDP(buf)
		if err != nil {
			select {
			case <-s.stop:
				return
			default:
			}
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			return
		}
		if n > 0 {
			_, _ = s.udp.WriteToUDP(buf[:n], addr)
		}
	}
}

// Close stops the endpoint (the tunnel keeps running).
func (s *speedServer) Close() {
	if s == nil {
		return
	}
	s.once.Do(func() { close(s.stop) })
	_ = s.ln.Close()
	if s.udp != nil {
		_ = s.udp.Close()
	}
}

// randFill fills b with incompressible bytes without blocking on the kernel's
// entropy pool: xorshift64* is plenty, and the point is only that the payload
// does not compress (a carrier that compresses must not report fake numbers).
func randFill(b []byte) {
	seed := uint64(time.Now().UnixNano()) | 1
	for i := range b {
		seed ^= seed << 13
		seed ^= seed >> 7
		seed ^= seed << 17
		b[i] = byte(seed >> 24)
	}
}
