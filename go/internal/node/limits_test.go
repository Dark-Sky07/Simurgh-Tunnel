package node

import (
	"bytes"
	"crypto/rand"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/config"
)

// Everything in this file runs a real exit and a real relay: the limits are a
// data-path feature, so only real traffic proves them.

// pairWithLimits builds the usual pair, but with one mapping that carries the
// limits under test.
func pairWithLimits(t *testing.T, mapping config.Mapping) (*Relay, string) {
	t.Helper()
	relayPort := freeTCPPort(t)
	targetPort := freeTCPPort(t)
	startTCPDataEcho(t, targetPort)
	exitPort := freeTCPPort(t)

	const token = "limits-e2e-token"
	exit := NewExit(&config.Exit{
		Token: token, Name: "limits-exit", LogLevel: "error",
		Listen: []config.ListenSpec{{
			Carrier: "plain", Host: "127.0.0.1", Port: exitPort,
			Path: "/ws", Padding: true, Fallback: "close", Enabled: true,
		}},
	}, t.TempDir())
	if err := exit.Start(); err != nil {
		t.Fatalf("exit start: %v", err)
	}
	t.Cleanup(exit.Stop)

	home := t.TempDir()
	mapping.Listen = relayPort
	mapping.ListenHost = "127.0.0.1"
	mapping.TargetHost = "127.0.0.1"
	mapping.TargetPort = targetPort
	mapping.Enabled = true
	if mapping.Name == "" {
		mapping.Name = "limited"
	}
	relay := NewRelay(&config.Relay{
		Token: token, Name: "limits-relay", Dial: "relay", Connections: 1,
		LogLevel: "error",
		Exit: config.Endpoint{
			Carrier: "plain", Address: "127.0.0.1", Port: exitPort, Path: "/ws",
			InsecureSkipVerify: true, Padding: true, Enabled: true,
		},
		Mappings: []config.Mapping{mapping},
	}, home)
	if err := relay.Start(); err != nil {
		t.Fatalf("relay start: %v", err)
	}
	t.Cleanup(relay.Stop)
	waitTunnelUp(t, relay)
	return relay, home
}

func TestMappingRateLimitSlowsTheLink(t *testing.T) {
	const rate = 64 << 10
	pair, _ := pairWithLimits(t, config.Mapping{RateLimit: "64k"})
	port := pair.cfg.Mappings[0].Listen

	payload := make([]byte, 192<<10) // three seconds' worth at 64 KiB/s
	if _, err := rand.Read(payload); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	got := roundTrip(t, port, payload)
	elapsed := time.Since(start)
	if !bytes.Equal(got, payload) {
		t.Fatal("a limited link still has to deliver every byte, in order")
	}
	// the bucket starts with one second of credit, so three seconds of traffic
	// cannot be quicker than two seconds
	if elapsed < 1500*time.Millisecond {
		t.Fatalf("192 KiB crossed a 64 KiB/s limit in %s: the limit is not applied", elapsed)
	}
	if elapsed > 20*time.Second {
		t.Fatalf("192 KiB took %s: the limiter is stalling instead of pacing", elapsed)
	}
	t.Logf("192 KiB through a 64 KiB/s mapping in %s", elapsed)
}

func TestMappingQuotaBlocksOnceUsedUp(t *testing.T) {
	relay, home := pairWithLimits(t, config.Mapping{Quota: "4k", QuotaPeriod: "month",
		QuotaAction: "block"})
	port := relay.cfg.Mappings[0].Listen

	// the first connection spends the quota (both directions are counted)
	payload := make([]byte, 4<<10)
	if _, err := rand.Read(payload); err != nil {
		t.Fatal(err)
	}
	roundTrip(t, port, payload)
	if used := relay.account.Usage(relay.cfg.Mappings[0].Key(), "month"); used <= 4096 {
		t.Fatalf("the ledger counted %d bytes, want more than the 4k quota", used)
	}

	// the next user is refused: the socket closes without a byte of answer
	conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 5*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := conn.Write([]byte("hello")); err == nil {
		if n, err := conn.Read(make([]byte, 16)); err == nil && n > 0 {
			t.Fatalf("a mapping over its quota answered %d bytes", n)
		}
	}
	if relay.blocked.Load() == 0 {
		t.Fatalf("the refusal was not counted (ledger: %v)", relay.ledgerSnapshot())
	}

	// the ledger on disk shows what was counted (flushed every 15 s and on the
	// way out, so a forced save is what a reader sees after a reload)
	relay.account.Save(true)
	raw, err := os.ReadFile(filepath.Join(home, "traffic.json"))
	if err != nil {
		t.Fatalf("read the ledger: %v", err)
	}
	if !bytes.Contains(raw, []byte("tcp:127.0.0.1:")) {
		t.Fatalf("the ledger has no row for the mapping: %s", raw)
	}
}

func TestMappingOverQuotaOnlyThrottles(t *testing.T) {
	relay, _ := pairWithLimits(t, config.Mapping{Quota: "1k", QuotaPeriod: "month",
		QuotaAction: "throttle"})
	port := relay.cfg.Mappings[0].Listen
	// spend the quota first
	small := make([]byte, 1<<10)
	if _, err := rand.Read(small); err != nil {
		t.Fatal(err)
	}
	roundTrip(t, port, small)

	// then make sure the link still works, at the trickle rate (128 KiB/s).
	// 512 KiB needs three seconds once the bucket's initial credit is gone.
	payload := make([]byte, 512<<10)
	if _, err := rand.Read(payload); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	got := roundTrip(t, port, payload)
	elapsed := time.Since(start)
	if !bytes.Equal(got, payload) {
		t.Fatal("a throttled mapping must still deliver the bytes")
	}
	if elapsed < 2500*time.Millisecond {
		t.Fatalf("a throttled mapping passed 512 KiB in %s: no trickle applied", elapsed)
	}
	t.Logf("512 KiB through a throttled mapping in %s", elapsed)
}

func TestMappingMaxConnsRefusesTheExtraUser(t *testing.T) {
	relay, _ := pairWithLimits(t, config.Mapping{MaxConns: 1})
	port := relay.cfg.Mappings[0].Listen

	first, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 5*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer first.Close()
	// make sure the first connection is inside the mapping before the second
	// arrives: it echoes what we send, so a round trip proves it is bridged
	_ = first.SetDeadline(time.Now().Add(10 * time.Second))
	if _, err := first.Write([]byte("ping")); err != nil {
		t.Fatalf("write: %v", err)
	}
	echo := make([]byte, 4)
	if _, err := io.ReadFull(first, echo); err != nil {
		t.Fatalf("the first connection is not working: %v", err)
	}

	second, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 5*time.Second)
	if err != nil {
		t.Fatalf("dial the second user: %v", err)
	}
	defer second.Close()
	_ = second.SetDeadline(time.Now().Add(5 * time.Second))
	_, _ = second.Write([]byte("ping"))
	if n, err := second.Read(make([]byte, 8)); err == nil && n > 0 {
		t.Fatalf("a mapping at max_conns=1 served a second user (%d bytes)", n)
	}

	// the first connection is untouched, and once it goes away the port is
	// usable again
	first.Close()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 2*time.Second)
		if err != nil {
			time.Sleep(50 * time.Millisecond)
			continue
		}
		_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
		if _, err := conn.Write([]byte("ping")); err == nil {
			echo := make([]byte, 4)
			if _, err := io.ReadFull(conn, echo); err == nil {
				conn.Close()
				return
			}
		}
		conn.Close()
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("the mapping never accepted a user again after the first one left")
}

func TestLedgerIsSharedWithThePythonEngine(t *testing.T) {
	relay, home := pairWithLimits(t, config.Mapping{})
	port := relay.cfg.Mappings[0].Listen
	payload := []byte("traffic worth counting")
	roundTrip(t, port, payload)

	rows := relay.trafficRows()
	if len(rows) != 1 {
		t.Fatalf("rows = %v", rows)
	}
	row := rows[0]
	if row["key"] != relay.cfg.Mappings[0].Key() {
		t.Fatalf("ledger key %v, want %v", row["key"], relay.cfg.Mappings[0].Key())
	}
	if row["in"].(int64) == 0 || row["out"].(int64) == 0 {
		t.Fatalf("both directions have to be counted: %v", row)
	}
	// flushed on the way out, exactly like the Python engine
	relay.Stop()
	raw, err := os.ReadFile(filepath.Join(home, "traffic.json"))
	if err != nil {
		t.Fatalf("read the ledger: %v", err)
	}
	if !bytes.Contains(raw, []byte(`"version":1`)) {
		t.Fatalf("the ledger is not in the shared format: %s", raw)
	}
}
