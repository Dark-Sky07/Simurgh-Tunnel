package node

import (
	"bytes"
	"fmt"
	"io"
	"net"
	"testing"
	"time"
)

// The Go engine serves the same speed endpoint as the Python one, and the
// Python `simurgh speedtest` client is the only thing that ever talks to it.
// These tests pin the three behaviours: echo for the latency probe, a firehose
// for the throughput test, and a UDP echo.

func startSpeedForTest(t *testing.T) (int, *speedServer) {
	t.Helper()
	port := freeTCPPort(t)
	srv := startSpeedServer(port)
	if srv == nil {
		t.Fatal("the speed endpoint did not start")
	}
	t.Cleanup(srv.Close)
	return port, srv
}

func TestSpeedEndpointEchoesTheProbe(t *testing.T) {
	port, _ := startSpeedForTest(t)
	conn, err := net.Dial("tcp", "127.0.0.1:"+fmt.Sprint(port))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))

	if _, err := conn.Write([]byte(echoMagic)); err != nil {
		t.Fatalf("write magic: %v", err)
	}
	back := make([]byte, len(echoMagic))
	if _, err := io.ReadFull(conn, back); err != nil {
		t.Fatalf("read magic: %v", err)
	}
	if string(back) != echoMagic {
		t.Fatalf("magic came back as %q", back)
	}

	// ... and then every byte is answered, which is what a round trip is
	for i := 0; i < 5; i++ {
		payload := []byte{byte(i), 0x42, 0x43, 0x44}
		if _, err := conn.Write(payload); err != nil {
			t.Fatalf("write sample: %v", err)
		}
		echo := make([]byte, len(payload))
		if _, err := io.ReadFull(conn, echo); err != nil {
			t.Fatalf("read sample: %v", err)
		}
		if !bytes.Equal(echo, payload) {
			t.Fatalf("sample %d came back as %v", i, echo)
		}
	}
}

func TestSpeedEndpointPoursAtASilentPeer(t *testing.T) {
	port, _ := startSpeedForTest(t)
	conn, err := net.Dial("tcp", "127.0.0.1:"+fmt.Sprint(port))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))

	// a real client sends a block of random bytes; one byte that is not the
	// start of the magic is enough to be understood as "throughput, please"
	if _, err := conn.Write([]byte("x")); err != nil {
		t.Fatalf("write: %v", err)
	}
	buf := make([]byte, 256*1024)
	if _, err := io.ReadFull(conn, buf); err != nil {
		t.Fatalf("the endpoint poured nothing: %v", err)
	}
}

func TestSpeedEndpointEchoesUDP(t *testing.T) {
	port, _ := startSpeedForTest(t)
	conn, err := net.Dial("udp", "127.0.0.1:"+fmt.Sprint(port))
	if err != nil {
		t.Fatalf("dial udp: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))

	for i := 0; i < 10; i++ {
		payload := []byte("SUDP-datagram-payload")
		if _, err := conn.Write(payload); err != nil {
			t.Fatalf("write: %v", err)
		}
		back := make([]byte, len(payload)+8)
		n, err := conn.Read(back)
		if err != nil {
			t.Fatalf("datagram %d was not answered: %v", i, err)
		}
		if !bytes.Equal(back[:n], payload) {
			t.Fatalf("datagram came back as %q", back[:n])
		}
	}
}

func TestSpeedEndpointIsOptional(t *testing.T) {
	if srv := startSpeedServer(0); srv != nil {
		t.Fatal("port 0 (disabled) must not start an endpoint")
	}
	var nilServer *speedServer
	nilServer.Close() // must not panic: the exit calls this unconditionally
}
