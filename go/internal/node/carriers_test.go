package node

import (
	"bytes"
	"crypto/rand"
	"fmt"
	"io"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/config"
)

// End-to-end per carrier: a real relay, a real exit, a real target service and
// a real client socket, once for every carrier the engine speaks. Data must come
// back byte for byte, for one user and for eight at the same time.

// startTCPDataEcho echoes whatever it reads, so arbitrary sizes can be checked.
func startTCPDataEcho(t *testing.T, port int) {
	t.Helper()
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		t.Fatalf("echo listen: %v", err)
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
				_, _ = io.Copy(c, c)
			}(conn)
		}
	}()
}

// pairForCarrier runs exit + relay over one carrier and returns the relay.
func pairForCarrier(t *testing.T, carrier string, relayPort, targetPort, split int) *Relay {
	t.Helper()
	exitPort := freeTCPPort(t)
	const token = "carrier-e2e-token"

	exit := NewExit(&config.Exit{
		Token:    token,
		Name:     "e2e-exit",
		LogLevel: "error",
		Listen: []config.ListenSpec{{
			Carrier: carrier, Host: "127.0.0.1", Port: exitPort, Path: "/ws",
			Padding: true, Fallback: "close", Enabled: true,
		}},
	}, t.TempDir())
	if err := exit.Start(); err != nil {
		t.Fatalf("exit start: %v", err)
	}
	t.Cleanup(exit.Stop)

	relay := NewRelay(&config.Relay{
		Token:       token,
		Name:        "e2e-relay",
		Dial:        "relay",
		Connections: 1,
		LogLevel:    "error",
		Exit: config.Endpoint{
			Carrier: carrier, Address: "127.0.0.1", Port: exitPort, Path: "/ws",
			InsecureSkipVerify: true, Padding: true, Split: split, Enabled: true,
		},
		Mappings: []config.Mapping{{
			Name: "web", Listen: relayPort, ListenHost: "127.0.0.1",
			TargetHost: "127.0.0.1", TargetPort: targetPort, Enabled: true,
		}},
	}, t.TempDir())
	if err := relay.Start(); err != nil {
		t.Fatalf("relay start: %v", err)
	}
	t.Cleanup(relay.Stop)
	return relay
}

// waitTunnelUp blocks until the relay reports a live tunnel.
func waitTunnelUp(t *testing.T, relay *Relay) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for {
		if connected, _ := relay.Status()["connected"].(bool); connected {
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("the tunnel never came up")
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// roundTrip sends payload through the relay port and reads all of it back.
func roundTrip(t *testing.T, port int, payload []byte) []byte {
	t.Helper()
	conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 5*time.Second)
	if err != nil {
		t.Fatalf("dial the relay port: %v", err)
	}
	defer conn.Close()
	got := make([]byte, 0, len(payload))
	done := make(chan error, 1)
	go func() {
		_ = conn.SetDeadline(time.Now().Add(30 * time.Second))
		if _, err := conn.Write(payload); err != nil {
			done <- err
			return
		}
		buf := make([]byte, len(payload))
		_, err := io.ReadFull(conn, buf)
		if err == nil {
			got = buf
		}
		done <- err
	}()
	if err := <-done; err != nil {
		t.Fatalf("round trip of %d bytes: %v", len(payload), err)
	}
	return got
}

func TestCarrierEndToEnd(t *testing.T) {
	for _, carrier := range []string{"plain", "tls", "wss", "raw"} {
		t.Run(carrier, func(t *testing.T) {
			targetPort := freeTCPPort(t)
			relayPort := freeTCPPort(t)
			startTCPDataEcho(t, targetPort)
			relay := pairForCarrier(t, carrier, relayPort, targetPort, 0)
			waitTunnelUp(t, relay)

			for _, size := range []int{41, 100 << 10, 3 << 20} {
				payload := make([]byte, size)
				if _, err := rand.Read(payload); err != nil {
					t.Fatal(err)
				}
				got := roundTrip(t, relayPort, payload)
				if !bytes.Equal(got, payload) {
					t.Fatalf("%s: %d bytes came back as %d (identical: %v)",
						carrier, size, len(got), bytes.Equal(got, payload))
				}
			}

			// eight users at once, each with its own payload
			var wg sync.WaitGroup
			errs := make([]error, 8)
			for i := 0; i < 8; i++ {
				wg.Add(1)
				go func(i int) {
					defer wg.Done()
					payload := bytes.Repeat([]byte{byte('a' + i)}, 64<<10)
					conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", relayPort), 5*time.Second)
					if err != nil {
						errs[i] = err
						return
					}
					defer conn.Close()
					_ = conn.SetDeadline(time.Now().Add(30 * time.Second))
					if _, err := conn.Write(payload); err != nil {
						errs[i] = err
						return
					}
					back := make([]byte, len(payload))
					if _, err := io.ReadFull(conn, back); err != nil {
						errs[i] = err
						return
					}
					if !bytes.Equal(back, payload) {
						errs[i] = fmt.Errorf("user %d got someone else's data", i)
					}
				}(i)
			}
			wg.Wait()
			for i, err := range errs {
				if err != nil {
					t.Fatalf("%s: concurrent user %d: %v", carrier, i, err)
				}
			}
		})
	}
}

// The split first write must not disturb a real TLS handshake: the ClientHello
// goes out in two TCP segments and the tunnel still carries traffic.
func TestSplitHelloStillTunnels(t *testing.T) {
	for _, split := range []int{1, 3, 500} {
		t.Run(fmt.Sprintf("split=%d", split), func(t *testing.T) {
			targetPort := freeTCPPort(t)
			relayPort := freeTCPPort(t)
			startTCPDataEcho(t, targetPort)
			relay := pairForCarrier(t, "tls", relayPort, targetPort, split)
			waitTunnelUp(t, relay)
			payload := bytes.Repeat([]byte("split"), 2000)
			if got := roundTrip(t, relayPort, payload); !bytes.Equal(got, payload) {
				t.Fatalf("split=%d: %d bytes came back as %d", split, len(payload), len(got))
			}
		})
	}
}

// A carrier that is not configured on the exit must fail loudly on the relay
// side instead of hanging: the client's socket simply closes.
func TestCarrierMismatchClosesTheStream(t *testing.T) {
	targetPort := freeTCPPort(t)
	relayPort := freeTCPPort(t)
	startTCPDataEcho(t, targetPort)

	// exit listens with plain, relay insists on raw (so the handshake never
	// completes)
	exitPort := freeTCPPort(t)
	const token = "mismatch-token"
	exit := NewExit(&config.Exit{Token: token, LogLevel: "error",
		Listen: []config.ListenSpec{{Carrier: "plain", Host: "127.0.0.1", Port: exitPort, Enabled: true}}},
		t.TempDir())
	if err := exit.Start(); err != nil {
		t.Fatalf("exit start: %v", err)
	}
	t.Cleanup(exit.Stop)

	relay := NewRelay(&config.Relay{Token: token, Dial: "relay", Connections: 1, LogLevel: "error",
		Exit: config.Endpoint{Carrier: "raw", Address: "127.0.0.1", Port: exitPort,
			InsecureSkipVerify: true, Enabled: true},
		Mappings: []config.Mapping{{Name: "web", Listen: relayPort, ListenHost: "127.0.0.1",
			TargetHost: "127.0.0.1", TargetPort: targetPort, Enabled: true}}}, t.TempDir())
	if err := relay.Start(); err != nil {
		t.Fatalf("relay start: %v", err)
	}
	t.Cleanup(relay.Stop)

	// the relay never gets a tunnel, and a client is told so by a closed socket
	time.Sleep(700 * time.Millisecond)
	conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", relayPort), 5*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
	if _, err := conn.Write([]byte("hello")); err == nil {
		buf := make([]byte, 16)
		if _, err := conn.Read(buf); err == nil {
			t.Fatal("a client was served although no tunnel exists")
		}
	}
	if connected, _ := relay.Status()["connected"].(bool); connected {
		t.Fatal("the relay reports a tunnel that cannot exist")
	}
}
