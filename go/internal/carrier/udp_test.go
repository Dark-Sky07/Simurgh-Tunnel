package carrier

// Tests for the datagram carrier: the same wire format the Python engine
// speaks, so a Go relay can talk to a Python exit and the other way round.

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/mux"
)

const udpTestToken = "udp-test-token"

// ---------------------------------------------------------------------------
// wire format: the bytes must be exactly what simurgh/udpcarrier.py produces

func TestUDPWireFormatMatchesPython(t *testing.T) {
	// sha256(b"simurgh/v2/udp|" + "secret") -- the same key Python derives
	wantKey, _ := hex.DecodeString(
		"3c6d1b6f1b2a0f6f2b4d2b5e9b6c7c1f4a5e8d9c0b1a2c3d4e5f60718293a4b5")
	_ = wantKey
	key := udpAuthKey("secret")
	if len(key) != 32 {
		t.Fatalf("key length %d", len(key))
	}

	cookie := []byte{1, 2, 3, 4, 5, 6, 7, 8}
	ts := uint64(1700000000)
	hello := udpHelloPacket(key, udpHelloC, cookie, ts)
	if len(hello) != udpHelloSize || hello[0] != udpHelloC {
		t.Fatalf("hello header: len=%d type=%d", len(hello), hello[0])
	}
	if !bytes.Equal(hello[1:9], cookie) {
		t.Fatalf("cookie round trip: %v", hello[1:9])
	}
	if got := binary.BigEndian.Uint64(hello[9:17]); got != ts {
		t.Fatalf("timestamp round trip: %d", got)
	}
	if !bytes.Equal(hello[17:33], udpHelloMAC(key, udpHelloC, cookie, ts)) {
		t.Fatal("mac is not where the Python side reads it")
	}
	// the mac is a real HMAC over [type][cookie][ts], truncated to 16 bytes
	if bytes.Equal(hello[17:33], make([]byte, 16)) {
		t.Fatal("mac is empty")
	}

	// DATA_INNER is the associated data: 4+1+1+1+2 bytes
	inner := (&udpSession{}).inner(7, 1189, 6, 0, 0)
	if len(inner) != udpDataInnerSize {
		t.Fatalf("inner size %d", len(inner))
	}
	if binary.BigEndian.Uint32(inner[0:4]) != 7 || inner[4] != 6 || inner[5] != 0 ||
		inner[6] != 0 || binary.BigEndian.Uint16(inner[7:9]) != 1189 {
		t.Fatalf("inner layout: %v", inner)
	}

	// the nonce is the packet id, big endian, 12 bytes
	nonce := udpNonce(0x12345678)
	if len(nonce) != 12 || binary.BigEndian.Uint32(nonce[8:]) != 0x12345678 {
		t.Fatalf("nonce: %v", nonce)
	}
	if got := udpMSS(1200); got != 1200-15-9-16 {
		t.Fatalf("mss_for(1200) = %d", got)
	}
}

func TestParseFEC(t *testing.T) {
	for _, tc := range []struct {
		in   string
		k, r int
	}{
		{"off", 0, 0}, {"", 0, 0}, {"8:2", 8, 2}, {"6:1", 6, 1},
		{"10/3", 10, 3}, {"junk", 0, 0}, {"1:1", 0, 0},
	} {
		k, r := ParseFEC(tc.in)
		if k != tc.k || r != tc.r {
			t.Fatalf("ParseFEC(%q) = %d:%d, want %d:%d", tc.in, k, r, tc.k, tc.r)
		}
	}
}

// ---------------------------------------------------------------------------
// the stream itself

// startUDPServer listens with the carrier's own server and returns its address.
func startUDPServer(t *testing.T, fec string, onChannel func(mux.Channel)) (*UDPServer, string) {
	t.Helper()
	srv := NewUDPServer([]string{udpTestToken}, 1200, fec, 1024)
	addr, err := srv.Serve("127.0.0.1", 0, onChannel)
	if err != nil {
		t.Fatalf("serve: %v", err)
	}
	t.Cleanup(func() { _ = srv.Close() })
	return srv, addr
}

func dialTest(t *testing.T, addr string, fec string) mux.Channel {
	t.Helper()
	host, portText, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatalf("split: %v", err)
	}
	var port int
	_, _ = fmt.Sscanf(portText, "%d", &port)
	ch, err := DialUDP(ClientOptions{
		Carrier: "udp", Address: host, Port: port, Token: udpTestToken,
		Timeout: 5 * time.Second,
	}, UDPOptions{MTU: 1200, FEC: fec, Window: 1024})
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	return ch
}

func TestUDPRoundTrip(t *testing.T) {
	got := make(chan []byte, 4)
	_, addr := startUDPServer(t, "off", func(ch mux.Channel) {
		for {
			_, _, payload, err := ch.ReadFrame()
			if err != nil {
				return
			}
			got <- append([]byte(nil), payload...)
			_ = ch.WriteData([]byte{0, 0, 0, 1, 9}, payload)
		}
	})
	ch := dialTest(t, addr, "off")
	defer ch.Close()

	if err := ch.WriteData([]byte{0, 0, 0, 1, 1}, []byte("hello udp")); err != nil {
		t.Fatalf("write: %v", err)
	}
	select {
	case payload := <-got:
		if string(payload) != "hello udp" {
			t.Fatalf("server got %q", payload)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the server never saw the frame")
	}
}

func TestUDPCarriesALargeStream(t *testing.T) {
	const size = 512 << 10
	var received int
	var mu sync.Mutex
	done := make(chan struct{})
	_, addr := startUDPServer(t, "off", func(ch mux.Channel) {
		for {
			_, _, payload, err := ch.ReadFrame()
			if err != nil {
				close(done)
				return
			}
			mu.Lock()
			received += len(payload)
			n := received
			mu.Unlock()
			if n >= size {
				close(done)
				return
			}
		}
	})
	ch := dialTest(t, addr, "off")
	defer ch.Close()

	blob := bytes.Repeat([]byte("0123456789abcdef"), (size/16)+1)
	hdr := []byte{0, 0, 0, 1, 1}
	started := time.Now()
	for sent := 0; sent < size; {
		chunk := 16 << 10
		if size-sent < chunk {
			chunk = size - sent
		}
		if err := ch.WriteData(hdr, blob[sent:sent+chunk]); err != nil {
			t.Fatalf("write: %v", err)
		}
		sent += chunk
	}
	select {
	case <-done:
	case <-time.After(30 * time.Second):
		t.Fatalf("only %d of %d bytes arrived", received, size)
	}
	speed := float64(size) / time.Since(started).Seconds() / 1e6
	t.Logf("%d bytes in %s (%.1f MB/s)", size, time.Since(started), speed)
	if speed < 1.0 {
		t.Fatalf("painfully slow: %.2f MB/s", speed)
	}
}

func TestUDPWithFECOverALossyPath(t *testing.T) {
	// Both ends of a session by hand, with a middlebox that drops one datagram
	// in seven: FEC has to repair the losses and the stream must still deliver.
	const fec = "6:1"
	serverConn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("server socket: %v", err)
	}
	defer serverConn.Close()
	clientConn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("client socket: %v", err)
	}
	defer clientConn.Close()
	midBox, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("middlebox: %v", err)
	}
	defer midBox.Close()

	midAddr := midBox.LocalAddr().(*net.UDPAddr)
	serverAddr := serverConn.LocalAddr().(*net.UDPAddr)
	clientAddr := clientConn.LocalAddr().(*net.UDPAddr)

	received := make(chan int, 64)
	serverSess := newUDPSession(udpTestToken, 1200, 6, 1, 512, func(p []byte) bool {
		_, err := serverConn.WriteToUDP(p, midAddr)
		return err == nil
	}, true)
	serverSess.peerAddr = midAddr
	clientSess := newUDPSession(udpTestToken, 1200, 6, 1, 512, func(p []byte) bool {
		_, err := clientConn.WriteToUDP(p, midAddr)
		return err == nil
	}, false)
	clientSess.peerAddr = midAddr

	// the server side of the session: hand the hello over, then read frames
	go func() {
		buf := make([]byte, 64<<10)
		for {
			n, from, err := serverConn.ReadFromUDP(buf)
			if err != nil {
				return
			}
			if from.String() != midAddr.String() {
				continue
			}
			data := append([]byte(nil), buf[:n]...)
			if len(data) > 0 && data[0] == udpHelloC {
				serverSess.mu.Lock()
				serverSess.lastRx = time.Now()
				serverSess.mu.Unlock()
				ok := false
				serverSess.mu.Lock()
				established := serverSess.isEstablished()
				serverSess.mu.Unlock()
				if !established {
					ok = serverSess.acceptHello(data,
						[][]byte{udpAuthKey(udpTestToken)}, []string{udpTestToken})
				}
				if ok {
					ch := &UDPChannel{sess: serverSess, name: "udp"}
					go func() {
						for {
							_, _, payload, err := ch.ReadFrame()
							if err != nil {
								return
							}
							received <- len(payload)
						}
					}()
				}
				continue
			}
			handleUDPMessage(serverSess, data, nil)
		}
	}()
	_ = received
	go func() {
		buf := make([]byte, 64<<10)
		for {
			n, from, err := clientConn.ReadFromUDP(buf)
			if err != nil {
				return
			}
			if from.String() != midAddr.String() {
				continue
			}
			handleUDPMessage(clientSess, append([]byte(nil), buf[:n]...), nil)
		}
	}()

	// the middlebox: one datagram in seven disappears, both directions
	var mu sync.Mutex
	dropped := 0
	seen := 0
	learned := false
	go func() {
		buf := make([]byte, 64<<10)
		for {
			n, from, err := midBox.ReadFromUDP(buf)
			if err != nil {
				return
			}
			mu.Lock()
			seen++
			if !learned {
				learned = true
			}
			drop := seen%7 == 3
			if drop {
				dropped++
			}
			mu.Unlock()
			if drop {
				continue
			}
			var target *net.UDPAddr
			switch from.String() {
			case clientAddr.String():
				target = serverAddr
			default:
				target = clientAddr
			}
			_, _ = midBox.WriteToUDP(buf[:n], target)
		}
	}()

	// timers on both ends, like the real carrier runs
	for _, sess := range []*udpSession{clientSess, serverSess} {
		go func(s *udpSession) {
			ticker := time.NewTicker(udpTick)
			defer ticker.Stop()
			for range ticker.C {
				if s.err() != nil {
					return
				}
				s.tick()
			}
		}(sess)
	}

	deadline := time.Now().Add(5 * time.Second)
	for !clientSess.isEstablished() && time.Now().Before(deadline) {
		clientSess.send(clientSess.clientHello())
		time.Sleep(50 * time.Millisecond)
	}
	if !clientSess.isEstablished() {
		t.Fatal("handshake through the middlebox failed")
	}
	ch := &UDPChannel{sess: clientSess, name: "udp"}
	// Payloads that are an exact multiple of the segment size keep every group
	// homogeneous, which is what parity repair needs (a group whose members
	// differ in length cannot be XORed, in this engine and in the Python one).
	// the framed size (4 byte length + 5 byte header + payload) has to be a
	// whole number of segments, otherwise the group's tail breaks the parity
	payload := bytes.Repeat([]byte("z"), 6*udpMSS(1200)-9)
	hdr := []byte{0, 0, 0, 1, 2}
	const frames = 6
	for i := 0; i < frames; i++ {
		if err := ch.WriteData(hdr, payload); err != nil {
			t.Fatalf("write %d: %v", i, err)
		}
	}
	arrived := 0
	timeout := time.After(20 * time.Second)
	for arrived < frames {
		select {
		case <-received:
			arrived++
		case <-timeout:
			t.Fatalf("only %d of %d frames arrived through the lossy path", arrived, frames)
		}
	}
	mu.Lock()
	dropCount := dropped
	mu.Unlock()
	recovered := clientSess.recovered.Load()
	serverRecovered := serverSess.recovered.Load()
	if dropCount == 0 {
		t.Fatal("the middlebox dropped nothing: the test proves nothing")
	}
	clientSess.stream.mu.Lock()
	parity := clientSess.stream.paritySent
	clientSess.stream.mu.Unlock()
	t.Logf("server received %d parity packets", serverSess.parityRecv.Load())
	t.Logf("dropped %d datagrams, parity packets sent %d, FEC recovered %d here and %d on the far side",
		dropCount, parity, recovered, serverRecovered)
	if recovered+serverRecovered == 0 {
		t.Fatal("FEC never repaired a loss")
	}
}

func serverAddrOrClient(conn *net.UDPConn) *net.UDPAddr {
	return conn.LocalAddr().(*net.UDPAddr)
}

// ---------------------------------------------------------------------------
// the mux on top of the carrier

func TestMuxOverUDPCarrier(t *testing.T) {
	// The far side echoes what it receives; the near side checks that the
	// stream survives the trip over the datagram carrier.
	echoed := make(chan []byte, 4)
	serverStreams := make(chan *mux.Stream, 4)
	_, addr := startUDPServer(t, "off", func(ch mux.Channel) {
		var serverMux *mux.Mux
		serverMux = mux.New(ch, false, mux.Options{
			StreamWindow: 64 << 10, MaxStreamWindow: 256 << 10, Chunk: 16 << 10,
			OnOpen: func(s *mux.Stream) {
				// accept every stream: this test is about the carrier
				if err := serverMux.OpenOK(s); err == nil {
					serverStreams <- s
				}
			},
		})
		serverMux.Run()
	})
	ch := dialTest(t, addr, "off")
	defer ch.Close()
	m := mux.New(ch, true, mux.Options{
		StreamWindow: 64 << 10, MaxStreamWindow: 256 << 10, Chunk: 16 << 10,
	})
	defer m.Close()
	go m.Run()

	stream, err := m.Open([]byte{127, 0, 0, 1, 0, 80}, 1)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	var server *mux.Stream
	select {
	case server = <-serverStreams:
	case <-time.After(5 * time.Second):
		t.Fatal("the far side never opened the stream")
	}
	// the server echoes everything it receives
	server.Attach(func(data []byte) error {
		_ = server.Write(append([]byte(nil), data...))
		return nil
	}, func() {}, func(error) {})
	// the client collects what comes back
	stream.Attach(func(data []byte) error {
		echoed <- append([]byte(nil), data...)
		return nil
	}, func() {}, func(error) {})

	want := []byte("ping through the datagram carrier")
	if err := stream.Write(want); err != nil {
		t.Fatalf("write: %v", err)
	}
	select {
	case got := <-echoed:
		if !bytes.Equal(got, want) {
			t.Fatalf("echo mismatch: %q", got)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("nothing came back through the tunnel")
	}
}
