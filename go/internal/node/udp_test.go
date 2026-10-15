package node

import (
	"fmt"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/config"
)

// ---------------------------------------------------------------------------
// helpers: a whole relay + exit pair in one process, talking over plain

func freeTCPPort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("free port: %v", err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

func freeUDPPort(t *testing.T) int {
	t.Helper()
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("free udp port: %v", err)
	}
	defer conn.Close()
	return conn.LocalAddr().(*net.UDPAddr).Port
}

// startUDPEcho answers every datagram with "echo:" + payload, keeping the
// datagram boundaries.
func startUDPEcho(t *testing.T, port int) {
	t.Helper()
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port})
	if err != nil {
		t.Fatalf("udp echo: %v", err)
	}
	t.Cleanup(func() { conn.Close() })
	go func() {
		buf := make([]byte, 65535)
		for {
			n, addr, err := conn.ReadFromUDP(buf)
			if err != nil {
				return
			}
			out := append([]byte("echo:"), buf[:n]...)
			_, _ = conn.WriteToUDP(out, addr)
		}
	}()
}

// startTCPEcho answers with one fixed line and closes.
func startTCPEcho(t *testing.T, port int, reply string) {
	t.Helper()
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		t.Fatalf("tcp echo: %v", err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				_, _ = c.Write([]byte(reply))
			}(conn)
		}
	}()
}

// testPair brings one exit and one relay up in this process and returns them.
func testPair(t *testing.T, mappings []config.Mapping) (*Relay, *Exit) {
	t.Helper()
	exitPort := freeTCPPort(t)
	const token = "udp-test-token"

	exit := NewExit(&config.Exit{
		Token:    token,
		Name:     "test-exit",
		LogLevel: "error",
		Listen: []config.ListenSpec{{
			Carrier: "plain", Host: "127.0.0.1", Port: exitPort, Enabled: true,
		}},
	}, t.TempDir())
	if err := exit.Start(); err != nil {
		t.Fatalf("exit start: %v", err)
	}
	t.Cleanup(exit.Stop)

	relay := NewRelay(&config.Relay{
		Token:       token,
		Name:        "test-relay",
		Dial:        "relay",
		Connections: 1,
		LogLevel:    "error",
		Exit:        config.Endpoint{Carrier: "plain", Address: "127.0.0.1", Port: exitPort, Enabled: true},
		Mappings:    mappings,
	}, t.TempDir())
	if err := relay.Start(); err != nil {
		t.Fatalf("relay start: %v", err)
	}
	t.Cleanup(relay.Stop)
	return relay, exit
}

// sendUDP sends one datagram and waits for the answer.
func sendUDP(t *testing.T, conn *net.UDPConn, port int, payload []byte, wait time.Duration) []byte {
	t.Helper()
	if _, err := conn.WriteToUDP(payload, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port}); err != nil {
		t.Fatalf("send: %v", err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(wait))
	buf := make([]byte, 65535)
	n, _, err := conn.ReadFromUDP(buf)
	if err != nil {
		t.Fatalf("no answer for %d bytes: %v", len(payload), err)
	}
	return buf[:n]
}

// waitUDPRetry keeps sending until the tunnel is up and the answer arrives.
func waitUDPRetry(t *testing.T, conn *net.UDPConn, port int, payload []byte) []byte {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		if _, err := conn.WriteToUDP(payload, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: port}); err != nil {
			t.Fatalf("send: %v", err)
		}
		_ = conn.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
		buf := make([]byte, 65535)
		n, _, err := conn.ReadFromUDP(buf)
		if err == nil {
			return buf[:n]
		}
		if time.Now().After(deadline) {
			t.Fatalf("tunnel never came up: %v", err)
		}
	}
}

// drainUDP throws away whatever is already waiting in a client socket.
func drainUDP(t *testing.T, conn *net.UDPConn) {
	t.Helper()
	buf := make([]byte, 65535)
	_ = conn.SetReadDeadline(time.Now().Add(150 * time.Millisecond))
	for {
		if _, _, err := conn.ReadFromUDP(buf); err != nil {
			break
		}
	}
	_ = conn.SetReadDeadline(time.Time{})
}

func newClient(t *testing.T) *net.UDPConn {
	t.Helper()
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("client socket: %v", err)
	}
	t.Cleanup(func() { conn.Close() })
	return conn
}

// ---------------------------------------------------------------------------
// tests

// One datagram in is one datagram out: the boundaries survive, whatever the
// size, and the payload is untouched.
func TestUDPKeepsDatagramBoundaries(t *testing.T) {
	relayPort := freeUDPPort(t)
	targetPort := freeUDPPort(t)
	startUDPEcho(t, targetPort)
	testPair(t, []config.Mapping{{
		Name: "echo", Listen: relayPort, ListenHost: "127.0.0.1",
		TargetHost: "127.0.0.1", TargetPort: targetPort, UDP: true, Enabled: true,
	}})
	_ = relayPort

	client := newClient(t)
	if got := waitUDPRetry(t, client, relayPort, []byte("hello")); string(got) != "echo:hello" {
		t.Fatalf("first answer = %q", got)
	}

	// five datagrams sent back to back, five separate answers back
	sizes := []int{1, 512, 1400, 4000, 60000}
	for _, size := range sizes {
		payload := make([]byte, size)
		for i := range payload {
			payload[i] = byte(i*7 + size)
		}
		got := sendUDP(t, client, relayPort, payload, 5*time.Second)
		if len(got) != size+5 || string(got[:5]) != "echo:" {
			t.Fatalf("size %d came back as %d bytes", size, len(got))
		}
		if string(got[5:]) != string(payload) {
			t.Fatalf("size %d payload changed", size)
		}
	}

	burst := newClient(t)
	for i := 0; i < 5; i++ {
		if _, err := burst.WriteToUDP([]byte(fmt.Sprintf("burst%d", i)),
			&net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: relayPort}); err != nil {
			t.Fatal(err)
		}
	}
	answers := map[string]bool{}
	_ = burst.SetReadDeadline(time.Now().Add(5 * time.Second))
	for i := 0; i < 5; i++ {
		buf := make([]byte, 65535)
		n, _, err := burst.ReadFromUDP(buf)
		if err != nil {
			t.Fatalf("burst answer %d: %v", i, err)
		}
		answers[string(buf[:n])] = true
	}
	for i := 0; i < 5; i++ {
		want := fmt.Sprintf("echo:burst%d", i)
		if !answers[want] {
			t.Fatalf("missing %q in %v (datagrams were merged?)", want, answers)
		}
	}
}

// Every client address gets its own stream, so answers go back to the right
// socket instead of to whoever spoke last.
func TestUDPKeepsClientsApart(t *testing.T) {
	relayPort := freeUDPPort(t)
	targetPort := freeUDPPort(t)
	startUDPEcho(t, targetPort)
	testPair(t, []config.Mapping{{
		Name: "echo", Listen: relayPort, ListenHost: "127.0.0.1",
		TargetHost: "127.0.0.1", TargetPort: targetPort, UDP: true, Enabled: true,
	}})

	a, b := newClient(t), newClient(t)
	if got := waitUDPRetry(t, a, relayPort, []byte("warm-up")); string(got) != "echo:warm-up" {
		t.Fatalf("warm-up answer = %q", got)
	}
	// the retries above may have left more "warm-up" answers in the queue
	drainUDP(t, a)

	var wg sync.WaitGroup
	results := make([]string, 2)
	for i, client := range []*net.UDPConn{a, b} {
		wg.Add(1)
		go func(i int, client *net.UDPConn) {
			defer wg.Done()
			payload := []byte(fmt.Sprintf("client-%d", i))
			results[i] = string(sendUDP(t, client, relayPort, payload, 5*time.Second))
		}(i, client)
	}
	wg.Wait()
	if results[0] != "echo:client-0" || results[1] != "echo:client-1" {
		t.Fatalf("answers crossed: %v", results)
	}
}

// UDP and TCP mappings live on the same tunnel, and the status the web panel
// reads keeps the python engine's shape (key, udp flag, counters).
func TestUDPStatusAndTCPTogether(t *testing.T) {
	relayPort := freeUDPPort(t)
	targetPort := freeUDPPort(t)
	tcpPort := freeTCPPort(t)
	tcpTarget := freeTCPPort(t)
	startUDPEcho(t, targetPort)
	startTCPEcho(t, tcpTarget, "tcp-ok")

	relay, _ := testPair(t, []config.Mapping{
		{Name: "dns", Listen: relayPort, ListenHost: "127.0.0.1",
			TargetHost: "127.0.0.1", TargetPort: targetPort, UDP: true, Enabled: true},
		{Name: "web", Listen: tcpPort, ListenHost: "127.0.0.1",
			TargetHost: "127.0.0.1", TargetPort: tcpTarget, Enabled: true},
	})

	client := newClient(t)
	if got := waitUDPRetry(t, client, relayPort, []byte("ping")); string(got) != "echo:ping" {
		t.Fatalf("udp answer = %q", got)
	}
	conn, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%d", tcpPort))
	if err != nil {
		t.Fatalf("tcp dial: %v", err)
	}
	defer conn.Close()
	buf := make([]byte, 16)
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	n, err := conn.Read(buf)
	if err != nil || string(buf[:n]) != "tcp-ok" {
		t.Fatalf("tcp through the same tunnel: %q %v", buf[:n], err)
	}

	status := relay.Status()
	mappings, _ := status["mappings"].([]map[string]any)
	if len(mappings) != 2 {
		t.Fatalf("mappings in status = %d", len(mappings))
	}
	var udpEntry map[string]any
	for _, m := range mappings {
		if m["udp"] == true {
			udpEntry = m
		}
	}
	if udpEntry == nil {
		t.Fatal("no udp mapping in the status")
	}
	key, _ := udpEntry["key"].(string)
	if key != fmt.Sprintf("udp:127.0.0.1:%d/127.0.0.1:%d", relayPort, targetPort) {
		t.Fatalf("udp key = %q", key)
	}
	if udpEntry["bound"] != true {
		t.Fatal("udp mapping is not reported as bound")
	}
	stats, _ := udpEntry["stats"].(map[string]any)
	if stats == nil {
		t.Fatal("udp mapping has no stats")
	}
	if in, _ := stats["in_bytes"].(int64); in <= 0 {
		t.Fatalf("in_bytes = %v", stats["in_bytes"])
	}
	if out, _ := stats["out_bytes"].(int64); out <= 0 {
		t.Fatalf("out_bytes = %v", stats["out_bytes"])
	}
	if conns, _ := stats["conns"].(int64); conns != 1 {
		t.Fatalf("conns = %v", stats["conns"])
	}
	if active, _ := stats["active"].(int64); active != 1 {
		t.Fatalf("active = %v (the flow should still be alive)", stats["active"])
	}
}

// A quiet client address is dropped after the flow timeout, and the next
// datagram from it opens a fresh stream instead of talking into the void.
func TestUDPFlowTimesOutAndComesBack(t *testing.T) {
	oldTimeout, oldEvery := udpFlowTimeout, udpReapEvery
	udpFlowTimeout, udpReapEvery = 300*time.Millisecond, 100*time.Millisecond
	t.Cleanup(func() { udpFlowTimeout, udpReapEvery = oldTimeout, oldEvery })

	relayPort := freeUDPPort(t)
	targetPort := freeUDPPort(t)
	startUDPEcho(t, targetPort)
	relay, _ := testPair(t, []config.Mapping{{
		Name: "echo", Listen: relayPort, ListenHost: "127.0.0.1",
		TargetHost: "127.0.0.1", TargetPort: targetPort, UDP: true, Enabled: true,
	}})

	client := newClient(t)
	if got := waitUDPRetry(t, client, relayPort, []byte("one")); string(got) != "echo:one" {
		t.Fatalf("first answer = %q", got)
	}

	// wait for the reaper to let the flow go
	deadline := time.Now().Add(5 * time.Second)
	for {
		status := relay.Status()
		mappings, _ := status["mappings"].([]map[string]any)
		active := int64(-1)
		for _, m := range mappings {
			if stats, ok := m["stats"].(map[string]any); ok {
				active, _ = stats["active"].(int64)
			}
		}
		if active == 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("flow was never reaped (active=%d)", active)
		}
		time.Sleep(50 * time.Millisecond)
	}

	// the same client address must work again on a brand new stream
	if got := sendUDP(t, client, relayPort, []byte("two"), 5*time.Second); string(got) != "echo:two" {
		t.Fatalf("answer after the reaper = %q", got)
	}
}

// A datagram that arrives before the stream is ready is not lost: it is
// buffered for a moment and then sent on.
func TestUDPBuffersBeforeTheStreamIsReady(t *testing.T) {
	relayPort := freeUDPPort(t)
	targetPort := freeUDPPort(t)
	startUDPEcho(t, targetPort)

	// bring the pair up by hand so the client can blast a datagram in the
	// instant the listener appears but the tunnel is still dialling
	exitPort := freeTCPPort(t)
	const token = "udp-early-token"
	exit := NewExit(&config.Exit{Token: token, LogLevel: "error",
		Listen: []config.ListenSpec{{Carrier: "plain", Host: "127.0.0.1", Port: exitPort, Enabled: true}}},
		t.TempDir())
	relay := NewRelay(&config.Relay{Token: token, Dial: "relay", Connections: 1, LogLevel: "error",
		Exit: config.Endpoint{Carrier: "plain", Address: "127.0.0.1", Port: exitPort, Enabled: true},
		Mappings: []config.Mapping{{Name: "echo", Listen: relayPort, ListenHost: "127.0.0.1",
			TargetHost: "127.0.0.1", TargetPort: targetPort, UDP: true, Enabled: true}}},
		t.TempDir())
	if err := relay.Start(); err != nil {
		t.Fatalf("relay start: %v", err)
	}
	t.Cleanup(relay.Stop)

	client := newClient(t)
	if _, err := client.WriteToUDP([]byte("too early"), &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: relayPort}); err != nil {
		t.Fatalf("early send: %v", err)
	}
	time.Sleep(200 * time.Millisecond)

	if err := exit.Start(); err != nil {
		t.Fatalf("exit start: %v", err)
	}
	t.Cleanup(exit.Stop)

	_ = client.SetReadDeadline(time.Now().Add(10 * time.Second))
	buf := make([]byte, 65535)
	n, _, err := client.ReadFromUDP(buf)
	if err != nil {
		t.Fatalf("buffered datagram never arrived: %v", err)
	}
	if string(buf[:n]) != "echo:too early" {
		t.Fatalf("buffered answer = %q", buf[:n])
	}
}
